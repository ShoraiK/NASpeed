# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Ookla Speedtest servers.

Ookla publishes no official API. This module speaks the same HTTP protocol
its web client uses (and long-standing third-party clients such as
speedtest-cli): it is an unofficial, compatible client and may stop working
if Ookla changes its service.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections import OrderedDict

from ..engine import netutil
from .base import HttpSession, Provider, ProviderError, ensure_family, pick_server, rank_servers

CONFIG_URL = "https://www.speedtest.net/speedtest-config.php"
SERVERS_URL = "https://www.speedtest.net/api/js/servers?engine=js&limit=%d&https_functional=true"
SERVER_LIMIT = 20
# Keep recently offered search results available for manual selection even
# after their probe data expires. Probe a chosen server again before use.
KNOWN_SERVER_LIMIT = 512
DEFAULT_PORT = 8080
# Requests are long on purpose: one request keeps a connection busy for the
# whole measurement, so there are no gaps between requests.
DOWNLOAD_SIZE = 1_000_000_000
UPLOAD_SIZE = 100_000_000


class Ookla(Provider):
    id = "ookla"
    name = "Ookla Speedtest"
    homepage = "https://www.speedtest.net"
    official_api = False
    searchable = True

    def __init__(self):
        super().__init__()
        self._known = OrderedDict()

    def _client(self):
        def load():
            status, _headers, body = netutil.http_fetch(CONFIG_URL)
            if status != 200:
                return {}
            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError:
                return {}
            node = root.find("client")
            if node is None:
                return {}
            return {
                "ip": node.get("ip", ""),
                "isp": node.get("isp", ""),
                "country": node.get("country", ""),
                "lat": node.get("lat", ""),
                "lon": node.get("lon", ""),
            }

        try:
            return dict(self._cached("client", load, 600))
        except netutil.NetError:
            return {}

    @staticmethod
    def _split_host(value):
        host, _sep, port = str(value).rpartition(":")
        if not host:
            return str(value), DEFAULT_PORT
        try:
            return host, int(port)
        except ValueError:
            return str(value), DEFAULT_PORT

    def _catalogue(self, ctx, query=""):
        query = (query or "").strip()[:60]

        def load():
            url = SERVERS_URL % SERVER_LIMIT
            if query:
                url += "&search=" + urllib.parse.quote(query)
            data = netutil.fetch_json(url)
            if not isinstance(data, list):
                raise ProviderError("bad_response", "server list")
            client = self._client()
            servers = []
            targets = {}
            for item in data:
                if not isinstance(item, dict) or not item.get("host") or not item.get("id"):
                    continue
                host, port = self._split_host(item["host"])
                server_id = str(item["id"])
                servers.append({
                    "id": server_id,
                    "name": str(item.get("name") or ""),
                    "sponsor": str(item.get("sponsor") or ""),
                    "country": str(item.get("country") or ""),
                    "cc": str(item.get("cc") or ""),
                    "host": "%s:%d" % (host, port),
                    "distance_km": netutil.haversine_km(
                        client.get("lat"), client.get("lon"), item.get("lat"), item.get("lon")),
                })
                targets[server_id] = ("http", host, port)
            return rank_servers(servers, targets, ctx.ip_version)

        try:
            return self._cached(("servers", query, ctx.ip_version), load)
        except netutil.NetError as exc:
            raise ProviderError("provider_unavailable", exc.detail)

    def list_servers(self, ctx, query=""):
        servers, _ = self._catalogue(ctx, query)
        with self._cache_lock:
            for server in servers:
                self._known[server["id"]] = server
                self._known.move_to_end(server["id"])
            while len(self._known) > KNOWN_SERVER_LIMIT:
                self._known.popitem(last=False)
        return [dict(s) for s in servers]

    def _hello(self, endpoint):
        """True when ``endpoint`` answers like an Ookla server."""
        try:
            status, body = netutil.pinned_get(endpoint, "/hello?nocache=1", timeout=5.0, limit=256)
        except Exception:
            return False
        return status == 200 and body.lower().startswith(b"hello")

    def open(self, ctx, server_id=""):
        servers, endpoints = self._catalogue(ctx)
        if server_id and server_id not in {s["id"] for s in servers}:
            # A server picked from an earlier search: probe just that one.
            with self._cache_lock:
                known = self._known.get(str(server_id))
            if known is None:
                raise ProviderError("server_not_found", server_id)
            host, port = self._split_host(known["host"])
            servers, endpoints = rank_servers([dict(known)], {known["id"]: ("http", host, port)}, ctx.ip_version)
        server = pick_server(servers, server_id)
        endpoint = endpoints.get(server["id"])
        if endpoint is None or server.get("latency_ms") is None:
            raise ProviderError("server_unreachable", server.get("host", ""))
        endpoint = ensure_family(endpoint, ctx.ip_version)

        # Plain HTTP costs far less CPU than TLS, which matters at multi-gigabit
        # speeds. Fall back to HTTPS when something on the path blocks it.
        if not self._hello(endpoint):
            secure = netutil.Endpoint("https", endpoint.host, endpoint.port, endpoint.ip, endpoint.family)
            if not self._hello(secure):
                raise ProviderError("server_unreachable", server.get("host", ""))
            endpoint = secure

        client = self._client()
        public_client = {k: client.get(k, "") for k in ("ip", "isp", "country")}
        return HttpSession(
            dict(server), endpoint, public_client,
            download_path="/download?nocache={rand}&size={size}",
            upload_path="/upload?nocache={rand}",
            download_size=DOWNLOAD_SIZE,
            upload_size=UPLOAD_SIZE,
        )

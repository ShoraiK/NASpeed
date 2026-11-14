# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Cloudflare speed test (speed.cloudflare.com).

Anycast network: there is a single host name and the nearest Cloudflare
data centre answers, so there is no server list to choose from.
"""

from __future__ import annotations

import json

from ..engine import netutil
from .base import HttpSession, Provider, ProviderError

HOST = "speed.cloudflare.com"
# The server answers 403 for 100 000 000 bytes or more.
DOWNLOAD_SIZE = 99_999_999
UPLOAD_SIZE = 25_000_000
# /meta is refused without this header.
REFERER = "https://speed.cloudflare.com/"


class Cloudflare(Provider):
    id = "cloudflare"
    name = "Cloudflare"
    homepage = "https://speed.cloudflare.com"
    selectable_servers = False

    def _endpoint(self, ctx):
        return netutil.pin_endpoint("https", HOST, 443, ctx.ip_version)

    def _get(self, endpoint, path):
        conn = netutil.PinnedHTTPConnection(endpoint, netutil.CONTROL_TIMEOUT)
        try:
            conn.request("GET", path, headers={
                "User-Agent": netutil.USER_AGENT, "Accept": "*/*", "Referer": REFERER,
            })
            resp = conn.getresponse()
            body = resp.read(netutil.MAX_CONTROL_BYTES)
            return resp.status, body
        finally:
            conn.close()

    def _describe_connection(self, endpoint):
        """Return ``(server, client)`` dicts from /meta, falling back to /cdn-cgi/trace."""
        server = {"id": "anycast", "name": "Anycast", "sponsor": "Cloudflare",
                  "country": "", "cc": "", "host": HOST}
        client = {}
        try:
            status, body = self._get(endpoint, "/meta")
            if status == 200:
                meta = json.loads(body.decode("utf-8", "replace"))
                colo = meta.get("colo") or {}
                if isinstance(colo, dict) and colo.get("iata"):
                    server["id"] = str(colo["iata"])
                    server["name"] = str(colo.get("city") or colo["iata"])
                    server["cc"] = str(colo.get("cca2") or "")
                    server["country"] = server["cc"]
                elif isinstance(colo, str) and colo:
                    server["id"] = server["name"] = colo
                client = {
                    "ip": str(meta.get("clientIp") or ""),
                    "isp": str(meta.get("asOrganization") or ""),
                    "country": str(meta.get("country") or ""),
                }
                return server, client
        except (OSError, ValueError, netutil.NetError):
            pass
        try:
            status, body = self._get(endpoint, "/cdn-cgi/trace")
            if status == 200:
                trace = dict(
                    line.split("=", 1) for line in body.decode("utf-8", "replace").splitlines() if "=" in line
                )
                if trace.get("colo"):
                    server["id"] = server["name"] = trace["colo"]
                client = {"ip": trace.get("ip", ""), "isp": "", "country": trace.get("loc", "")}
        except (OSError, ValueError, netutil.NetError):
            pass
        return server, client

    def list_servers(self, ctx, query=""):
        def load():
            endpoint = self._endpoint(ctx)
            server, _client = self._describe_connection(endpoint)
            return [server]

        return [dict(s) for s in self._cached(("servers", ctx.ip_version), load)]

    def open(self, ctx, server_id=""):
        try:
            endpoint = self._endpoint(ctx)
        except netutil.NetError as exc:
            raise ProviderError("server_unreachable", exc.detail)
        server, client = self._describe_connection(endpoint)
        return HttpSession(
            self, server, endpoint, client,
            download_path="/__down?bytes={size}",
            upload_path="/__up?r={rand}",
            download_size=DOWNLOAD_SIZE,
            upload_size=UPLOAD_SIZE,
            headers={"Referer": REFERER},
        )

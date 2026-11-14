# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""LibreSpeed public servers (https://librespeed.org).

LibreSpeed is an open-source speed test. Its public server list is meant to
be used by clients; each server exposes a handful of small PHP endpoints.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from ..engine import netutil
from .base import HttpSession, Provider, ProviderError, ensure_family, pick_server, rank_servers

LIST_URL = "https://librespeed.org/backend-servers/servers.php"
DOWNLOAD_SIZE = 100 * (1 << 20)   # ckSize=100, the value the official client uses
UPLOAD_SIZE = 20 * (1 << 20)      # same as the official client's upload blob


def _join(base, relative):
    return urllib.parse.urljoin(base if base.endswith("/") else base + "/", relative or "")


def _with_query(url, query):
    return url + ("&" if "?" in url else "?") + query


class LibreSpeed(Provider):
    id = "librespeed"
    name = "LibreSpeed"
    homepage = "https://librespeed.org"

    def __init__(self):
        super().__init__()
        self._raw = {}
        self._endpoints = {}

    def _load(self, ctx):
        data = netutil.fetch_json(LIST_URL)
        if not isinstance(data, list):
            raise ProviderError("bad_response", "server list")
        servers = []
        targets = {}
        for item in data:
            if not isinstance(item, dict) or not item.get("server"):
                continue
            base = str(item["server"]).strip()
            if base.startswith("//"):
                base = "https:" + base
            parts = urllib.parse.urlsplit(base)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                continue
            port = parts.port or (443 if parts.scheme == "https" else 80)
            server_id = str(item.get("id", len(servers)))
            self._raw[server_id] = {
                "base": base,
                "scheme": parts.scheme,
                "host": parts.hostname,
                "port": port,
                "dl": _join(base, item.get("dlURL")),
                "ul": _join(base, item.get("ulURL")),
                "ip": _join(base, item.get("getIpURL")),
            }
            # Names look like "Amsterdam, Netherlands (Clouvider)": the part in
            # brackets is the host organisation, which we show separately.
            name = str(item.get("name") or parts.hostname).strip()
            sponsor = str(item.get("sponsorName") or "").strip()
            match = re.match(r"^(.*\S)\s*\(([^()]+)\)$", name)
            if match:
                name = match.group(1)
                sponsor = sponsor or match.group(2)
            servers.append({
                "id": server_id,
                "name": name,
                "sponsor": sponsor,
                "country": "",
                "cc": "",
                "host": parts.hostname,
            })
            targets[server_id] = (parts.scheme, parts.hostname, port)
        ranked, endpoints = rank_servers(servers, targets, ctx.ip_version)
        self._endpoints.update(endpoints)
        return ranked

    def list_servers(self, ctx, query=""):
        try:
            servers = self._cached(("servers", ctx.ip_version), lambda: self._load(ctx), 600)
        except netutil.NetError as exc:
            raise ProviderError("provider_unavailable", exc.detail)
        return [dict(s) for s in servers]

    def _client(self, endpoint, url):
        """Ask the server for our public IP and operator."""
        if not url:
            return {}
        path = urllib.parse.urlsplit(_with_query(url, "isp=true&distance=km"))
        conn = netutil.PinnedHTTPConnection(endpoint, 6.0)
        try:
            conn.request("GET", path.path + "?" + path.query,
                         headers={"User-Agent": netutil.USER_AGENT, "Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read(65536)
            if resp.status != 200:
                return {}
            text = body.decode("utf-8", "replace").strip()
        except (OSError, Exception):
            return {}
        finally:
            conn.close()
        processed = text
        isp = ""
        country = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                processed = str(data.get("processedString") or "")
                raw = data.get("rawIspInfo")
                if isinstance(raw, dict):
                    isp = str(raw.get("org") or raw.get("isp") or "")
                    country = str(raw.get("country") or "")
        except ValueError:
            pass
        # processedString looks like "203.0.113.7 - Operator name, ES (123 km)".
        ip, _sep, rest = processed.partition(" - ")
        if not isp and rest:
            isp = re.sub(r"\s*\([^)]*\)\s*$", "", rest).strip()
            match = re.search(r",\s*([A-Z]{2})$", isp)
            if match:
                country = country or match.group(1)
                isp = isp[: match.start()].strip()
        isp = re.sub(r"^AS\d+\s+", "", isp)
        if isp.lower().startswith("unknown"):
            isp = ""
        return {"ip": ip.strip(), "isp": isp, "country": country}

    def open(self, ctx, server_id=""):
        servers = self.list_servers(ctx)
        server = pick_server(servers, server_id)
        raw = self._raw.get(server["id"])
        endpoint = self._endpoints.get(server["id"])
        if raw is None or endpoint is None or server.get("latency_ms") is None:
            raise ProviderError("server_unreachable", server.get("host", ""))
        endpoint = ensure_family(endpoint, ctx.ip_version)
        download =urllib.parse.urlsplit(_with_query(raw["dl"], "ckSize={size_mib}&r={rand}"))
        upload = urllib.parse.urlsplit(_with_query(raw["ul"], "r={rand}"))
        return HttpSession(
            self, dict(server), endpoint, self._client(endpoint, raw["ip"]),
            download_path=download.path + "?" + download.query,
            upload_path=upload.path + "?" + upload.query,
            download_size=DOWNLOAD_SIZE,
            upload_size=UPLOAD_SIZE,
        )

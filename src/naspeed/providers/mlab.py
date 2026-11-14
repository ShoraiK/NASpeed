# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Measurement Lab (M-Lab) NDT7.

M-Lab is an open measurement platform; ndt7 is its current test protocol
and third-party clients are welcome. Two things set it apart:

* The test uses a single TCP connection for about ten seconds per direction.
* M-Lab publishes every result, including the client IP address, as open
  data. The user has to accept that before the first test.
"""

from __future__ import annotations

import json
import urllib.parse

from .. import APP_ID, __version__
from ..engine import netutil
from .base import Provider, ProviderError, Session, ensure_family, pick_server, rank_servers

LOCATE_URL = "https://locate.measurementlab.net/v2/nearest/ndt/ndt7?client_name=%s&client_version=%s"
PRIVACY_URL = "https://www.measurementlab.net/privacy/"
DOWNLOAD_KEY = "wss:///ndt/v7/download"
UPLOAD_KEY = "wss:///ndt/v7/upload"
# The server ends a download after about 10 s, so the measured window
# (after warm-up) cannot be longer than this.
MAX_WINDOW = 8.0


class Ndt7Session(Session):
    kind = "ndt7"
    self_terminating = True

    def __init__(self, provider, server, endpoint, paths):
        super().__init__(provider, server, endpoint, {})
        self.paths = paths
        self.initial_streams = 1
        self.max_streams = 1
        self.fixed_streams = 1
        self.max_window = MAX_WINDOW

    def spec(self, direction):
        return {
            "kind": "ndt7",
            "direction": direction,
            "endpoint": self.endpoint.to_dict(),
            "path": self.paths[direction],
            "timeout": 15,
        }


class MLab(Provider):
    id = "mlab"
    name = "M-Lab NDT7"
    homepage = "https://speed.measurementlab.net"
    notice = "mlab_privacy"
    notice_url = PRIVACY_URL
    single_stream = True

    def __init__(self):
        super().__init__()
        self._endpoints = {}

    def _locate(self):
        url = LOCATE_URL % (APP_ID, urllib.parse.quote(__version__.split("-")[0]))
        try:
            status, _headers, body = netutil.http_fetch(url)
        except netutil.NetError as exc:
            raise ProviderError("provider_unavailable", exc.detail)
        if status in (204, 429, 502, 503):
            # No capacity right now: M-Lab asks clients to back off, not retry.
            raise ProviderError("provider_busy", "HTTP %d" % status)
        if status != 200:
            raise ProviderError("provider_unavailable", "HTTP %d" % status)
        try:
            results = json.loads(body.decode("utf-8", "replace")).get("results") or []
        except (ValueError, AttributeError):
            raise ProviderError("bad_response", "locate")
        usable = []
        for item in results:
            urls = item.get("urls") or {}
            if item.get("machine") and urls.get(DOWNLOAD_KEY) and urls.get(UPLOAD_KEY):
                usable.append(item)
        if not usable:
            raise ProviderError("no_servers", "locate returned no ndt7 server")
        return usable

    @staticmethod
    def _server(item):
        location = item.get("location") or {}
        host = urllib.parse.urlsplit(item["urls"][DOWNLOAD_KEY]).hostname or item["machine"]
        return {
            "id": str(item["machine"]),
            "name": str(location.get("city") or item["machine"]),
            "sponsor": "M-Lab",
            "country": str(location.get("country") or ""),
            "cc": str(location.get("country") or ""),
            "host": host,
        }

    def _rank(self, results, ctx):
        servers = [self._server(item) for item in results]
        targets = {s["id"]: ("wss", s["host"], 443) for s in servers}
        ranked, endpoints = rank_servers(servers, targets, ctx.ip_version)
        self._endpoints.update(endpoints)
        return ranked

    def list_servers(self, ctx, query=""):
        servers = self._cached(("servers", ctx.ip_version), lambda: self._rank(self._locate(), ctx), 120)
        return [dict(s) for s in servers]

    def open(self, ctx, server_id=""):
        # Access tokens are short-lived: always ask for fresh URLs.
        results = self._locate()
        ranked = self._rank(results, ctx)
        try:
            server = pick_server(ranked, server_id)
        except ProviderError:
            # The preferred machine is not offered right now: take the best one.
            server = pick_server(ranked, "")
        item = next(r for r in results if str(r["machine"]) == server["id"])
        endpoint = self._endpoints.get(server["id"])
        if endpoint is None:
            raise ProviderError("server_unreachable", server.get("host", ""))
        endpoint = ensure_family(endpoint, ctx.ip_version)
        paths = {}
        for direction, key in (("download", DOWNLOAD_KEY), ("upload", UPLOAD_KEY)):
            parts = urllib.parse.urlsplit(item["urls"][key])
            paths[direction] = parts.path + ("?" + parts.query if parts.query else "")
        return Ndt7Session(self, dict(server), endpoint, paths)

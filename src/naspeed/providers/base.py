# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Provider interface.

A *provider* knows how to find test servers and how to describe a transfer
against one of them. The measurement itself (latency sampling, parallel
connections, warm-up, averaging) is done by the engine and is identical for
every provider, so results are comparable.
"""

from __future__ import annotations

import threading
import time

from ..engine import latency


class ProviderError(Exception):
    """Provider failure with a stable ``code`` for the UI."""

    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = str(detail)


class Context:
    """Per-request settings handed to provider calls."""

    def __init__(self, cfg=None, ip_version="auto", stop=None):
        self.cfg = cfg
        self.ip_version = str(ip_version)
        self.stop = stop


class Session:
    """Everything the engine needs to run one test against one server."""

    kind = "http"
    # The transfer ends by itself (ndt7): a finished pool is not an error.
    self_terminating = False

    def __init__(self, provider, server, endpoint, client=None):
        self.provider = provider
        self.server = server
        self.endpoint = endpoint
        self.client = client or {}
        self.initial_streams = 4
        self.max_streams = 32
        self.fixed_streams = None
        self.max_window = None

    def spec(self, direction):
        raise NotImplementedError


class HttpSession(Session):
    """Download with GET and upload with POST over persistent connections."""

    def __init__(self, provider, server, endpoint, client=None, *, download_path,
                 upload_path, download_size, upload_size, headers=None):
        super().__init__(provider, server, endpoint, client)
        self.download_path = download_path
        self.upload_path = upload_path
        self.download_size = int(download_size)
        self.upload_size = int(upload_size)
        self.headers = dict(headers or {})

    def spec(self, direction):
        spec = {
            "kind": "http",
            "direction": direction,
            "endpoint": self.endpoint.to_dict(),
            "headers": self.headers,
            "timeout": 15,
        }
        if direction == "download":
            spec["path"] = self.download_path
            spec["size"] = self.download_size
        else:
            spec["path"] = self.upload_path
            spec["size"] = self.upload_size
        return spec


class Provider:
    id = ""
    name = ""
    homepage = ""
    # The user can choose among several servers.
    selectable_servers = True
    # The server list can be filtered with a free-text query.
    searchable = False
    # False when the provider has no public API and we speak its protocol
    # as an unofficial, compatible client.
    official_api = True
    # Identifier of a notice the user must accept before the first test, and
    # where the user can read the full text.
    notice = ""
    notice_url = ""
    single_stream = False
    cache_ttl = 300.0

    def __init__(self):
        self._cache = {}
        self._cache_lock = threading.Lock()

    def describe(self):
        return {
            "id": self.id,
            "name": self.name,
            "homepage": self.homepage,
            "selectable_servers": self.selectable_servers,
            "searchable": self.searchable,
            "official_api": self.official_api,
            "notice": self.notice,
            "notice_url": self.notice_url,
            "single_stream": self.single_stream,
        }

    # -- to implement ----------------------------------------------------
    def list_servers(self, ctx, query=""):
        """Return the candidate servers, best (lowest latency) first."""
        raise NotImplementedError

    def open(self, ctx, server_id=""):
        """Return a Session for ``server_id`` (or the best server)."""
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------
    def _cached(self, key, loader, ttl=None):
        ttl = self.cache_ttl if ttl is None else ttl
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = loader()
        with self._cache_lock:
            self._cache[key] = (now, value)
        return value

    def _forget(self, key):
        with self._cache_lock:
            self._cache.pop(key, None)


def rank_servers(servers, targets, ip_version="auto"):
    """Probe every server and sort by latency.

    ``targets`` maps server id -> ``(scheme, host, port)``. Each server dict
    gets a ``latency_ms`` value (None when unreachable). Returns
    ``(sorted_servers, endpoints_by_id)``.
    """
    probes = latency.probe_many(targets, ip_version)
    endpoints = {}
    for server in servers:
        endpoint, best = probes.get(server["id"], (None, None))
        server["latency_ms"] = round(best, 1) if best is not None else None
        if endpoint is not None:
            endpoints[server["id"]] = endpoint
    servers.sort(key=lambda s: (s["latency_ms"] is None, s["latency_ms"] or 0.0))
    return servers, endpoints


def ensure_family(endpoint, ip_version):
    """Re-pin ``endpoint`` when it was resolved for another IP version."""
    from ..engine import netutil

    wanted = netutil.family_for(ip_version)
    if not wanted or endpoint.family == wanted:
        return endpoint
    try:
        return netutil.pin_endpoint(endpoint.scheme, endpoint.host, endpoint.port, ip_version)
    except netutil.NetError as exc:
        raise ProviderError("server_unreachable", exc.detail)


def pick_server(servers, server_id=""):
    """Choose the requested server, or the best reachable one."""
    if server_id:
        for server in servers:
            if str(server["id"]) == str(server_id):
                return server
        raise ProviderError("server_not_found", server_id)
    for server in servers:
        if server.get("latency_ms") is not None:
            return server
    raise ProviderError("no_servers", "no reachable server")

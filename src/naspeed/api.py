# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""HTTP application: JSON API plus the static web UI.

API (all under ``/api``; the web UI calls it through the TOS proxy as
``/v2/proxy/naspeed/api/...``):

    GET  /api/info                 application, providers and settings
    GET  /api/state                live state of the current/last test
    GET  /api/servers?provider=&q= candidate servers, best latency first
    POST /api/test/start           {provider, server, options, accept_notice, hide_notice}
    POST /api/test/stop
    GET  /api/history?limit=
    POST /api/history/delete       {id}
    POST /api/history/clear
    GET  /api/settings
    POST /api/settings             partial settings object
    GET  /health                   no authentication, for health checks
"""

from __future__ import annotations

import os
import tarfile
import time
import urllib.parse

from . import APP_ID, APP_NAME, BETA, __version__, auth, log
from .engine import netutil
from .engine.runner import TestError
from .httpd import Response
from .providers import Context, ProviderError
from .store import CONNECTION_CHOICES, DURATIONS, HISTORY_HARD_LIMIT, IP_VERSIONS

logger = log.get("api")

# The platform proxy may or may not strip its prefix before forwarding.
PREFIXES = ("/v2/proxy/" + APP_ID, "/" + APP_ID)

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)

MAX_STATIC_FILE = 5 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, code, detail="", extra=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def strip_prefix(path):
    for prefix in PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
    return path


class StaticFiles:
    """Serves the web UI from a directory (development) or from webui.bz2."""

    def __init__(self, cfg):
        self.dev = cfg.dev
        self.directory = cfg.webui_dir
        self.files = {}
        if not self.directory:
            self._load_archive(cfg.webui_archive)

    def _load_archive(self, path):
        try:
            with tarfile.open(path, "r:bz2") as archive:
                for member in archive:
                    if not member.isfile() or member.size > MAX_STATIC_FILE:
                        continue
                    name = "/" + member.name.lstrip("./")
                    handle = archive.extractfile(member)
                    if handle is not None:
                        self.files[name] = handle.read()
            logger.info("web UI loaded from %s (%d files)", path, len(self.files))
        except (OSError, tarfile.TarError) as exc:
            logger.warning("web UI archive not available (%s): %s", path, exc)

    def _read(self, path):
        if not self.directory:
            return self.files.get(path)
        root = os.path.realpath(self.directory)
        full = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            return None
        try:
            if os.path.isfile(full) and os.path.getsize(full) <= MAX_STATIC_FILE:
                with open(full, "rb") as handle:
                    return handle.read()
        except OSError:
            pass
        return None

    def serve(self, path):
        if path.endswith("/"):
            path += "index.html"
        data = self._read(path)
        if data is None:
            return Response(404, b"Not found", "text/plain; charset=utf-8")
        extension = os.path.splitext(path)[1].lower()
        headers = {"X-Content-Type-Options": "nosniff"}
        if extension == ".html":
            # Packaged builds are stamped at build time; this covers development.
            data = data.replace(b"__VERSION__", __version__.encode("ascii", "replace"))
            headers["Cache-Control"] = "no-cache"
            headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            headers["Referrer-Policy"] = "no-referrer"
        else:
            headers["Cache-Control"] = "no-cache" if self.dev else "public, max-age=86400"
        return Response(200, data, MIME_TYPES.get(extension, "application/octet-stream"), headers)


class App:
    def __init__(self, cfg, store, runner, registry):
        self.cfg = cfg
        self.store = store
        self.runner = runner
        self.registry = registry
        self.static = StaticFiles(cfg)
        self._routes = {
            ("GET", "/info"): self._info,
            ("GET", "/state"): self._state,
            ("GET", "/servers"): self._servers,
            ("POST", "/test/start"): self._start,
            ("POST", "/test/stop"): self._stop,
            ("GET", "/history"): self._history,
            ("POST", "/history/delete"): self._history_delete,
            ("POST", "/history/clear"): self._history_clear,
            ("GET", "/settings"): self._settings_get,
            ("POST", "/settings"): self._settings_set,
        }

    # ---- dispatch ---------------------------------------------------------
    def handle(self, request):
        path = strip_prefix(request.path)
        if request.method == "OPTIONS":
            return self._preflight(request)
        if path == "/health":
            return Response.json({"status": "ok", "app": APP_ID, "version": __version__},
                                 headers={"Cache-Control": "no-store"})
        if path == "/api" or path.startswith("/api/"):
            denied = auth.check_api(request, self.cfg)
            if denied is not None:
                return self._finish(request, denied)
            route = path[4:] or "/"
            handler = self._routes.get((request.method, route))
            if handler is None:
                known = any(known_route == route for _method, known_route in self._routes)
                return self._finish(request, Response.error(405 if known else 404,
                                                            "method_not_allowed" if known else "not_found"))
            try:
                response = handler(request)
            except ApiError as exc:
                response = Response.error(exc.status, exc.code, exc.detail, exc.extra)
            except ValueError as exc:
                response = Response.error(400, "bad_request", exc)
            return self._finish(request, response)
        if request.method in ("GET", "HEAD"):
            denied = auth.check_network(request, self.cfg)
            if denied is not None:
                return denied
            return self.static.serve(path)
        return Response.error(405, "method_not_allowed")

    def _finish(self, request, response):
        response.headers.setdefault("Cache-Control", "no-store")
        return self._cors(request, response)

    def _same_origin(self, request):
        origin = request.header("Origin")
        if not origin:
            return ""
        host = request.header("X-Forwarded-Host") or request.header("Host")
        if urllib.parse.urlsplit(origin).netloc.lower() == host.split(",")[0].strip().lower():
            return origin
        return ""

    def _cors(self, request, response):
        origin = self._same_origin(request)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    def _preflight(self, request):
        response = Response(204, b"", "text/plain; charset=utf-8", {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Csrf-Token, Cookie, X-Requested-With",
            "Access-Control-Max-Age": "600",
        })
        return self._cors(request, response)

    # ---- helpers ----------------------------------------------------------
    def _provider(self, provider_id):
        provider = self.registry.get(str(provider_id or ""))
        if provider is None:
            raise ApiError(400, "unknown_provider", provider_id)
        return provider

    def _mode(self, request):
        if self.cfg.dev:
            return "dev"
        return "platform" if request.transport == "unix" else "port"

    # ---- handlers ---------------------------------------------------------
    def _info(self, request):
        return Response.json({
            "app": APP_ID,
            "name": APP_NAME,
            "version": __version__,
            "beta": BETA,
            "mode": self._mode(request),
            "providers": [provider.describe() for provider in self.registry.values()],
            "settings": self.store.get_settings(),
            "options": {
                "durations": DURATIONS,
                "connections": list(CONNECTION_CHOICES),
                "ip_versions": list(IP_VERSIONS),
                "history_max": HISTORY_HARD_LIMIT,
            },
            "history_count": self.store.history_count(),
            "running": self.runner.running(),
        })

    def _state(self, request):
        return Response.json({"running": self.runner.running(), "test": self.runner.snapshot()})

    def _servers(self, request):
        provider = self._provider(request.query.get("provider"))
        settings = self.store.get_settings()
        ctx = Context(self.cfg, settings.get("ip_version", "auto"))
        query = request.query.get("q", "") if provider.searchable else ""
        try:
            servers = provider.list_servers(ctx, query)
        except (ProviderError, netutil.NetError) as exc:
            raise ApiError(502, exc.code, exc.detail)
        return Response.json({
            "provider": provider.id,
            "selectable": provider.selectable_servers,
            "servers": servers,
        })

    def _start(self, request):
        body = request.json()
        provider = self._provider(body.get("provider"))
        settings = self.store.get_settings()
        accepted = body.get("accept_notice") is True
        if provider.notice and not settings.get("accepted_notices", {}).get(provider.notice):
            if accepted:
                self.store.update_settings({"accepted_notices": {provider.notice: int(time.time())}})
            else:
                raise ApiError(412, "notice_required", extra={"notice": provider.notice})
        if provider.notice and accepted and body.get("hide_notice") is True:
            # "Don't show this again", ticked in the notice the user has just accepted.
            self.store.update_settings({"hidden_notices": {provider.notice: True}})
        options = body.get("options") if isinstance(body.get("options"), dict) else None
        try:
            state = self.runner.start(provider.id, str(body.get("server") or ""), options, watched=True)
        except TestError as exc:
            raise ApiError(409 if exc.code == "busy" else 400, exc.code, exc.detail)
        if settings.get("default_provider") != provider.id:
            self.store.update_settings({"default_provider": provider.id}, tuple(self.registry))
        return Response.json({"running": True, "test": state})

    def _stop(self, request):
        self.runner.stop()
        return Response.json({"ok": True})

    def _history(self, request):
        settings = self.store.get_settings()
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0
        items = self.store.list_history(limit if limit > 0 else None)
        return Response.json({
            "items": items,
            "count": self.store.history_count(),
            "enabled": bool(settings.get("save_history", True)),
            "limit": int(settings.get("history_limit", 300)),
        })

    def _history_delete(self, request):
        result_id = str(request.json().get("id") or "")
        if not result_id:
            raise ApiError(400, "bad_request", "id is required")
        return Response.json({"ok": self.store.delete_result(result_id)})

    def _history_clear(self, request):
        self.store.clear_history()
        return Response.json({"ok": True})

    def _settings_get(self, request):
        return Response.json(self.store.get_settings())

    def _settings_set(self, request):
        patch = request.json()
        patch.pop("accepted_notices", None)  # only set through /test/start
        hidden = patch.get("hidden_notices")
        if isinstance(hidden, dict):
            # Only notices that exist, so made-up keys never reach settings.json.
            known = {provider.notice for provider in self.registry.values() if provider.notice}
            patch["hidden_notices"] = {key: value for key, value in hidden.items() if key in known}
        return Response.json(self.store.update_settings(patch, tuple(self.registry)))

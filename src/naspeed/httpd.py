# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Small threaded HTTP/1.1 server, usable over TCP and over a Unix socket.

On TOS the backend is reached through the platform proxy, which talks HTTP
to ``/var/api/naspeed.sock``. The TCP listener is used for development and
for the optional "own port" mode.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import urllib.parse

from . import APP_NAME, log

logger = log.get("http")

MAX_BODY = 64 * 1024
IDLE_TIMEOUT = 30
BACKLOG = 128

UNIX_AVAILABLE = hasattr(socket, "AF_UNIX") and hasattr(socketserver, "UnixStreamServer")


class Request:
    __slots__ = ("method", "path", "query", "headers", "body", "transport", "client_ip")

    def __init__(self, method, path, query, headers, body, transport, client_ip):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.transport = transport
        self.client_ip = client_ip

    def header(self, name, default=""):
        value = self.headers.get(name)
        return default if value is None else str(value)

    def header_values(self, name):
        return [str(v) for v in (self.headers.get_all(name) or [])]

    def json(self):
        """Parse the body as a JSON object ({} when empty)."""
        if not self.body:
            return {}
        data = json.loads(self.body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data


class Response:
    __slots__ = ("status", "body", "content_type", "headers")

    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = dict(headers or {})

    @classmethod
    def json(cls, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return cls(status, body, "application/json; charset=utf-8", headers)

    @classmethod
    def error(cls, status, code, detail="", extra=None):
        payload = {"error": code}
        if detail:
            payload["detail"] = str(detail)[:300]
        if extra:
            payload.update(extra)
        return cls.json(payload, status)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP_NAME
    sys_version = ""
    timeout = IDLE_TIMEOUT

    def setup(self):
        super().setup()
        if self.server.transport == "tcp":
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

    def version_string(self):
        return APP_NAME

    def address_string(self):
        return self._client_ip() or "unix"

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        logger.debug("%s %s", self.address_string(), format % args)

    def _client_ip(self):
        address = self.client_address
        if isinstance(address, tuple) and address and self.server.transport == "tcp":
            return str(address[0])
        return ""

    def _handle(self):
        try:
            split = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(split.path) or "/"
            query = {
                key: values[-1]
                for key, values in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()
            }
            body = b""
            length = self.headers.get("Content-Length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    size = -1
                if size < 0 or size > MAX_BODY:
                    self.close_connection = True
                    return self._write(Response.error(413, "body_too_large"))
                if size:
                    body = self.rfile.read(size)
            elif self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self._write(Response.error(411, "length_required"))
            request = Request(self.command, path, query, self.headers, body,
                              self.server.transport, self._client_ip())
            response = self.server.app.handle(request)
        except Exception:
            logger.exception("unhandled error while serving %s %s", self.command, self.path)
            response = Response.error(500, "internal_error")
        self._write(response)

    do_GET = do_POST = do_HEAD = do_OPTIONS = _handle

    def _write(self, response):
        body = response.body or b""
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except OSError:
            self.close_connection = True


class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = BACKLOG
    transport = "tcp"

    def __init__(self, host, port, app):
        self.app = app
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), _Handler)

    def handle_error(self, request, client_address):
        logger.debug("connection error from %s", client_address, exc_info=True)


def tcp_server(host, port, app):
    return _TCPServer(host, port, app)


if UNIX_AVAILABLE:

    class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        request_queue_size = BACKLOG
        transport = "unix"

        def __init__(self, path, app, mode):
            self.app = app
            self.socket_mode = mode
            super().__init__(path, _Handler)

        def server_bind(self):
            path = self.server_address
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            except OSError:
                pass  # bind() below reports the real problem, if there is one
            try:
                os.unlink(path)  # stale socket left by a previous run
            except FileNotFoundError:
                pass
            super().server_bind()
            os.chmod(path, self.socket_mode)

        def get_request(self):
            request, _address = super().get_request()
            return request, ("", 0)

        def handle_error(self, request, client_address):
            logger.debug("connection error on the unix socket", exc_info=True)


def unix_server(path, app, mode=0o660):
    if not UNIX_AVAILABLE:
        raise OSError("Unix sockets are not available on this platform")
    return _UnixServer(path, app, mode)

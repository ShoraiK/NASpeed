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
import threading
import urllib.parse

from . import APP_NAME, log

logger = log.get("http")

MAX_BODY = 64 * 1024
MAX_CHUNKS = 256
MAX_TRAILERS = 32
MAX_LINE = 1024
MAX_WORKERS = 32
IDLE_TIMEOUT = 30
BACKLOG = 128

_BUSY_BODY = b'{"error":"server_busy"}'
_BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: application/json; charset=utf-8\r\n"
    b"Content-Length: %d\r\n"
    b"Retry-After: 1\r\n"
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n\r\n" % len(_BUSY_BODY)
) + _BUSY_BODY

UNIX_AVAILABLE = hasattr(socket, "AF_UNIX") and hasattr(socketserver, "UnixStreamServer")


class _BodyTooLarge(ValueError):
    """Chunked request body exceeds ``MAX_BODY``."""


def _read_exact(rfile, size):
    """Read exactly ``size`` bytes from ``rfile``, or raise ValueError on EOF."""
    data = bytearray()
    while len(data) < size:
        chunk = rfile.read(size - len(data))
        if not chunk:
            raise ValueError("truncated request body")
        data += chunk
    return bytes(data)


def _read_line(rfile, limit=MAX_LINE):
    """Read one LF-terminated line within ``limit`` bytes, or raise ValueError."""
    line = rfile.readline(limit + 1)
    if not line:
        raise ValueError("unexpected end of chunked body")
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("chunked line too long or unterminated")
    return line


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

    def _read_chunked(self):
        """A body in chunked transfer encoding; ValueError if malformed or too big."""
        body = bytearray()
        chunks = 0
        while True:
            raw_size = _read_line(self.rfile).split(b";", 1)[0].strip()
            if not raw_size or raw_size.startswith((b"+", b"-")) or b"_" in raw_size:
                raise ValueError("invalid chunk size")
            size = int(raw_size, 16)
            if size == 0:
                for _ in range(MAX_TRAILERS + 1):
                    if _read_line(self.rfile) in (b"\r\n", b"\n"):
                        return bytes(body)
                raise ValueError("too many chunked trailers")
            chunks += 1
            if chunks > MAX_CHUNKS:
                raise ValueError("too many chunks")
            if len(body) + size > MAX_BODY:
                raise _BodyTooLarge("body too large")
            body += _read_exact(self.rfile, size)
            if _read_line(self.rfile) not in (b"\r\n", b"\n"):
                raise ValueError("malformed chunk terminator")

    def _client_ip(self):
        address = self.client_address
        if isinstance(address, tuple) and address and self.server.transport == "tcp":
            return str(address[0])
        return ""

    def _method_not_allowed(self):
        self.close_connection = True
        resp = Response.error(405, "method_not_allowed")
        resp.headers["Allow"] = "GET, POST, HEAD, OPTIONS"
        resp.headers["Cache-Control"] = "no-store"
        self._write(resp)

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _handle(self):
        try:
            split = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(split.path) or "/"
            query = {
                key: values[-1]
                for key, values in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()
            }
            body = b""
            lengths = self.headers.get_all("Content-Length")
            transfer_encodings = self.headers.get_all("Transfer-Encoding")
            has_te = transfer_encodings is not None and any(v.strip() for v in transfer_encodings)
            if lengths and has_te:
                self.close_connection = True
                return self._write(Response.error(400, "bad_request"))
            if lengths:
                raw_values = [value.strip() for value in lengths]
                if len(set(raw_values)) != 1 or not raw_values[0].isascii() or not raw_values[0].isdigit():
                    self.close_connection = True
                    return self._write(Response.error(400, "bad_request"))
                size = int(raw_values[0])
                if size > MAX_BODY:
                    self.close_connection = True
                    return self._write(Response.error(413, "body_too_large"))
                if size:
                    try:
                        body = _read_exact(self.rfile, size)
                    except ValueError:
                        self.close_connection = True
                        return self._write(Response.error(400, "bad_request"))
            elif has_te:
                # Some proxies send even an empty body in chunks.
                if len(transfer_encodings) == 1 and transfer_encodings[0].strip().lower() == "chunked":
                    try:
                        body = self._read_chunked()
                    except _BodyTooLarge:
                        self.close_connection = True
                        return self._write(Response.error(413, "body_too_large"))
                    except ValueError:
                        self.close_connection = True
                        return self._write(Response.error(400, "bad_request"))
                else:
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
            if self.close_connection and not any(k.lower() == "connection" for k in response.headers):
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except OSError:
            self.close_connection = True


class _BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """Thread-per-request mix-in with a hard cap on active worker threads."""

    daemon_threads = True
    block_on_close = False
    max_workers = MAX_WORKERS

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_workers)
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            logger.warning("worker limit reached (%d); rejecting %s", self.max_workers, client_address)
            try:
                request.settimeout(1.0)
                request.sendall(_BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise


class _TCPServer(_BoundedThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    request_queue_size = BACKLOG
    transport = "tcp"

    def __init__(self, host, port, app, max_workers=MAX_WORKERS):
        self.app = app
        self.max_workers = max(1, int(max_workers))
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), _Handler)

    def handle_error(self, request, client_address):
        logger.debug("connection error from %s", client_address, exc_info=True)


def tcp_server(host, port, app, max_workers=MAX_WORKERS):
    return _TCPServer(host, port, app, max_workers=max_workers)


if UNIX_AVAILABLE:

    class _UnixServer(_BoundedThreadingMixIn, socketserver.UnixStreamServer):
        request_queue_size = BACKLOG
        transport = "unix"

        def __init__(self, path, app, mode, max_workers=MAX_WORKERS):
            self.app = app
            self.socket_mode = mode
            self.max_workers = max(1, int(max_workers))
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


def unix_server(path, app, mode=0o660, max_workers=MAX_WORKERS):
    if not UNIX_AVAILABLE:
        raise OSError("Unix sockets are not available on this platform")
    return _UnixServer(path, app, mode, max_workers=max_workers)

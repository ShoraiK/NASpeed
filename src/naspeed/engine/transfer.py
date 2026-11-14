# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Data-plane streams: the loops that actually move bytes.

They normally run inside dedicated worker processes (``naspeed worker``).
Python's interpreter lock makes several TLS connections in one process
collapse to a few Gbit/s in total, while one connection per process stays
above 10 Gbit/s, so the pool starts one process per TLS connection.
The same code can also run in threads of the main process as a fallback.

A worker reads one JSON line (the spec) on stdin and reports on stdout:

    P <bytes> <active> <errors>     progress (cumulative byte count)
    I <json>                        provider-specific measurements (ndt7)
    E <text>                        last error message
    D                               all streams have ended

It stops when stdin is closed or receives the line ``stop``.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import random
import select
import socket
import struct
import sys
import threading
import time

from . import netutil

RECV_BUFFER = 1 << 20
UPLOAD_BLOCK = 1 << 20
MIN_CHUNK = 16 * 1024
MAX_CHUNK = 1 << 20
REPORT_INTERVAL = 0.025
MAX_STREAM_FAILURES = 4

NDT7_SUBPROTOCOL = "net.measurementlab.ndt.v7"
NDT7_MIN_MESSAGE = 1 << 13
NDT7_MAX_MESSAGE = 1 << 20
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class TransferError(Exception):
    """A stream-level failure. ``fatal`` means retrying cannot help."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class Counters:
    """State shared between the stream threads and whoever reports it."""

    def __init__(self, streams):
        self.bytes = [0] * streams
        self.active = [0] * streams
        self.socks = [None] * streams
        self.errors = 0
        self.last_error = ""
        self.info = {}
        self.info_seq = 0

    def total(self):
        return sum(self.bytes)

    def active_count(self):
        return sum(self.active)

    def fail(self, exc):
        self.errors += 1
        text = "%s: %s" % (type(exc).__name__, exc)
        self.last_error = " ".join(text.split())[:240]

    def set_info(self, **values):
        self.info.update(values)
        self.info_seq += 1

    def close_sockets(self):
        """Unblock streams stuck in a send/recv by shutting their sockets."""
        for sock in list(self.socks):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass


# Linux only. Caps how much *unsent* data the kernel will queue per socket.
# Without it the send buffer keeps growing while an upload ramps up, and bytes
# handed to the kernel would be counted long before they reach the wire.
_NOTSENT_LOWAT = getattr(socket, "TCP_NOTSENT_LOWAT", 25 if sys.platform.startswith("linux") else None)
NOTSENT_LOWAT_BYTES = 128 * 1024


def _tune_upload_socket(sock):
    if _NOTSENT_LOWAT is None or sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, _NOTSENT_LOWAT, NOTSENT_LOWAT_BYTES)
    except (OSError, ValueError):
        pass


_payload_view = None
_payload_lock = threading.Lock()


def _payload():
    """Random (incompressible) upload data, generated once per process."""
    global _payload_view
    if _payload_view is None:
        with _payload_lock:
            if _payload_view is None:
                _payload_view = memoryview(os.urandom(UPLOAD_BLOCK))
    return _payload_view


def _fill(template, size):
    return (
        template.replace("{size_mib}", str(max(1, size // (1 << 20))))
        .replace("{size}", str(size))
        .replace("{rand}", "%012x" % random.getrandbits(48))
    )


def _first_size(size, idx):
    """Size of a stream's first request.

    All streams start together; with equal request sizes they would also
    finish together and pause at the same instant while waiting for the
    server's reply. Shortening the first request by a different amount on
    each stream spreads those pauses out.
    """
    return max(min(size, 1 << 20), size * ((idx % 4) + 1) // 4)


def _headers(spec, extra=None):
    headers = {"User-Agent": netutil.USER_AGENT, "Accept": "*/*"}
    headers.update(spec.get("headers") or {})
    if extra:
        headers.update(extra)
    return headers


def _close(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP ----

def _http_download(spec, endpoint, idx, ctr, stop):
    view = memoryview(bytearray(RECV_BUFFER))
    headers = _headers(spec)
    timeout = float(spec.get("timeout", 15))
    verify = bool(spec.get("verify", True))
    full_size = int(spec["size"])
    size = _first_size(full_size, idx)
    counts = ctr.bytes
    conn = None
    failures = 0
    while not stop.is_set():
        try:
            if conn is None:
                conn = netutil.PinnedHTTPConnection(endpoint, timeout, verify)
            if conn.sock is None:
                conn.connect()
            ctr.socks[idx] = conn.sock
            conn.request("GET", _fill(spec["path"], size), headers=headers)
            size = full_size
            resp = conn.getresponse()
            if resp.status != 200:
                raise TransferError("HTTP %d" % resp.status, fatal=400 <= resp.status < 500)
            ctr.active[idx] = 1
            readinto = resp.readinto
            while not stop.is_set():
                got = readinto(view)
                if not got:
                    break
                counts[idx] += got
            failures = 0
        except (OSError, http.client.HTTPException, TransferError) as exc:
            ctr.active[idx] = 0
            _close(conn)
            conn = None
            if stop.is_set():
                break
            failures += 1
            ctr.fail(exc)
            if failures >= MAX_STREAM_FAILURES or getattr(exc, "fatal", False):
                break
            stop.wait(0.15 * failures)
    _close(conn)


def _http_upload(spec, endpoint, idx, ctr, stop):
    payload = _payload()
    timeout = float(spec.get("timeout", 15))
    verify = bool(spec.get("verify", True))
    full_size = int(spec["size"])
    size = _first_size(full_size, idx)
    counts = ctr.bytes
    state = {"chunk": MIN_CHUNK}
    conn = None
    failures = 0

    def body(size):
        # Chunks are sized so each takes a few tens of milliseconds to send:
        # small enough for smooth progress, large enough to stay efficient.
        sent = 0
        chunk = state["chunk"]
        last = time.perf_counter()
        while sent < size and not stop.is_set():
            count = min(chunk, size - sent)
            yield payload[:count]
            sent += count
            counts[idx] += count
            now = time.perf_counter()
            elapsed = now - last
            last = now
            if elapsed < 0.012 and chunk < MAX_CHUNK:
                chunk *= 2
            elif elapsed > 0.08 and chunk > MIN_CHUNK:
                chunk //= 2
        state["chunk"] = chunk
        state["complete"] = sent >= size

    while not stop.is_set():
        try:
            if conn is None:
                conn = netutil.PinnedHTTPConnection(endpoint, timeout, verify)
            if conn.sock is None:
                conn.connect()
                _tune_upload_socket(conn.sock)
            ctr.socks[idx] = conn.sock
            headers = _headers(spec, {"Content-Length": str(size)})
            headers.setdefault("Content-Type", "application/octet-stream")
            state["complete"] = False
            ctr.active[idx] = 1
            conn.request("POST", _fill(spec["path"], size), body=body(size), headers=headers)
            if not state["complete"]:
                break  # stopped half-way through the body: just drop the connection
            resp = conn.getresponse()
            resp.read()
            if resp.status not in (200, 201, 202, 204):
                raise TransferError("HTTP %d" % resp.status, fatal=400 <= resp.status < 500)
            failures = 0
            size = full_size
        except (OSError, http.client.HTTPException, TransferError) as exc:
            ctr.active[idx] = 0
            _close(conn)
            conn = None
            if stop.is_set():
                break
            failures += 1
            ctr.fail(exc)
            if failures >= MAX_STREAM_FAILURES or getattr(exc, "fatal", False):
                break
            stop.wait(0.15 * failures)
    _close(conn)


# ---------------------------------------------------------------- ndt7 ----

def _ws_connect(endpoint, path, timeout, verify=True):
    """Open a WebSocket to an ndt7 server. Returns ``(sock, leftover_bytes)``."""
    sock = netutil.open_socket(endpoint, timeout, verify)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        default_port = 443 if endpoint.tls else 80
        host = endpoint.host if endpoint.port == default_port else "%s:%d" % (endpoint.host, endpoint.port)
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: %s\r\n"
            "User-Agent: %s\r\n\r\n"
        ) % (path, host, key, NDT7_SUBPROTOCOL, netutil.USER_AGENT)
        sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise TransferError("websocket handshake closed by server")
            head += chunk
            if len(head) > 16384:
                raise TransferError("websocket handshake too large")
        head, _sep, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            code = status[1] if len(status) > 1 else "?"
            raise TransferError("websocket upgrade refused (HTTP %s)" % code, fatal=True)
        headers = {}
        for line in lines[1:]:
            name, _colon, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise TransferError("websocket handshake mismatch", fatal=True)
        return sock, rest
    except BaseException:
        sock.close()
        raise


class _WsReader:
    """Minimal WebSocket frame reader (server-to-client frames are unmasked)."""

    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.pending = bytearray(initial)

    def exact(self, count):
        while len(self.pending) < count:
            chunk = self.sock.recv(count - len(self.pending))
            if not chunk:
                raise EOFError("connection closed")
            self.pending += chunk
        data = bytes(self.pending[:count])
        del self.pending[:count]
        return data

    def header(self):
        first = self.exact(2)
        opcode = first[0] & 0x0F
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.exact(8))[0]
        if first[1] & 0x80:
            self.exact(4)
        return opcode, length

    def readable(self):
        if self.pending:
            return True
        pending = getattr(self.sock, "pending", None)
        if pending is not None and pending():
            return True
        return bool(select.select([self.sock], [], [], 0)[0])


def _ws_mask(data, key):
    count = len(data)
    pad = (key * (count // 4 + 1))[:count]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(count, "big")


def _ws_frame(opcode, payload, key):
    """Build one masked client-to-server frame."""
    count = len(payload)
    if count < 126:
        head = bytes([0x80 | opcode, 0x80 | count])
    elif count < 65536:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", count)
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", count)
    return head + key + (_ws_mask(payload, key) if count else b"")


def _ndt7_measurement(ctr, raw):
    """Keep the interesting bits of a server measurement message."""
    try:
        message = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return
    values = {}
    conn_info = message.get("ConnectionInfo") or {}
    client = str(conn_info.get("Client") or "")
    if client:
        values["client_ip"] = client.rsplit(":", 1)[0].strip("[]")
    tcp = message.get("TCPInfo") or {}
    for source, target, scale in (
        ("MinRTT", "min_rtt_ms", 0.001),
        ("RTT", "rtt_ms", 0.001),
        ("BytesReceived", "server_bytes_received", 1),
        ("BytesAcked", "server_bytes_acked", 1),
        ("BytesRetrans", "server_bytes_retrans", 1),
        ("ElapsedTime", "server_elapsed_us", 1),
    ):
        value = tcp.get(source)
        if isinstance(value, (int, float)):
            values[target] = round(value * scale, 3) if scale != 1 else value
    if values:
        ctr.set_info(**values)


def _ws_control(reader, sock, key, ctr, opcode, length):
    """Handle a non-binary frame. Returns False when the peer closed."""
    if opcode == 0x8:
        return False
    if length > 65536:
        raise TransferError("oversized websocket control/text frame")
    data = reader.exact(length)
    if opcode == 0x1:
        _ndt7_measurement(ctr, data)
    elif opcode == 0x9:
        sock.sendall(_ws_frame(0xA, data[:125], key))
    return True


def _ndt7_download(spec, endpoint, idx, ctr, stop):
    timeout = float(spec.get("timeout", 15))
    sock, rest = _ws_connect(endpoint, spec["path"], timeout, bool(spec.get("verify", True)))
    ctr.socks[idx] = sock
    reader = _WsReader(sock, rest)
    view = memoryview(bytearray(RECV_BUFFER))
    counts = ctr.bytes
    key = os.urandom(4)
    recv_into = sock.recv_into
    try:
        ctr.active[idx] = 1
        while not stop.is_set():
            opcode, length = reader.header()
            if opcode in (0x0, 0x2):
                left = length
                take = min(len(reader.pending), left)
                if take:
                    del reader.pending[:take]
                    left -= take
                    counts[idx] += take
                while left > 0:
                    got = recv_into(view if left >= RECV_BUFFER else view[:left])
                    if not got:
                        raise EOFError("connection closed")
                    left -= got
                    counts[idx] += got
                    if stop.is_set():
                        return
            else:
                counts[idx] += length
                if not _ws_control(reader, sock, key, ctr, opcode, length):
                    break
    except EOFError:
        pass
    finally:
        ctr.active[idx] = 0
        try:
            sock.close()
        except OSError:
            pass


def _ndt7_upload(spec, endpoint, idx, ctr, stop):
    timeout = float(spec.get("timeout", 15))
    sock, rest = _ws_connect(endpoint, spec["path"], timeout, bool(spec.get("verify", True)))
    _tune_upload_socket(sock)
    ctr.socks[idx] = sock
    reader = _WsReader(sock, rest)
    counts = ctr.bytes
    key = os.urandom(4)
    # Masking is done once: any prefix of the masked block is a validly
    # masked payload for a frame of that length.
    masked = _ws_mask(os.urandom(NDT7_MAX_MESSAGE), key)
    frames = {}

    def frame(size):
        data = frames.get(size)
        if data is None:
            if size < 65536:
                head = bytes([0x82, 0x80 | 126]) + struct.pack("!H", size)
            else:
                head = bytes([0x82, 0x80 | 127]) + struct.pack("!Q", size)
            data = frames[size] = head + key + masked[:size]
        return data

    size = NDT7_MIN_MESSAGE
    total = 0
    last_poll = time.perf_counter()
    try:
        ctr.active[idx] = 1
        while not stop.is_set():
            sock.sendall(frame(size))
            total += size
            counts[idx] += size
            # ndt7 message scaling: keep each message under 1/16 of the bytes sent.
            if size < NDT7_MAX_MESSAGE and size < total // 16:
                size *= 2
            now = time.perf_counter()
            if now - last_poll >= 0.1:
                last_poll = now
                open_ = True
                while open_ and reader.readable():
                    opcode, length = reader.header()
                    open_ = _ws_control(reader, sock, key, ctr, opcode, length)
                if not open_:
                    break
        try:
            sock.sendall(_ws_frame(0x8, b"", key))
        except OSError:
            pass
    except EOFError:
        pass
    finally:
        ctr.active[idx] = 0
        try:
            sock.close()
        except OSError:
            pass


# ------------------------------------------------------------- running ----

_STREAMS = {
    ("http", "download"): _http_download,
    ("http", "upload"): _http_upload,
    ("ndt7", "download"): _ndt7_download,
    ("ndt7", "upload"): _ndt7_upload,
}


def _guard(target, spec, endpoint, idx, ctr, stop):
    try:
        target(spec, endpoint, idx, ctr, stop)
    except Exception as exc:  # a stream must never take the worker down
        if not stop.is_set():
            ctr.fail(exc)
    finally:
        ctr.active[idx] = 0


def start_streams(spec, stop):
    """Start the stream threads for ``spec``. Returns ``(counters, threads)``."""
    endpoint = netutil.Endpoint.from_dict(spec["endpoint"])
    streams = max(1, int(spec.get("streams", 1)))
    target = _STREAMS[(spec.get("kind", "http"), spec["direction"])]
    ctr = Counters(streams)
    threads = []
    for idx in range(streams):
        thread = threading.Thread(
            target=_guard, args=(target, spec, endpoint, idx, ctr, stop),
            name="stream-%d" % idx, daemon=True,
        )
        thread.start()
        threads.append(thread)
    return ctr, threads


def worker_main():
    """Entry point of ``naspeed worker``."""
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    try:
        spec = json.loads(stdin.readline().decode("utf-8"))
        stop = threading.Event()
        ctr, threads = start_streams(spec, stop)
    except Exception as exc:
        out.write(b"E " + ("bad spec: %s" % exc).encode("utf-8", "replace")[:240] + b"\nD\n")
        out.flush()
        os._exit(2)

    def watch_stdin():
        try:
            while True:
                line = stdin.readline()
                if not line or line.strip() == b"stop":
                    break
        except Exception:
            pass
        stop.set()

    threading.Thread(target=watch_stdin, name="stdin", daemon=True).start()

    last = None
    last_info = 0
    last_error = ""
    try:
        while True:
            alive = any(thread.is_alive() for thread in threads)
            snapshot = (ctr.total(), ctr.active_count(), ctr.errors)
            lines = []
            if snapshot != last:
                last = snapshot
                lines.append(b"P %d %d %d\n" % snapshot)
            if ctr.info_seq != last_info:
                last_info = ctr.info_seq
                lines.append(b"I " + json.dumps(ctr.info).encode("utf-8") + b"\n")
            if ctr.last_error != last_error:
                last_error = ctr.last_error
                lines.append(b"E " + last_error.encode("utf-8", "replace") + b"\n")
            if lines:
                out.write(b"".join(lines))
                out.flush()
            if stop.is_set() or not alive:
                break
            stop.wait(REPORT_INTERVAL)
    except OSError:
        pass  # the parent went away
    stop.set()
    ctr.close_sockets()
    for thread in threads:
        thread.join(0.5)
    try:
        out.write(b"P %d 0 %d\nD\n" % (ctr.total(), ctr.errors))
        out.flush()
    except OSError:
        pass
    # Skip interpreter teardown: a stream thread may still be inside a TLS
    # call, and finalising under it can crash the process.
    os._exit(0)

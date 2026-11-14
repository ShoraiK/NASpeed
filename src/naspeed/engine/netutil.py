# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Low-level networking helpers shared by the control and data planes."""

from __future__ import annotations

import http.client
import json
import math
import socket
import ssl
import threading

from .. import APP_NAME, __version__

# urllib is only needed for control-plane requests and is imported lazily:
# transfer workers never use it, and skipping it makes them start faster.

# We identify ourselves honestly; every provider endpoint we use accepts it.
USER_AGENT = "%s/%s (network speed test client)" % (APP_NAME, __version__.split("-")[0])

CONTROL_TIMEOUT = 8.0
MAX_CONTROL_BYTES = 4 * 1024 * 1024


class NetError(Exception):
    """A network-level failure with a stable ``code`` the UI can translate."""

    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail) if detail else code)
        self.code = code
        self.detail = str(detail)


class Endpoint:
    """A test server address, pinned to one resolved IP so that the latency
    probe and every transfer connection talk to the very same machine."""

    __slots__ = ("scheme", "host", "port", "ip", "family")

    def __init__(self, scheme, host, port, ip="", family=0):
        self.scheme = scheme
        self.host = host
        self.port = int(port)
        self.ip = ip
        self.family = int(family)

    @property
    def tls(self):
        return self.scheme in ("https", "wss")

    @property
    def origin(self):
        default = 443 if self.tls else 80
        scheme = "https" if self.tls else "http"
        if self.port == default:
            return "%s://%s" % (scheme, self.host)
        return "%s://%s:%d" % (scheme, self.host, self.port)

    def to_dict(self):
        return {"scheme": self.scheme, "host": self.host, "port": self.port,
                "ip": self.ip, "family": self.family}

    @classmethod
    def from_dict(cls, data):
        return cls(data["scheme"], data["host"], data["port"],
                   data.get("ip", ""), data.get("family", 0))

    def __repr__(self):
        return "Endpoint(%s://%s:%d ip=%s)" % (self.scheme, self.host, self.port, self.ip or "?")


_ctx_lock = threading.Lock()
_contexts = {}


def tls_context(verify=True):
    """Shared client TLS context (HTTP/1.1 only, certificates verified)."""
    with _ctx_lock:
        ctx = _contexts.get(verify)
        if ctx is None:
            ctx = ssl.create_default_context()
            try:
                # TOS ships python3-certifi; use it as an extra trust source.
                import certifi  # type: ignore

                ctx.load_verify_locations(certifi.where())
            except Exception:
                pass
            try:
                ctx.set_alpn_protocols(["http/1.1"])
            except (NotImplementedError, ssl.SSLError):
                pass
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            _contexts[verify] = ctx
        return ctx


def family_for(ip_version):
    if str(ip_version) == "4":
        return socket.AF_INET
    if str(ip_version) == "6":
        return socket.AF_INET6
    return socket.AF_UNSPEC


def resolve(host, port, ip_version="auto"):
    """Return ``[(family, ip), ...]`` in the order the OS prefers them."""
    try:
        infos = socket.getaddrinfo(host, port, family_for(ip_version), socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise NetError("dns_failed", "%s: %s" % (host, exc))
    seen = []
    for family, _type, _proto, _canon, sockaddr in infos:
        item = (int(family), sockaddr[0])
        if item not in seen:
            seen.append(item)
    if not seen:
        raise NetError("dns_failed", host)
    return seen


def tcp_connect(ip, port, family, timeout):
    sock = socket.socket(family or socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except BaseException:
        sock.close()
        raise
    return sock


def pin_endpoint(scheme, host, port, ip_version="auto", timeout=4.0):
    """Resolve ``host`` and return an Endpoint pinned to the first address
    that accepts a TCP connection."""
    last = None
    for family, ip in resolve(host, port, ip_version):
        try:
            tcp_connect(ip, port, family, timeout).close()
            return Endpoint(scheme, host, port, ip, family)
        except OSError as exc:
            last = exc
    raise NetError("connect_failed", "%s:%s (%s)" % (host, port, last))


def open_socket(endpoint, timeout, verify=True):
    """Open a (TLS-wrapped when needed) socket to a pinned endpoint."""
    if endpoint.ip:
        sock = tcp_connect(endpoint.ip, endpoint.port, endpoint.family, timeout)
    else:
        sock = socket.create_connection((endpoint.host, endpoint.port), timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if endpoint.tls:
        try:
            sock = tls_context(verify).wrap_socket(sock, server_hostname=endpoint.host)
        except BaseException:
            sock.close()
            raise
    return sock


class PinnedHTTPConnection(http.client.HTTPConnection):
    """``http.client`` connection that dials a pinned Endpoint (with SNI and
    certificate checks still done against the host name)."""

    def __init__(self, endpoint, timeout=15.0, verify=True):
        self.default_port = 443 if endpoint.tls else 80
        super().__init__(endpoint.host, endpoint.port, timeout=timeout)
        self._endpoint = endpoint
        self._verify = verify

    def connect(self):
        self.sock = open_socket(self._endpoint, self.timeout, self._verify)


_opener = None


def _control_opener():
    global _opener
    if _opener is None:
        import urllib.request

        _opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=tls_context(True)),
        )
    return _opener


def http_fetch(url, headers=None, timeout=CONTROL_TIMEOUT, method="GET", data=None):
    """Small control-plane request. Returns ``(status, headers, body)``."""
    import urllib.error
    import urllib.request

    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    hdrs.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _control_opener().open(request, timeout=timeout) as resp:
            body = resp.read(MAX_CONTROL_BYTES + 1)
            if len(body) > MAX_CONTROL_BYTES:
                raise NetError("response_too_large", url)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except Exception:
            body = b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise NetError("tls_cert_failed", "%s: %s" % (url.split("?")[0], reason))
        raise NetError("request_failed", "%s: %s" % (url.split("?")[0], reason))


def fetch_json(url, headers=None, timeout=CONTROL_TIMEOUT):
    status, _headers, body = http_fetch(url, headers, timeout)
    if status != 200:
        raise NetError("http_status", "%s -> HTTP %s" % (url.split("?")[0], status))
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise NetError("bad_response", "%s: %s" % (url.split("?")[0], exc))


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres (None when a coordinate is missing)."""
    try:
        lat1, lon1, lat2, lon2 = (math.radians(float(v)) for v in (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        return None
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return round(6371.0 * 2 * math.asin(min(1.0, math.sqrt(a))), 1)

# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Access control.

Two situations:

* **Unix socket (TOS platform proxy).** The TOS session is carried by two
  headers that must both be present (guide, sections 8.9 and 8.10):
  ``Cookie`` (with ``TMSESSNAME`` and ``X-Csrf-Token``) and ``X-Csrf-Token``,
  whose value has to match the cookie. Anything else is ``401``.

* **TCP port.** The platform cannot authenticate this traffic, so the
  application restricts it itself: only clients on local/private networks,
  only LAN-style host names (which defeats DNS rebinding), and a custom header
  on state-changing requests (which defeats cross-site request forgery).
"""

from __future__ import annotations

import hmac
import ipaddress
import urllib.parse

from . import APP_NAME
from .httpd import Response

SESSION_COOKIE = "TMSESSNAME"
CSRF_NAME = "X-Csrf-Token"
REQUESTED_WITH = APP_NAME

LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")


def _parse_cookies(values):
    cookies = {}
    for raw in values:
        for piece in raw.split(";"):
            name, sep, value = piece.partition("=")
            if sep:
                cookies[name.strip()] = value.strip()
    return cookies


def _same(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def platform_session_ok(request):
    """True when the request carries a consistent TOS session."""
    token = request.header(CSRF_NAME).strip()
    cookies = _parse_cookies(request.header_values("Cookie"))
    session = cookies.get(SESSION_COOKIE, "")
    cookie_token = cookies.get(CSRF_NAME, "")
    if not session or not token or not cookie_token:
        return False
    return _same(token, cookie_token) or _same(
        urllib.parse.unquote(token), urllib.parse.unquote(cookie_token))


def client_is_local(ip):
    """True for loopback, private, link-local and other non-public addresses."""
    try:
        address = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return not address.is_global


def host_is_allowed(host_header, allowed_hosts=()):
    """Accept IP literals and LAN-style names; reject public DNS names."""
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        name = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES):
        return True
    return name in allowed_hosts


def check_network(request, cfg):
    """Restrictions that apply to every request on the TCP listener."""
    if cfg.dev or request.transport != "tcp":
        return None
    if not cfg.allow_remote and not client_is_local(request.client_ip):
        return Response.error(403, "forbidden_network")
    if not host_is_allowed(request.header("Host"), cfg.allowed_hosts):
        return Response.error(403, "forbidden_host")
    return None


def check_api(request, cfg):
    """Return a Response to send instead of handling the call, or None."""
    if cfg.dev:
        return None
    if request.transport == "unix":
        if cfg.platform_auth and not platform_session_ok(request):
            return Response.error(401, "unauthorized")
        return None
    denied = check_network(request, cfg)
    if denied is not None:
        return denied
    if request.method == "POST":
        if request.header("X-Requested-With") != REQUESTED_WITH:
            return Response.error(403, "csrf_check_failed")
        origin = request.header("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc.lower() != request.header("Host").lower():
            return Response.error(403, "csrf_check_failed")
    return None

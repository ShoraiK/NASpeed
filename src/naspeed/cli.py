# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Command-line front end, mainly for development and troubleshooting.

    naspeed providers
    naspeed test --provider cloudflare
    naspeed test --provider ookla --list-servers
    naspeed test --provider mlab --accept-notice
"""

from __future__ import annotations

import json
import sys
import time

from . import log
from .engine.runner import TestRunner
from .providers import Context, ProviderError, build_registry
from .store import Store

# Same notices the web UI asks the user to accept before the first test.
NOTICE_TEXTS = {
    "mlab_privacy": "M-Lab publishes the result of every test as open data, including your public IP address.",
}


def _out(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _fmt_speed(mbps):
    if mbps is None:
        return "-"
    if mbps >= 1000:
        return "%.2f Gbps" % (mbps / 1000.0)
    return "%.1f Mbps" % mbps


def _list_servers(provider, args):
    try:
        servers = provider.list_servers(Context(ip_version=args.ip_version))
    except ProviderError as exc:
        _out("error: %s" % exc)
        return 1
    _out("%-10s %8s  %-28s %s" % ("id", "ms", "location", "sponsor / host"))
    for server in servers:
        latency = "-" if server.get("latency_ms") is None else "%.1f" % server["latency_ms"]
        _out("%-10s %8s  %-28s %s (%s)" % (
            server["id"][:10], latency, server.get("name", "")[:28],
            server.get("sponsor", ""), server.get("host", "")))
    return 0


def run(command, cfg, args):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    log.setup_logging("WARNING")
    registry = build_registry()

    if command == "providers":
        for provider in registry.values():
            info = provider.describe()
            _out("%-12s %-18s official_api=%s notice=%s" % (
                info["id"], info["name"], info["official_api"], info["notice"] or "-"))
        return 0

    provider = registry.get(args.provider)
    if provider is None:
        _out("unknown provider %r; try: %s" % (args.provider, ", ".join(registry)))
        return 2
    if args.list_servers:
        return _list_servers(provider, args)
    if provider.notice and not getattr(args, "accept_notice", False):
        _out("%s needs your consent before the test:" % provider.name)
        _out("  " + NOTICE_TEXTS.get(provider.notice, provider.notice))
        if provider.notice_url:
            _out("  Details: " + provider.notice_url)
        _out("Run the test again with --accept-notice to accept this and continue.")
        return 2

    store = Store("", persistent=False)
    runner = TestRunner(cfg, store, registry)
    runner.start(provider.id, args.server, {
        "duration": args.duration, "connections": args.connections, "ip_version": args.ip_version,
    })
    last_phase = None
    last_print = 0.0
    try:
        while True:
            state = runner.snapshot()
            phase = state["phase"]
            if phase != last_phase:
                if last_phase == "init" and state.get("server"):
                    server = state["server"]
                    client = state.get("client") or {}
                    if not args.json:
                        _out("server: %s - %s (%s)" % (
                            server.get("sponsor", ""), server.get("name", ""), server.get("host", "")))
                        if client.get("isp"):
                            _out("client: %s" % client["isp"])
                if not args.json and phase != "done":
                    _out("[%s]" % phase)
                last_phase = phase
            now = time.monotonic()
            if not args.json and phase in ("download", "upload") and now - last_print >= 1.0:
                last_print = now
                data = state.get(phase) or {}
                _out("  %-9s live %-12s avg %-12s conns %-2s %s" % (
                    phase, _fmt_speed(data.get("live_mbps")), _fmt_speed(data.get("mbps")),
                    data.get("streams", 0), "measuring" if data.get("measuring") else "warm-up"))
            if state["status"] != "running":
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        runner.shutdown()
        _out("cancelled")
        return 130

    state = runner.snapshot()
    if state["status"] != "done":
        error = state.get("error") or {"code": state["status"], "detail": ""}
        _out("test failed: %s %s" % (error["code"], error.get("detail", "")))
        return 1
    result = state["result"]
    if args.json:
        _out(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    ping = result["ping"]
    _out("")
    _out("ping      %s ms (low %s, high %s, jitter %s)" % (
        ping["latency"], ping["low"], ping["high"], ping["jitter"]))
    for direction in ("download", "upload"):
        data = result[direction]
        _out("%-9s %-12s peak %-12s %d conns, %.1f MB in %.1f s, loaded ping %s ms, errors %d" % (
            direction, _fmt_speed(data["mbps"]), _fmt_speed(data["peak_mbps"]), data["streams"],
            data["bytes"] / 1e6, data["seconds"], data["latency"], data["errors"]))
        load = data.get("cpu")
        if load:
            _out("%-9s NAS CPU %s%%, busiest core %s%%%s" % (
                "", load["busy"], "-" if load["peak_core"] is None else load["peak_core"],
                "; the NAS may have limited this result (%s)" % load["limit"] if load["limit"] else ""))
    return 0

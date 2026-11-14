# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Service start-up and shutdown."""

from __future__ import annotations

import os
import platform
import signal
import threading

from . import APP_NAME, __version__, httpd, log
from .api import App
from .engine.runner import TestRunner
from .providers import build_registry
from .store import Store

logger = log.get("main")


def _socket_hint(path, exc):
    directory = os.path.dirname(path) or "."
    logger.error("cannot listen on the unix socket %s: %s", path, exc)
    logger.error(
        "check that %s exists and is writable by the service user, and that the "
        "systemd unit lists it in ReadWritePaths (ProtectSystem=strict makes it read-only otherwise)",
        directory,
    )


def serve(cfg):
    log.setup_logging(cfg.log_level, "" if cfg.dev else cfg.log_dir)
    logger.info("%s %s starting (python %s, pid %d)", APP_NAME, __version__,
                platform.python_version(), os.getpid())

    store = Store(cfg.data_dir)
    registry = build_registry()
    runner = TestRunner(cfg, store, registry)
    app = App(cfg, store, runner, registry)

    servers = []
    if cfg.socket_path:
        try:
            servers.append(httpd.unix_server(cfg.socket_path, app, cfg.socket_mode))
            logger.info("listening on unix socket %s (mode %o, platform auth %s)",
                        cfg.socket_path, cfg.socket_mode, "on" if cfg.platform_auth else "off")
        except OSError as exc:
            _socket_hint(cfg.socket_path, exc)
    if cfg.http_port:
        try:
            servers.append(httpd.tcp_server(cfg.http_bind, cfg.http_port, app))
            logger.info("listening on http://%s:%d%s", cfg.http_bind, cfg.http_port,
                        " (development mode, no access control)" if cfg.dev else "")
        except OSError as exc:
            logger.error("cannot listen on %s:%d: %s", cfg.http_bind, cfg.http_port, exc)
    if not servers:
        logger.error("no listener could be started; exiting")
        return 1

    stop = threading.Event()

    def on_signal(signum, _frame):
        logger.info("signal %d received, shutting down", signum)
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass

    for server in servers:
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                         name="http-%s" % server.transport, daemon=True).start()

    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass

    runner.shutdown()
    for server in servers:
        server.shutdown()
        server.server_close()
    if cfg.socket_path:
        try:
            os.unlink(cfg.socket_path)
        except OSError:
            pass
    logger.info("stopped")
    return 0

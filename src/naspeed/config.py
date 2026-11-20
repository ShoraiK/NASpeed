# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Runtime configuration.

Values come from (lowest to highest priority): built-in defaults, environment
variables (``NASPEED_*``, normally loaded by systemd from ``naspeed.env``) and
command-line flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

from . import APP_ID, __version__

DEFAULT_APP_DIR = "/usr/local/" + APP_ID
DEFAULT_SOCKET = "/var/api/" + APP_ID + ".sock"
DEFAULT_DEV_PORT = 18321


def _env(name, default=""):
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_bool(name, default):
    value = _env(name, "")
    if value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_int(name, default, base=10):
    value = _env(name, "")
    if value == "":
        return default
    try:
        return int(value, base)
    except ValueError:
        return default


@dataclass
class Config:
    app_dir: str = DEFAULT_APP_DIR
    data_dir: str = ""
    log_dir: str = ""
    # Unix socket used by the TOS platform proxy ("" disables it).
    socket_path: str = DEFAULT_SOCKET
    socket_mode: int = 0o660
    # Optional TCP listener (0 disables it). Used for development and for the
    # "new tab / own port" packaging mode.
    http_bind: str = "0.0.0.0"
    http_port: int = 0
    # Enforce the TOS session headers (Cookie + X-Csrf-Token) on socket requests.
    platform_auth: bool = True
    # TCP listener only: accept clients outside private/LAN address ranges.
    allow_remote: bool = False
    allowed_hosts: tuple = ()
    # Where the web UI comes from: a directory (development) or webui.bz2.
    webui_dir: str = ""
    webui_archive: str = ""
    log_level: str = "INFO"
    dev: bool = False
    # "process" (one worker process per TLS connection) or "thread".
    worker_mode: str = "process"
    # Command prefix used to spawn worker processes.
    launcher: list = field(default_factory=list)
    launcher_env: dict = field(default_factory=dict)

    def finalize(self):
        if not self.data_dir:
            self.data_dir = os.path.join(self.app_dir, "data")
        if not self.log_dir:
            self.log_dir = os.path.join(self.app_dir, "logs")
        if not self.webui_dir and not self.webui_archive:
            self.webui_archive = os.path.join(self.app_dir, "webui.bz2")
        return self


def _checkout_root(pkg_dir=None):
    """Root of the source checkout when running from one, else ''.

    A checkout keeps the package in ``<root>/src/`` and the web UI in
    ``<root>/webui/``. Both are versioned, so a fresh clone qualifies too.
    """
    pkg_dir = pkg_dir or os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(pkg_dir)
    root = os.path.dirname(src_dir)
    if os.path.basename(src_dir) == "src" and os.path.isfile(os.path.join(root, "webui", "index.html")):
        return root
    return ""


def detect_launcher():
    """Work out how to start another copy of ourselves as a transfer worker.

    Packaged builds run from a zipapp (``bin/naspeed``): the package's parent
    "directory" is then the archive file itself. From a source checkout we use
    ``python -m naspeed`` with PYTHONPATH pointing at ``src``.
    """
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(pkg_dir)
    exe = sys.executable or "python3"
    if os.path.isfile(parent):
        return [exe, "-I", parent, "worker"], {}
    env = {"PYTHONPATH": parent}
    return [exe, "-m", APP_ID, "worker"], env


def build_parser():
    parser = argparse.ArgumentParser(
        prog=APP_ID,
        description="NASpeed %s - multi-provider network speed test backend" % __version__,
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    # Defaults on the subparser would overwrite options supplied before "serve".
    serve = sub.add_parser("serve", help="run the HTTP API server (default)",
                           argument_default=argparse.SUPPRESS)
    for target in (parser, serve):
        target.add_argument("--dev", action="store_true", help="development mode: TCP only, no auth")
        target.add_argument("--socket", metavar="PATH", help="Unix socket path ('' to disable)")
        target.add_argument("--port", type=int, metavar="N", help="TCP port (0 to disable)")
        target.add_argument("--bind", metavar="ADDR", help="TCP bind address")
        target.add_argument("--app-dir", metavar="DIR")
        target.add_argument("--data-dir", metavar="DIR")
        target.add_argument("--log-dir", metavar="DIR")
        target.add_argument("--webui", metavar="DIR", help="serve the web UI from this directory")
        target.add_argument("--log-level", metavar="LEVEL")

    sub.add_parser("worker", help="internal: transfer worker (reads a JSON spec on stdin)")

    test = sub.add_parser("test", help="run one speed test from the command line")
    test.add_argument("--provider", default="cloudflare")
    test.add_argument("--server", default="", help="server id (default: automatic)")
    test.add_argument("--duration", default="standard", choices=("quick", "standard", "long"))
    test.add_argument("--connections", default="auto")
    test.add_argument("--ip-version", default="auto", choices=("auto", "4", "6"))
    test.add_argument("--threads", action="store_true", help="use threads instead of worker processes")
    test.add_argument("--json", action="store_true", help="print the final result as JSON")
    test.add_argument("--list-servers", action="store_true")
    test.add_argument("--accept-notice", action="store_true",
                      help="accept the provider's notice (M-Lab publishes every result, including your IP address)")

    sub.add_parser("providers", help="list the available providers")
    return parser


def load_config(args=None):
    """Build the Config from the environment and (optionally) parsed CLI args."""
    cfg = Config()
    cfg.app_dir = _env("NASPEED_APP_DIR", DEFAULT_APP_DIR)
    cfg.data_dir = _env("NASPEED_DATA_DIR", "")
    cfg.log_dir = _env("NASPEED_LOG_DIR", "")
    cfg.socket_path = os.environ.get("NASPEED_SOCKET", DEFAULT_SOCKET).strip()
    cfg.socket_mode = _env_int("NASPEED_SOCKET_MODE", 0o660, 8)
    cfg.http_bind = _env("NASPEED_HTTP_BIND", "0.0.0.0")
    cfg.http_port = _env_int("NASPEED_HTTP_PORT", 0)
    cfg.platform_auth = _env_bool("NASPEED_PLATFORM_AUTH", True)
    cfg.allow_remote = _env_bool("NASPEED_ALLOW_REMOTE", False)
    cfg.allowed_hosts = tuple(
        h.strip().lower() for h in _env("NASPEED_ALLOWED_HOSTS", "").split(",") if h.strip()
    )
    cfg.webui_dir = _env("NASPEED_WEBUI_DIR", "")
    cfg.log_level = _env("NASPEED_LOG_LEVEL", "INFO").upper()
    cfg.worker_mode = _env("NASPEED_WORKERS", "process").lower()

    if args is not None:
        if getattr(args, "dev", False):
            cfg.dev = True
            cfg.socket_path = ""
            cfg.platform_auth = False
            cfg.http_bind = "127.0.0.1"
            if not cfg.http_port:
                cfg.http_port = DEFAULT_DEV_PORT
            # Keep development data and the web UI inside the source checkout
            # instead of the production paths under /usr/local.
            root = _checkout_root()
            if root:
                if not cfg.data_dir:
                    cfg.data_dir = os.path.join(root, ".devdata")
                if not cfg.log_dir:
                    cfg.log_dir = os.path.join(root, ".devdata", "logs")
                if not cfg.webui_dir and os.path.isdir(os.path.join(root, "webui")):
                    cfg.webui_dir = os.path.join(root, "webui")
        if getattr(args, "socket", None) is not None:
            cfg.socket_path = args.socket
        if getattr(args, "port", None) is not None:
            cfg.http_port = args.port
        if getattr(args, "bind", None):
            cfg.http_bind = args.bind
        if getattr(args, "app_dir", None):
            cfg.app_dir = args.app_dir
        if getattr(args, "data_dir", None):
            cfg.data_dir = args.data_dir
        if getattr(args, "log_dir", None):
            cfg.log_dir = args.log_dir
        if getattr(args, "webui", None):
            cfg.webui_dir = args.webui
        if getattr(args, "log_level", None):
            cfg.log_level = args.log_level.upper()
        if getattr(args, "threads", False):
            cfg.worker_mode = "thread"

    if cfg.worker_mode not in ("process", "thread"):
        cfg.worker_mode = "process"
    cfg.launcher, cfg.launcher_env = detect_launcher()
    return cfg.finalize()

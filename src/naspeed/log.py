# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Logging setup.

Format required by the TOS 7 guide (section 8.7.1)::

    [YYYY-MM-DD HH:MM:SS] [LEVEL] [component] message

stdout/stderr are captured by the systemd journal. When the log directory is
writable we also keep a small size-rotated file there.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

LOG_FILE_NAME = "naspeed.log"
LOG_FILE_BYTES = 512 * 1024
LOG_FILE_BACKUPS = 2


def setup_logging(level="INFO", log_dir=""):
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    # DEBUG is a development aid only; production units ship with INFO.
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT, _DATEFMT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, LOG_FILE_NAME),
                maxBytes=LOG_FILE_BYTES,
                backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            root.addHandler(handler)
        except OSError as exc:
            logging.getLogger("main").warning("file logging disabled (%s): %s", log_dir, exc)
    return root


def get(name):
    return logging.getLogger(name)

# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Entry point: ``naspeed [serve|test|providers|worker]``."""

from __future__ import annotations

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["worker"]:
        # Transfer workers must start fast: skip argument parsing and config.
        from .engine.transfer import worker_main

        return worker_main()

    from .config import build_parser, load_config

    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    cfg = load_config(args)

    if command in ("test", "providers"):
        from . import cli

        return cli.run(command, cfg, args)

    from . import server

    return server.serve(cfg)


if __name__ == "__main__":
    sys.exit(main())

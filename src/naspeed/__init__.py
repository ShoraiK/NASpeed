# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""NASpeed - multi-provider network speed test for TerraMaster TOS 7.

The backend is pure standard-library Python and must stay compatible with
Python 3.10, the interpreter that ships with TOS 7.
"""

APP_ID = "naspeed"
APP_NAME = "NASpeed"

# Single source of the version. The package build copies both values into
# config.ini ("version", "beta") and DEBIAN/control ("Version"). The TOS App
# Center only detects updates between plain numeric versions (1 to 3 segments),
# so beta status is a separate flag, never a "-beta" suffix.
__version__ = "0.1.1"
BETA = True

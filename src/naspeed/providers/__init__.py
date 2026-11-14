# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Speed test providers.

To add a provider: write a module with a ``Provider`` subclass (see
``base.py``) and register an instance in ``build_registry`` below. The order
here is the order shown in the interface.
"""

from __future__ import annotations

from collections import OrderedDict

from .base import Context, Provider, ProviderError, Session
from .cloudflare import Cloudflare
from .librespeed import LibreSpeed
from .mlab import MLab
from .ookla import Ookla


def build_registry():
    registry = OrderedDict()
    for provider in (Ookla(), Cloudflare(), MLab(), LibreSpeed()):
        registry[provider.id] = provider
    return registry


__all__ = ["Context", "Provider", "ProviderError", "Session", "build_registry"]

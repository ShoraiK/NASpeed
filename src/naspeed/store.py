# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Persistent state: user settings and the result history.

Everything lives in two small JSON files under the application data
directory (``/usr/local/naspeed/data`` on TOS). Writes are atomic
(temp file + rename) and the files are only readable by the service user.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid

from . import log

logger = log.get("store")

SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"

HISTORY_HARD_LIMIT = 1000

DURATIONS = {"quick": 5.0, "standard": 8.0, "long": 15.0}
CONNECTION_CHOICES = ("auto", "1", "2", "4", "8", "16", "32")
IP_VERSIONS = ("auto", "4", "6")
LANGUAGES = ("en", "es", "fr", "de", "pt", "zh")

DEFAULT_SETTINGS = {
    # "language" is absent until someone picks one; the web UI then uses English.
    "default_provider": "librespeed",
    "duration": "standard",
    "connections": "auto",
    "ip_version": "auto",
    "save_history": True,
    "history_limit": 300,
    # Notices accepted at least once, with the time of the first acceptance.
    "accepted_notices": {},
    # Notices the web UI no longer shows before every test ("Don't show this again").
    "hidden_notices": {},
}


def new_id():
    return uuid.uuid4().hex[:12]


def _atomic_write_json(path, payload):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s (%s); starting from defaults", path, exc)
        # Keep the unreadable file for inspection instead of overwriting it.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default


def validate_settings(patch, provider_ids=()):
    """Return a cleaned copy of ``patch`` holding only known, valid keys."""
    clean = {}
    if not isinstance(patch, dict):
        return clean
    value = patch.get("language")
    if value in LANGUAGES:
        clean["language"] = value
    value = patch.get("default_provider")
    if isinstance(value, str) and (not provider_ids or value in provider_ids):
        clean["default_provider"] = value
    value = patch.get("duration")
    if value in DURATIONS:
        clean["duration"] = value
    value = patch.get("connections")
    if value is not None and str(value) in CONNECTION_CHOICES:
        clean["connections"] = str(value)
    value = patch.get("ip_version")
    if value is not None and str(value) in IP_VERSIONS:
        clean["ip_version"] = str(value)
    value = patch.get("save_history")
    if isinstance(value, bool):
        clean["save_history"] = value
    value = patch.get("history_limit")
    if isinstance(value, int) and not isinstance(value, bool):
        clean["history_limit"] = max(10, min(HISTORY_HARD_LIMIT, value))
    value = patch.get("accepted_notices")
    if isinstance(value, dict):
        clean["accepted_notices"] = {
            str(k)[:32]: int(v) for k, v in value.items() if isinstance(v, (int, float))
        }
    value = patch.get("hidden_notices")
    if isinstance(value, dict):
        clean["hidden_notices"] = {str(k)[:32]: v for k, v in value.items() if isinstance(v, bool)}
    return clean


class Store:
    def __init__(self, data_dir, persistent=True):
        self.data_dir = data_dir
        self._lock = threading.RLock()
        self._settings = None
        self._history = None
        self.writable = bool(persistent)
        if not persistent:
            # In-memory only (command-line tests): never touch the disk.
            self._settings = copy.deepcopy(DEFAULT_SETTINGS)
            self._history = []
            return
        try:
            os.makedirs(data_dir, exist_ok=True)
            # Remove temp files left behind by a crash (see the runtime manifest).
            for name in os.listdir(data_dir):
                if ".tmp-" in name:
                    try:
                        os.unlink(os.path.join(data_dir, name))
                    except OSError:
                        pass
        except OSError as exc:
            self.writable = False
            logger.error("data directory %s is not usable: %s", data_dir, exc)

    # ---- settings -------------------------------------------------------
    def _settings_path(self):
        return os.path.join(self.data_dir, SETTINGS_FILE)

    def _history_path(self):
        return os.path.join(self.data_dir, HISTORY_FILE)

    def get_settings(self):
        with self._lock:
            if self._settings is None:
                merged = copy.deepcopy(DEFAULT_SETTINGS)
                merged.update(validate_settings(_read_json(self._settings_path(), {})))
                self._settings = merged
            return copy.deepcopy(self._settings)

    def update_settings(self, patch, provider_ids=()):
        clean = validate_settings(patch, provider_ids)
        with self._lock:
            current = self.get_settings()
            notices = clean.pop("accepted_notices", None)
            hidden = clean.pop("hidden_notices", None)
            current.update(clean)
            if notices:
                current["accepted_notices"].update(notices)
            # True hides a notice and False shows it again; other notices keep their state.
            for notice, hide in (hidden or {}).items():
                if hide:
                    current["hidden_notices"][notice] = True
                else:
                    current["hidden_notices"].pop(notice, None)
            self._settings = current
            self._save(self._settings_path(), current)
            # A smaller limit takes effect at once: drop the oldest entries.
            history = self._load_history()
            limit = int(current.get("history_limit", 300))
            if len(history) > limit:
                del history[: len(history) - limit]
                self._save(self._history_path(), history)
            return copy.deepcopy(current)

    def history_count(self):
        with self._lock:
            return len(self._load_history())

    # ---- history --------------------------------------------------------
    def _load_history(self):
        if self._history is None:
            data = _read_json(self._history_path(), [])
            self._history = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        return self._history

    def list_history(self, limit=None):
        with self._lock:
            items = list(reversed(self._load_history()))
        if limit:
            items = items[: int(limit)]
        return copy.deepcopy(items)

    def add_result(self, result):
        with self._lock:
            history = self._load_history()
            entry = copy.deepcopy(result)
            entry.setdefault("id", new_id())
            entry.setdefault("ts", int(time.time()))
            history.append(entry)
            limit = int(self.get_settings().get("history_limit", 300))
            if len(history) > limit:
                del history[: len(history) - limit]
            self._save(self._history_path(), history)
            return entry

    def delete_result(self, result_id):
        with self._lock:
            history = self._load_history()
            before = len(history)
            history[:] = [r for r in history if r.get("id") != result_id]
            if len(history) != before:
                self._save(self._history_path(), history)
                return True
            return False

    def clear_history(self):
        with self._lock:
            self._history = []
            self._save(self._history_path(), self._history)

    def _save(self, path, payload):
        if not self.writable:
            return
        try:
            _atomic_write_json(path, payload)
        except OSError as exc:
            logger.error("could not write %s: %s", path, exc)

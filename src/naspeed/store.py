# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Persistent state: user settings and the result history.

Everything lives in two small JSON files under the application data
directory (``/usr/local/naspeed/data`` on TOS). Writes are atomic
(temp file + rename) and the files are only readable by the service user.
"""

from __future__ import annotations

import copy
import errno
import hashlib
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


class StorageError(Exception):
    """Persistent state could not be read or durably written."""


def _fsync_dir(directory):
    if not hasattr(os, "O_DIRECTORY"):
        return
    dir_fd = os.open(directory or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        ignored = (errno.EINVAL, errno.EBADF, errno.EROFS, getattr(errno, "EOPNOTSUPP", errno.EINVAL))
        if exc.errno not in ignored:
            raise
    finally:
        os.close(dir_fd)


def _atomic_write_json(path, payload):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(os.path.dirname(path) or ".")


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except OSError as exc:
        logger.error("could not read %s: %s", path, exc)
        raise StorageError() from exc
    except ValueError as exc:
        logger.warning("could not read %s (%s); starting from defaults", path, exc)
        # Keep the unreadable file for inspection instead of overwriting it.
        try:
            os.replace(path, path + ".corrupt")
        except OSError as move_error:
            raise StorageError() from move_error
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
    if isinstance(value, str) and value in DURATIONS:
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
        self._revision_value = None
        self._persistent = persistent
        if not persistent:
            # In-memory only (command-line tests): never touch the disk.
            self._settings = copy.deepcopy(DEFAULT_SETTINGS)
            self._history = []
            self._revision_value = self._revision(self._history)
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
            if current == self._settings:
                return current
            history = self._load_history() if current["history_limit"] != self._settings["history_limit"] else ()
            self._save(self._settings_path(), current)
            limit = current["history_limit"]
            if len(history) > limit:
                try:
                    self._commit_history(history[-limit:])
                except StorageError:
                    # The old history file is intact. Restore the old settings too.
                    # If this write also fails, reload disk state before the next use.
                    try:
                        self._save(self._settings_path(), self._settings)
                    finally:
                        self._settings = self._history = self._revision_value = None
                    raise
            self._settings = current
            return copy.deepcopy(current)

    def history_count(self):
        with self._lock:
            return len(self._load_history())

    # ---- history --------------------------------------------------------
    def _load_history(self):
        if self._history is None:
            data = _read_json(self._history_path(), [])
            history = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
            limit = self.get_settings()["history_limit"]
            if len(history) > limit:
                # Recover a restart between publishing a smaller limit and trimming.
                self._commit_history(history[-limit:])
            else:
                self._history = history
                self._revision_value = self._revision(history)
        return self._history

    def list_history(self, limit=None):
        with self._lock:
            history = self._load_history()
            if limit:
                history = history[-int(limit):]
            return copy.deepcopy(history[::-1])

    @staticmethod
    def _revision(history):
        ids = json.dumps([entry.get("id") for entry in history], separators=(",", ":"))
        return hashlib.sha256(ids.encode("ascii")).hexdigest()

    def _current_revision(self):
        history = self._load_history()
        if self._revision_value is None:
            self._revision_value = self._revision(history)
        return self._revision_value

    def history_page(self, limit=None, ids=None, summary=False):
        with self._lock:
            settings = self.get_settings()
            history = self._load_history()
            count = len(history)
            revision = self._current_revision()
            if ids is not None:
                history = [entry for entry in history if entry.get("id") in ids]
            items = history[-limit:] if limit else history
            items = items[::-1]
        if summary:
            items = [_result_summary(entry) for entry in items]
        else:
            items = copy.deepcopy(items)
        return {
            "items": items, "count": count, "revision": revision,
            "enabled": settings["save_history"], "limit": settings["history_limit"],
        }

    def add_result(self, result, *, if_enabled=False):
        with self._lock:
            settings = self.get_settings()
            if if_enabled and not settings["save_history"]:
                return None
            history = self._load_history()
            entry = copy.deepcopy(result)
            entry.setdefault("id", new_id())
            entry.setdefault("ts", int(time.time()))
            limit = settings["history_limit"]
            self._commit_history((history + [entry])[-limit:])
            return copy.deepcopy(entry)

    def delete_result(self, result_id):
        with self._lock:
            history = self._load_history()
            kept = [r for r in history if r.get("id") != result_id]
            if len(kept) != len(history):
                self._commit_history(kept)
                return True
            return False

    def clear_history(self, revision=None):
        with self._lock:
            if revision is not None and revision != self._current_revision():
                return False
            self._commit_history([])
            return True

    def _commit_history(self, history):
        self._save(self._history_path(), history)
        self._history = history
        self._revision_value = self._revision(history)

    def _save(self, path, payload):
        if not self._persistent:
            return
        try:
            _atomic_write_json(path, payload)
        except OSError as exc:
            logger.error("could not write %s: %s", path, exc)
            raise StorageError() from exc


def _result_summary(entry):
    item = {key: copy.deepcopy(entry[key]) for key in ("id", "ts", "provider", "provider_name", "server")
            if key in entry}
    for section, field in (("ping", "latency"), ("download", "mbps"), ("upload", "mbps")):
        values = entry.get(section)
        if isinstance(values, dict) and field in values:
            item[section] = {field: values[field]}
    return item

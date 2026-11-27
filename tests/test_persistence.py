# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Offline regressions for durable state changes and conditional requests."""

import errno
import os
import socket
import time
import types
import unittest
from unittest import mock

if __package__:
    from . import test_backend as backend
else:
    import test_backend as backend
from naspeed import store
from naspeed.config import build_parser, load_config
from naspeed.engine.runner import TestRunner


def disk_full():
    return OSError(errno.ENOSPC, "disk full")


class PersistenceTests(backend.AppCase):
    def test_failed_fsync_preserves_memory_file_and_pending_operation(self):
        self.call("POST", "/api/settings", {"save_history": True, "language": "en"})
        with mock.patch("naspeed.store.os.fsync", side_effect=disk_full()):
            status, data, _ = self.call("POST", "/api/settings", {"save_history": False})
        self.assertEqual((status, data["error"]), (503, "storage_error"))
        self.assertTrue(self.store.get_settings()["save_history"])
        self.assertTrue(store.Store(self.cfg.data_dir).get_settings()["save_history"])
        self.assertFalse(any(".tmp-" in name for name in os.listdir(self.cfg.data_dir)))
        self.assertFalse(self.call("POST", "/api/settings", {"save_history": False})[1]["save_history"])

    def test_unsaved_history_changes_leave_original_after_restart(self):
        self.store.add_result({"id": "original"})
        with mock.patch("naspeed.store._atomic_write_json", side_effect=disk_full()):
            for route, body in (("delete", {"id": "original"}), ("clear", {})):
                with self.subTest(route=route):
                    status, result, _ = self.call("POST", "/api/history/" + route, body)
                    self.assertEqual((status, result["error"]), (503, "storage_error"))
            with self.assertRaises(store.StorageError):
                self.store.add_result({"id": "second"})
        for current in (self.store, store.Store(self.cfg.data_dir)):
            self.assertEqual([entry["id"] for entry in current.list_history()], ["original"])

    def test_disabled_history_skips_the_result_without_touching_disk(self):
        self.store.update_settings({"save_history": False})
        with mock.patch("naspeed.store._atomic_write_json") as write:
            self.assertIsNone(self.store.add_result({"id": "discard"}, if_enabled=True))
        write.assert_not_called()
        self.assertEqual(self.store.history_count(), 0)

    def test_read_error_does_not_discard_or_quarantine_valid_state(self):
        self.store.add_result({"id": "safe"})
        again = store.Store(self.cfg.data_dir)
        with mock.patch("builtins.open", side_effect=PermissionError()):
            with self.assertRaises(store.StorageError):
                again.history_count()
        self.assertEqual(again.list_history()[0]["id"], "safe")

    def test_trimming_both_files_can_be_retried_after_disk_failure(self):
        for index in range(12):
            self.store.add_result({"id": str(index)})
        write = store._atomic_write_json

        def fail_history(path, data):
            if os.path.basename(path) == store.HISTORY_FILE:
                raise disk_full()
            write(path, data)

        with mock.patch("naspeed.store._atomic_write_json", side_effect=fail_history):
            status, data, _ = self.call("POST", "/api/settings", {"history_limit": 10})
        self.assertEqual((status, data["error"]), (503, "storage_error"))
        for current in (self.store, store.Store(self.cfg.data_dir)):
            self.assertEqual(current.get_settings()["history_limit"], 300)
            self.assertEqual(current.history_count(), 12)
        self.call("POST", "/api/settings", {"history_limit": 10})
        self.assertEqual(store.Store(self.cfg.data_dir).history_count(), 10)

    def test_interrupted_trim_does_not_resurrect_entries_when_limit_is_raised(self):
        for index in range(12):
            self.store.add_result({"id": str(index)})
        # The process stopped after replacing settings.json and before trimming history.json.
        store._atomic_write_json(os.path.join(self.cfg.data_dir, store.SETTINGS_FILE),
                                 {"history_limit": 10})
        again = store.Store(self.cfg.data_dir)
        again.update_settings({"history_limit": 300})
        self.assertEqual([r["id"] for r in again.list_history()], [str(i) for i in range(11, 1, -1)])
        self.assertEqual(store.Store(self.cfg.data_dir).history_count(), 10)

    def test_consent_failure_blocks_start_but_default_provider_failure_keeps_started_test(self):
        with mock.patch("naspeed.store._atomic_write_json", side_effect=disk_full()):
            status, data, _ = self.call("POST", "/api/test/start", {
                "provider": "mlab", "accept_notice": True, "hide_notice": True})
            self.assertEqual((status, data["error"], self.runner.started), (503, "storage_error", []))
            status, data, _ = self.call("POST", "/api/test/start", {"provider": "ookla"})
            self.assertEqual((status, data["running"], data["settings_error"]), (200, True, "storage_error"))
        self.assertEqual(self.store.get_settings()["default_provider"], "librespeed")

    def test_failed_result_save_preserves_completed_measurement(self):
        runner = TestRunner(self.cfg, self.store, {})
        runner._state = {"id": "completed"}
        runner._t0 = time.perf_counter()
        session = types.SimpleNamespace(endpoint=types.SimpleNamespace(family=socket.AF_INET), client={}, server={})
        provider = types.SimpleNamespace(id="demo", name="Demo", open=lambda *args: session)
        with mock.patch.object(runner, "_ping", return_value={"latency": 2}), \
                mock.patch.object(runner, "_transfer", return_value={"mbps": 12}), \
                mock.patch("naspeed.store._atomic_write_json", side_effect=disk_full()):
            runner._run(provider, "", {"ip_version": "auto"})
        state = runner.snapshot()
        self.assertEqual((state["status"], state["saved"], state["save_error"]), ("done", False, "storage_error"))
        self.assertEqual(state["result"]["download"]["mbps"], 12)
        self.assertEqual(self.store.history_count(), 0)

    @unittest.skipUnless(hasattr(os, "O_DIRECTORY"), "POSIX directory fsync required")
    def test_atomic_write_fsyncs_parent_directory_on_posix(self):
        path = os.path.join(self.cfg.data_dir, "durability.json")
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        real_fsync = os.fsync
        calls = []

        def tracked_fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        with mock.patch("naspeed.store.os.fsync", side_effect=tracked_fsync):
            store._atomic_write_json(path, {"ok": True})
        self.assertEqual(len(calls), 2)

        attempt = 0

        def fail_dir_fsync(fd):
            nonlocal attempt
            attempt += 1
            if attempt == 2:
                raise OSError(errno.EINVAL, "directory fsync not supported")
            return real_fsync(fd)

        with mock.patch("naspeed.store.os.fsync", side_effect=fail_dir_fsync):
            store._atomic_write_json(path, {"ok": 2})
        self.assertEqual(store._read_json(path, {}), {"ok": 2})


class ConditionalApiTests(backend.AppCase):
    def test_summary_detail_and_legacy_history(self):
        self.store.add_result({"id": "a", "ts": 1, "download": {"mbps": 3, "series": [1, 2, 3]},
                               "ping": {"latency": 4, "values": [4]}, "client": {"isp": "Example"}})
        self.store.add_result({"id": "b", "ts": 2, "download": None})
        status, summary, _ = self.call("GET", "/api/history", query={"summary": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(summary["items"], [
            {"id": "b", "ts": 2}, {"id": "a", "ts": 1, "download": {"mbps": 3}, "ping": {"latency": 4}}])
        self.assertEqual(self.call("GET", "/api/history", query={"ids": ""})[1]["items"], [])
        details = self.call("GET", "/api/history", query={"ids": "missing,a,a"})[1]
        self.assertEqual((details["count"], details["revision"]), (2, summary["revision"]))
        self.assertEqual(details["items"][0]["download"]["series"], [1, 2, 3])
        self.assertEqual(self.call("GET", "/api/history")[1]["items"][1], details["items"][0])
        for invalid in (",".join(["a"] * 101), "a" * 65, "a,"):
            self.assertEqual(self.call("GET", "/api/history", query={"ids": invalid})[0], 400)

    def test_clear_with_stale_revision_preserves_newer_entries(self):
        self.store.add_result({"id": "a"})
        revision = self.call("GET", "/api/history", query={"summary": "1"})[1]["revision"]
        self.store.add_result({"id": "b"})
        status, data, _ = self.call("POST", "/api/history/clear", {"revision": revision})
        self.assertEqual((status, data["error"], self.store.history_count()), (409, "history_changed", 2))
        new_revision = self.call("GET", "/api/history", query={"limit": "1"})[1]["revision"]
        status, _, _ = self.call("POST", "/api/history/clear", {"revision": new_revision})
        self.assertEqual((status, self.store.history_count()), (200, 0))

    def test_stop_only_affects_the_expected_test(self):
        self.app.runner = TestRunner(self.cfg, self.store, {})
        self.app.runner._state = {"id": "new"}
        status, data, _ = self.call("POST", "/api/test/stop", {"id": "old"})
        self.assertEqual((status, data["error"], self.app.runner._stop.is_set()), (409, "test_changed", False))
        status, _, _ = self.call("POST", "/api/test/stop", {"id": "new"})
        self.assertEqual((status, self.app.runner._stop.is_set()), (200, True))


class ServeArgsTests(unittest.TestCase):
    def test_serve_honours_options_before_and_after_subcommand(self):
        flags = ["--dev", "--port", "12345", "--socket", ""]
        parser = build_parser()
        for args in (flags, [*flags, "serve"], ["serve", *flags]):
            with self.subTest(args=args):
                cfg = load_config(parser.parse_args(args))
                self.assertEqual((cfg.dev, cfg.http_port, cfg.socket_path), (True, 12345, ""))
        opts = parser.parse_args(["--port", "12345", "serve", "--port", "23456"])
        self.assertEqual(opts.port, 23456)

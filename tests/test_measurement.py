# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the measurement engine against a local, rate-limited HTTP peer."""

import http.server
import os
import socket
import sys
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from naspeed.config import Config, detect_launcher
from naspeed.engine import netutil, runner, transfer
from naspeed.providers import Context
from naspeed.providers.base import HttpSession, Provider, ProviderError
from naspeed.providers.ookla import Ookla
from naspeed.store import Store


class LocalPeer(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass  # The client closes keep-alive sockets when a phase ends.

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(8 << 20))
        self.end_headers()
        block = bytes(8192)
        try:
            for _ in range(1024):
                self.wfile.write(block)
                time.sleep(0.04)
        except OSError:
            pass  # The measurement deliberately ends during the response.

    def do_POST(self):
        size = int(self.headers["Content-Length"])
        self.server.posts.append(size)
        try:
            remaining = size
            while remaining:
                block = self.rfile.read(min(128 * 1024, remaining))
                if not block:
                    return
                remaining -= len(block)
                if self.path == "/accept":
                    time.sleep(0.006)
            status = 200
            if self.path == "/reject-all":
                status = 403
            elif self.path == "/reject-data" and size > transfer.UPLOAD_PROBE_BYTES:
                # This response arrives after half the measurement window.
                time.sleep(1.15)
                status = 403
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass


class MeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.peer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalPeer)
        cls.peer.posts = []
        cls.thread = threading.Thread(target=cls.peer.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.peer.shutdown()
        cls.thread.join()
        cls.peer.server_close()

    def run_local_test(self, mode, path, upload_size=1 << 20):
        self.peer.posts.clear()
        endpoint = netutil.Endpoint("http", "localhost", self.peer.server_port, "127.0.0.1", socket.AF_INET)
        session = HttpSession({"id": "local"}, endpoint, download_path="/slow", upload_path=path,
                              download_size=8 << 20, upload_size=upload_size)
        provider = Provider()
        provider.id = "local"
        provider.open = lambda _ctx, _server: session
        cfg = Config(worker_mode=mode)
        cfg.launcher, cfg.launcher_env = detect_launcher()
        store = Store("", persistent=False)
        test = runner.TestRunner(cfg, store, {"local": provider})
        with mock.patch.dict(runner.DURATIONS, quick=1.6), mock.patch.object(runner, "WARMUP_MIN", 0.25), \
                mock.patch.object(runner, "NO_DATA_TIMEOUT", 3), mock.patch.object(runner, "PING_SAMPLES", 2):
            try:
                test.start("local", options={"duration": "quick", "connections": "1"})
                test._thread.join(15)
                self.assertFalse(test.running())
                return test.snapshot(), store.list_history()
            finally:
                test.shutdown()

    def test_slow_download_and_accepted_upload_in_each_worker_mode(self):
        for mode in ("thread", "process"):
            with self.subTest(mode=mode):
                # The probe completes, but the real upload body outlasts the
                # deadline. A successful measurement must still be possible.
                state, history = self.run_local_test(mode, "/accept", upload_size=256 << 20)
                self.assertEqual(state["status"], "done", state["error"])
                self.assertEqual(len(history), 1)
                down = history[0]["download"]
                self.assertLess(down["bytes"], transfer.RECV_BUFFER)
                self.assertGreater(down["bytes"], down["measured_bytes"])
                for direction in ("download", "upload"):
                    data = history[0][direction]
                    self.assertGreater(data["measured_bytes"], 0)
                    self.assertEqual(data["mbps"], round(data["measured_bytes"] * 8 / data["seconds"] / 1e6, 2))
                self.assertEqual(self.peer.posts, [transfer.UPLOAD_PROBE_BYTES, 64 << 20])

    def test_explicit_upload_rejection_never_enters_history(self):
        for mode, path in (("thread", "/reject-all"), ("thread", "/reject-data"),
                           ("process", "/reject-data")):
            with self.subTest(mode=mode, path=path):
                with self.assertLogs("runner", level="WARNING"):
                    state, history = self.run_local_test(mode, path)
                self.assertEqual(state["status"], "error")
                self.assertEqual(state["error"]["code"], "transfer_failed")
                self.assertIn("HTTP 403", state["error"]["detail"])
                self.assertEqual(history, [])
                self.assertEqual(len(self.peer.posts), 1 if path == "/reject-all" else 2)


class ProviderCacheTests(unittest.TestCase):
    def test_same_key_shares_result_and_failure_and_can_retry(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                provider = Provider()
                entered, waiting, release = threading.Event(), threading.Event(), threading.Event()

                class ObservedFuture(Future):
                    def result(self, timeout=None):
                        waiting.set()
                        return super().result(timeout)

                def load():
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("loader did not get released")
                    if failure:
                        raise ProviderError("provider_busy")
                    return "catalogue"

                loader = mock.Mock(side_effect=load)
                with mock.patch("naspeed.providers.base.Future", ObservedFuture), ThreadPoolExecutor(2) as pool:
                    owner = pool.submit(provider._cached, "query", loader)
                    try:
                        self.assertTrue(entered.wait(2))
                        waiter = pool.submit(provider._cached, "query", loader)
                        self.assertTrue(waiting.wait(2))
                        self.assertEqual(provider._cached("other", lambda: "independent"), "independent")
                    finally:
                        release.set()
                    for job in (owner, waiter):
                        if failure:
                            with self.assertRaises(ProviderError):
                                job.result(2)
                        else:
                            self.assertEqual(job.result(2), "catalogue")
                loader.assert_called_once()
                if failure:
                    self.assertEqual(provider._cached("query", lambda: "retried"), "retried")

    def test_expired_queries_and_least_recently_used_entries_are_evicted(self):
        provider = Provider()
        provider.cache_limit = 2
        with mock.patch("naspeed.providers.base.time.monotonic", return_value=1) as now:
            for key in ("a", "b", "a", "c"):
                provider._cached(key, lambda: key, ttl=10)
            self.assertEqual(list(provider._cache), ["a", "c"])
            now.return_value = 11
            provider._cached("d", lambda: "fresh")
            self.assertEqual(list(provider._cache), ["d"])

    def test_search_selection_stays_usable_when_probe_cache_expires(self):
        provider = Ookla()
        chosen = {"id": "42", "host": "localhost:8080", "name": "local"}
        endpoint = netutil.Endpoint("http", "localhost", 8080, "127.0.0.1", socket.AF_INET)

        def rank(servers, _targets, _family):
            for server in servers:
                server["latency_ms"] = 1
            return servers, {server["id"]: endpoint for server in servers}

        with mock.patch("naspeed.providers.ookla.netutil.fetch_json", side_effect=[[chosen], []]), \
                mock.patch("naspeed.providers.ookla.rank_servers", side_effect=rank), \
                mock.patch.object(provider, "_client", return_value={}), \
                mock.patch.object(provider, "_hello", return_value=True), \
                mock.patch("naspeed.providers.base.time.monotonic", return_value=0) as now:
            provider.list_servers(Context(), "local")
            now.return_value = 900
            self.assertEqual(provider.open(Context(), "42").endpoint, endpoint)

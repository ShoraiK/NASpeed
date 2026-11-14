# SPDX-FileCopyrightText: 2026 NASpeed contributors
# SPDX-License-Identifier: GPL-3.0-only
"""Backend tests that need no network: access control, routing, settings
and history.

Run from the project root:

    python -m unittest discover -s tests -v
"""

import contextlib
import email.message
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from naspeed import BETA, __version__, auth, cli, config  # noqa: E402
from naspeed.api import App, strip_prefix  # noqa: E402
from naspeed.config import Config, _checkout_root, build_parser, load_config  # noqa: E402
from naspeed.engine import latency  # noqa: E402
from naspeed.engine.runner import TestError, _recent_rate, _resample, streams_for_speed  # noqa: E402
from naspeed.httpd import Request  # noqa: E402
from naspeed.providers import build_registry  # noqa: E402
from naspeed.store import Store  # noqa: E402

SESSION = {"X-Csrf-Token": "tok123", "Cookie": "TMSESSNAME=sess-1; X-Csrf-Token=tok123;"}


def make_request(method="GET", path="/", headers=None, body=None, transport="unix",
                 client_ip="", query=None):
    message = email.message.Message()
    for name, value in (headers or {}).items():
        message[name] = value
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return Request(method, path, query or {}, message, raw, transport, client_ip)


class FakeRunner:
    """Stands in for the real runner so no test traffic is generated."""

    def __init__(self):
        self.started = []
        self.stopped = 0
        self.busy = False

    def running(self):
        return self.busy

    def snapshot(self):
        return {"status": "running"} if self.busy else None

    def start(self, provider_id, server_id="", options=None):
        if self.busy:
            raise TestError("busy")
        self.started.append((provider_id, server_id, options))
        self.busy = True
        return {"status": "running", "provider": {"id": provider_id}}

    def stop(self):
        self.stopped += 1
        self.busy = False


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(app_dir=self.tmp.name, data_dir=os.path.join(self.tmp.name, "data"),
                          webui_dir=os.path.join(self.tmp.name, "webui"))
        self.cfg.finalize()
        os.makedirs(self.cfg.webui_dir)
        with open(os.path.join(self.cfg.webui_dir, "index.html"), "w", encoding="utf-8") as handle:
            handle.write("<!doctype html><title>NASpeed __VERSION__</title>")
        with open(os.path.join(self.tmp.name, "secret.txt"), "w", encoding="utf-8") as handle:
            handle.write("outside the web root")
        self.store = Store(self.cfg.data_dir)
        self.runner = FakeRunner()
        self.app = App(self.cfg, self.store, self.runner, build_registry())

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=SESSION, **kwargs):
        response = self.app.handle(make_request(method, path, headers, body, **kwargs))
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except ValueError:
            payload = response.body
        return response.status, payload, response


class PlatformAuthTests(AppCase):
    def test_both_session_headers_are_required(self):
        api = "/v2/proxy/naspeed/api/info"
        self.assertEqual(self.call("GET", api, headers={})[0], 401)
        self.assertEqual(self.call("GET", api, headers={"Cookie": SESSION["Cookie"]})[0], 401)
        self.assertEqual(self.call("GET", api, headers={"X-Csrf-Token": "tok123"})[0], 401)
        self.assertEqual(self.call("GET", api, headers=SESSION)[0], 200)

    def test_token_must_match_the_cookie(self):
        headers = {"X-Csrf-Token": "other", "Cookie": SESSION["Cookie"]}
        self.assertEqual(self.call("GET", "/api/info", headers=headers)[0], 401)

    def test_session_cookie_is_required(self):
        headers = {"X-Csrf-Token": "tok123", "Cookie": "X-Csrf-Token=tok123"}
        self.assertEqual(self.call("GET", "/api/info", headers=headers)[0], 401)

    def test_post_is_rejected_without_a_session(self):
        self.assertEqual(self.call("POST", "/api/test/stop", {}, headers={})[0], 401)
        self.assertEqual(self.runner.stopped, 0)

    def test_health_needs_no_session(self):
        status, payload, _ = self.call("GET", "/v2/proxy/naspeed/health", headers={})
        self.assertEqual((status, payload["status"]), (200, "ok"))

    def test_auth_can_be_switched_off(self):
        self.cfg.platform_auth = False
        self.assertEqual(self.call("GET", "/api/info", headers={})[0], 200)


class PortModeTests(AppCase):
    def call_tcp(self, method, path, body=None, headers=None, client_ip="192.168.1.20"):
        base = {"Host": "192.168.1.5:18321"}
        base.update(headers or {})
        return self.call(method, path, body, base, transport="tcp", client_ip=client_ip)

    def test_lan_clients_are_allowed(self):
        self.assertEqual(self.call_tcp("GET", "/api/info")[0], 200)
        self.assertEqual(self.call_tcp("GET", "/api/info", client_ip="127.0.0.1")[0], 200)
        self.assertEqual(self.call_tcp("GET", "/api/info", client_ip="fd00::5")[0], 200)

    def test_public_clients_are_rejected(self):
        status, payload, _ = self.call_tcp("GET", "/api/info", client_ip="8.8.8.8")
        self.assertEqual((status, payload["error"]), (403, "forbidden_network"))
        self.assertEqual(self.call_tcp("GET", "/", client_ip="8.8.8.8")[0], 403)

    def test_public_clients_allowed_when_configured(self):
        self.cfg.allow_remote = True
        self.assertEqual(self.call_tcp("GET", "/api/info", client_ip="8.8.8.8")[0], 200)

    def test_public_host_names_are_rejected(self):
        status, payload, _ = self.call_tcp("GET", "/api/info", headers={"Host": "attacker.example.com"})
        self.assertEqual((status, payload["error"]), (403, "forbidden_host"))
        self.assertEqual(self.call_tcp("GET", "/api/info", headers={"Host": "nas.local:18321"})[0], 200)
        self.assertEqual(self.call_tcp("GET", "/api/info", headers={"Host": "nas"})[0], 200)

    def test_post_needs_the_custom_header(self):
        self.assertEqual(self.call_tcp("POST", "/api/test/stop", {})[0], 403)
        ok = {"X-Requested-With": "NASpeed"}
        self.assertEqual(self.call_tcp("POST", "/api/test/stop", {}, ok)[0], 200)

    def test_post_from_another_origin_is_rejected(self):
        headers = {"X-Requested-With": "NASpeed", "Origin": "http://evil.example"}
        self.assertEqual(self.call_tcp("POST", "/api/test/stop", {}, headers)[0], 403)


class RoutingTests(AppCase):
    def test_proxy_prefixes_are_stripped(self):
        self.assertEqual(strip_prefix("/v2/proxy/naspeed/api/state"), "/api/state")
        self.assertEqual(strip_prefix("/naspeed/api/state"), "/api/state")
        self.assertEqual(strip_prefix("/naspeed/"), "/")
        self.assertEqual(strip_prefix("/naspeed"), "/")
        self.assertEqual(strip_prefix("/api/state"), "/api/state")
        self.assertEqual(strip_prefix("/naspeedy/api"), "/naspeedy/api")

    def test_unknown_route_and_wrong_method(self):
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("GET", "/api/test/start")[0], 405)

    def test_info_lists_the_providers(self):
        _status, payload, _ = self.call("GET", "/api/info")
        self.assertEqual([p["id"] for p in payload["providers"]], ["ookla", "cloudflare", "mlab", "librespeed"])
        self.assertEqual(payload["mode"], "platform")
        self.assertFalse(payload["running"])

    def test_static_files_and_traversal(self):
        status, body, response = self.call("GET", "/naspeed/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__VERSION__", body)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertEqual(self.call("GET", "/../secret.txt")[0], 404)
        self.assertEqual(self.call("GET", "/missing.js")[0], 404)

    def test_preflight(self):
        headers = {"Origin": "http://nas.local:8181", "Host": "nas.local:8181"}
        status, _payload, response = self.call("OPTIONS", "/v2/proxy/naspeed/api/state", headers=headers)
        self.assertEqual(status, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://nas.local:8181")
        self.assertIn("X-Csrf-Token", response.headers["Access-Control-Allow-Headers"])

    def test_foreign_origin_gets_no_cors_headers(self):
        headers = dict(SESSION, Origin="http://evil.example", Host="nas.local:8181")
        _status, _payload, response = self.call("GET", "/api/state", headers=headers)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)


class TestFlowTests(AppCase):
    def test_start_stop_and_busy(self):
        status, payload, _ = self.call("POST", "/api/test/start", {"provider": "cloudflare", "server": "BCN"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])
        self.assertEqual(self.runner.started[0][:2], ("cloudflare", "BCN"))
        status, payload, _ = self.call("POST", "/api/test/start", {"provider": "ookla"})
        self.assertEqual((status, payload["error"]), (409, "busy"))
        self.assertEqual(self.call("POST", "/api/test/stop", {})[0], 200)
        self.assertEqual(self.runner.stopped, 1)

    def test_unknown_provider(self):
        status, payload, _ = self.call("POST", "/api/test/start", {"provider": "fast"})
        self.assertEqual((status, payload["error"]), (400, "unknown_provider"))

    def test_last_provider_is_remembered(self):
        self.call("POST", "/api/test/start", {"provider": "librespeed"})
        self.assertEqual(self.store.get_settings()["default_provider"], "librespeed")

    def test_mlab_needs_the_privacy_notice(self):
        status, payload, _ = self.call("POST", "/api/test/start", {"provider": "mlab"})
        self.assertEqual((status, payload["error"], payload["notice"]), (412, "notice_required", "mlab_privacy"))
        self.assertEqual(self.runner.started, [])
        status, _payload, _ = self.call("POST", "/api/test/start", {"provider": "mlab", "accept_notice": True})
        self.assertEqual(status, 200)
        self.assertIn("mlab_privacy", self.store.get_settings()["accepted_notices"])
        # Once accepted it is not asked again.
        self.runner.busy = False
        self.assertEqual(self.call("POST", "/api/test/start", {"provider": "mlab"})[0], 200)

    def test_notices_cannot_be_accepted_through_settings(self):
        self.call("POST", "/api/settings", {"accepted_notices": {"mlab_privacy": 1}})
        self.assertEqual(self.store.get_settings()["accepted_notices"], {})

    def test_invalid_json_body(self):
        request = make_request("POST", "/api/settings", SESSION)
        request.body = b"{broken"
        self.assertEqual(self.app.handle(request).status, 400)


class SettingsAndHistoryTests(AppCase):
    def result(self, index):
        return {"id": "id%03d" % index, "ts": 1000 + index, "provider": "cloudflare",
                "download": {"mbps": float(index)}, "upload": {"mbps": 1.0}}

    def test_settings_are_validated(self):
        _status, payload, _ = self.call("POST", "/api/settings", {
            "duration": "long", "connections": 8, "ip_version": "6", "history_limit": 5,
            "save_history": False, "bogus": 1})
        self.assertEqual(payload["duration"], "long")
        self.assertEqual(payload["connections"], "8")
        self.assertEqual(payload["ip_version"], "6")
        self.assertEqual(payload["history_limit"], 10)       # clamped to the minimum
        self.assertFalse(payload["save_history"])
        self.assertNotIn("bogus", payload)
        _status, payload, _ = self.call("POST", "/api/settings", {
            "duration": "forever", "connections": "3", "ip_version": "5", "history_limit": 10 ** 6})
        self.assertEqual((payload["duration"], payload["connections"], payload["ip_version"]), ("long", "8", "6"))
        self.assertEqual(payload["history_limit"], 1000)     # clamped to the maximum

    def test_settings_survive_a_restart(self):
        self.call("POST", "/api/settings", {"history_limit": 40, "duration": "quick"})
        again = Store(self.cfg.data_dir).get_settings()
        self.assertEqual((again["history_limit"], again["duration"]), (40, "quick"))

    def test_history_is_capped_and_newest_first(self):
        self.store.update_settings({"history_limit": 10})
        for index in range(15):
            self.store.add_result(self.result(index))
        _status, payload, _ = self.call("GET", "/api/history")
        self.assertEqual(payload["count"], 10)
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["items"][0]["id"], "id014")
        self.assertEqual(payload["items"][-1]["id"], "id005")

    def test_lowering_the_limit_trims_at_once(self):
        for index in range(30):
            self.store.add_result(self.result(index))
        self.call("POST", "/api/settings", {"history_limit": 12})
        self.assertEqual(self.store.history_count(), 12)
        self.assertEqual(self.store.list_history()[0]["id"], "id029")

    def test_delete_and_clear(self):
        for index in range(3):
            self.store.add_result(self.result(index))
        self.assertEqual(self.call("POST", "/api/history/delete", {"id": "id001"})[1], {"ok": True})
        self.assertEqual(self.call("POST", "/api/history/delete", {"id": "nope"})[1], {"ok": False})
        self.assertEqual(self.call("POST", "/api/history/delete", {})[0], 400)
        self.assertEqual(self.store.history_count(), 2)
        self.call("POST", "/api/history/clear", {})
        self.assertEqual(self.store.history_count(), 0)

    def test_history_reports_whether_saving_is_enabled(self):
        self.call("POST", "/api/settings", {"save_history": False})
        self.assertFalse(self.call("GET", "/api/history")[1]["enabled"])

    def test_data_files_are_private(self):
        self.call("POST", "/api/settings", {"duration": "quick"})
        path = os.path.join(self.cfg.data_dir, "settings.json")
        self.assertTrue(os.path.exists(path))
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_corrupt_file_falls_back_to_defaults(self):
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        with open(os.path.join(self.cfg.data_dir, "history.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        store = Store(self.cfg.data_dir)
        self.assertEqual(store.list_history(), [])
        self.assertTrue(os.path.exists(os.path.join(self.cfg.data_dir, "history.json.corrupt")))


class AccessHelperTests(unittest.TestCase):
    def test_local_addresses(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.9", "172.16.0.1", "169.254.1.1",
                   "100.64.0.7", "::1", "fe80::1", "fd12::1", "::ffff:192.168.1.9"):
            self.assertTrue(auth.client_is_local(ip), ip)
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8", "not-an-ip", ""):
            self.assertFalse(auth.client_is_local(ip), ip)

    def test_host_names(self):
        for host in ("192.168.1.5", "192.168.1.5:18321", "[fd00::1]:18321", "localhost:1", "nas",
                     "nas.local", "nas.lan:80", "tnas.home.arpa"):
            self.assertTrue(auth.host_is_allowed(host), host)
        for host in ("example.com", "evil.example.com:80", "", "nas.local.evil.com"):
            self.assertFalse(auth.host_is_allowed(host), host)
        self.assertTrue(auth.host_is_allowed("nas.example.com", ("nas.example.com",)))


class EngineMathTests(unittest.TestCase):
    def test_latency_summary(self):
        summary = latency.summarize([10.0, 12.0, 11.0, 30.0], attempts=5)
        self.assertEqual(summary["latency"], 11.5)           # median
        self.assertEqual((summary["low"], summary["high"]), (10.0, 30.0))
        self.assertEqual(summary["jitter"], round((2 + 1 + 19) / 3, 2))
        self.assertEqual(summary["loss"], 0.2)
        self.assertIsNone(latency.summarize([], attempts=3)["latency"])

    def test_connection_scaling(self):
        self.assertEqual([streams_for_speed(v) for v in (10, 149, 150, 599, 600, 2499, 2500, 5999, 6000, 10000)],
                         [4, 4, 8, 8, 16, 16, 24, 24, 32, 32])

    def test_recent_rate(self):
        # 125 000 bytes every 0.1 s is exactly 10 Mbit/s.
        samples = [(round(i * 0.1, 1), i * 125000) for i in range(21)]
        self.assertAlmostEqual(_recent_rate(samples, 2.0, 0.5), 10.0, places=6)
        self.assertEqual(_recent_rate(samples[:1], 0.0, 0.5), 0.0)

    def test_resample(self):
        series = [[i * 0.1, float(i)] for i in range(100)]
        self.assertEqual(len(_resample(series, 48)), 48)
        self.assertEqual(_resample(series[:5], 48), [0.0, 1.0, 2.0, 3.0, 4.0])


class DevModeTests(unittest.TestCase):
    def test_a_fresh_clone_is_a_checkout(self):
        # packaging/ only holds empty folders for now, so a clone does not have it.
        with tempfile.TemporaryDirectory() as root:
            pkg = os.path.join(root, "src", "naspeed")
            os.makedirs(pkg)
            os.makedirs(os.path.join(root, "webui"))
            open(os.path.join(root, "webui", "index.html"), "w").close()
            self.assertEqual(_checkout_root(pkg), root)
            installed = os.path.join(root, "lib", "site-packages", "naspeed")
            os.makedirs(installed)
            self.assertEqual(_checkout_root(installed), "")

    def test_dev_mode_stays_inside_the_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            pkg = os.path.join(root, "src", "naspeed")
            webui = os.path.join(root, "webui")
            os.makedirs(pkg)
            os.makedirs(webui)
            open(os.path.join(webui, "index.html"), "w").close()
            with mock.patch.object(config, "__file__", os.path.join(pkg, "config.py")), \
                    mock.patch.dict(os.environ, {}, clear=True):
                for argv in (["--dev"], ["serve", "--dev"]):
                    with self.subTest(argv=argv):
                        cfg = load_config(build_parser().parse_args(argv))
                        self.assertEqual(cfg.webui_dir, webui)
                        self.assertEqual(cfg.data_dir, os.path.join(root, ".devdata"))
                        self.assertEqual(cfg.log_dir, os.path.join(root, ".devdata", "logs"))
                        self.assertEqual(cfg.http_bind, "127.0.0.1")
                        self.assertEqual(cfg.http_port, config.DEFAULT_DEV_PORT)
                        self.assertEqual(cfg.socket_path, "")
                        self.assertFalse(cfg.platform_auth)

    def test_dev_paths_respect_environment_and_cli_overrides(self):
        env = {"NASPEED_DATA_DIR": "env-data", "NASPEED_WEBUI_DIR": "env-webui"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config(build_parser().parse_args(["--dev"]))
            self.assertEqual((cfg.data_dir, cfg.webui_dir), ("env-data", "env-webui"))
            cfg = load_config(build_parser().parse_args([
                "--dev", "--data-dir", "cli-data", "--webui", "cli-webui"]))
            self.assertEqual((cfg.data_dir, cfg.webui_dir), ("cli-data", "cli-webui"))


class CommandLineTests(unittest.TestCase):
    def run_test_command(self, *argv):
        args = build_parser().parse_args(["test"] + list(argv))
        # A stand-in runner: no test traffic, and the test ends as cancelled.
        with mock.patch.object(cli, "TestRunner") as runner_class, \
                mock.patch.object(cli.log, "setup_logging"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            runner_class.return_value.snapshot.return_value = {
                "status": "cancelled", "phase": "done", "error": {"code": "cancelled", "detail": ""}}
            code = cli.run("test", Config(), args)
        return code, output.getvalue(), runner_class

    def test_mlab_needs_the_privacy_notice(self):
        for flags in ([], ["--json"]):
            with self.subTest(flags=flags):
                code, output, runner_class = self.run_test_command("--provider", "mlab", *flags)
                self.assertEqual(code, 2)
                self.assertIn("--accept-notice", output)
                self.assertIn("public IP address", output)
                self.assertIn("https://www.measurementlab.net/privacy/", output)
                runner_class.assert_not_called()

    def test_mlab_with_consent_starts(self):
        code, _output, runner = self.run_test_command("--provider", "mlab", "--accept-notice")
        self.assertEqual(code, 1)
        runner.return_value.start.assert_called_once_with(
            "mlab", "", {"duration": "standard", "connections": "auto", "ip_version": "auto"})

    def test_providers_without_a_notice_need_no_flag(self):
        for provider in ("cloudflare", "ookla", "librespeed"):
            with self.subTest(provider=provider):
                _code, _output, runner = self.run_test_command("--provider", provider)
                self.assertEqual(runner.return_value.start.call_args[0][0], provider)

    def test_listing_servers_does_not_start_a_test(self):
        with mock.patch.object(cli, "_list_servers", return_value=0) as list_servers:
            code, _output, runner = self.run_test_command("--provider", "mlab", "--list-servers")
        self.assertEqual(code, 0)
        list_servers.assert_called_once()
        runner.assert_not_called()


class VersionTests(unittest.TestCase):
    def test_version_is_comparable_by_the_app_center_and_the_readme_matches(self):
        # The App Center only offers updates between plain numeric versions of 1 to 3 segments.
        self.assertRegex(__version__, r"^\d+(\.\d+){0,2}$")
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        badges = re.findall(r"img\.shields\.io/badge/version-([^-?]+)-", readme)
        self.assertEqual(badges, [__version__ + ("%20beta" if BETA else "")])
        self.assertIn("The current version is **%s**" % __version__, readme)


if __name__ == "__main__":
    unittest.main()

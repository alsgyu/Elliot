import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from elliottbot.service import Service
from elliottbot.web import handler


class ElliottHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, sample=True)
        self.service.tick()
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.service))
        except PermissionError:
            self.service.close()
            self.temp.cleanup()
            self.skipTest("loopback binding requires permission")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.request("/api/status")["csrf"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.service.close()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers or {})
        with urlopen(request, timeout=10) as r:
            return json.loads(r.read())

    def test_csrf_and_origin_required(self):
        for headers in ({}, {"X-CSRF-Token": self.token, "Origin": "https://evil.example"}):
            with self.assertRaises(HTTPError) as error:
                self.request("/api/control/start", {}, headers)
            self.assertEqual(error.exception.code, 403)

    def test_foreign_host_and_static_traversal(self):
        with self.assertRaises(HTTPError):
            self.request("/api/status", headers={"Host": "evil.example"})
        with self.assertRaises(HTTPError):
            self.request("/%2e%2e/service.py")

    def test_controls_and_setting_limits(self):
        h = {"X-CSRF-Token": self.token}
        self.request("/api/control/start", {}, h)
        self.assertTrue(self.service.account["running"])
        self.request("/api/control/pause", {}, h)
        self.request("/api/settings", {"leverage": 10}, h)
        with self.assertRaises(HTTPError):
            self.request("/api/settings", {"leverage": 11}, h)
        self.assertEqual(self.service.settings.leverage, 10)

    def test_status_never_advances_data_or_sends_order(self):
        index = self.service._sample_index
        for _ in range(3):
            status = self.request("/api/status")
            self.assertNotIn("secret", status)
        self.assertEqual(self.service._sample_index, index)

    def test_sample_cannot_become_live(self):
        with self.assertRaises(HTTPError):
            self.request("/api/mode", {"mode": "live"}, {"X-CSRF-Token": self.token})
        self.assertEqual(self.service.mode, "paper")

    def test_chart_is_read_only_and_reference_mode_explicit(self):
        before = self.service.status()
        chart = self.request("/api/chart?timeframe=4h")
        self.assertGreater(len(chart["candles"]), 180)
        self.assertEqual(self.service.status(), before)
        self.assertTrue(self.request("/api/chart?timeframe=30m")["reference_only"])
        with self.assertRaises(HTTPError):
            self.request("/api/chart?timeframe=1s")
        with self.assertRaises(HTTPError):
            self.request("/api/chart?timeframe=4h", headers={"Host": "evil.example"})

    def test_active_settings_and_new_chart_contract_without_starting(self):
        before = self.request("/api/status")
        self.request("/api/settings", {"strategy_mode": "active", "max_entries_per_day": 8,
                     "cooldown_minutes": 15, "max_stop_percent": 1}, {"X-CSRF-Token": self.token})
        self.service.tick()
        status = self.request("/api/status")
        self.assertEqual(status["settings"]["strategy_mode"], "active")
        self.assertIn("active", status["limits"]["strategy_modes"])
        self.assertIn("15m", status["limits"]["chart_timeframes"])
        for key in ("capital", "risk_percent", "leverage", "daily_loss_percent", "max_notional"):
            self.assertEqual(status["settings"][key], before["settings"][key])
        self.assertFalse(status["account"]["running"])
        self.assertIsNone(status["account"]["position"])
        chart = self.request("/api/chart?timeframe=30m")
        self.assertEqual(chart["frame"], status["decision"]["frames"]["30m"])
        fifteen = self.request("/api/chart?timeframe=15m")
        self.assertEqual(fifteen["interval"], 900000)
        self.assertIn("독립 주문 없음", fifteen["role"])


if __name__ == "__main__":
    unittest.main()

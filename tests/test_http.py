import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from kgridbot.app import App
from kgridbot.config import load_config
from test_kgridbot import write_config


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(load_config(write_config(Path(self.temp.name))))
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.app._make_handler())
        except PermissionError:
            self.app.shutdown()
            self.temp.cleanup()
            self.skipTest("loopback binding requires execution permission")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.app.engine.tick()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.app.shutdown()
        self.temp.cleanup()

    def request(self, path, data=None, **headers):
        req = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        with urlopen(req, timeout=5) as response:
            return response.status, response.read()

    def test_status_does_not_change_simulation(self):
        tick = self.app.exchange._tick
        _, body = self.request("/api/status")
        payload = json.loads(body)
        self.assertEqual(self.app.exchange._tick, tick)
        self.assertFalse(payload["flags"]["can_enter"])
        self.assertNotIn("api_secret", payload)

    def test_csrf_and_foreign_origin_rejected(self):
        for headers in ({}, {"X-CSRF-Token": self.app.csrf_token, "Origin": "https://example.com"}):
            with self.assertRaises(HTTPError) as raised:
                self.request("/api/control/resume", {}, **headers)
            self.assertEqual(raised.exception.code, 403)
        self.assertFalse(self.app.store.flags().can_enter)

    def test_dns_rebinding_host_rejected(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/status", **{"Host": "example.com"})
        self.assertEqual(raised.exception.code, 403)

    def test_static_traversal_cannot_read_source(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/static/%2e%2e/app.py")
        self.assertEqual(raised.exception.code, 404)

    def test_settings_api_rejects_invalid_leverage(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/api/settings", {"leverage": 6}, **{"X-CSRF-Token": self.app.csrf_token})
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.app.config.grid.leverage, 2)

    def test_settings_api_and_start_roundtrip(self):
        headers = {"X-CSRF-Token": self.app.csrf_token}
        code, body = self.request("/api/settings", {"leverage": 3}, **headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["preview"]["grid"]["leverage"], 3)
        self.request("/api/control/resume", {}, **headers)
        self.assertTrue(self.app.store.flags().can_enter)

    def test_port_bind_failure_does_not_start_engine(self):
        with patch("kgridbot.app.ThreadingHTTPServer", side_effect=OSError("port in use")):
            with self.assertRaises(OSError):
                self.app.start()
        self.assertIsNone(self.app.engine._thread)


if __name__ == "__main__":
    unittest.main()

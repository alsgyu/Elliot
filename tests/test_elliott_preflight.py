import tempfile
import time
import unittest
from unittest.mock import patch

from elliottbot.config import Settings
from elliottbot.execution import RemoteBroker
from elliottbot.journal import Journal
from kgridbot.exchange.binance import BinanceAPIError


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(self.temp.name)
        self.addCleanup(self.journal.close)
        self.broker = RemoteBroker("testnet", ("k" * 64, "s" * 64), self.journal)
        self.responses = {
            "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
            "/fapi/v1/multiAssetsMargin": {"multiAssetsMargin": False},
            "/fapi/v1/symbolConfig": [{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 1}],
            "/fapi/v1/accountConfig": {"canTrade": True},
            "/fapi/v3/account": {"assets": [{"asset": "USDT", "availableBalance": "1000"}]},
            "/fapi/v3/positionRisk": [],
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": [],
            "/fapi/v1/commissionRate": {"takerCommissionRate": "0.0005"},
            "/fapi/v1/order/test": {},
        }
        self.calls = []

    def inspect(self, with_quote=True):
        def request(method, path, params=None):
            self.calls.append((method, path))
            if isinstance(self.responses[path], Exception):
                raise self.responses[path]
            return self.responses[path]
        quote = {"price": 80000, "rules": {"min_qty": "0.001", "step": "0.001", "min_notional": "50"}}
        with patch.object(self.broker, "request", side_effect=request), patch("elliottbot.market.public", return_value={"serverTime": int(time.time() * 1000)}):
            return self.broker.preflight(Settings(), {}, quote if with_quote else None)

    def test_v3_without_cantrade_uses_account_configuration(self):
        report = self.inspect()
        self.assertTrue(report["ok"])
        self.assertIn(("GET", "/fapi/v1/accountConfig"), self.calls)
        self.assertIn(("POST", "/fapi/v1/order/test"), self.calls)
        self.assertNotIn(("POST", "/fapi/v1/order"), self.calls)

    def test_configuration_false_is_not_overridden_by_legacy_balance_flag(self):
        self.responses["/fapi/v1/accountConfig"]["canTrade"] = False
        self.responses["/fapi/v3/account"]["canTrade"] = True
        report = self.inspect()
        self.assertFalse(report["ok"])
        self.assertEqual([c["name"] for c in report["checks"] if not c["ok"]], ["거래 권한"])
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_missing_permission_is_explicitly_unknown_and_blocks(self):
        self.responses["/fapi/v1/accountConfig"] = {}
        report = self.inspect()
        check = next(c for c in report["checks"] if c["name"] == "거래 권한")
        self.assertFalse(check["ok"])
        self.assertIn("미확인", check["detail"])
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_string_true_is_not_silently_accepted(self):
        self.responses["/fapi/v1/accountConfig"]["canTrade"] = "true"
        self.assertFalse(self.inspect()["ok"])
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_real_settings_mismatches_still_block_order_test(self):
        self.responses["/fapi/v1/multiAssetsMargin"]["multiAssetsMargin"] = True
        self.responses["/fapi/v1/symbolConfig"][0].update(marginType="CROSSED", leverage=10)
        report = self.inspect()
        failed = {c["name"]: c["detail"] for c in report["checks"] if not c["ok"]}
        self.assertEqual(set(failed), {"단일 담보", "격리 증거금", "레버리지"})
        self.assertIn("Multi-Assets", failed["단일 담보"])
        self.assertIn("CROSSED", failed["격리 증거금"])
        self.assertTrue(all(method == "GET" for method, _ in self.calls))

    def test_order_test_401_keeps_current_margin_results_and_blocks(self):
        self.responses["/fapi/v1/order/test"] = BinanceAPIError(-2015, "Binance HTTP 401: Invalid API-key, IP, or permissions for action")
        report = self.inspect()
        self.assertFalse(report["ok"])
        checks = {c["name"]: c for c in report["checks"]}
        self.assertTrue(checks["격리 증거금"]["ok"])
        self.assertIn("현재 ISOLATED", checks["격리 증거금"]["detail"])
        self.assertTrue(checks["거래 권한"]["ok"])
        self.assertFalse(checks["주문 권한 검사"]["ok"])
        self.assertIn("Futures", checks["주문 권한 검사"]["detail"])
        self.assertIn("401", checks["주문 권한 검사"]["detail"])
        self.assertEqual(self.calls.count(("POST", "/fapi/v1/order/test")), 1)
        self.assertNotIn(("POST", "/fapi/v1/order"), self.calls)

    def test_order_test_timeout_is_not_success_or_automatically_retried(self):
        self.responses["/fapi/v1/order/test"] = TimeoutError("request timeout")
        report = self.inspect()
        self.assertFalse(report["ok"])
        self.assertIn("timeout", report["checks"][-1]["detail"])
        self.assertEqual(self.calls.count(("POST", "/fapi/v1/order/test")), 1)

    def test_missing_quote_does_not_claim_permission_check_completed(self):
        report = self.inspect(with_quote=False)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][-1]["name"], "주문 권한 검사")
        self.assertTrue(all(method == "GET" for method, _ in self.calls))


if __name__ == "__main__":
    unittest.main()

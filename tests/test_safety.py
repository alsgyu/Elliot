from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kgridbot.analytics import availability, performance
from kgridbot.app import App
from kgridbot.config import ConfigError, load_config, updated_grid
from kgridbot.engine import TradingEngine
from kgridbot.exchange.base import ExchangeError
from kgridbot.exchange.binance import BinanceFuturesAdapter, _order_from_binance
from kgridbot.exchange.paper import PaperExchange
from kgridbot.models import BotFlags, HaltReason, Mode, OrderRequest, OrderStatus, OrderType, Position, Side
from kgridbot.process_lock import ProcessLock
from kgridbot.risk import RiskManager
from kgridbot.store import Store
from test_kgridbot import write_config


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(write_config(Path(self.temp.name)))
        self.store = Store(self.config.database_path)
        self.addCleanup(self.store.close)
        self.exchange = PaperExchange(self.store, self.config.market_rules, Decimal("77500"), [Decimal("77500")])
        self.engine = TradingEngine(self.config, self.store, self.exchange)

    def test_each_candidate_checked_before_submission(self):
        self.config.grid.max_position_notional = Decimal("200")
        self.engine.tick()
        self.assertEqual(len(self.exchange.open_orders()), 1)
        self.assertTrue(self.engine.risk.exposure_check(self.exchange.position(), self.exchange.open_orders(), Decimal("77500")).ok)

    def test_equity_loss_reduces_exposure_budget(self):
        self.store.save_position(Position(self.config.symbol, realized_pnl=Decimal("-350")))
        self.engine._place_entries(Decimal("77500"))
        self.assertEqual(self.exchange.open_orders(), [])

    def test_fast_fill_between_submissions_is_included_in_next_check(self):
        self.config.grid.max_position_notional = Decimal("200")
        original = self.exchange.place_order
        def fast_fill(request):
            order = original(request)
            self.exchange._fill_order(order, request.price, force_full=True)
            return order
        with patch.object(self.exchange, "place_order", side_effect=fast_fill):
            self.engine._place_entries(Decimal("77500"))
        self.assertEqual(self.exchange.position().quantity, Decimal("0.002"))
        self.assertEqual(len(self.store.list_orders()), 1)

    def test_pause_cancels_pending_buys(self):
        self.engine.tick()
        self.engine.pause_entries()
        self.assertEqual(self.exchange.open_orders(), [])
        self.engine.tick()
        self.assertEqual(self.exchange.open_orders(), [])

    def test_pause_does_not_clear_emergency_latch(self):
        self.engine.emergency_stop()
        self.engine.pause_entries()
        self.assertEqual(self.store.flags().halt_reason, HaltReason.EMERGENCY_STOP)

    def test_resume_rejects_unresolved_losses(self):
        self.store.save_position(Position(self.config.symbol, realized_pnl=Decimal("-90")))
        self.engine.tick()
        with self.assertRaises(ExchangeError):
            self.engine.resume_entries()
        self.assertFalse(self.store.flags().can_enter)

    def test_status_is_read_only_even_before_first_tick(self):
        with patch.object(self.exchange, "market_snapshot", side_effect=AssertionError("status must not read market")):
            self.assertIsNone(self.engine.status()["market"])
        self.assertEqual(self.exchange._tick, 0)

    def test_stop_protects_increased_position(self):
        self.store.save_position(Position(self.config.symbol, quantity=Decimal("0.002"), average_entry_price=Decimal("77000")))
        self.exchange.place_or_replace_protection_stop(Decimal("74800"))
        self.store.save_position(Position(self.config.symbol, quantity=Decimal("0.006"), average_entry_price=Decimal("77000")))
        self.exchange.set_price(Decimal("74000"))
        self.assertEqual(self.exchange.position().quantity, 0)

    def test_failed_close_keeps_protection(self):
        self.store.save_position(Position(self.config.symbol, quantity=Decimal("0.002"), average_entry_price=Decimal("77000")))
        stop = self.exchange.place_or_replace_protection_stop(Decimal("74800"))
        with patch.object(self.exchange, "place_order", side_effect=ExchangeError("network down")):
            with self.assertRaises(ExchangeError):
                self.engine.cancel_entries_and_flatten()
        self.assertEqual(self.store.load_order(stop.client_order_id).status, OrderStatus.NEW)

    def test_failed_protection_cancels_buys_and_flattens(self):
        self.engine.tick()
        self.store.save_position(Position(self.config.symbol, quantity=Decimal("0.002"), average_entry_price=Decimal("77000")))
        self.exchange.protection_fail_next = True
        self.engine.tick()
        self.assertEqual(self.exchange.position().quantity, 0)
        self.assertFalse(self.exchange.open_orders())
        self.assertEqual(self.store.flags().halt_reason, HaltReason.PROTECTION_FAILED)

    def test_resume_does_not_clear_new_protection_failure(self):
        self.engine.tick()
        self.engine.pause_entries()
        self.store.save_position(Position(self.config.symbol, quantity=Decimal("0.002"), average_entry_price=Decimal("77000")))
        self.exchange.protection_fail_next = True
        with self.assertRaises(ExchangeError):
            self.engine.resume_entries()
        self.assertEqual(self.exchange.position().quantity, 0)
        self.assertEqual(self.store.flags().halt_reason, HaltReason.PROTECTION_FAILED)
        self.assertFalse(self.store.flags().can_enter)

    def test_range_exit_cancels_resting_buys(self):
        self.engine.tick()
        self.exchange._price_script = [Decimal("80000")]
        self.engine.tick()
        self.assertFalse([o for o in self.exchange.open_orders() if o.purpose == "entry"])

    def test_restart_order_and_fill_ids_remain_unique(self):
        for nonce in ("first", "second"):
            exchange = PaperExchange(self.store, self.config.market_rules, Decimal("77500"), partial_fill_ratio=Decimal("1"))
            request = self.engine.planner.make_entry_request(0, Decimal("76000"), nonce)
            exchange.place_order(request)
            exchange.set_price(Decimal("76000"))
        fills = self.store.list_fills()
        self.assertEqual(len(fills), 2)
        self.assertEqual(len({fill.fill_id for fill in fills}), 2)

    def test_fill_failure_rolls_back_position_order_and_fees(self):
        request = self.engine.planner.make_entry_request(0, Decimal("76000"), "atomic")
        self.exchange.place_order(request)
        with patch.object(self.store, "save_fill", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                self.exchange.set_price(Decimal("76000"))
        self.assertEqual(self.exchange.position().quantity, 0)
        self.assertEqual(self.exchange.position().fees_paid, 0)
        self.assertEqual(self.store.load_order(request.client_order_id).filled_quantity, 0)

    def test_response_loss_does_not_duplicate_take_profit(self):
        self.engine.tick()
        self.exchange._price_script = [Decimal("76000")]
        actual = self.exchange.place_order
        lost = False
        def once(request):
            nonlocal lost
            order = actual(request)
            if request.purpose == "take_profit" and not lost:
                lost = True
                raise ExchangeError("lost reply")
            return order
        with patch.object(self.exchange, "place_order", side_effect=once):
            with self.assertRaises(ExchangeError):
                self.engine.tick()
        self.engine.tick()
        tp = [o for o in self.exchange.open_orders() if o.purpose == "take_profit"]
        self.assertEqual(sum(o.remaining_quantity for o in tp), self.exchange.position().quantity)
        self.assertEqual(len({o.client_order_id for o in tp}), len(tp))

    def test_concurrent_ticks_and_pause_do_not_duplicate_levels(self):
        errors = []
        def tick():
            try:
                self.engine.tick()
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=tick) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        entries = [o for o in self.exchange.open_orders() if o.purpose == "entry"]
        self.assertEqual(len(entries), len({o.level_index for o in entries}))
        self.assertLessEqual(len(entries), self.config.grid.max_open_orders)

    def test_invalid_settings_rejected_without_mutation(self):
        for values in ({"leverage": "5.9"}, {"leverage": True}, {"lower_price": "NaN"},
                       {"stop_loss_price": "-1"}, {"grid_count": 500}, {"grid_count": 200},
                       {"daily_loss_limit": "-1"}, {"allocated_margin": "Infinity"}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                self.engine.preview_settings(values)
        self.assertEqual(self.config.grid.leverage, 2)

    def test_settings_persist_and_allow_leverage_and_capital(self):
        self.engine.pause_entries()
        self.engine.update_settings({"leverage": "5", "allocated_margin": "600", "grid_spacing": "geometric"})
        cfg = load_config(write_config(Path(self.temp.name)))
        engine = TradingEngine(cfg, self.store, self.exchange)
        self.assertEqual(engine.config.grid.leverage, 5)
        self.assertEqual(engine.config.grid.allocated_margin, 600)
        self.assertEqual(engine.config.grid.grid_spacing, "geometric")
        self.assertFalse(self.store.flags().can_enter)

    def test_settings_rejected_while_running(self):
        with self.assertRaises(ValueError):
            self.engine.update_settings({"leverage": 3})
        self.assertIsNone(self.store.get_json("grid_settings"))

    def test_funding_is_signed_and_idempotent(self):
        self.store.save_position(Position(self.config.symbol, quantity=Decimal("0.01")))
        self.exchange.settle_funding("first", Decimal("0.001"), Decimal("77000"))
        self.exchange.settle_funding("first", Decimal("0.001"), Decimal("77000"))
        self.exchange.settle_funding("second", Decimal("-0.0005"), Decimal("77000"))
        self.assertEqual(self.exchange.position().funding_paid, Decimal("0.385"))

    def test_post_only_does_not_fill_crossing_order(self):
        request = self.engine.planner.make_entry_request(6, Decimal("79000"), "cross")
        with self.assertRaises(ExchangeError):
            self.exchange.place_order(request)
        self.assertEqual(self.exchange.position().quantity, 0)

    def test_single_process_lock(self):
        path = Path(self.temp.name) / "owner.lock"
        first = ProcessLock(path)
        try:
            with self.assertRaises(RuntimeError):
                ProcessLock(path)
        finally:
            first.close()
        second = ProcessLock(path)
        second.close()

    def test_live_is_blocked_even_with_boolean_enabled(self):
        with self.assertRaises(ConfigError):
            App(replace(self.config, mode=Mode.LIVE, live_trading_enabled=True))

    def test_binance_uses_algo_api_for_protective_stop(self):
        adapter = BinanceFuturesAdapter(self.store, self.config.market_rules, "https://testnet.binancefuture.com", "UNUSED", "UNUSED", True)
        request = OrderRequest("kgb-protect-unit", self.config.symbol, Side.SELL, OrderType.STOP_MARKET,
                               Decimal("0.002"), stop_price=Decimal("74800"), reduce_only=True,
                               close_position=True, post_only=False, purpose="protection_stop")
        response = {"algoId": 1, "clientAlgoId": request.client_order_id, "orderType": "STOP_MARKET", "algoStatus": "NEW",
                    "symbol": "BTCUSDT", "side": "SELL", "triggerPrice": "74800", "closePosition": "true"}
        with patch.object(adapter, "_signed", return_value=response) as signed:
            order = adapter.place_order(request)
        method, path, params = signed.call_args.args
        self.assertEqual((method, path), ("POST", "/fapi/v1/algoOrder"))
        self.assertEqual(params["triggerPrice"], "74800")
        self.assertNotIn("quantity", params)
        self.assertNotIn("reduceOnly", params)
        self.assertEqual(order.purpose, "protection_stop")
        self.assertEqual(order.status, OrderStatus.NEW)

    def test_binance_query_failure_never_returns_stale_success(self):
        adapter = BinanceFuturesAdapter(self.store, self.config.market_rules, "https://testnet.binancefuture.com", "UNUSED", "UNUSED", True)
        request = self.engine.planner.make_entry_request(0, Decimal("76000"), "query")
        self.exchange.place_order(request)
        with patch.object(adapter, "_signed", side_effect=ExchangeError("timeout")):
            with self.assertRaises(ExchangeError):
                adapter.query_order(request.client_order_id)

    def test_binance_lost_response_is_queried_without_resubmission(self):
        adapter = BinanceFuturesAdapter(self.store, self.config.market_rules, "https://testnet.binancefuture.com", "UNUSED", "UNUSED", True)
        request = self.engine.planner.make_entry_request(0, Decimal("76000"), "lost")
        response = {"clientOrderId": request.client_order_id, "orderId": 42, "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "status": "NEW", "origQty": "0.002", "price": "76000"}
        posts = []
        def signed(method, path, params):
            if method == "POST":
                posts.append(params)
                raise ExchangeError("reply lost after acceptance")
            return response
        with patch.object(adapter, "_signed", side_effect=signed):
            first = adapter.place_order(request)
            second = adapter.place_order(request)
        self.assertEqual(len(posts), 1)
        self.assertEqual(first.exchange_order_id, second.exchange_order_id)
        self.assertEqual(second.level_index, 0)

    def test_binance_unknown_intent_survives_query_failure(self):
        adapter = BinanceFuturesAdapter(self.store, self.config.market_rules, "https://testnet.binancefuture.com", "UNUSED", "UNUSED", True)
        request = self.engine.planner.make_entry_request(0, Decimal("76000"), "unknown")
        with patch.object(adapter, "_signed", side_effect=ExchangeError("offline")) as signed:
            for _ in range(2):
                with self.assertRaises(ExchangeError):
                    adapter.place_order(request)
        self.assertEqual(sum(call.args[0] == "POST" for call in signed.call_args_list), 1)
        self.assertEqual(self.store.load_order(request.client_order_id).status, OrderStatus.UNKNOWN)

    def test_binance_funding_income_is_idempotent(self):
        adapter = BinanceFuturesAdapter(self.store, self.config.market_rules, "https://testnet.binancefuture.com", "UNUSED", "UNUSED", True)
        rows = [{"tranId": 1, "asset": "USDT", "income": "-0.25", "time": 1700000000000}]
        with patch.object(adapter, "_signed", return_value=rows):
            adapter._sync_funding()
            adapter._last_income_sync = 0
            adapter._sync_funding()
        self.assertEqual(self.exchange.position().funding_paid, Decimal("0.25"))

    def test_minute_history_retains_capital_changes(self):
        self.store.record_sample(120, Decimal("77000"), Decimal("400"), Decimal("400"))
        self.store.record_sample(121, Decimal("77001"), Decimal("401"), Decimal("400"))
        self.assertEqual(len(self.store.samples()), 1)
        self.store.record_sample(122, Decimal("77001"), Decimal("501"), Decimal("500"))
        self.assertEqual(len(self.store.samples()), 2)

    def test_daily_baseline_rolls_in_seoul_without_clearing_halt(self):
        from datetime import datetime
        from kgridbot.risk import SEOUL
        self.engine.risk.clock = lambda: datetime(2026, 9, 29, 23, 59, tzinfo=SEOUL)
        position = Position(self.config.symbol, realized_pnl=Decimal("-10"))
        self.engine.risk.ensure_daily_baseline(position, Decimal("77000"))
        self.engine.halt(HaltReason.DAILY_LOSS_LIMIT)
        self.engine.risk.clock = lambda: datetime(2026, 9, 30, 0, 1, tzinfo=SEOUL)
        self.engine.risk.ensure_daily_baseline(position, Decimal("77000"))
        self.assertEqual(self.engine.risk.pnl_snapshot(position, Decimal("77000")).daily_net_pnl, 0)
        self.assertFalse(self.store.flags().can_enter)

    def test_binance_metadata_and_string_booleans_preserved(self):
        request = self.engine.planner.make_entry_request(1, Decimal("76500"), "metadata")
        previous = self.exchange.place_order(request)
        order = _order_from_binance({"clientOrderId": request.client_order_id, "orderId": 23, "type": "LIMIT", "status": "EXPIRED", "reduceOnly": "false"}, previous)
        self.assertEqual(order.level_index, 1)
        self.assertEqual(order.purpose, "entry")
        self.assertFalse(order.reduce_only)
        self.assertEqual(order.status, OrderStatus.CANCELED)

    @unittest.skipUnless(availability()["available"], "optional QuantStats is not installed")
    def test_quantstats_excludes_cash_contributions(self):
        samples = [{"timestamp": 1704067200, "equity": "100", "capital": "100"},
                   {"timestamp": 1704070800, "equity": "200", "capital": "200"},
                   {"timestamp": 1704074400, "equity": "190", "capital": "200"}]
        result = performance(samples, 100)
        self.assertAlmostEqual(result["total_return"], -0.05)
        self.assertAlmostEqual(result["max_drawdown"], -0.05)
        self.assertIsNone(result["sharpe"])


if __name__ == "__main__":
    unittest.main()

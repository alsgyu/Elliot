from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from kgridbot.config import ConfigError, load_config
from kgridbot.engine import TradingEngine
from kgridbot.exchange.paper import PaperExchange
from kgridbot.models import BotFlags, HaltReason, OrderRequest, OrderStatus, OrderType, Position, Side
from kgridbot.risk import RiskManager
from kgridbot.store import Store
from kgridbot.strategy import GridPlanner


def write_config(tmp: Path, **overrides) -> Path:
    raw = {
        "mode": "paper",
        "exchange": "binance_perpetual",
        "symbol": "BTCUSDT",
        "database_path": str(tmp / "bot.sqlite3"),
        "host": "127.0.0.1",
        "port": 0,
        "grid": {
            "lower_price": "76000",
            "upper_price": "79000",
            "grid_count": 7,
            "allocated_margin": "400",
            "order_quote": "160",
            "max_open_orders": 3,
            "leverage": 2,
            "stop_loss_price": "74800",
            "daily_loss_limit": "30",
            "total_loss_limit": "80",
            "max_position_notional": "700",
            "max_open_order_notional": "480",
            "spread_bps": "2",
            "expected_funding_bps": "3",
        },
    }
    for key, value in overrides.items():
        if key == "grid":
            raw["grid"].update(value)
        else:
            raw[key] = value
    path = tmp / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class KGridBotTest(unittest.TestCase):
    def make_engine(self, tmp: Path, prices: list[str] | None = None):
        config = load_config(write_config(tmp))
        store = Store(config.database_path)
        self.addCleanup(store.close)
        exchange = PaperExchange(
            store,
            config.market_rules,
            Decimal("77500"),
            [Decimal(price) for price in prices] if prices else None,
        )
        return config, store, exchange, TradingEngine(config, store, exchange)

    def test_rejects_leverage_above_five(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_config(Path(td), grid={"leverage": 6})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_quantity_rounding_respects_step_and_min_notional(self):
        with tempfile.TemporaryDirectory() as td:
            config = load_config(write_config(Path(td)))
            planner = GridPlanner(config.grid, config.market_rules)
            request = planner.make_entry_request(0, Decimal("76000"), "n")
            self.assertEqual(request.quantity % config.market_rules.step_size, Decimal("0"))
            self.assertGreaterEqual(request.quantity * request.price, config.market_rules.min_notional)

    def test_exposure_includes_open_entries(self):
        with tempfile.TemporaryDirectory() as td:
            config, store, exchange, engine = self.make_engine(Path(td))
            engine.tick()
            risk = RiskManager(store, config.grid, config.market_rules)
            check = risk.exposure_check(exchange.position(), exchange.open_orders(), Decimal("77500"))
            self.assertTrue(check.ok)
            tight = load_config(write_config(Path(td), database_path=str(Path(td) / "tight.sqlite3"), grid={"max_open_order_notional": "10"}))
            tight_store = Store(tight.database_path)
            self.addCleanup(tight_store.close)
            tight_exchange = PaperExchange(tight_store, tight.market_rules, Decimal("77500"))
            tight_engine = TradingEngine(tight, tight_store, tight_exchange)
            tight_engine.tick()
            tight_check = RiskManager(tight_store, tight.grid, tight.market_rules).exposure_check(
                tight_exchange.position(), tight_exchange.open_orders(), Decimal("77500")
            )
            self.assertTrue(tight_check.ok)
            self.assertEqual(tight_exchange.open_orders(), [])

    def test_partial_fills_create_take_profit_only_for_filled_quantity(self):
        with tempfile.TemporaryDirectory() as td:
            config, store, exchange, engine = self.make_engine(Path(td), prices=["77500", "76000", "76000"])
            engine.tick()
            exchange.set_price(Decimal("76000"))
            engine.tick()
            entry_filled = sum(
                order.filled_quantity
                for order in store.list_orders()
                if order.purpose == "entry" and order.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}
            )
            tp_open = sum(order.quantity for order in exchange.open_orders() if order.purpose == "take_profit")
            self.assertGreater(entry_filled, Decimal("0"))
            self.assertLessEqual(tp_open, entry_filled)

    def test_loss_halt_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as td:
            config, store, exchange, engine = self.make_engine(Path(td), prices=["77500"])
            store.save_position(
                Position(
                    symbol=config.symbol,
                    quantity=Decimal("0"),
                    average_entry_price=Decimal("0"),
                    realized_pnl=Decimal("-100"),
                )
            )
            engine.tick()
            self.assertEqual(store.flags().halt_reason, HaltReason.TOTAL_LOSS_LIMIT)
            restarted = Store(config.database_path)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.flags().halt_reason, HaltReason.TOTAL_LOSS_LIMIT)
            self.assertTrue(restarted.flags().manual_resume_required)

    def test_protection_failure_blocks_new_entries(self):
        with tempfile.TemporaryDirectory() as td:
            config, store, exchange, engine = self.make_engine(Path(td), prices=["77500"])
            store.save_position(Position(symbol=config.symbol, quantity=Decimal("0.01"), average_entry_price=Decimal("77000")))
            exchange.protection_fail_next = True
            engine.tick()
            self.assertEqual(store.flags().halt_reason, HaltReason.PROTECTION_FAILED)
            self.assertFalse(store.flags().can_enter)

    def test_exchange_stop_fill_cancels_entries_and_halts(self):
        with tempfile.TemporaryDirectory() as td:
            config, store, exchange, engine = self.make_engine(Path(td), prices=["77500", "74700"])
            engine.tick()
            store.save_position(Position(symbol=config.symbol, quantity=Decimal("0.01"), average_entry_price=Decimal("77000")))
            exchange.place_or_replace_protection_stop(config.grid.stop_loss_price)
            exchange.set_price(Decimal("74700"))
            engine.tick()
            self.assertEqual(store.flags().halt_reason, HaltReason.EMERGENCY_STOP)
            self.assertFalse(any(order.purpose == "entry" for order in exchange.open_orders()))
            self.assertEqual(exchange.position().quantity, Decimal("0"))

    def test_client_order_id_prevents_duplicate_submit_after_lost_response(self):
        with tempfile.TemporaryDirectory() as td:
            config, store, exchange, _engine = self.make_engine(Path(td))
            request = OrderRequest(
                client_order_id="same-id",
                symbol=config.symbol,
                side=Side.BUY,
                order_type=OrderType.LIMIT_MAKER,
                quantity=Decimal("0.002"),
                price=Decimal("76000"),
            )
            first = exchange.place_order(request)
            second = exchange.place_order(request)
            self.assertEqual(first.exchange_order_id, second.exchange_order_id)
            self.assertEqual(len(exchange.open_orders()), 1)

    def test_reconcile_after_network_recovery_keeps_single_order_set(self):
        with tempfile.TemporaryDirectory() as td:
            _config, _store, exchange, engine = self.make_engine(Path(td), prices=["77500"])
            engine.tick()
            count_before = len(exchange.open_orders())
            exchange.network_down = True
            with self.assertRaises(Exception):
                engine.tick()
            exchange.network_down = False
            exchange.reconcile()
            self.assertEqual(len(exchange.open_orders()), count_before)

    def test_status_api_read_does_not_create_extra_orders(self):
        with tempfile.TemporaryDirectory() as td:
            _config, _store, exchange, engine = self.make_engine(Path(td), prices=["77500"])
            engine.tick()
            count_before = len(exchange.open_orders())
            engine.status()
            self.assertEqual(len(exchange.open_orders()), count_before)


if __name__ == "__main__":
    unittest.main()

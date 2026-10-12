from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict
from collections import deque
from decimal import Decimal
from functools import wraps
from uuid import uuid4

from .config import RuntimeConfig, grid_json, updated_grid
from .exchange.base import ExchangeAdapter, ExchangeError, ProtectionOrderError
from .models import BotFlags, HaltReason, Order, OrderRequest, OrderStatus, OrderType, Side
from .risk import RiskManager
from .store import Store
from .strategy import GridPlanner


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


class TradingEngine:
    def __init__(self, config: RuntimeConfig, store: Store, exchange: ExchangeAdapter):
        self.config, self.store, self.exchange = config, store, exchange
        self.lock = threading.RLock()
        saved = store.get_json("grid_settings")
        if saved:
            config.grid = updated_grid(config.grid, saved, config.market_rules)
        self.planner = GridPlanner(config.grid, config.market_rules)
        self.risk = RiskManager(store, config.grid, config.market_rules)
        self._stop = threading.Event()
        self._thread = None
        self._last_snapshot = None
        self.last_error = None
        self.last_tick = None
        self.history = deque(store.samples(300), maxlen=300)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self.lock:
            self.exchange.reconcile()
            if self.store.flags().manual_resume_required:
                self._cancel_entries()
            self._manage_protection(self.exchange.position())
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="kgridbot-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise RuntimeError("engine shutdown is still pending")

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
                    self.store.set_flags(BotFlags(HaltReason.RECONCILIATION_REQUIRED, False, True, True))
                    self.store.add_event("ERROR", "engine tick failed; manual reconciliation required", {"error": str(exc)})
                    try:
                        self._cancel_entries()
                    except ExchangeError:
                        pass
            self._stop.wait(1)

    @serialized
    def tick(self) -> dict[str, object]:
        before = self.exchange.position()
        baseline_price = self._last_snapshot.mark_price if self._last_snapshot else before.average_entry_price
        self.risk.ensure_daily_baseline(before, baseline_price)
        snapshot = self.exchange.market_snapshot()
        self._last_snapshot = snapshot
        self._process_stop_fills()
        position = self.exchange.position()
        if position.quantity > 0 and snapshot.mark_price <= self.config.grid.stop_loss_price:
            self.halt(HaltReason.EMERGENCY_STOP)
            self._flatten()
        self._manage_protection(self.exchange.position())
        self._process_new_fills()
        position = self.exchange.position()
        pnl = self.risk.pnl_snapshot(position, snapshot.mark_price)
        fault = self.risk.check_market_freshness(snapshot, time.time()) or self.risk.check_loss_limits(pnl)
        if fault and self.store.flags().halt_reason not in {HaltReason.EMERGENCY_STOP, HaltReason.PROTECTION_FAILED}:
            self.halt(fault.halt_reason)
        if not self.config.grid.lower_price <= snapshot.mark_price <= self.config.grid.upper_price:
            self._cancel_entries()
        flags = self.store.flags()
        if flags.can_enter and not flags.manual_resume_required:
            self._place_entries(snapshot.mark_price)
        self.last_tick = time.time()
        self.last_error = None
        self.store.record_sample(self.last_tick, snapshot.mark_price, self.config.grid.allocated_margin + pnl.net_pnl, self.config.grid.allocated_margin)
        self.history.append({"timestamp": self.last_tick, "price": str(snapshot.mark_price),
                             "equity": str(self.config.grid.allocated_margin + pnl.net_pnl),
                             "capital": str(self.config.grid.allocated_margin)})
        return self.status()

    def _process_stop_fills(self) -> None:
        for fill in self.exchange.recent_fills():
            if self.store.get_json(f"processed_fill:{fill.fill_id}"):
                continue
            order = self.store.load_order(fill.client_order_id)
            if order and order.purpose == "protection_stop":
                self.halt(HaltReason.EMERGENCY_STOP)
                # A stop can fill before a remaining buy during a disconnected interval.
                self._flatten()
                self.store.set_json(f"processed_fill:{fill.fill_id}", True)

    def _process_new_fills(self) -> None:
        for fill in reversed(self.exchange.recent_fills()):
            key = f"processed_fill:{fill.fill_id}"
            if self.store.get_json(key):
                continue
            order = self.store.load_order(fill.client_order_id)
            if order is None:
                self.halt(HaltReason.RECONCILIATION_REQUIRED)
                raise ExchangeError("fill belongs to an unknown order")
            if order.purpose == "entry" and fill.side == Side.BUY:
                if not self._place_take_profit(order, fill.quantity, fill.fill_id):
                    continue
            self.store.set_json(key, True)

    def _place_take_profit(self, entry: Order, quantity: Decimal, fill_id: str) -> bool:
        nonce = hashlib.sha256(fill_id.encode()).hexdigest()[:16]
        request = self.planner.make_take_profit_request(entry, quantity, nonce)
        if self.store.load_order(request.client_order_id):
            return True
        reserved = sum((order.remaining_quantity for order in self.exchange.open_orders()
                        if order.purpose == "take_profit"), Decimal("0"))
        available = max(Decimal("0"), self.exchange.position().quantity - reserved)
        if available <= 0:
            return self.exchange.position().quantity == 0
        request.quantity = min(quantity, available)
        # Keep a marketable maker exit pending until it can rest without crossing.
        if self._last_snapshot and request.price <= self._last_snapshot.bid:
            self.halt(HaltReason.RECONCILIATION_REQUIRED)
            return False
        self.exchange.place_order(request)
        return True

    def _manage_protection(self, position) -> bool:
        if position.quantity <= 0:
            for order in self.exchange.open_orders():
                if order.purpose in {"protection_stop", "take_profit"}:
                    self.exchange.cancel_order(order.client_order_id)
            return True
        if any(order.purpose == "protection_stop" for order in self.exchange.open_orders()):
            return True
        try:
            stop = self.exchange.place_or_replace_protection_stop(self.config.grid.stop_loss_price)
            if stop is None or stop.status not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                raise ProtectionOrderError("protection order is not active")
            return True
        except ExchangeError:
            self.halt(HaltReason.PROTECTION_FAILED)
            self._flatten()
            return False

    def _place_entries(self, price: Decimal) -> None:
        if not 1 <= self.config.grid.leverage <= 5:
            raise ExchangeError("leverage must be between 1 and 5")
        orders = self.exchange.open_orders()
        position = self.exchange.position()
        capacity = self.config.grid.max_open_orders - sum(o.purpose == "entry" for o in orders)
        for index, level in self.planner.entry_levels(price, orders)[:max(0, capacity)]:
            position = self.exchange.position()
            orders = self.exchange.open_orders()
            request = self.planner.make_entry_request(index, level, uuid4().hex[:16])
            candidate = Order(**asdict(request), exchange_order_id=None, status=OrderStatus.NEW)
            check = self.risk.exposure_check(position, orders + [candidate], price)
            if not check.ok:
                break
            orders.append(self.exchange.place_order(request))

    @serialized
    def pause_entries(self) -> None:
        self.halt(HaltReason.USER_PAUSED)

    @serialized
    def resume_entries(self) -> None:
        self.exchange.reconcile()
        snapshot = self._last_snapshot
        if not snapshot or self.risk.check_market_freshness(snapshot, time.time()):
            raise ExchangeError("fresh market data is required before resuming")
        if self.risk.check_loss_limits(self.risk.pnl_snapshot(self.exchange.position(), snapshot.mark_price)):
            raise ExchangeError("loss limit is still exceeded")
        if snapshot.mark_price <= self.config.grid.stop_loss_price:
            raise ExchangeError("market is below the stop-loss price")
        if not self._manage_protection(self.exchange.position()):
            raise ExchangeError("protection failed during resume; entries remain halted")
        if self.exchange.position().quantity > 0 and not any(o.purpose == "protection_stop" for o in self.exchange.open_orders()):
            raise ExchangeError("active protection is required")
        self.store.set_flags(BotFlags())
        self.store.add_event("INFO", "manual resume accepted")

    @serialized
    def emergency_stop(self) -> None:
        self.halt(HaltReason.EMERGENCY_STOP)
        self._flatten()

    @serialized
    def cancel_entries_and_flatten(self) -> None:
        self.halt(HaltReason.USER_PAUSED)
        self._flatten()

    def _flatten(self) -> None:
        for order in self.exchange.open_orders():
            if order.purpose in {"entry", "take_profit"}:
                self.exchange.cancel_order(order.client_order_id)
        position = self.exchange.position()
        if position.quantity > 0:
            self.exchange.place_order(OrderRequest(
                client_order_id=f"kgb-close-{uuid4().hex[:16]}", symbol=self.config.symbol,
                side=Side.SELL, order_type=OrderType.MARKET, quantity=position.quantity,
                reduce_only=True, post_only=False, purpose="manual_close"))
        if self.exchange.position().quantity != 0:
            raise ExchangeError("close pending: remaining position is not zero")
        for order in self.exchange.open_orders():
            if order.purpose == "protection_stop":
                self.exchange.cancel_order(order.client_order_id)

    def halt(self, reason: HaltReason) -> None:
        current = self.store.flags()
        if reason == HaltReason.USER_PAUSED and current.halt_reason not in {HaltReason.NONE, HaltReason.USER_PAUSED}:
            reason = current.halt_reason
        self.store.set_flags(BotFlags(reason, False, reason == HaltReason.RECONCILIATION_REQUIRED, True))
        self._cancel_entries()
        if current.halt_reason != reason:
            self.store.add_event("WARN", "bot halted", {"reason": reason.value})

    def _cancel_entries(self) -> None:
        for order in self.exchange.open_orders():
            if order.purpose == "entry" and order.client_order_id.startswith("kgb-entry-"):
                self.exchange.cancel_order(order.client_order_id)

    @serialized
    def preview_settings(self, values: dict) -> dict:
        grid = updated_grid(self.config.grid, values, self.config.market_rules)
        planner = GridPlanner(grid, self.config.market_rules)
        costs = RiskManager(self.store, grid, self.config.market_rules).validate_grid_costs()
        cap = min(grid.max_position_notional, grid.allocated_margin * grid.leverage * Decimal("0.90"))
        return {"grid": grid_json(grid), "levels": [str(p) for p in planner.levels],
                "effective_notional_cap": str(cap), "costs": costs.details,
                "orders": [{"price": str(price), "quantity": str(planner.make_entry_request(i, price, "preview").quantity)}
                           for i, price in enumerate(planner.levels[:-1])]}

    @serialized
    def update_settings(self, values: dict) -> dict:
        preview = self.preview_settings(values)
        if self.config.mode.value != "paper":
            raise ValueError("settings changes are available in paper mode only")
        if self.store.flags().can_enter or self.exchange.position().quantity != 0 or self.exchange.open_orders():
            raise ValueError("pause and flatten all positions and orders before applying settings")
        grid = updated_grid(self.config.grid, values, self.config.market_rules)
        if grid.allocated_margin + self.risk.pnl_snapshot(self.exchange.position(), self._last_snapshot.mark_price if self._last_snapshot else Decimal("0")).net_pnl <= 0:
            raise ValueError("allocated capital plus cumulative PnL must remain positive")
        self.store.set_json("grid_settings", grid_json(grid))
        self.config.grid = grid
        if hasattr(self.exchange, "slippage_bps"):
            self.exchange.slippage_bps = grid.slippage_bps
        self.planner = GridPlanner(grid, self.config.market_rules)
        self.risk = RiskManager(self.store, grid, self.config.market_rules)
        self.store.add_event("INFO", "grid settings updated", grid_json(grid))
        return preview

    @serialized
    def status(self) -> dict[str, object]:
        snapshot = self._last_snapshot
        mark = snapshot.mark_price if snapshot else Decimal("0")
        position = self.store.load_position(self.config.symbol)
        pnl = self.risk.pnl_snapshot(position, mark) if snapshot else None
        orders = self.store.list_orders()
        return {
            "mode": self.config.mode.value, "exchange": self.config.exchange, "symbol": self.config.symbol,
            "engine": "local-paper" if self.config.mode.value == "paper" else "binance-rest",
            "hb_connector": self.config.hb_connector, "hb_trading_pair": self.config.hb_trading_pair,
            "flags": self.store.flags().to_json(), "market": asdict(snapshot) if snapshot else None,
            "position": {**position.to_json(mark), "leverage": self.config.grid.leverage,
                         "margin": str(abs(position.quantity) * mark / self.config.grid.leverage)},
            "pnl": pnl.to_json() if pnl else None, "grid": grid_json(self.config.grid),
            "orders": [o.to_json() for o in orders[:200]],
            "fills": [f.to_json() for f in self.store.list_fills(100)], "events": self.store.events(30),
            "market_rules": self.config.market_rules.to_json(), "grid_levels": [str(p) for p in self.planner.levels],
            "risk": asdict(self.risk.exposure_check(position, orders, mark)),
            "history": list(self.history),
            "connection": {"last_tick": self.last_tick, "error": self.last_error,
                           "connected": self.last_tick is not None and time.time() - self.last_tick < self.config.grid.stale_price_seconds},
        }

from __future__ import annotations

import math
from functools import wraps
from decimal import Decimal
from uuid import uuid4
from time import time

from ..decimal_utils import quantize_price, quantize_quantity
from ..models import Fill, MarketRules, MarketSnapshot, Order, OrderRequest, OrderStatus, OrderType, Position, Side
from ..store import Store
from .base import ExchangeAdapter, ExchangeError, ProtectionOrderError


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.transaction():
            return method(self, *args, **kwargs)
    return wrapped


class PaperExchange(ExchangeAdapter):
    def __init__(
        self,
        store: Store,
        rules: MarketRules,
        initial_price: Decimal,
        price_script: list[Decimal] | None = None,
        partial_fill_ratio: Decimal = Decimal("0.5"),
        slippage_bps: Decimal = Decimal("5"),
    ):
        self.store = store
        self.rules = rules
        self._price = initial_price
        self._initial_price = initial_price
        self._price_script = price_script or []
        self._tick = 0
        self.slippage_bps = slippage_bps
        self.partial_fill_ratio = partial_fill_ratio
        self.protection_fail_next = False
        self.network_down = False

    def market_snapshot(self) -> MarketSnapshot:
        if self.network_down:
            raise ExchangeError("paper network is disconnected")
        if self._price_script:
            self._price = self._price_script[min(self._tick, len(self._price_script) - 1)]
        else:
            drift = Decimal(str(math.sin(self._tick / 18) * float(self._initial_price) * 0.018))
            self._price = quantize_price(self._initial_price + drift, self.rules.tick_size)
        self._tick += 1
        self.process_price(self._price)
        spread = self.rules.tick_size
        return MarketSnapshot(
            symbol=self.rules.symbol,
            price=self._price,
            bid=quantize_price(self._price - spread, self.rules.tick_size),
            ask=quantize_price(self._price + spread, self.rules.tick_size),
            mark_price=self._price,
            funding_rate=Decimal("0"),
            next_funding_time_ms=0,
            timestamp=time(),
            source="paper-simulated",
        )

    def set_price(self, price: Decimal) -> None:
        self._price = price
        self.process_price(price)

    def place_order(self, request: OrderRequest) -> Order:
        if self.network_down:
            raise ExchangeError("paper network is disconnected")
        remembered = self.store.get_idempotency(request.client_order_id)
        if remembered:
            existing = self.store.load_order(remembered["client_order_id"])
            if existing is not None:
                return existing
        if request.post_only and request.price is not None:
            crosses = request.price >= self._price + self.rules.tick_size if request.side == Side.BUY else request.price <= self._price - self.rules.tick_size
            if crosses:
                raise ExchangeError("post-only order would cross the book")
        order = Order(
            client_order_id=request.client_order_id,
            exchange_order_id=f"paper-{uuid4().hex}",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            stop_price=request.stop_price,
            status=OrderStatus.NEW,
            reduce_only=request.reduce_only,
            post_only=request.post_only,
            close_position=request.close_position,
            level_index=request.level_index,
            purpose=request.purpose,
        )
        self.store.save_order(order)
        self.store.remember_idempotency(request.client_order_id, {"client_order_id": request.client_order_id})
        self.store.add_event("INFO", "paper order accepted", order.to_json())
        if order.order_type == OrderType.MARKET:
            self._fill_order(order, self._price, force_full=True, maker=False)
        return order

    def cancel_order(self, client_order_id: str) -> Order:
        order = self.store.load_order(client_order_id)
        if order is None:
            raise ExchangeError(f"unknown order {client_order_id}")
        if order.status in {OrderStatus.FILLED, OrderStatus.CANCELED}:
            return order
        order.status = OrderStatus.CANCELED
        order.updated_at = time()
        self.store.save_order(order)
        self.store.add_event("INFO", "paper order canceled", {"client_order_id": client_order_id})
        return order

    def open_orders(self) -> list[Order]:
        return self.store.list_orders([OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED])

    def position(self) -> Position:
        return self.store.load_position(self.rules.symbol)

    def recent_fills(self) -> list[Fill]:
        return self.store.list_fills(100000)

    def place_or_replace_protection_stop(self, stop_price) -> Order | None:
        if self.protection_fail_next:
            self.protection_fail_next = False
            raise ProtectionOrderError("simulated protection stop registration failure")
        position = self.position()
        for order in self.open_orders():
            if order.purpose == "protection_stop":
                self.cancel_order(order.client_order_id)
        if position.quantity <= 0:
            return None
        req = OrderRequest(
            client_order_id=f"kgb-protect-{uuid4().hex[:16]}",
            symbol=self.rules.symbol,
            side=Side.SELL,
            order_type=OrderType.STOP_MARKET,
            quantity=position.quantity,
            stop_price=Decimal(stop_price),
            reduce_only=True,
            post_only=False,
            close_position=True,
            purpose="protection_stop",
        )
        return self.place_order(req)

    def reconcile(self) -> None:
        # Paper state is local but still exercises restart reconciliation paths.
        position = self.position()
        open_orders = self.open_orders()
        if position.quantity == 0:
            for order in open_orders:
                if order.purpose in {"take_profit", "protection_stop"}:
                    self.cancel_order(order.client_order_id)
        self.store.add_event("INFO", "paper reconciliation completed", {"open_orders": len(open_orders)})

    def process_price(self, price: Decimal) -> None:
        for order in list(self.open_orders()):
            if order.order_type == OrderType.LIMIT_MAKER:
                if order.side == Side.BUY and price <= (order.price or price):
                    self._fill_order(order, order.price or price)
                elif order.side == Side.SELL and price >= (order.price or price):
                    self._fill_order(order, order.price or price)
            elif order.order_type == OrderType.STOP_MARKET and order.side == Side.SELL:
                if order.stop_price is not None and price <= order.stop_price:
                    self._fill_order(order, price, force_full=True, maker=False)

    @atomic
    def _fill_order(self, order: Order, price: Decimal, force_full: bool = False, maker: bool = True) -> None:
        if order.close_position:
            order.quantity = order.filled_quantity + abs(self.position().quantity)
        remaining = order.remaining_quantity
        if remaining <= 0:
            return
        if force_full or order.status == OrderStatus.PARTIALLY_FILLED:
            fill_qty = remaining
        else:
            fill_qty = quantize_quantity(remaining * self.partial_fill_ratio, self.rules.step_size)
            if fill_qty <= 0:
                fill_qty = remaining
        if fill_qty <= 0:
            return
        if order.reduce_only:
            fill_qty = min(fill_qty, abs(self.position().quantity))
        if fill_qty <= 0:
            order.status = OrderStatus.CANCELED
            order.updated_at = time()
            self.store.save_order(order)
            return

        if not maker:
            price = quantize_price(price * (1 + (1 if order.side == Side.BUY else -1) * self.slippage_bps / 10000), self.rules.tick_size)
        fee_rate = self.rules.maker_fee_rate if maker else self.rules.taker_fee_rate
        fee = abs(fill_qty * price * fee_rate)
        realized = self._apply_position(order.side, fill_qty, price, fee)
        order.filled_quantity += fill_qty
        order.average_fill_price = price
        order.status = OrderStatus.FILLED if order.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
        order.updated_at = time()
        self.store.save_order(order)
        fill = Fill(
            fill_id=f"{order.exchange_order_id}-{order.filled_quantity}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=price,
            fee=fee,
            fee_asset=self.rules.margin_asset,
            realized_pnl=realized,
            maker=maker,
        )
        if self.store.save_fill(fill):
            self.store.add_event("INFO", "paper fill", fill.to_json())

    def _apply_position(self, side: Side, quantity: Decimal, price: Decimal, fee: Decimal) -> Decimal:
        position = self.position()
        realized = Decimal("0")
        if side == Side.BUY:
            new_qty = position.quantity + quantity
            if new_qty > 0:
                position.average_entry_price = (
                    (position.quantity * position.average_entry_price) + (quantity * price)
                ) / new_qty
            position.quantity = new_qty
        else:
            close_qty = min(quantity, position.quantity)
            realized = (price - position.average_entry_price) * close_qty
            position.quantity -= close_qty
            position.realized_pnl += realized
            if position.quantity == 0:
                position.average_entry_price = Decimal("0")
        position.fees_paid += fee
        self.store.save_position(position)
        return realized

    @atomic
    def settle_funding(self, event_id: str, rate: Decimal, mark_price: Decimal) -> None:
        if self.store.get_json(f"funding:{event_id}"):
            return
        position = self.position()
        payment = position.quantity * mark_price * rate
        position.funding_paid += payment
        self.store.save_position(position)
        self.store.set_json(f"funding:{event_id}", str(payment))
        self.store.add_event("INFO", "paper funding settlement", {"rate": str(rate), "payment": str(payment)})

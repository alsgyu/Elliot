from __future__ import annotations

from decimal import Decimal

from .config import GridConfig
from .decimal_utils import quantize_price, quantize_quantity
from .models import MarketRules, Order, OrderRequest, OrderStatus, OrderType, Side


class GridPlanner:
    def __init__(self, grid: GridConfig, rules: MarketRules):
        self.grid = grid
        self.rules = rules
        interval = (grid.upper_price - grid.lower_price) / Decimal(grid.grid_count - 1)
        self.levels = [
            quantize_price(
                grid.lower_price * (grid.upper_price / grid.lower_price) ** (Decimal(index) / (grid.grid_count - 1))
                if grid.grid_spacing == "geometric" else grid.lower_price + interval * Decimal(index), rules.tick_size)
            for index in range(grid.grid_count)
        ]

    def entry_levels(self, current_price: Decimal, open_orders: list[Order]) -> list[tuple[int, Decimal]]:
        if current_price < self.grid.lower_price or current_price > self.grid.upper_price:
            return []
        active_level_ids = {
            order.level_index
            for order in open_orders
            if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
            and order.level_index is not None
            and order.purpose in {"entry", "take_profit"}
        }
        candidates: list[tuple[int, Decimal]] = []
        for index, price in enumerate(self.levels[:-1]):
            if index in active_level_ids:
                continue
            if price < current_price:
                candidates.append((index, price))
        candidates.sort(key=lambda item: abs(current_price - item[1]))
        return candidates[: self.grid.max_open_orders]

    def make_entry_request(self, level_index: int, price: Decimal, nonce: str) -> OrderRequest:
        quantity = quantize_quantity(self.grid.order_quote / price, self.rules.step_size)
        if quantity < self.rules.min_qty:
            raise ValueError("entry quantity below exchange minimum")
        if quantity * price < self.rules.min_notional:
            raise ValueError("entry notional below exchange minimum")
        return OrderRequest(
            client_order_id=f"kgb-entry-{level_index}-{nonce}",
            symbol=self.rules.symbol,
            side=Side.BUY,
            order_type=OrderType.LIMIT_MAKER,
            quantity=quantity,
            price=price,
            reduce_only=False,
            post_only=True,
            level_index=level_index,
            purpose="entry",
        )

    def make_take_profit_request(self, entry_order: Order, fill_quantity: Decimal, nonce: str) -> OrderRequest:
        if entry_order.level_index is None:
            raise ValueError("entry order lacks level index")
        tp_index = min(entry_order.level_index + 1, len(self.levels) - 1)
        tp_price = self.levels[tp_index]
        quantity = quantize_quantity(fill_quantity, self.rules.step_size)
        if quantity <= 0:
            raise ValueError("take-profit quantity rounds to zero")
        return OrderRequest(
            client_order_id=f"kgb-tp-{entry_order.level_index}-{nonce}",
            symbol=self.rules.symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT_MAKER,
            quantity=quantity,
            price=tp_price,
            reduce_only=True,
            post_only=True,
            level_index=entry_order.level_index,
            purpose="take_profit",
        )

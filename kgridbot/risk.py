from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import GridConfig
from .decimal_utils import bps_to_rate
from .models import BotFlags, HaltReason, MarketRules, MarketSnapshot, Order, OrderStatus, PnLSnapshot, Position
from .store import Store

SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(slots=True)
class RiskCheck:
    ok: bool
    reason: str
    details: dict[str, str]


class RiskManager:
    def __init__(self, store: Store, grid: GridConfig, rules: MarketRules):
        self.store = store
        self.grid = grid
        self.rules = rules
        self.clock = lambda: datetime.now(SEOUL)

    def validate_grid_costs(self) -> RiskCheck:
        from .strategy import GridPlanner
        levels = GridPlanner(self.grid, self.rules).levels
        interval_rate = min((b - a) / a for a, b in zip(levels, levels[1:]))
        round_trip_fee = self.rules.maker_fee_rate * Decimal("2")
        friction = round_trip_fee + bps_to_rate(self.grid.spread_bps + self.grid.expected_funding_bps + self.grid.slippage_bps)
        if interval_rate <= friction:
            return RiskCheck(
                ok=False,
                reason="grid interval is too small after fees/spread/funding",
                details={"interval_rate": str(interval_rate), "estimated_friction": str(friction)},
            )
        return RiskCheck(ok=True, reason="ok", details={"interval_rate": str(interval_rate), "estimated_friction": str(friction)})

    def exposure_check(self, position: Position, open_orders: list[Order], mark_price: Decimal) -> RiskCheck:
        entry_notional = Decimal("0")
        for order in open_orders:
            if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED} and order.purpose == "entry":
                entry_notional += order.remaining_quantity * max(order.price or mark_price, mark_price)
        position_notional = abs(position.quantity) * mark_price
        total_if_filled = position_notional + entry_notional
        required_margin = total_if_filled / Decimal(self.grid.leverage)
        equity = max(Decimal("0"), self.grid.allocated_margin + self.pnl_snapshot(position, mark_price).net_pnl)
        strict_notional_cap = min(
            self.grid.max_position_notional,
            equity * Decimal(self.grid.leverage),
        )
        if entry_notional > self.grid.max_open_order_notional:
            return RiskCheck(False, "open entry order notional limit exceeded", {"entry_notional": str(entry_notional)})
        if total_if_filled > strict_notional_cap:
            return RiskCheck(
                False,
                "position notional limit exceeded if all entries fill",
                {"total_if_filled": str(total_if_filled), "cap": str(strict_notional_cap)},
            )
        margin_buffer = equity * Decimal("0.90")
        if required_margin > margin_buffer:
            return RiskCheck(
                False,
                "allocated margin buffer would be exceeded",
                {"required_margin": str(required_margin), "buffer": str(margin_buffer)},
            )
        return RiskCheck(
            True,
            "ok",
            {
                "entry_notional": str(entry_notional),
                "position_notional": str(position_notional),
                "required_margin": str(required_margin),
                "equity": str(equity),
                "cap": str(strict_notional_cap),
                "total_if_filled": str(total_if_filled),
            },
        )

    def pnl_snapshot(self, position: Position, mark_price: Decimal) -> PnLSnapshot:
        unrealized = position.unrealized_pnl(mark_price)
        realized = position.realized_pnl
        fees = position.fees_paid
        funding = position.funding_paid
        net = realized + unrealized - fees - funding
        start_key = self._daily_start_key()
        baseline = Decimal(self.store.get_json(start_key) or "0")
        daily = net - baseline
        return PnLSnapshot(
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            fees_paid=fees,
            funding_paid=funding,
            net_pnl=net,
            daily_net_pnl=daily,
        )

    def ensure_daily_baseline(self, position: Position, mark_price: Decimal) -> None:
        key = self._daily_start_key()
        if self.store.get_json(key) is None:
            snapshot = self.pnl_snapshot(position, mark_price)
            self.store.set_json(key, str(snapshot.net_pnl))

    def check_loss_limits(self, pnl: PnLSnapshot) -> BotFlags | None:
        if pnl.daily_net_pnl <= -abs(self.grid.daily_loss_limit):
            return BotFlags(HaltReason.DAILY_LOSS_LIMIT, can_enter=False, manual_resume_required=True)
        if pnl.net_pnl <= -abs(self.grid.total_loss_limit):
            return BotFlags(HaltReason.TOTAL_LOSS_LIMIT, can_enter=False, manual_resume_required=True)
        return None

    def check_market_freshness(self, snapshot: MarketSnapshot, now: float) -> BotFlags | None:
        if now - snapshot.timestamp > self.grid.stale_price_seconds:
            return BotFlags(HaltReason.STALE_MARKET_DATA, can_enter=False, manual_resume_required=True)
        return None

    def _daily_start_key(self) -> str:
        today = self.clock().strftime("%Y-%m-%d")
        return f"daily_pnl_baseline:{today}"

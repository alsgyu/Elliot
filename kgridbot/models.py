from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from time import time
from typing import Any


class Mode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT_MAKER = "LIMIT_MAKER"
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class HaltReason(StrEnum):
    NONE = "NONE"
    USER_PAUSED = "USER_PAUSED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    TOTAL_LOSS_LIMIT = "TOTAL_LOSS_LIMIT"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    PROTECTION_FAILED = "PROTECTION_FAILED"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    EXCHANGE_MISMATCH = "EXCHANGE_MISMATCH"
    UNKNOWN_ORDER_STATE = "UNKNOWN_ORDER_STATE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(slots=True)
class MarketRules:
    symbol: str
    hb_trading_pair: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    trigger_protect: Decimal = Decimal("0")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
        return data


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    mark_price: Decimal
    funding_rate: Decimal
    next_funding_time_ms: int
    timestamp: float = field(default_factory=time)
    source: str = "paper"


@dataclass(slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    post_only: bool = True
    close_position: bool = False
    level_index: int | None = None
    purpose: str = "entry"


@dataclass(slots=True)
class Order:
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None
    status: OrderStatus
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal = Decimal("0")
    stop_price: Decimal | None = None
    reduce_only: bool = False
    post_only: bool = True
    close_position: bool = False
    level_index: int | None = None
    purpose: str = "entry"
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)

    @property
    def remaining_quantity(self) -> Decimal:
        remaining = self.quantity - self.filled_quantity
        return remaining if remaining > 0 else Decimal("0")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, StrEnum):
                data[key] = value.value
        return data


@dataclass(slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    realized_pnl: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    timestamp: float = field(default_factory=time)
    maker: bool = True

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, StrEnum):
                data[key] = value.value
        return data


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal = Decimal("0")
    average_entry_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        if self.quantity == 0:
            return Decimal("0")
        return (price - self.average_entry_price) * self.quantity

    def to_json(self, mark_price: Decimal | None = None) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
        if mark_price is not None:
            data["unrealized_pnl"] = str(self.unrealized_pnl(mark_price))
            data["notional"] = str(abs(self.quantity) * mark_price)
        return data


@dataclass(slots=True)
class PnLSnapshot:
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    net_pnl: Decimal
    daily_net_pnl: Decimal

    def to_json(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


@dataclass(slots=True)
class BotFlags:
    halt_reason: HaltReason = HaltReason.NONE
    can_enter: bool = True
    needs_reconciliation: bool = False
    manual_resume_required: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "halt_reason": self.halt_reason.value,
            "can_enter": self.can_enter,
            "needs_reconciliation": self.needs_reconciliation,
            "manual_resume_required": self.manual_resume_required,
        }

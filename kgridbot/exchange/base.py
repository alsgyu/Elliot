from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Fill, MarketSnapshot, Order, OrderRequest, Position


class ExchangeError(RuntimeError):
    pass


class ProtectionOrderError(ExchangeError):
    pass


class ExchangeAdapter(ABC):
    @abstractmethod
    def market_snapshot(self) -> MarketSnapshot:
        raise NotImplementedError

    @abstractmethod
    def place_order(self, request: OrderRequest) -> Order:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> Order:
        raise NotImplementedError

    @abstractmethod
    def open_orders(self) -> list[Order]:
        raise NotImplementedError

    @abstractmethod
    def position(self) -> Position:
        raise NotImplementedError

    @abstractmethod
    def recent_fills(self) -> list[Fill]:
        raise NotImplementedError

    @abstractmethod
    def place_or_replace_protection_stop(self, stop_price) -> Order | None:
        raise NotImplementedError

    @abstractmethod
    def reconcile(self) -> None:
        raise NotImplementedError

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import time
from dataclasses import asdict
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import Fill, MarketRules, MarketSnapshot, Order, OrderRequest, OrderStatus, OrderType, Position, Side
from ..store import Store
from .base import ExchangeAdapter, ExchangeError, ProtectionOrderError


class BinanceFuturesAdapter(ExchangeAdapter):
    def __init__(
        self,
        store: Store,
        rules: MarketRules,
        base_url: str,
        api_key_env: str,
        api_secret_env: str,
        allow_trading: bool,
    ):
        self.store = store
        self.rules = rules
        self.base_url = base_url.rstrip("/")
        self.api_key = os.environ.get(api_key_env, "")
        self.api_secret = os.environ.get(api_secret_env, "")
        self.allow_trading = allow_trading
        self._last_income_sync = 0.0
        if self.store.get_json("exchange_start_ms") is None:
            self.store.set_json("exchange_start_ms", int(time.time() * 1000))

    def market_snapshot(self) -> MarketSnapshot:
        ticker = self._public("GET", "/fapi/v1/ticker/bookTicker", {"symbol": self.rules.symbol})
        premium = self._public("GET", "/fapi/v1/premiumIndex", {"symbol": self.rules.symbol})
        bid = Decimal(ticker["bidPrice"])
        ask = Decimal(ticker["askPrice"])
        mark = Decimal(premium["markPrice"])
        return MarketSnapshot(
            symbol=self.rules.symbol,
            price=(bid + ask) / Decimal("2"),
            bid=bid,
            ask=ask,
            mark_price=mark,
            funding_rate=Decimal(premium["lastFundingRate"]),
            next_funding_time_ms=int(premium["nextFundingTime"]),
            timestamp=min(int(premium["time"]), int(ticker.get("time", premium["time"]))) / 1000,
            source="binance_public",
        )

    def place_order(self, request: OrderRequest) -> Order:
        if not self.allow_trading:
            raise ExchangeError("trading disabled by configuration")
        remembered = self.store.load_order(request.client_order_id)
        if remembered:
            existing = self.query_order(request.client_order_id)
            if existing is not None:
                return existing
            raise ExchangeError("previous submission cannot be confirmed; reconcile before retry")
        if request.symbol != self.rules.symbol or len(request.client_order_id) > 36:
            raise ExchangeError("invalid symbol or client order identifier")
        intent = Order(**asdict(request), exchange_order_id=None, status=OrderStatus.UNKNOWN)
        self.store.save_order(intent)
        self.store.remember_idempotency(request.client_order_id, {"client_order_id": request.client_order_id})
        params: dict[str, object] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": _binance_type(request.order_type),
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if request.order_type == OrderType.LIMIT_MAKER:
            params.update(
                {
                    "timeInForce": "GTX",
                    "quantity": str(request.quantity),
                    "price": str(request.price),
                }
            )
        elif request.order_type == OrderType.MARKET:
            params["quantity"] = str(request.quantity)
        elif request.order_type == OrderType.STOP_MARKET:
            params.pop("newClientOrderId")
            params.pop("newOrderRespType")
            params["clientAlgoId"] = request.client_order_id
            params["algoType"] = "CONDITIONAL"
            params["triggerPrice"] = str(request.stop_price)
            params["workingType"] = "MARK_PRICE"
            params["priceProtect"] = "false"
            if request.close_position:
                params["closePosition"] = "true"
            else:
                params["quantity"] = str(request.quantity)
        if request.reduce_only and not request.close_position:
            params["reduceOnly"] = "true"
        path = "/fapi/v1/algoOrder" if request.order_type == OrderType.STOP_MARKET else "/fapi/v1/order"
        try:
            raw = self._signed("POST", path, params)
        except ExchangeError:
            confirmed = self.query_order(request.client_order_id)
            if confirmed is None:
                raise ExchangeError("order submission outcome unknown; no automatic resubmission")
            return confirmed
        order = self._remember(raw, request)
        self.store.save_order(order)
        self.store.remember_idempotency(request.client_order_id, {"client_order_id": request.client_order_id})
        return order

    def cancel_order(self, client_order_id: str) -> Order:
        existing = self.store.load_order(client_order_id)
        if existing is None:
            raise ExchangeError("refusing to cancel an unowned order")
        if existing.order_type == OrderType.STOP_MARKET:
            self._signed("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id})
            confirmed = self.query_order(client_order_id)
            if confirmed is None or confirmed.status not in {OrderStatus.CANCELED, OrderStatus.FILLED}:
                raise ExchangeError("conditional cancellation is not confirmed")
            return confirmed
        raw = self._signed(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": self.rules.symbol, "origClientOrderId": client_order_id},
        )
        order = self._remember(raw, existing)
        self.store.save_order(order)
        return order

    def query_order(self, client_order_id: str) -> Order | None:
        previous = self.store.load_order(client_order_id)
        algo = previous is not None and previous.order_type == OrderType.STOP_MARKET
        try:
            raw = self._signed(
                "GET",
                "/fapi/v1/algoOrder" if algo else "/fapi/v1/order",
                {"clientAlgoId": client_order_id} if algo else {"symbol": self.rules.symbol, "origClientOrderId": client_order_id},
            )
        except BinanceAPIError as exc:
            if exc.code == -2013:
                return None
            raise
        order = self._remember(raw, previous)
        self.store.save_order(order)
        return order

    def open_orders(self) -> list[Order]:
        raws = self._signed("GET", "/fapi/v1/openOrders", {"symbol": self.rules.symbol})
        algos = self._signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": self.rules.symbol})
        orders = []
        for raw in list(raws) + list(algos):
            client_id = raw.get("clientAlgoId", raw.get("clientOrderId"))
            previous = self.store.load_order(str(client_id))
            if previous is None:
                raise ExchangeError("unowned open order on this symbol; dedicated account required")
            orders.append(self._remember(raw, previous))
        ids = {order.client_order_id for order in orders}
        for previous in self.store.list_orders([OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN]):
            if previous.client_order_id not in ids:
                confirmed = self.query_order(previous.client_order_id)
                if confirmed is None or confirmed.status == OrderStatus.UNKNOWN:
                    raise ExchangeError("order state is unknown during reconciliation")
        return orders

    def _remember(self, raw, previous=None):
        order = _order_from_binance(raw, previous)
        self.store.save_order(order)
        if raw.get("actualOrderId") and str(raw["actualOrderId"]) != "0":
            self.store.set_json(f"exchange_order:{raw['actualOrderId']}", order.client_order_id)
        if order.exchange_order_id and order.order_type != OrderType.STOP_MARKET:
            self.store.set_json(f"exchange_order:{order.exchange_order_id}", order.client_order_id)
        return order

    def position(self) -> Position:
        raws = self._signed("GET", "/fapi/v3/positionRisk", {"symbol": self.rules.symbol})
        raw = raws[0] if raws else {}
        position = Position(
            symbol=self.rules.symbol,
            quantity=Decimal(raw.get("positionAmt", "0")),
            average_entry_price=Decimal(raw.get("entryPrice", "0")),
        )
        persisted = self.store.load_position(self.rules.symbol)
        position.realized_pnl = persisted.realized_pnl
        position.fees_paid = persisted.fees_paid
        position.funding_paid = persisted.funding_paid
        self.store.save_position(position)
        return position

    def recent_fills(self) -> list[Fill]:
        cursor = self.store.get_json("trade_cursor")
        params = {"symbol": self.rules.symbol, "limit": 1000}
        if cursor is not None:
            params["fromId"] = int(cursor) + 1
        raws = self._signed("GET", "/fapi/v1/userTrades", params)
        fills: list[Fill] = []
        for raw in raws:
            client_id = self.store.get_json(f"exchange_order:{raw['orderId']}")
            if client_id is None:
                self.store.set_json("trade_cursor", str(raw["id"]))
                continue
            fill = Fill(
                fill_id=str(raw["id"]),
                client_order_id=client_id,
                symbol=raw["symbol"],
                side=Side(raw["side"]),
                quantity=Decimal(raw["qty"]),
                price=Decimal(raw["price"]),
                fee=Decimal(raw["commission"]),
                fee_asset=raw["commissionAsset"],
                realized_pnl=Decimal(raw.get("realizedPnl", "0")),
                maker=bool(raw.get("maker", False)),
                timestamp=int(raw["time"]) / 1000,
            )
            if fill.fee_asset != self.rules.margin_asset:
                raise ExchangeError("fee asset conversion is not verified")
            with self.store.transaction():
                if self.store.save_fill(fill):
                    position = self.store.load_position(self.rules.symbol)
                    position.realized_pnl += fill.realized_pnl
                    position.fees_paid += fill.fee
                    self.store.save_position(position)
                    fills.append(fill)
                self.store.set_json("trade_cursor", str(raw["id"]))
        if len(raws) == 1000:
            return self.recent_fills()
        self._sync_funding()
        return self.store.list_fills(100000)

    def _sync_funding(self):
        if time.monotonic() - self._last_income_sync < 30:
            return
        start = self.store.get_json("funding_cursor_ms") or self.store.get_json("exchange_start_ms")
        rows = self._signed("GET", "/fapi/v1/income", {"symbol": self.rules.symbol,
                            "incomeType": "FUNDING_FEE", "startTime": start, "limit": 1000})
        for raw in rows:
            key = f"funding_income:{raw['tranId']}"
            if raw["asset"] != self.rules.margin_asset:
                raise ExchangeError("funding asset conversion is not verified")
            with self.store.transaction():
                if self.store.get_json(key) is None:
                    position = self.store.load_position(self.rules.symbol)
                    position.funding_paid -= Decimal(raw["income"])
                    self.store.save_position(position)
                    self.store.set_json(key, True)
                self.store.set_json("funding_cursor_ms", int(raw["time"]))
        if len(rows) >= 1000:
            raise ExchangeError("funding history requires manual pagination reconciliation")
        self._last_income_sync = time.monotonic()

    def place_or_replace_protection_stop(self, stop_price) -> Order | None:
        position = self.position()
        previous = [order for order in self.open_orders() if order.purpose == "protection_stop"]
        if position.quantity <= 0:
            return None
        req = OrderRequest(
            client_order_id=f"kgb-protect-{int(time.time() * 1000)}",
            symbol=self.rules.symbol,
            side=Side.SELL,
            order_type=OrderType.STOP_MARKET,
            quantity=abs(position.quantity),
            stop_price=Decimal(stop_price),
            reduce_only=True,
            post_only=False,
            close_position=True,
            purpose="protection_stop",
        )
        try:
            placed = self.place_order(req)
            confirmed = self.query_order(placed.client_order_id)
            if confirmed is None or confirmed.status != OrderStatus.NEW:
                raise ProtectionOrderError("conditional stop activation not confirmed")
            for old in previous:
                self.cancel_order(old.client_order_id)
            return confirmed
        except ExchangeError as exc:
            raise ProtectionOrderError(str(exc)) from exc

    def reconcile(self) -> None:
        open_orders = self.open_orders()
        position = self.position()
        self.recent_fills()
        owned_qty = sum((f.quantity if f.side == Side.BUY else -f.quantity for f in self.store.list_fills(100000)), Decimal("0"))
        if position.quantity != owned_qty or position.quantity < 0:
            raise ExchangeError("position does not match bot-owned fills; manual reconciliation required")
        if position.quantity == 0:
            for order in open_orders:
                if order.client_order_id.startswith("kgb-tp-") or order.client_order_id.startswith("kgb-protect-"):
                    self.cancel_order(order.client_order_id)
        self.store.add_event(
            "INFO",
            "binance reconciliation completed",
            {"open_orders": len(open_orders), "position_qty": str(position.quantity)},
        )

    def verify_account_prerequisites(self, leverage: int) -> list[str]:
        errors: list[str] = []
        if not 1 <= leverage <= 5:
            return ["leverage must be between 1 and 5"]
        if not self.api_key or not self.api_secret:
            errors.append("missing Binance API credentials")
            return errors
        remote_time = self._public("GET", "/fapi/v1/time", {})
        if abs(int(remote_time["serverTime"]) - int(time.time() * 1000)) > 1000:
            errors.append("clock differs from exchange by more than one second")
        account_config = self._signed("GET", "/fapi/v1/positionSide/dual", {})
        if str(account_config.get("dualSidePosition", "")).lower() != "false":
            errors.append("account must be in One-way mode")
        symbol_configs = self._signed("GET", "/fapi/v1/symbolConfig", {"symbol": self.rules.symbol})
        if symbol_configs:
            cfg = symbol_configs[0]
            if cfg.get("marginType") != "ISOLATED":
                errors.append("symbol margin type must be ISOLATED")
            if int(cfg.get("leverage", 0)) != leverage:
                errors.append("exchange leverage must equal configured leverage")
        else:
            errors.append("symbol configuration is unavailable")
        commission = self._signed("GET", "/fapi/v1/commissionRate", {"symbol": self.rules.symbol})
        self.store.set_json("last_commission_rate", commission)
        self.rules.maker_fee_rate = Decimal(commission["makerCommissionRate"])
        self.rules.taker_fee_rate = Decimal(commission["takerCommissionRate"])
        account = self._signed("GET", "/fapi/v3/account", {})
        if not account.get("canTrade", False):
            errors.append("account trading permission is unavailable")
        if not any(asset["asset"] == self.rules.margin_asset and Decimal(asset.get("availableBalance", "0")) > 0 for asset in account.get("assets", [])):
            errors.append("available collateral balance is zero or unavailable")
        info = self._public("GET", "/fapi/v1/exchangeInfo", {})
        symbol = next((s for s in info["symbols"] if s["symbol"] == self.rules.symbol), None)
        if symbol is None or symbol.get("status") != "TRADING" or symbol.get("marginAsset") != self.rules.margin_asset:
            errors.append("symbol trading rules could not be verified")
        else:
            filters = {f["filterType"]: f for f in symbol["filters"]}
            self.rules.tick_size = Decimal(filters["PRICE_FILTER"]["tickSize"])
            self.rules.step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
            self.rules.min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
            self.rules.min_notional = Decimal(filters["MIN_NOTIONAL"]["notional"])
        return errors

    def _public(self, method: str, path: str, params: dict[str, object]) -> object:
        query = urlencode(params)
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        return _request_json(method, url, headers={})

    def _signed(self, method: str, path: str, params: dict[str, object]) -> object:
        if method != "GET" and not self.allow_trading:
            raise ExchangeError("trading disabled by configuration")
        if not self.api_key or not self.api_secret:
            raise ExchangeError("missing Binance API credentials")
        payload = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        payload.setdefault("recvWindow", 5000)
        query = urlencode(payload)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = f"{query}&signature={signature}"
        url = f"{self.base_url}{path}"
        headers = {"X-MBX-APIKEY": self.api_key}
        if method in {"GET", "DELETE"}:
            return _request_json(method, f"{url}?{query}", headers=headers)
        return _request_json(method, url, headers=headers, body=query.encode())


def _request_json(method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> object:
    req = Request(url, data=body, method=method, headers=headers)
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with urlopen(req, timeout=10, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read())
        except ValueError:
            payload = {}
        raise BinanceAPIError(payload.get("code"), f"Binance HTTP {exc.code}: {payload.get('msg', 'request failed')}") from exc
    except URLError as exc:
        raise ExchangeError(f"binance network error: {exc.reason}") from exc


def _binance_type(order_type: OrderType) -> str:
    if order_type == OrderType.LIMIT_MAKER:
        return "LIMIT"
    return order_type.value


def _order_from_binance(raw: dict[str, object], request: OrderRequest | None) -> Order:
    raw = dict(raw)
    if "algoId" in raw or "clientAlgoId" in raw:
        raw.update(clientOrderId=raw.get("clientAlgoId"), orderId=raw.get("algoId"),
                   type=raw.get("orderType", raw.get("type", "STOP_MARKET")),
                   status=raw.get("algoStatus", "UNKNOWN"), stopPrice=raw.get("triggerPrice"),
                   origQty=raw.get("quantity") or (request.quantity if request else "0"))
    side = Side(str(raw.get("side", request.side.value if request else "BUY")))
    order_type = OrderType.LIMIT_MAKER if raw.get("type") == "LIMIT" else OrderType(str(raw.get("type", "MARKET")))
    status_text = str(raw.get("status", "UNKNOWN"))
    status = OrderStatus({"EXPIRED": "CANCELED", "EXPIRED_IN_MATCH": "CANCELED", "FINISHED": "FILLED", "TRIGGERED": "PARTIALLY_FILLED"}.get(status_text, status_text))
    price_raw = raw.get("price") or (str(request.price) if request and request.price is not None else None)
    stop_raw = raw.get("stopPrice") or (str(request.stop_price) if request and request.stop_price is not None else None)
    return Order(
        client_order_id=str(raw.get("clientOrderId") or raw.get("origClientOrderId") or (request.client_order_id if request else "")),
        exchange_order_id=str(raw.get("orderId")) if raw.get("orderId") is not None else None,
        symbol=str(raw.get("symbol", request.symbol if request else "")),
        side=side,
        order_type=order_type,
        quantity=Decimal(str(raw.get("origQty", request.quantity if request else "0"))),
        price=Decimal(price_raw) if price_raw is not None and Decimal(str(price_raw)) != 0 else None,
        status=status,
        filled_quantity=Decimal(str(raw.get("executedQty", "0"))),
        average_fill_price=Decimal(str(raw.get("avgPrice", "0"))),
        stop_price=Decimal(stop_raw) if stop_raw is not None and Decimal(str(stop_raw)) != 0 else None,
        reduce_only=str(raw.get("reduceOnly", request.reduce_only if request else False)).lower() == "true",
        post_only=True,
        close_position=str(raw.get("closePosition", request.close_position if request else False)).lower() == "true",
        level_index=request.level_index if request else None,
        purpose=request.purpose if request else "unknown",
        updated_at=time.time(),
    )


class BinanceAPIError(ExchangeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

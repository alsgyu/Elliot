from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import time
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from kgridbot.exchange.binance import BinanceAPIError, _request_json
from .config import ENDPOINTS, validated_leverage
from kgridbot.process_lock import ProcessLock


D = lambda value: Decimal(str(value))


def rounded(value, unit, up=False):
    return (D(value) / D(unit)).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * D(unit)


def payoff(price, stop, target, direction, fee, slip):
    entry = D(price) * (1 + direction * D(slip))
    stop_fill = D(stop) * (1 - direction * D(slip))
    target_fill = D(target) * (1 - direction * D(slip))
    loss = (entry - stop_fill) * direction + (entry + stop_fill) * D(fee)
    reward = (target_fill - entry) * direction - (entry + target_fill) * D(fee)
    return loss, reward


def size_trade(decision, quote, settings, equity, taker_bps=None, risk_cap=None):
    leverage = validated_leverage(settings.leverage)
    setup = decision["selection"]
    direction = 1 if decision["action"] == "LONG" else -1
    price = D(quote["ask"] if direction > 0 else quote["bid"])
    if (D(quote["ask"]) - D(quote["bid"])) / price * 10000 > D(settings.max_spread_bps):
        raise ValueError("호가 스프레드가 허용 범위를 넘었습니다.")
    rules = quote["rules"]
    stop = rounded(setup["stop"], rules["tick"], up=direction < 0)
    target = rounded(setup["target"], rules["tick"], up=direction < 0)
    fee = max(D(settings.fee_bps), D(taker_bps or 0)) / 10000
    slip = D(settings.slippage_bps) / 10000
    distance = (price - stop) * direction
    loss, reward = payoff(price, stop, target, direction, fee, slip)
    if distance <= 0 or loss <= 0 or reward / loss < D(settings.min_rr):
        raise ValueError("현재 호가 기준 손익비가 기준 미달입니다. 추격 진입하지 않습니다.")
    multiplier = D(decision.get("risk_multiplier", 1))
    if not multiplier.is_finite() or not 0 < multiplier <= 1:
        raise ValueError("위험 배수는 0 초과 1 이하여야 합니다.")
    if settings.strategy_mode in {"multi", "active"}:
        if quote.get("time", 0) >= decision.get("signal_expires_at", 0):
            raise ValueError("돌파 신호 유효 시간이 지나 관망합니다.")
        reference = D(decision["trigger"]["bar_close"])
        initial_distance = (reference - stop) * direction
        if initial_distance <= 0 or (price - reference) * direction > initial_distance * D(settings.max_entry_drift_r):
            raise ValueError("신호 종가 대비 추격 허용 거리를 넘었습니다.")
        if (loss - distance) / loss > D(settings.max_cost_fraction):
            raise ValueError("손절 위험 대비 거래 비용 비중이 너무 큽니다.")
        if settings.strategy_mode == "active" and distance / price * 100 > D(settings.max_stop_percent):
            raise ValueError("현재 호가 기준 구조 손절 거리가 단기 전략 상한을 넘었습니다. 손절을 임의로 당기지 않습니다.")
    risk_budget = D(equity) * D(settings.risk_percent) / 100 * multiplier
    if risk_cap is not None:
        risk_budget = min(risk_budget, max(D(0), D(risk_cap)))
    qty = min(risk_budget / loss,
              min(D(settings.max_notional), D(equity) * leverage * D("0.9")) / (price * (1 + slip)))
    if rules.get("max_qty") is not None:
        qty = min(qty, D(rules["max_qty"]))
    qty = rounded(qty, rules["step"])
    if qty <= 0 or qty < D(rules["min_qty"]) or qty * min(price, D(quote["price"])) < D(rules["min_notional"]):
        raise ValueError("위험 예산으로 산출한 수량이 거래소 최소 주문보다 작아 관망합니다.")
    return {"direction": direction, "quantity": str(qty), "entry_reference": str(price), "stop": str(stop),
            "target": str(target), "risk_budget": str(risk_budget), "risk_at_stop": str(qty * loss),
            "rr": str(reward / loss), "fee_rate": str(fee), "slippage_rate": str(slip), "leverage": leverage,
            "estimated_initial_margin": str(qty * price * (1 + direction * slip) / leverage),
            "risk_multiplier": str(multiplier)}


class RemoteBroker:
    def __init__(self, mode, credentials, journal):
        if mode not in {"testnet", "live"}:
            raise ValueError("remote mode required")
        self.mode, self.base, self.journal = mode, ENDPOINTS[mode], journal
        self.key, self.secret = credentials
        self.armed = False
        self.taker_bps = 5
        self.account_lock = None

    def arm(self):
        if not self.key or not self.secret:
            raise ValueError("API 키가 등록되지 않았습니다.")
        if self.account_lock is None:
            identity = hashlib.sha256((self.base + self.key + "BTCUSDT").encode()).hexdigest()
            self.account_lock = ProcessLock(Path(tempfile.gettempdir()) / f"elliott-{identity}.lock")
        self.armed = True

    def close(self):
        if self.account_lock:
            self.account_lock.close()
            self.account_lock = None
        self.armed = False

    def request(self, method, path, params=None, purpose=None):
        safe = (method == "POST" and path == "/fapi/v1/order/test")
        setup = purpose == "configuration" and path in {"/fapi/v1/marginType", "/fapi/v1/leverage"}
        if method != "GET" and not (self.armed or safe or setup):
            raise ValueError("명시적인 시작 전 거래 요청은 차단됩니다.")
        if not self.key or not self.secret:
            raise ValueError("거래소 연결 화면에 해당 모드의 API 키를 등록하세요.")
        values = dict(params or {}, timestamp=int(time.time() * 1000), recvWindow=5000)
        query = urlencode(values)
        query += "&signature=" + hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = self.base + path
        return _request_json(method, url + ("?" + query if method in {"GET", "DELETE"} else ""),
                             {"X-MBX-APIKEY": self.key}, None if method in {"GET", "DELETE"} else query.encode())

    def position(self):
        rows = self.request("GET", "/fapi/v3/positionRisk", {"symbol": "BTCUSDT"})
        if len(rows) > 1 or any(r.get("positionSide", "BOTH") != "BOTH" for r in rows):
            raise ValueError("One-way 모드가 아닙니다.")
        return rows[0] if rows else {"positionAmt": "0", "entryPrice": "0"}

    def owned_orders(self):
        return self.journal.get("remote_orders", {})

    def preflight(self, settings, state, quote):
        checks = []
        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
        from .market import public
        server = int(public(self.base, "/fapi/v1/time")["serverTime"])
        add("시간 동기화", abs(server - int(time.time() * 1000)) < 1000, "거래소와 1초 미만")
        mode = self.request("GET", "/fapi/v1/positionSide/dual")
        add("One-way", mode.get("dualSidePosition") is False, "Hedge 모드는 Binance 화면에서 변경")
        multi = self.request("GET", "/fapi/v1/multiAssetsMargin")
        multi_enabled = multi.get("multiAssetsMargin")
        asset_mode = "Multi-Assets" if multi_enabled is True else "Single-Asset" if multi_enabled is False else "미확인"
        add("단일 담보", multi_enabled is False, f"현재 {asset_mode} / 필요 Single-Asset")
        cfgs = self.request("GET", "/fapi/v1/symbolConfig", {"symbol": "BTCUSDT"})
        cfg = cfgs[0] if cfgs else {}
        add("격리 증거금", cfg.get("marginType") == "ISOLATED", f"BTCUSDT 현재 {cfg.get('marginType', '미확인')} / 필요 ISOLATED")
        add("레버리지", int(cfg.get("leverage", 0)) == settings.leverage, f"요청 {settings.leverage}배 / 거래소 {cfg.get('leverage', '?')}배")
        # V3 account reports balances/positions; permission flags are in accountConfig.
        account_config = self.request("GET", "/fapi/v1/accountConfig")
        can_trade = account_config.get("canTrade")
        trade_status = "true" if can_trade is True else "false" if can_trade is False else "미확인(필드 누락/형식 오류)"
        add("거래 권한", can_trade is True, f"선물 계정 canTrade={trade_status} · accountConfig 조회 · API 주문 권한은 별도 검사")
        account = self.request("GET", "/fapi/v3/account")
        balance = next((D(a["availableBalance"]) for a in account.get("assets", []) if a["asset"] == "USDT"), D(0))
        add("가용 담보", balance >= (D(settings.capital) if not state.get("position") else 0), f"가용 {balance} USDT / 배정 {settings.capital} USDT")
        rows = self.request("GET", "/fapi/v3/positionRisk")
        existing = state.get("position")
        expected = D(existing["quantity"]) * existing["direction"] if existing else D(0)
        btc = sum((D(r["positionAmt"]) for r in rows if r["symbol"] == "BTCUSDT"), D(0))
        add("포지션 소유권", btc == expected and all(D(r["positionAmt"]) == 0 for r in rows if r["symbol"] != "BTCUSDT"), "수동 포지션이나 다른 종목과 혼용 불가")
        opened = self.request("GET", "/fapi/v1/openOrders") + self.request("GET", "/fapi/v1/openAlgoOrders")
        owned = self.owned_orders()
        add("주문 소유권", all(str(o.get("clientOrderId", o.get("clientAlgoId", ""))) in owned for o in opened), "전용 선물 계정, 외부 주문 없음")
        rate = self.request("GET", "/fapi/v1/commissionRate", {"symbol": "BTCUSDT"})
        self.taker_bps = float(D(rate["takerCommissionRate"]) * 10000)
        add("계정 수수료", 0 <= self.taker_bps < 100, f"Taker {self.taker_bps} bps")
        add("미결 주문 의도", not state.get("pending"), "결과 불명 주문은 먼저 복구해야 함")
        if all(c["ok"] for c in checks):
            if not quote:
                add("주문 권한 검사", False, "최신 시세 수신 전에는 주문 권한 검사를 완료할 수 없습니다.")
            else:
                try:
                    qty = max(D(quote["rules"]["min_qty"]), rounded(D(quote["rules"]["min_notional"]) / D(quote["price"]), quote["rules"]["step"], up=True))
                    self.request("POST", "/fapi/v1/order/test", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": str(qty)})
                except Exception as exc:
                    detail = f"POST /fapi/v1/order/test 실패: {exc}"
                    if isinstance(exc, BinanceAPIError) and exc.code == -2015:
                        detail += " · Binance API 관리에서 Futures 권한과 이 PC 공인 IPv4 허용 목록을 확인하세요."
                    add("주문 권한 검사", False, detail)
                else:
                    add("주문 권한 검사", True, "/order/test 통과: 매칭 엔진에 주문을 보내지 않음")
        return {"ok": all(c["ok"] for c in checks), "checks": checks, "checked_at": time.time(), "available": str(balance)}

    def configure(self, leverage):
        leverage = validated_leverage(leverage)
        self.arm()
        rows = self.request("GET", "/fapi/v3/positionRisk")
        orders = self.request("GET", "/fapi/v1/openOrders") + self.request("GET", "/fapi/v1/openAlgoOrders")
        if orders or any(D(r["positionAmt"]) != 0 for r in rows):
            raise ValueError("계정 설정 변경은 포지션과 주문이 모두 없을 때만 가능합니다.")
        if self.request("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition") is not False:
            raise ValueError("계정 전체에 영향을 주는 One-way 변경은 Binance 웹에서 직접 수행하세요.")
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": "BTCUSDT", "marginType": "ISOLATED"}, purpose="configuration")
        except BinanceAPIError as exc:
            if exc.code != -4046:
                raise
        self.request("POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": int(leverage)}, purpose="configuration")

    def query(self, client):
        item = self.owned_orders().get(client)
        if item is None:
            raise ValueError("봇 소유가 아닌 주문입니다.")
        return self.request("GET", "/fapi/v1/algoOrder" if item["algo"] else "/fapi/v1/order",
                            {"clientAlgoId": client} if item["algo"] else {"symbol": "BTCUSDT", "origClientOrderId": client})

    def place(self, role, params, algo=False):
        if not self.armed:
            raise ValueError("명시적인 시작 전 주문은 차단됩니다.")
        client = "ew-" + role[:5] + "-" + uuid4().hex[:22]
        owned = self.owned_orders()
        owned[client] = {"algo": algo, "role": role, "params": params, "status": "UNKNOWN", "created_at": time.time()}
        self.journal.set("remote_orders", owned)
        data = {"symbol": "BTCUSDT", **params, "clientAlgoId" if algo else "newClientOrderId": client}
        self.journal.record("ORDER_INTENT", {"mode": self.mode, "client": client, "role": role, "params": data})
        try:
            raw = self.request("POST", "/fapi/v1/algoOrder" if algo else "/fapi/v1/order", data)
        except BinanceAPIError as exc:
            if exc.code in {-1013, -1021, -1022, -1100, -1102, -1111, -1116, -1117, -1118, -1121, -2014, -2015, -2019, -2021, -2022, -4061, -4164}:
                owned[client]["status"] = "REJECTED"
                self.journal.set("remote_orders", owned)
                raise ValueError(f"거래소가 주문을 거부했습니다 (코드 {exc.code}). 복구 대조로 미결 의도를 정리하세요.") from exc
            try:
                raw = self.query(client)
            except Exception as query_error:
                self.journal.record("ORDER_UNKNOWN", {"mode": self.mode, "client": client, "reason": "거래소 오류 이후 조회 실패"})
                raise ValueError("주문 결과 불명. 재전송하지 않습니다.") from query_error
        except Exception:
            try:
                raw = self.query(client)
            except Exception as exc:
                self.journal.record("ORDER_UNKNOWN", {"mode": self.mode, "client": client, "reason": "응답 유실 및 조회 실패. 재전송하지 않음"})
                raise ValueError("주문 결과 불명. 보호 주문을 보존하고 신규 진입을 중단합니다.") from exc
        owned[client].update(status=raw.get("algoStatus", raw.get("status", "UNKNOWN")),
                             exchange_id=str(raw.get("algoId", raw.get("orderId", ""))))
        self.journal.set("remote_orders", owned)
        return client, raw

    def cancel_protection(self):
        for client, order in self.owned_orders().items():
            if not order["algo"] or order["status"] == "REJECTED":
                continue
            raw = self.query(client)
            if raw.get("algoStatus") in {"NEW", "TRIGGERED"}:
                self.request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client})
                if self.query(client).get("algoStatus") not in {"CANCELED", "FINISHED", "EXPIRED", "REJECTED"}:
                    raise ValueError("보호 주문 취소 확인 실패")

    def enter(self, plan):
        leverage = validated_leverage(plan["leverage"])
        cfgs = self.request("GET", "/fapi/v1/symbolConfig", {"symbol": "BTCUSDT"})
        cfg = cfgs[0] if cfgs else {}
        if int(cfg.get("leverage", 0)) != leverage or cfg.get("marginType") != "ISOLATED":
            raise ValueError("주문 직전 격리·레버리지 설정 불일치")
        if D(self.position()["positionAmt"]) != 0:
            raise ValueError("신규 진입 전 계좌가 비어 있지 않습니다.")
        close_side = "SELL" if plan["direction"] > 0 else "BUY"
        stop_id, _ = self.place("stop", {"algoType": "CONDITIONAL", "side": close_side, "type": "STOP_MARKET",
                                       "triggerPrice": plan["stop"], "workingType": "MARK_PRICE", "closePosition": "true", "priceProtect": "false"}, True)
        if self.query(stop_id).get("algoStatus") != "NEW":
            raise ValueError("진입 전 거래소 손절 활성화가 확인되지 않았습니다.")
        entry_id, raw = self.place("entry", {"side": "BUY" if plan["direction"] > 0 else "SELL", "type": "MARKET",
                                           "quantity": plan["quantity"], "newOrderRespType": "RESULT"})
        if raw.get("status") != "FILLED" or D(raw.get("executedQty", "0")) != D(plan["quantity"]):
            raise ValueError("시장가 주문 최종 체결 대조 필요. 기존 손절은 유지합니다.")
        position = self.position()
        if D(position["positionAmt"]) != D(plan["quantity"]) * plan["direction"]:
            raise ValueError("주문 체결량과 계정 포지션이 다릅니다.")
        if self.query(stop_id).get("algoStatus") != "NEW":
            self.flatten()
            raise ValueError("진입 중 보호 손절 상태 변경. 노출을 정리하고 복구 대조를 요구합니다.")
        return entry_id, raw, stop_id

    def target(self, plan):
        client, _ = self.place("target", {"algoType": "CONDITIONAL", "side": "SELL" if plan["direction"] > 0 else "BUY",
                                         "type": "TAKE_PROFIT_MARKET", "triggerPrice": plan["target"],
                                         "workingType": "MARK_PRICE", "closePosition": "true", "priceProtect": "false"}, True)
        if self.query(client).get("algoStatus") != "NEW":
            raise ValueError("거래소 익절 활성화 확인 실패. 손절을 유지하고 신규 진입 중단")
        return client

    def target_state(self, position):
        """Reconcile every target intent for this position before any replacement."""
        ids = [cid for cid, item in self.owned_orders().items()
               if item["role"] == "target" and item["created_at"] * 1000 >= position["opened_at"]]
        if position.get("target_id") and position["target_id"] not in ids:
            ids.append(position["target_id"])
        active, finished = [], []
        side = "SELL" if position["direction"] > 0 else "BUY"
        for cid in ids:
            raw = self.query(cid)
            status = raw.get("algoStatus")
            if status in {"NEW", "TRIGGERED"}:
                if (raw.get("symbol") != "BTCUSDT" or raw.get("side") != side
                        or raw.get("orderType") != "TAKE_PROFIT_MARKET"
                        or raw.get("workingType") != "MARK_PRICE"
                        or raw.get("closePosition") not in (True, "true")
                        or D(raw.get("triggerPrice", 0)) != D(position["target"])):
                    raise ValueError("익절 주문 조건이 포지션 계획과 다릅니다. 자동 재주문 금지")
                active.append((cid, status))
            elif status == "FINISHED":
                finished.append((cid, status))
            elif status not in {"CANCELED", "EXPIRED", "REJECTED"}:
                raise ValueError("익절 주문 상태 불명. 중복 방지를 위해 재주문하지 않습니다.")
        if len(active) > 1:
            raise ValueError("활성 익절 주문이 중복되어 수동 대조가 필요합니다.")
        return (active or finished or [(None, "MISSING")])[-1]

    def flatten(self):
        position = self.position()
        amount = D(position["positionAmt"])
        if amount:
            self.place("exit", {"side": "SELL" if amount > 0 else "BUY", "type": "MARKET", "quantity": str(abs(amount)),
                                "reduceOnly": "true", "newOrderRespType": "RESULT"})
        if D(self.position()["positionAmt"]) != 0:
            raise ValueError("잔여 포지션이 있어 보호 주문을 유지합니다.")

    def settlement(self, position):
        owned = self.owned_orders()
        ids = {v.get("exchange_id") for v in owned.values() if not v["algo"]}
        for client, value in owned.items():
            if value["algo"] and value["status"] != "REJECTED":
                raw = self.query(client)
                if raw.get("actualOrderId"):
                    ids.add(str(raw["actualOrderId"]))
        start = int(position.get("history_from", position["opened_at"]))
        rows = self.history("/fapi/v1/userTrades", start)
        trades = [r for r in rows if str(r["orderId"]) in ids]
        entry_side = "BUY" if position["direction"] > 0 else "SELL"
        entries = [r for r in trades if r["side"] == entry_side]
        exits = [r for r in trades if r["side"] != entry_side]
        expected = D(position["quantity"])
        if (sum((D(r["qty"]) for r in entries), D(0)) != expected
                or sum((D(r["qty"]) for r in exits), D(0)) != expected
                or any(r["commissionAsset"] != "USDT" for r in trades)):
            raise ValueError("실제 체결 또는 수수료 대조가 완료되지 않았습니다. BNB 수수료 환산은 지원하지 않습니다.")
        funding = self.history("/fapi/v1/income", min(int(t["time"]) for t in trades), {"incomeType": "FUNDING_FEE"})
        if any(r["asset"] != "USDT" for r in funding):
            raise ValueError("펀딩 정산 대조 실패")
        return {"gross": str(sum((D(r["realizedPnl"]) for r in trades), D(0))),
                "fees": str(sum((D(r["commission"]) for r in trades), D(0))),
                "exit_fees": str(sum((D(r["commission"]) for r in exits), D(0))),
                "funding_income": str(sum((D(r["income"]) for r in funding), D(0))),
                "price": str(sum((D(r["price"]) * D(r["qty"]) for r in exits), D(0)) / expected),
                "trades": trades, "funding": funding}

    def history(self, path, start, extra=None):
        end = int(time.time() * 1000)
        result = []
        while start <= end:
            until = min(end, start + 7 * 86400000 - 1)
            rows = self.request("GET", path, {"symbol": "BTCUSDT", "startTime": start, "endTime": until, "limit": 1000, **(extra or {})})
            if len(rows) >= 1000:
                raise ValueError("계정 기록이 단일 포지션 예상 범위를 초과했습니다. 대조 전 신규 진입 금지")
            result.extend(rows)
            start = until + 1
        return result

    def open_costs(self, position):
        entry = self.query(position["entry_id"])
        rows = self.history("/fapi/v1/userTrades", int(position.get("history_from", position["opened_at"])))
        trades = [r for r in rows if str(r["orderId"]) == str(entry["orderId"])]
        if sum((D(r["qty"]) for r in trades), D(0)) != D(position["quantity"]):
            raise ValueError("진입 체결 기록 동기화 대기. 비용 대조 전 신규 진입 중단")
        if any(r["commissionAsset"] != "USDT" for r in trades):
            raise ValueError("USDT 외 수수료 환산은 지원하지 않습니다.")
        funding = self.history("/fapi/v1/income", min(int(t["time"]) for t in trades), {"incomeType": "FUNDING_FEE"})
        if any(r["asset"] != "USDT" for r in funding):
            raise ValueError("펀딩 통화 불일치")
        return sum((D(r["commission"]) for r in trades), D(0)), -sum((D(r["income"]) for r in funding), D(0))

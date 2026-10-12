from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

from kgridbot.process_lock import ProcessLock
from .config import ENDPOINTS, MAX_LEVERAGE, MIN_LEVERAGE, Settings
from .execution import D, RemoteBroker, size_trade
from .journal import Journal
from .market import Market, sample_frames
from .models import INTERVALS, ALL_INTERVALS, decision_timeframe
from .secrets import SecretStore
from .strategy import decide
from . import risk


class Service:
    def __init__(self, root, mode="paper", sample=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.process_lock = ProcessLock(self.root / "engine.lock")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.secrets = SecretStore(self.root)
        self.generation = 0
        self.quote, self.frames, self.decision = None, {}, None
        self.last_update, self.error, self.preflight_result = None, None, None
        self.sample = sample
        self._sample_all = sample_frames() if sample else None
        self._sample_index = 4800
        self._last_funding_poll = 0
        try:
            self._load_mode(mode)
        except Exception:
            if hasattr(self, "journal"):
                self.journal.close()
            self.process_lock.close()
            raise

    def _load_mode(self, mode):
        if mode not in ENDPOINTS or (self.sample and mode != "paper"):
            raise ValueError("합성 시세는 모의거래에서만 사용 가능합니다.")
        name = "sample" if self.sample else mode
        journal, broker = Journal(self.root / name), None
        try:
            settings = Settings().updated(journal.get("settings", {}))
            if settings.research_mode and mode != "paper":
                raise ValueError("미검증 연구 모드는 모의거래에서만 실행할 수 있습니다.")
            account = journal.get("account", {"cash": str(settings.capital), "position": None,
                "running": False, "halt": None, "pending": None, "daily_date": None, "daily_equity": str(settings.capital),
                "fees": "0", "funding": "0", "realized": "0"})
            account["running"] = False
            account.setdefault("risk", {"day": account.get("daily_date"), "entries": 0,
                "daily_equity": account["daily_equity"], "peak": str(max(D(settings.capital), D(account["cash"])))})
            broker = None if mode == "paper" else RemoteBroker(mode, self.secrets.load(mode), journal)
            if broker and broker.key:
                prior = journal.get("account_fingerprint")
                if prior and prior != hashlib.sha256(broker.key.encode()).hexdigest():
                    raise ValueError("기존 기록과 등록된 API 키의 소유권이 다릅니다.")
                if account.get("position"):
                    broker.arm()
            journal.set("account", account)
        except Exception:
            journal.close()
            if broker:
                broker.close()
            raise
        self.mode, self.journal, self.settings, self.account, self.broker = mode, journal, settings, account, broker
        self.market = Market(ENDPOINTS[mode], self.root / name / "market-cache.json")
        if self.sample:
            self._sample_all = sample_frames(intraday=settings.strategy_mode != "strict", active=settings.strategy_mode == "active")
        self.quote, self.frames, self.decision = None, {}, None
        self.error, self.last_update, self.preflight_result = None, None, None
        self.generation += 1

    def start_worker(self):
        self.thread = threading.Thread(target=self._loop, name="elliott-engine", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            generation = self.generation
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.error = str(exc)
                    self.account["running"] = False
                    self.account["halt"] = self.account["halt"] or "DATA_OR_EXECUTION_ERROR"
                    self.journal.record("ERROR", {"reason": self.error, "mode": self.mode}, self.account)
            self.stop_event.wait(15)

    def tick(self):
        with self.lock:
            generation, market, settings = self.generation, self.market, self.settings
        if self.sample:
            clock = decision_timeframe(settings.strategy_mode)
            ratio = INTERVALS["1h"] // ALL_INTERVALS[clock]
            index = min(int(self._sample_index * ratio), len(self._sample_all[clock]))
            asof = self._sample_all[clock][index - 1].end
            frames = {tf: [c for c in cs if c.end <= asof] for tf, cs in self._sample_all.items()}
            price = frames[clock][-1].close
            quote = {"price": price, "bid": price - 1, "ask": price + 1, "funding_rate": 0,
                     "next_funding": None, "time": asof, "source": "합성 재생 · 실제 시장 가격 아님",
                     "rules": {"tick": "0.1", "step": "0.001", "min_qty": "0.001", "min_notional": "100"}}
            self._sample_index += 1 / ratio
        else:
            frames, quote, _ = market.load(intraday=settings.strategy_mode != "strict", active=settings.strategy_mode == "active")
            asof = frames[decision_timeframe(settings.strategy_mode)][-1].end
        with self.lock:
            if generation != self.generation:
                return
            prior_equity = self.equity()
            day = datetime.fromtimestamp(quote["time"] / 1000, ZoneInfo("Asia/Seoul")).date().isoformat()
            if self.account["daily_date"] != day:
                self.account.update(daily_date=day, daily_equity=str(prior_equity))
            self.frames, self.quote = frames, quote
            risk.refresh(self.account["risk"], quote["time"], prior_equity)
            risk.refresh(self.account["risk"], quote["time"], self.equity())
            self.last_update, self.error = time.time(), None
            changed = not self.decision or self.decision["asof"] != asof or self.decision["settings"] != settings.to_json()
        decision = decide(frames, asof, settings) if changed else None
        with self.lock:
            if generation != self.generation:
                return
            if decision:
                self.decision = decision
                self.journal.decision(decision, frames)
            if self.account.get("pending"):
                self.account["running"] = False
                self.error = "미결 주문 의도 존재: 거래소 연결 화면에서 복구 대조 필요"
                return
            self._mark_position()
            equity = self.equity()
            breached = risk.budget(self.account["risk"], settings, equity) <= 0
            if breached:
                self.account.update(running=False, halt="LOSS_LIMIT")
                if self.account["position"]:
                    self._exit("배정 자산의 일일·누적·고점 대비 손실 한도", self.quote["price"])
            gate = risk.entry_gate(self.account["risk"], settings, quote["time"], equity)
            if not gate and self.account["running"] and not self.account["position"] and self.decision and self.decision["action"] != "WAIT":
                key = self.decision.get("signal_key", self.decision["selection"]["id"])
                plan = None
                try:
                    plan = size_trade(self.decision, quote, self.settings, equity, self.broker.taker_bps if self.broker else None,
                                      risk_cap=risk.budget(self.account["risk"], settings, equity))
                except ValueError as exc:
                    self.error = str(exc)
                if plan and self.journal.consume(key):
                    self._enter(plan)
            self.journal.set("account", self.account)

    def equity(self):
        value = D(self.account["cash"])
        p = self.account.get("position")
        if p and self.quote:
            value += (D(self.quote["price"]) - D(p["entry"])) * D(p["quantity"]) * p["direction"]
        return value

    def _enter(self, plan):
        if self.settings.research_mode and (self.mode != "paper" or self.broker):
            raise ValueError("연구 모드의 거래소 주문은 차단됩니다.")
        opened_at = int(time.time() * 1000) if not self.sample else self.quote["time"]
        p = {**plan, "opened_at": opened_at, "history_from": opened_at - 5000, "decision": self.decision, "funding_cursor": opened_at, "funding_paid": "0"}
        self.account["pending"] = {"kind": "ENTRY", "plan": p}
        risk.entered(self.account["risk"])
        self.journal.record("ENTRY_INTENT", {"mode": self.mode, "decision": self.decision, "plan": plan}, self.account)
        if self.broker:
            entry_id, raw, stop_id = self.broker.enter(plan)
            p.update(entry=raw["avgPrice"], entry_id=entry_id, stop_id=stop_id, fee="0")
        else:
            fill = D(plan["entry_reference"]) * (1 + p["direction"] * D(plan["slippage_rate"]))
            fee = fill * D(plan["quantity"]) * D(plan["fee_rate"])
            p.update(entry=str(fill), fee=str(fee))
            self.account["cash"] = str(D(self.account["cash"]) - fee)
            self.account["fees"] = str(D(self.account["fees"]) + fee)
        self.account.update(position=p, pending=None)
        self.journal.record("ENTRY", {"mode": self.mode, "decision": self.decision, "direction": "LONG" if p["direction"] > 0 else "SHORT",
                                      "price": p["entry"], "quantity": p["quantity"], "fee": None if self.broker else p["fee"],
                                      "fee_status": "PENDING_TRADE_RECONCILIATION" if self.broker else "SIMULATED", "plan": plan}, self.account)
        if self.broker:
            p["target_id"] = self.broker.target(plan)
            p.update(stop_status="NEW", target_status="NEW", protection_checked_at=time.time())
            self.journal.set("account", self.account)

    def _mark_position(self):
        p = self.account.get("position")
        if not p:
            return
        if self.broker:
            remote = self.broker.position()
            amount = D(remote["positionAmt"])
            if amount == 0:
                self._exit("거래소 보호 손절·익절 또는 외부 청산: 실제 체결 대조", self.quote["price"], already_flat=True)
                return
            if amount != D(p["quantity"]) * p["direction"]:
                raise ValueError("거래소 잔여 수량 불일치: 수동 복구 대조 필요")
            stop = self.broker.query(p["stop_id"])
            p["stop_status"] = stop.get("algoStatus", "UNKNOWN")
            p["protection_checked_at"] = None
            if stop.get("algoStatus") not in {"NEW", "TRIGGERED"}:
                self.account.update(running=False, halt="PROTECTION_FAILED")
                self._exit("거래소 보호 손절 비활성: 포지션 정리", self.quote["price"])
                return
            if stop.get("algoStatus") == "TRIGGERED":
                return
            if ((D(self.quote["price"]) - D(p["target"])) * p["direction"] < 0
                    and (D(self.quote["price"]) - D(p["stop"])) * p["direction"] > 0):
                self._ensure_target(p)
                if p["target_status"] == "FINISHED":
                    self.account.update(running=False, halt="TARGET_RECONCILE")
                    raise ValueError("익절 처리 완료 상태와 잔여 포지션이 불일치합니다. 손절 보존, 수동 대조 필요")
                if p["target_status"] == "TRIGGERED":
                    return
            p["protection_checked_at"] = time.time()
            if time.time() - self._last_funding_poll >= 60:
                fees, paid = self.broker.open_costs(p)
                fee_delta, funding_delta = fees - D(p["fee"]), paid - D(p["funding_paid"])
                self.account["cash"] = str(D(self.account["cash"]) - fee_delta - funding_delta)
                self.account["fees"] = str(D(self.account["fees"]) + fee_delta)
                self.account["funding"] = str(D(self.account["funding"]) + funding_delta)
                p["fee"], p["funding_paid"] = str(fees), str(paid)
                self.journal.record("COST_SYNC", {"mode": self.mode, "fees": str(fees), "funding_paid": str(paid)}, self.account)
                self._last_funding_poll = time.time()
        elif not self.sample and time.time() - self._last_funding_poll >= 60:
            rows = self.market.funding(p["funding_cursor"], self.quote["time"])
            for row in rows:
                if int(row["fundingTime"]) < p["opened_at"]:
                    continue
                paid = D(row["fundingRate"]) * D(row["markPrice"]) * D(p["quantity"]) * p["direction"]
                self.account["cash"] = str(D(self.account["cash"]) - paid)
                self.account["funding"] = str(D(self.account["funding"]) + paid)
                p["funding_paid"] = str(D(p["funding_paid"]) + paid)
                p["funding_cursor"] = int(row["fundingTime"]) + 1
                self.journal.record("FUNDING", {"paid": str(paid), "event": row}, self.account)
            self._last_funding_poll = time.time()
        price, direction = D(self.quote["price"]), p["direction"]
        if (price - D(p["stop"])) * direction <= 0:
            self._exit("엘리엇 진입 파동의 거래 무효화 가격 도달", price)
        elif (price - D(p["target"])) * direction >= 0:
            self._exit("엘리엇 파동 길이 기반 목표 도달", price)

    def _ensure_target(self, position):
        position["target_status"] = "UNKNOWN"
        client, status = self.broker.target_state(position)
        position["target_status"] = status
        if client:
            position["target_id"] = client
            return
        self.account.update(running=False, halt="TARGET_REPAIR_REVIEW")
        if position.get("target_repairs", 0) >= 1:
            raise ValueError("익절 복구 횟수 초과. 손절을 유지하고 수동 대조를 기다립니다.")
        position["target_repairs"] = position.get("target_repairs", 0) + 1
        self.journal.record("TARGET_REPAIR_INTENT", {"reason": "익절 의도 대조 후 누락 확인; 1회만 복구"}, self.account)
        position["target_id"] = self.broker.target(position)
        position["target_status"] = "NEW"
        self.journal.record("TARGET_REPAIRED", {"reason": "익절 복구 확인. 신규 진입 중단, 운영자 확인 필요"}, self.account)

    def _exit(self, reason, price, already_flat=False):
        p = self.account.get("position")
        if not p:
            return
        if self.broker:
            if not already_flat:
                self.account["pending"] = {"kind": "EXIT", "plan": p}
                self.journal.record("EXIT_INTENT", {"mode": self.mode, "reason": reason, "entry_decision": p["decision"]}, self.account)
                self.broker.flatten()
            settlement = self.broker.settlement(p)
            price, gross, fees = D(settlement["price"]), D(settlement["gross"]), D(settlement["fees"])
            funding = -D(settlement["funding_income"])
            self.broker.cancel_protection()
            fee_delta, funding_delta = fees - D(p["fee"]), funding - D(p["funding_paid"])
            self.account["cash"] = str(D(self.account["cash"]) + gross - fee_delta - funding_delta)
            self.account["fees"] = str(D(self.account["fees"]) + fee_delta)
            self.account["funding"] = str(D(self.account["funding"]) + funding_delta)
            net = gross - fees - funding
            exit_fee, total_fees = settlement["exit_fees"], str(fees)
        else:
            price = D(price) * (1 - p["direction"] * D(p["slippage_rate"]))
            gross = (price - D(p["entry"])) * D(p["quantity"]) * p["direction"]
            fees = price * D(p["quantity"]) * D(p["fee_rate"])
            self.account["cash"] = str(D(self.account["cash"]) + gross - fees)
            self.account["fees"] = str(D(self.account["fees"]) + fees)
            net = gross - fees - D(p["fee"]) - D(p["funding_paid"])
            exit_fee, total_fees = str(fees), str(fees + D(p["fee"]))
            settlement = None
        self.account["realized"] = str(D(self.account["realized"]) + gross)
        self.account.update(position=None, pending=None)
        risk.exited(self.account["risk"], self.settings,
                    self.quote["time"] if self.quote else int(time.time() * 1000), net)
        self.journal.record("EXIT", {"mode": self.mode, "reason": reason, "entry_decision": p["decision"],
            "exit_decision": self.decision, "direction": "LONG" if p["direction"] > 0 else "SHORT", "price": str(price),
            "quantity": p["quantity"], "gross": str(gross), "net": str(net), "exit_fee": exit_fee,
            "total_fees": total_fees, "settlement": settlement}, self.account,
            updates={"remote_orders": {}} if self.broker else None)

    def control(self, action, data):
        with self.lock:
            if action == "pause":
                self.account["running"] = False
            elif action in {"flatten", "emergency"}:
                self.account.update(running=False, halt="MANUAL_STOP" if action == "emergency" else self.account["halt"])
                if self.account.get("pending"):
                    raise ValueError("결과 불명 주문을 먼저 복구 대조하세요. 반복 시장가 주문은 차단됩니다.")
                if not self.quote:
                    raise ValueError("현재 가격이 없어 포지션 정리를 실행하지 않았습니다.")
                self._exit("사용자 비상 정지" if action == "emergency" else "사용자 포지션 정리", self.quote["price"])
            elif action == "start":
                if self.settings.research_mode and self.mode != "paper":
                    raise ValueError("연구 모드는 실제 주문을 전송할 수 없습니다.")
                if not self.quote or not self.last_update or time.time() - self.last_update > 40:
                    raise ValueError("최신 시세 수신 전 시작할 수 없습니다.")
                if self.account.get("pending"):
                    raise ValueError("미결 주문 의도 복구가 필요합니다.")
                equity = self.equity()
                if risk.budget(self.account["risk"], self.settings, equity) <= 0:
                    raise ValueError("손실 한도가 아직 초과되어 있습니다.")
                if self.broker:
                    if not self.journal.get("settings_confirmed"):
                        raise ValueError("설정 화면에서 본인의 배정 자본·위험 한도를 저장해야 합니다.")
                    if self.mode == "live" and (data.get("confirmation") != "LIVE BTCUSDT" or data.get("risk_accepted") is not True):
                        raise ValueError("LIVE BTCUSDT 입력과 실거래 위험 확인이 필요합니다.")
                    report = self.check_account()
                    if not report["ok"]:
                        raise ValueError("거래소 연결의 필수 검사를 통과하지 못했습니다.")
                    self.broker.arm()
                self.account.update(running=True, halt=None)
            else:
                raise ValueError("알 수 없는 명령")
            self.journal.record("CONTROL", {"action": action, "mode": self.mode}, self.account)
            return {"ok": True}

    def check_account(self):
        with self.lock:
            self.preflight_result = None
            if not self.broker:
                return {"ok": True, "checks": [{"name": "모의거래", "ok": True, "detail": "API 키 불필요"}]}
            fingerprint = hashlib.sha256(self.broker.key.encode()).hexdigest()
            prior = self.journal.get("account_fingerprint")
            if prior and prior != fingerprint:
                raise ValueError("이 모드의 거래 기록과 API 키 계정이 다릅니다. 기존 기록을 다른 계정으로 인수할 수 없습니다.")
            try:
                report = self.broker.preflight(self.settings, self.account, self.quote)
            except Exception as exc:
                self.preflight_result = {"ok": False, "checked_at": time.time(), "checks": [
                    {"name": "계정 조회", "ok": False, "detail": f"최신 검사 중단: {exc}. 이전 검사 결과는 폐기했습니다."}
                ]}
                raise
            self.preflight_result = report
            if report["ok"]:
                self.journal.set("account_fingerprint", fingerprint)
            return report

    def configure_account(self):
        with self.lock:
            self._require_idle()
            if not self.broker:
                raise ValueError("거래소 연결 모드에서만 가능합니다.")
            self.broker.configure(self.settings.leverage)
            self.preflight_result = None
            return {"ok": True}

    def _require_idle(self):
        if self.account["running"] or self.account["position"] or self.account.get("pending"):
            raise ValueError("신규 진입을 중단하고 포지션·미결 주문을 정리한 뒤 변경하세요.")

    def update_settings(self, data):
        with self.lock:
            self._require_idle()
            new = self.settings.updated(data)
            if new.research_mode and self.mode != "paper":
                raise ValueError("연구 모드는 모의거래에서만 설정할 수 있습니다.")
            delta = D(new.capital) - D(self.settings.capital)
            cash = D(self.account["cash"]) + delta
            if cash <= 0:
                raise ValueError("누적 손익 반영 후 배정 자산은 양수여야 합니다.")
            self.account["cash"] = str(cash)
            self.account["daily_equity"] = str(D(self.account["daily_equity"]) + delta)
            for key in ("daily_equity", "peak"):
                self.account["risk"][key] = str(D(self.account["risk"][key]) + delta)
            if self.sample and new.strategy_mode != self.settings.strategy_mode:
                self._sample_all = sample_frames(intraday=new.strategy_mode != "strict", active=new.strategy_mode == "active")
            self.settings = new
            self.decision = None
            self.journal.set_many({"settings": new.to_json(), "settings_confirmed": True, "account": self.account})
            self.journal.record("SETTINGS", new.to_json(), self.account)
            self.preflight_result = None
            self.generation += 1
            return {"ok": True, "settings": new.to_json()}

    def switch_mode(self, mode):
        with self.lock:
            self._require_idle()
            if mode == self.mode:
                return {"ok": True}
            if mode not in ENDPOINTS or self.sample:
                raise ValueError("이 실행에서 선택할 수 없는 모드입니다.")
            if self.settings.research_mode:
                raise ValueError("연구 모드를 해제한 뒤 거래소 모드로 전환하세요.")
            if self.broker and self.broker.key:
                if D(self.broker.position()["positionAmt"]) != 0:
                    raise ValueError("거래소 포지션이 남아 있습니다.")
                if self.broker.owned_orders():
                    raise ValueError("거래소 주문 복구 대조 후 모드를 변경하세요.")
            old_journal, old_broker = self.journal, self.broker
            self._load_mode(mode)
            old_journal.close()
            if old_broker:
                old_broker.close()
            return {"ok": True}

    def save_credentials(self, data):
        with self.lock:
            self._require_idle()
            if not self.broker:
                raise ValueError("먼저 데모 또는 실거래 모드를 선택하세요.")
            if self.broker.owned_orders():
                raise ValueError("소유 주문의 복구 대조 후 키를 변경하세요.")
            key, secret = data.get("key"), data.get("secret")
            prior = self.journal.get("account_fingerprint")
            if prior and isinstance(key, str) and hashlib.sha256(key.encode()).hexdigest() != prior:
                raise ValueError("기록이 있는 계정의 키 교체는 계정 동일성 확인 없이는 허용하지 않습니다.")
            self.secrets.save(self.mode, key, secret)
            self.broker.close()
            self.broker = RemoteBroker(self.mode, (key, secret), self.journal)
            self.preflight_result = None
            return {"ok": True}

    def recover(self):
        with self.lock:
            if not self.broker or self.account["running"]:
                raise ValueError("데모·실거래의 진입 중단 상태에서만 복구할 수 있습니다.")
            pending = self.account.get("pending")
            remote = self.broker.position()
            amount = D(remote["positionAmt"])
            orders = self.broker.owned_orders()
            raws = {client: ({"status": "REJECTED", "algoStatus": "REJECTED", "executedQty": "0"}
                             if item["status"] == "REJECTED" else self.broker.query(client)) for client, item in orders.items()}
            if any(raw.get("status", raw.get("algoStatus")) in {"NEW", "PARTIALLY_FILLED"} for client, raw in raws.items() if not orders[client]["algo"]):
                raise ValueError("진입·청산 주문이 아직 미결입니다. 완료 전 재주문하지 않습니다.")
            self.broker.arm()
            if pending and pending["kind"] == "ENTRY":
                p = pending["plan"]
                entries = [(client, raw) for client, raw in raws.items() if orders[client]["role"] == "entry"]
                if entries and entries[-1][1].get("status") == "FILLED":
                    client, raw = entries[-1]
                    stops = [cid for cid, item in orders.items() if item["role"] == "stop"]
                    if not stops:
                        raise ValueError("소유 손절 주문이 없어 자동 인수하지 않습니다.")
                    p.update(entry=raw["avgPrice"], quantity=raw["executedQty"], entry_id=client, stop_id=stops[-1], fee="0")
                    self.account.update(position=p, pending=None)
                    self.journal.record("RECOVERY", {"mode": self.mode, "entry_decision": p["decision"], "reason": "응답 유실 주문의 실제 체결 인수"}, self.account)
                elif amount == 0 and all(raw.get("status") in {"CANCELED", "REJECTED", "EXPIRED"} and D(raw.get("executedQty", 0)) == 0 for _, raw in entries):
                    self.account["pending"] = None
                else:
                    raise ValueError("체결과 소유권이 일치하지 않아 복구를 확정하지 않았습니다.")
            elif pending and pending["kind"] == "EXIT":
                self.account["pending"] = None
            if self.account["position"]:
                if amount == 0:
                    self._exit("재시작 후 거래소 청산 대조", self.quote["price"] if self.quote else 0, already_flat=True)
                elif amount != D(self.account["position"]["quantity"]) * self.account["position"]["direction"]:
                    raise ValueError("복구 포지션 수량 불일치")
            elif amount == 0:
                self.broker.cancel_protection()
                self.journal.set("remote_orders", {})
            else:
                raise ValueError("봇 소유가 아닌 포지션은 자동 인수하지 않습니다.")
            self.account["running"] = False
            self.journal.record("RECOVERY", {"mode": self.mode, "reason": "주문 대조 완료. 매매는 별도 시작 필요"}, self.account)
            return {"ok": True}

    def status(self):
        with self.lock:
            p = self.account.get("position")
            equity = self.equity()
            payload = {"mode": self.mode, "sample": self.sample, "settings": self.settings.to_json(), "account": self.account,
                    "limits": {"leverage_min": MIN_LEVERAGE, "leverage_max": MAX_LEVERAGE,
                               "strategy_modes": ["strict", "multi", "active"], "chart_timeframes": ["1w", "1d", "4h", "1h", "30m", "15m"]},
                    "equity": str(equity), "net": str(equity - D(self.settings.capital)), "quote": self.quote,
                    "entry_gate": risk.entry_gate(self.account["risk"], self.settings,
                        self.quote["time"] if self.quote else int(time.time() * 1000), equity),
                    "remaining_risk": str(risk.budget(self.account["risk"], self.settings, equity)),
                    "decision": self.decision, "last_update": self.last_update, "error": self.error,
                    "candles": {tf: [asdict(c) for c in cs[-180:]] for tf, cs in self.frames.items()},
                    "events": self.journal.events(60), "preflight": self.preflight_result,
                    "research_review": self.journal.get("research_review") if self.settings.research_mode else None,
                    "credentials_saved": bool(self.broker and self.broker.key and self.broker.secret),
                    "journal_path": str(self.journal.root / "trades")}
            return json.loads(json.dumps(payload))

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(120)
            if self.thread.is_alive():
                raise RuntimeError("가격 조회 종료 대기 중. DB를 닫지 않았습니다.")
        with self.lock:
            self.account["running"] = False
            self.journal.set("account", self.account)
            self.journal.close()
            if self.broker:
                self.broker.close()
            self.process_lock.close()

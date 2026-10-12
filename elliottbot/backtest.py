from __future__ import annotations

from .execution import D, size_trade
from .strategy import decide
from .models import decision_timeframe
from . import risk
from dataclasses import replace


def run(frames, settings, rules, funding=None, bars=240, *, controls=True, start_time=None, history_limits=None):
    clock = decision_timeframe(settings.strategy_mode)
    if not frames.get(clock):
        raise ValueError(f"{settings.strategy_mode} 전략의 시간순 검증에는 {clock} 원본이 필요합니다.")
    hours = frames.get("15m") or frames.get("30m") or frames["1h"]
    start = max(168, len(hours) - bars)
    if start_time is not None:
        start = next((i for i, c in enumerate(hours) if c.time >= start_time), len(hours))
    if len(hours) <= start:
        raise ValueError("시간순 검증을 위한 최소 169개 기준 시간봉이 필요합니다.")
    cash, peak, max_dd = D(settings.capital), D(settings.capital), D(0)
    pending, position = None, None
    fills, curve, used, decisions = [], [], set(), 0
    daily_key, baseline, halted = None, cash, False
    ledger, blocked = {}, {}
    waits, scenario_blocks, candidate_observations = {}, {}, 0
    last_decision_at = None
    from datetime import datetime
    from zoneinfo import ZoneInfo

    for i in range(start, len(hours)):
        bar = hours[i]
        marked = cash + ((D(hours[i - 1].close) - position["entry"]) * D(position["quantity"]) * position["direction"] if position else 0)
        risk.refresh(ledger, bar.time, marked)
        day = datetime.fromtimestamp(bar.time / 1000, ZoneInfo("Asia/Seoul")).date().isoformat()
        if day != daily_key:
            baseline = cash + ((D(hours[i - 1].close) - position["entry"]) * D(position["quantity"]) * position["direction"] if position else 0)
            daily_key = day
        if pending and not position and not halted:
            quote = {"price": bar.open, "bid": bar.open, "ask": bar.open, "rules": rules, "time": bar.time}
            try:
                gate = risk.entry_gate(ledger, settings, bar.time, cash) if controls else None
                if gate:
                    raise ValueError(gate)
                plan = size_trade(pending, quote, settings, cash,
                                  risk_cap=risk.budget(ledger, settings, cash) if controls else None)
            except ValueError as exc:
                blocked[str(exc)] = blocked.get(str(exc), 0) + 1
                plan = None
            if plan:
                risk.entered(ledger)
                entry = D(bar.open) * (1 + plan["direction"] * D(plan["slippage_rate"]))
                fee = entry * D(plan["quantity"]) * D(plan["fee_rate"])
                cash -= fee
                position = {**plan, "entry": entry, "entry_fee": fee, "decision": pending, "funding": D(0), "time": bar.time}
                fills.append({"kind": "ENTRY", "time": bar.time, "price": str(entry), "quantity": plan["quantity"], "decision": pending})
            pending = None
        if position:
            p, d = position, position["direction"]
            stop, target = D(p["stop"]), D(p["target"])
            # Unknown intrabar order: stop takes precedence over target.
            stop_hit = D(bar.low) <= stop if d > 0 else D(bar.high) >= stop
            target_hit = D(bar.high) >= target if d > 0 else D(bar.low) <= target
            for item in funding or []:
                if bar.time <= int(item["fundingTime"]) <= bar.end:
                    # At a possible intrabar exit, conservatively charge the full settlement.
                    paid = D(item["fundingRate"]) * D(item["markPrice"]) * D(p["quantity"]) * d
                    cash -= paid
                    p["funding"] += paid
            mark_equity = cash + (D(bar.close) - p["entry"]) * D(p["quantity"]) * d
            loss_hit = (baseline - mark_equity >= D(settings.capital) * D(settings.daily_loss_percent) / 100
                        or D(settings.capital) - mark_equity >= D(settings.capital) * D(settings.total_loss_percent) / 100)
            if controls:
                risk.refresh(ledger, bar.end, mark_equity)
                loss_hit = risk.budget(ledger, settings, mark_equity) <= 0
            if stop_hit or target_hit or loss_hit:
                if stop_hit:
                    price = min(D(bar.open), stop) if d > 0 else max(D(bar.open), stop)
                    reason = "파동 거래 무효화 가격 / 동일 봉 양쪽 도달 시 손절 우선"
                else:
                    price = target if target_hit else D(bar.close)
                    reason = "파동 목표 도달" if target_hit else "손실 한도"
                price *= 1 - d * D(p["slippage_rate"])
                fee = price * D(p["quantity"]) * D(p["fee_rate"])
                gross = (price - p["entry"]) * D(p["quantity"]) * d
                cash += gross - fee
                net = gross - fee - p["entry_fee"] - p["funding"]
                risk.exited(ledger, settings, bar.end, net)
                fills.append({"kind": "EXIT", "time": bar.end, "price": str(price), "reason": reason,
                              "net": str(net), "risk_at_stop": p["risk_at_stop"], "entry_decision": p["decision"]["id"]})
                position = None
                if loss_hit:
                    halted = True
        equity = cash + ((D(bar.close) - position["entry"]) * D(position["quantity"]) * position["direction"] if position else 0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
        curve.append({"time": bar.end, "equity": float(equity)})
        if not position and not halted:
            current = {tf: [c for c in cs if c.end <= bar.end] for tf, cs in frames.items()}
            if history_limits:
                current = {tf: cs[-history_limits[tf]:] for tf, cs in current.items()}
            if not current.get(clock):
                continue
            asof = current[clock][-1].end
            if last_decision_at == asof:
                continue
            last_decision_at = asof
            decision = decide(current, asof, settings)
            decisions += 1
            if decision["action"] == "WAIT":
                reason = decision.get("reason", "WAIT")
                waits[reason] = waits.get(reason, 0) + 1
            for scenario in decision.get("scenarios", []):
                candidate_observations += 1
                for check in scenario["checks"]:
                    if not check["passed"]:
                        key = check["detail"]
                        scenario_blocks[key] = scenario_blocks.get(key, 0) + 1
            key = decision.get("signal_key") or (decision["selection"]["id"] if decision.get("selection") else None)
            if decision["action"] != "WAIT" and key not in used:
                used.add(key)
                pending = decision
    exits = [f for f in fills if f["kind"] == "EXIT"]
    return {"from": hours[start].time, "to": hours[-1].end, "bars": len(hours) - start, "decisions": decisions,
            "net": str(equity - D(settings.capital)), "return": float(equity / D(settings.capital) - 1),
            "max_drawdown": float(max_dd), "closed_trades": len(exits), "open_position": bool(position),
            "win_rate": sum(D(f["net"]) > 0 for f in exits) / len(exits) if exits else None,
            "probability_calibrated": False, "curve": curve, "fills": fills,
            "settings": settings.to_json(), "enhanced_controls": controls, "entry_blocks": blocked,
            "wait_reasons": waits, "scenario_blocks": scenario_blocks, "candidate_observations": candidate_observations,
            "average_trade": float(sum((D(f["net"]) for f in exits), D(0)) / len(exits)) if exits else None,
            "profit_factor": float(sum((D(f["net"]) for f in exits if D(f["net"]) > 0), D(0)) /
                -sum(D(f["net"]) for f in exits if D(f["net"]) < 0)) if any(D(f["net"]) < 0 for f in exits) else None,
            "assumptions": ["고정 규칙을 시간순 재생. 해당 시점에 마감된 봉만 사용. 미래 최종 ZigZag 미사용.",
                "신호 봉의 다음 봉 시가 진입. 같은 봉에서 손절·익절 모두 닿으면 손절 우선.",
                "고정 시장가 슬리피지·수수료, 전량 체결 가정. 호가 대기열·부분체결·급변 청산 모델 없음.",
                "실제 공시 펀딩 반영. 봉 중 청산과 펀딩 순서 불명 시 해당 봉 펀딩 전액 반영." if funding is not None else "이 재생에는 펀딩 기록이 없어 펀딩 0 가정.",
                "목표·손절은 거래가격 OHLC 대용. 실운용 MARK_PRICE 트리거와 차이 가능.",
                "짧은 재생 결과는 수익성·승률 검증이 아님. 거래 0건도 유효한 관망 결과."]}


def compare(frames, settings, rules, funding=None, bars=480):
    if not frames.get("30m"):
        raise ValueError("동일한 30분 체결 타임라인 비교에 30분봉이 필요합니다.")
    reports = {}
    variants = [("baseline", "strict", False, 1), ("multi", "multi", True, 1), ("multi_stress", "multi", True, 2)]
    if frames.get("15m"):
        variants += [("active", "active", True, 1), ("active_stress", "active", True, 2)]
    for name, mode, controls, cost in variants:
        config = replace(settings, strategy_mode=mode, fee_bps=settings.fee_bps * cost,
                         slippage_bps=settings.slippage_bps * cost, research_mode=False)
        reports[name] = run(frames, config, rules, funding, bars, controls=controls)
    if settings.research_mode and frames.get("15m"):
        for name, cost in (("research", 1), ("research_stress", 2)):
            config = replace(settings, fee_bps=settings.fee_bps * cost, slippage_bps=settings.slippage_bps * cost)
            reports[name] = run(frames, config, rules, funding, bars)
    sufficient = all(reports[k]["closed_trades"] >= 30 for k in ("baseline", "multi", "active") if k in reports)
    return {"comparison": reports, "sufficient_trades": sufficient, "profitability_proven": False,
            "verdict": "동일 기간 탐색적 비교. 별도 미사용 기간 검증 필요." if sufficient else "거래 표본 부족: 수익률·위험 개선 판정 불가",
            "notes": [f"동일 {'15분' if frames.get('15m') else '30분'}봉 체결·비용·자본·설정된 진입 횟수/대기 한도. 기존 엄격 전략만 새 위험 통제 없이 비교.",
                      "스트레스는 수수료와 슬리피지 각각 2배. 파라미터 최적화 없음.",
                      "기간 말 미청산 포지션은 종가 평가손익 포함; 청산 수수료 미차감."]}

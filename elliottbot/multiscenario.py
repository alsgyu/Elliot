"""Causal, bounded multi-timeframe scenarios; ranking scores are not probabilities."""
from dataclasses import replace

from .config import ACTIVE_VERSION, MULTI_VERSION, RESEARCH_VERSION, SCALES
from .execution import D, payoff
from .models import ALL_INTERVALS, digest, strategy_intervals
from .strategy import choose
from .waves import candidates, confirmed_pivots


def proven(c):
    return bool(c and c.subdivisions and all(s["confirmed"] for s in c.subdivisions))


def c_wave(setup, parents):
    """A/B geometry is ambiguous without a completed parent impulse at its origin."""
    if setup.pattern != "WAVE_3" or not proven(setup):
        return None, None
    origin = setup.pivots[0]
    for parent in parents:
        if parent.pattern != "IMPULSE_COMPLETE" or parent.direction != setup.direction or not proven(parent):
            continue
        end = parent.pivots[-1]
        if (end.time <= origin.time < end.time + ALL_INTERVALS[parent.timeframe]
                and abs(origin.price - end.price) <= max(1e-8, end.price * 1e-8)):
            length_a = abs(setup.pivots[1].price - origin.price)
            return replace(setup, pattern="WAVE_C", target=setup.stop + setup.direction * length_a), parent
    return None, None


def breakout(setup, candles, timeframe):
    if len(candles) < 3:
        return None
    scales = SCALES[timeframe]
    wanted = "H" if setup.direction > 0 else "L"
    for scale in scales:
        ps = confirmed_pivots(candles, scale)
        levels = [p for p in ps if p.kind == wanted and p.time > setup.pivots[-1].time
                  and p.confirmed_at <= candles[-2].end]
        if not levels:
            continue
        p = levels[-1]
        previous, current = candles[-2].close, candles[-1].close
        if (previous - p.price) * setup.direction <= 0 < (current - p.price) * setup.direction:
            return {"price": p.price, "confirmed_at": p.confirmed_at, "timeframe": timeframe,
                    "bar_close": current, "bar_end": candles[-1].end, "pivot_time": p.time}
    return None


def pivot_confirmation(setup, candles, timeframe, provisional=False):
    if not candles or not (proven(setup) or provisional and any(s["confirmed"] for s in setup.subdivisions)):
        return None
    bar, pivot = candles[-1], setup.pivots[-1]
    known_at = max([pivot.confirmed_at] + [p.confirmed_at for s in setup.subdivisions for p in s.get("pivots", [])])
    threshold = pivot.price * (1 + setup.direction * setup.scale)
    if (0 <= bar.end - known_at < ALL_INTERVALS[timeframe]
            and (bar.close - threshold) * setup.direction > 0):
        return {"price": threshold, "confirmed_at": known_at, "timeframe": timeframe,
                "bar_close": bar.close, "bar_end": bar.end, "pivot_time": pivot.time,
                "method": "confirmed_pivot_reversal"}
    return None


def decide_multi(frames, asof, settings):
    active = settings.strategy_mode == "active"
    intervals = strategy_intervals(settings.strategy_mode)
    frames = {tf: [c for c in frames.get(tf, []) if c.end <= asof] for tf in intervals}
    reports, counts = {}, {}
    hierarchy = [("1w", "1d"), ("1d", "4h"), ("4h", "1h"), ("1h", "30m"), ("30m", "15m" if active else "30m")]
    if active:
        hierarchy.append(("15m", "15m"))
    for tf, lower in hierarchy:
        cs, rejects = candidates(tf, frames[tf], frames[lower], scales=SCALES[tf], adaptive=settings.research_mode)
        pick, state = choose(cs, settings.conflict_gap)
        counts[tf] = cs
        reports[tf] = {"timeframe": tf, "state": state, "bias": pick.direction if pick else 0,
                       "selected": pick.id if pick else None, "candidates": [c.to_json() for c in cs],
                       "rejected": rejects, "closed_at": frames[tf][-1].end if frames[tf] else None,
                       "candle_count": len(frames[tf])}
    result = {"version": RESEARCH_VERSION if settings.research_mode else ACTIVE_VERSION if active else MULTI_VERSION, "asof": asof, "action": "WAIT", "reason": "단기 진입 후보 대기" if active else "다중 시나리오 후보 대기",
              "probability": None, "score_label": "우선순위 점수, 승률·기대수익 추정 아님", "frames": reports,
              "settings": settings.to_json(), "selection": None, "trigger": None, "checks": [], "scenarios": [],
              "data_hash": digest(frames), "config_hash": digest(settings.to_json())}

    def finish():
        result["id"] = digest(result)[:24]
        return result

    fresh = all(len(frames[tf]) >= 12 and 0 <= asof - frames[tf][-1].end < intervals[tf] + 120_000 for tf in intervals)
    result["checks"].append({"id": "M00", "passed": fresh, "detail": "주·일·4시간·1시간·30분" + ("·15분" if active else "") + " 마감 자료 최신성"})
    if not fresh:
        result["reason"] = f"{'15분봉' if active else '30분봉'}을 포함한 마감 자료가 부족하거나 지연됨"
        return finish()
    executable = []
    seen = set()
    setups = [("4h", "1h", "1d"), ("1h", "30m", "4h")]
    if active:
        setups.append(("30m", "15m", "1h"))
    for tf, lower, parent_tf in setups:
        for original in counts[tf]:
            if original.pattern not in {"WAVE_3", "WAVE_5"}:
                continue
            converted, parent_c = c_wave(original, counts[parent_tf])
            setup = converted or original
            # 1-2 and A-B share endpoints: never count them as independent votes.
            key = digest({"direction": setup.direction, "anchor": setup.pivots[-1].time // ALL_INTERVALS["4h"],
                          "stop": round(setup.stop, 6)})[:24]
            identity = (tf, key)
            if identity in seen:
                continue
            seen.add(identity)
            checks = []
            def require(code, ok, detail):
                checks.append({"id": code, "passed": bool(ok), "detail": detail})
                return bool(ok)
            full_proof = setup.eligible and proven(setup)
            partial_proof = (any(s["confirmed"] for s in setup.subdivisions)
                             and all(c["passed"] for c in setup.rules if c["id"] != "S01"))
            require("M01", partial_proof if settings.research_mode else full_proof,
                    "모의 연구: 가격 규칙 및 최소 1개 내부 분할 확인" if settings.research_mode else "선행 추진·조정 내부 5-3 구조 확인")
            age = (asof - setup.pivots[-1].confirmed_at) / ALL_INTERVALS[tf]
            require("M02", 0 <= age <= 12, "마지막 조정 확인 후 12개 후보 시간봉 이내")
            threshold = settings.research_min_score if settings.research_mode else settings.min_score
            require("M03", setup.score >= threshold, f"모의 연구 적합도 기준 {threshold:g}점" if settings.research_mode else "파동 적합도 최소 기준")
            rivals = [c for c in counts[tf] if c.direction != setup.direction and proven(c)
                      and c.score >= setup.score - settings.conflict_gap]
            require("M04", not rivals, "같은 시간봉의 확인된 반대 해석과 우열 확보")
            strong_opposite = [c for c in counts[parent_tf] if proven(c) and c.direction == -setup.direction]
            aligned = [c for c in counts[parent_tf] if c.direction == setup.direction and proven(c)]
            context = "correction_c" if parent_c else "aligned" if aligned and not strong_opposite else "unresolved"
            require("M05", parent_c is not None or not strong_opposite, "확인된 상위 반대 구조 차단; C파는 상위 완료 위치 증거 필요")
            trigger = breakout(setup, frames[lower], lower)
            if not trigger and settings.research_mode:
                trigger = pivot_confirmation(setup, frames[lower], lower, provisional=True)
            require("M06", trigger is not None, f"{lower} 신규 돌파 또는 조정 끝 확인 종가" if settings.research_mode else f"{lower} 확정 소피벗을 새로 돌파한 마감 종가")
            price = frames[lower][-1].close
            loss, reward = payoff(price, setup.stop, setup.target, setup.direction,
                                  settings.fee_bps / 10000, settings.slippage_bps / 10000)
            distance = (price - setup.stop) * setup.direction
            rr = float(reward / loss) if loss > 0 and distance > 0 else 0
            cost_fraction = float((loss - D(distance)) / loss) if loss > 0 and distance > 0 else 1
            require("M07", rr >= settings.min_rr and distance > 0, "비용 후 손익비와 유효 손절 거리")
            require("M08", cost_fraction <= settings.max_cost_fraction, "손절 위험 중 수수료·슬리피지 비중 제한")
            require("M09", setup.direction > 0 or settings.allow_short, "진입 방향 허용")
            stop_percent = distance / price * 100
            if active:
                require("M11", 0 < stop_percent <= settings.max_stop_percent,
                        f"구조 손절 거리 {stop_percent:.3f}% / 상한 {settings.max_stop_percent:g}% · 초과 시 진입 제외, 손절을 임의로 당기지 않음")
            factor = .5 if context != "aligned" else .75 if tf == "1h" else 1.0
            if tf == "30m":
                factor = .5 if context == "aligned" else .25
            weekly_opposed = any(proven(c) and c.direction == -setup.direction for c in counts["1w"])
            if weekly_opposed:
                factor = min(factor, .25 if tf == "30m" else .5)
            if settings.research_mode:
                factor = min(settings.research_risk_multiplier, .25 if weekly_opposed else .5)
            priority = round(setup.score + (6 if context == "aligned" else 2 if parent_c else 0)
                             + 2 * min(max(rr, 0), 3) - 10 * max(cost_fraction, 0) - max(age, 0) / 3
                             - (4 if weekly_opposed else 0), 3)
            payload = setup.to_json()
            if converted and not any(c["id"] == setup.id for c in reports[tf]["candidates"]):
                reports[tf]["candidates"].append(payload)
            row = {"id": setup.id, "signal_key": key, "timeframe": tf, "pattern": setup.pattern,
                   "direction": setup.direction, "context": context, "priority": priority,
                   "risk_multiplier": factor, "rr": round(rr, 4), "cost_fraction": round(cost_fraction, 4),
                   "stop_percent": round(stop_percent, 4),
                   "passed": all(c["passed"] for c in checks), "checks": checks, "selection": payload,
                   "trigger": trigger, "parent_evidence": parent_c.id if parent_c else None,
                   "weekly_opposed": weekly_opposed}
            row["proof_status"] = "confirmed" if full_proof else "provisional"
            result["scenarios"].append(row)
            if row["passed"]:
                executable.append(row)
    executable.sort(key=lambda r: (-r["priority"], -ALL_INTERVALS[r["timeframe"]], r["id"]))
    if not executable:
        failed = max(result["scenarios"], key=lambda r: sum(c["passed"] for c in r["checks"]), default=None)
        if failed:
            result["checks"] += failed["checks"]
            result["reason"] = "관망: " + next(c["detail"] for c in failed["checks"] if not c["passed"])
        return finish()
    best = executable[0]
    opposite = next((r for r in executable[1:] if r["direction"] != best["direction"]), None)
    unique = opposite is None or best["priority"] - opposite["priority"] >= settings.conflict_gap
    result["checks"] += best["checks"] + [{"id": "M10", "passed": unique, "detail": "시간봉 간 반대 진입 후보의 우선순위 차이 확보"}]
    if not unique:
        result["reason"] = "관망: 시간봉 간 롱·숏 후보의 우선순위 충돌"
        return finish()
    result.update(action="LONG" if best["direction"] > 0 else "SHORT", selection=best["selection"],
                  trigger=best["trigger"], rr=best["rr"], signal_key=best["signal_key"],
                  risk_multiplier=best["risk_multiplier"], context=best["context"], priority=best["priority"],
                  signal_expires_at=best["trigger"]["bar_end"] + ALL_INTERVALS[best["trigger"]["timeframe"]],
                  reason=f"{best['timeframe']} {best['pattern']} · 구조·신규 돌파·비용 조건 통과 · 위험 배수 {best['risk_multiplier']}")
    if settings.research_mode:
        result["reason"] = f"모의 연구 · {'내부 구조 확인' if best['proof_status'] == 'confirmed' else '잠정 카운트, 내부 구조 미완전'} · 위험 배수 {best['risk_multiplier']}"
        result["proof_status"] = best["proof_status"]
    reports[best["timeframe"]].update(selected=best["id"], bias=best["direction"], state="SELECTED")
    return finish()

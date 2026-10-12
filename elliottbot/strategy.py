from __future__ import annotations

from dataclasses import asdict

from .config import STRATEGY_VERSION, Settings
from .execution import payoff
from .models import INTERVALS, digest
from .waves import candidates


def _rank(c):
    proven = bool(c.subdivisions and all(s["confirmed"] for s in c.subdivisions))
    return (proven, c.score, c.pivots[-1].confirmed_at)


def choose(candidates_, gap):
    if not candidates_:
        return None, "NO_COUNT"
    ranked = sorted(candidates_, key=_rank, reverse=True)
    first = ranked[0]
    for alternative in ranked[1:]:
        if (alternative.direction != first.direction and _rank(alternative)[0] == _rank(first)[0]
                and first.score - alternative.score < gap):
            return None, "CONFLICT"
    if not first.direction:
        return None, "COMPLEX_CORRECTION"
    return first, "SELECTED"


def decide(frames, asof, settings: Settings):
    if settings.strategy_mode in {"multi", "active"}:
        from .multiscenario import decide_multi
        return decide_multi(frames, asof, settings)
    frames = {tf: [c for c in frames.get(tf, []) if c.end <= asof] for tf in INTERVALS}
    reports, selected = {}, {}
    for tf, lower_tf in (("1w", "1d"), ("1d", "4h"), ("4h", "1h"), ("1h", "1h")):
        cs, rejects = candidates(tf, frames[tf], frames[lower_tf])
        pick, state = choose(cs, settings.conflict_gap)
        selected[tf] = pick
        reports[tf] = {"timeframe": tf, "state": state, "bias": pick.direction if pick else 0,
                       "selected": pick.id if pick else None, "closed_at": frames[tf][-1].end if frames[tf] else None,
                       "candidates": [c.to_json() for c in cs], "rejected": rejects,
                       "candle_count": len(frames[tf])}
    result = {"version": STRATEGY_VERSION, "asof": asof, "action": "WAIT", "reason": "확정 파동 대기",
              "probability": None, "score_label": "규칙 적합도, 승률 아님", "frames": reports,
              "settings": settings.to_json(), "selection": None, "trigger": None, "checks": [],
              "data_hash": digest(frames), "config_hash": digest(settings.to_json())}

    def require(code, condition, text):
        result["checks"].append({"id": code, "passed": bool(condition), "detail": text})
        if not condition:
            result["reason"] = text
        return condition

    def finish():
        result["id"] = digest({k: v for k, v in result.items() if k != "id"})[:24]
        return result

    if not require("P00", all(len(frames[tf]) >= 12 and 0 <= asof - frames[tf][-1].end < INTERVALS[tf] + 120_000 for tf in INTERVALS),
                   "마감된 주봉·일봉·4시간봉·1시간봉 데이터가 모두 최신이어야 함"):
        return finish()
    setup = selected["4h"]
    if not require("P01", setup is not None and setup.eligible, "4시간봉의 내부 파동까지 확인된 3파 또는 5파 진입 후보 필요"):
        return finish()
    result["selection"] = setup.to_json()
    weekly, daily, hourly = selected["1w"], selected["1d"], selected["1h"]
    if not require("P02", reports["1w"]["state"] not in {"CONFLICT", "COMPLEX_CORRECTION"} and (weekly is None or weekly.direction == setup.direction),
                   "주봉의 반대 카운트 또는 동급 대안 충돌은 진입 차단"):
        return finish()
    if not require("P03", daily is not None and daily.direction == setup.direction,
                   "일봉의 우선 카운트와 4시간봉 방향 일치 필요"):
        return finish()
    if not require("P04", setup.score >= settings.min_score, "4시간봉 규칙 적합도가 설정 기준 미달"):
        return finish()
    if not require("P05", hourly is not None and hourly.eligible and hourly.direction == setup.direction
                   and hourly.pivots[-1].time >= setup.pivots[-1].time,
                   "4시간봉 조정 이후 1시간봉의 같은 방향 5-3 구조 확인 필요"):
        return finish()
    price = frames["1h"][-1].close
    trigger = hourly.pivots[-2].price
    result["trigger"] = {"price": trigger, "confirmed_at": hourly.pivots[-1].confirmed_at,
                          "candidate": hourly.id, "bar_close": price}
    if not require("P06", (price - trigger) * setup.direction > 0, "1시간봉 종가가 확인된 소파동 고점·저점을 돌파해야 함"):
        return finish()
    if not require("P07", setup.direction > 0 or settings.allow_short, "숏 진입이 설정에서 비활성화됨"):
        return finish()
    loss = (price - setup.stop) * setup.direction
    modeled_loss, reward = payoff(price, setup.stop, setup.target, setup.direction, settings.fee_bps / 10000, settings.slippage_bps / 10000)
    rr = float(reward / modeled_loss) if modeled_loss > 0 and loss > 0 else 0
    result["rr"] = round(rr, 4)
    if not require("P08", loss > 0 and rr >= settings.min_rr, "수수료·슬리피지 차감 후 손익비 기준 충족 필요"):
        return finish()
    result.update(action="LONG" if setup.direction > 0 else "SHORT", reason="상위 파동 정합성, 하위 파동 구조, 종가 돌파와 비용 후 손익비 통과")
    return finish()

from __future__ import annotations

from .config import SCALES
from .models import Candle, Candidate, Pivot


def confirmed_pivots(candles: list[Candle], reversal: float) -> list[Pivot]:
    """Directional-change pivots. An outside bar cannot both extend and confirm."""
    if not 0 < reversal < 1:
        raise ValueError("reversal must be between zero and one")
    if not candles:
        return []
    first = candles[0]
    high, low = (first.high, first.time), (first.low, first.time)
    direction = 0
    result = []
    for bar in candles[1:]:
        if direction == 0:
            higher, lower = bar.high > high[0], bar.low < low[0]
            if higher:
                high = (bar.high, bar.time)
            if lower:
                low = (bar.low, bar.time)
            if higher and lower:
                continue
            if not lower and bar.close >= low[0] * (1 + reversal):
                result.append(Pivot(low[1], low[0], "L", bar.end))
                direction, high = 1, (bar.high, bar.time)
            elif not higher and bar.close <= high[0] * (1 - reversal):
                result.append(Pivot(high[1], high[0], "H", bar.end))
                direction, low = -1, (bar.low, bar.time)
        elif direction == 1:
            if bar.high > high[0]:
                high = (bar.high, bar.time)
            elif bar.low <= high[0] * (1 - reversal) and bar.time > high[1]:
                result.append(Pivot(high[1], high[0], "H", bar.end))
                direction, low = -1, (bar.low, bar.time)
        else:
            if bar.low < low[0]:
                low = (bar.low, bar.time)
            elif bar.high >= low[0] * (1 + reversal) and bar.time > low[1]:
                result.append(Pivot(low[1], low[0], "L", bar.end))
                direction, high = 1, (bar.high, bar.time)
    return result


def rule(code, passed, detail):
    return {"id": code, "passed": bool(passed), "detail": detail}


def impulse_rules(p: list[Pivot], complete=True):
    if len(p) not in (3, 5, 6):
        return [rule("I00", False, "충격파는 1·2, 1~4, 1~5의 연속 피벗으로 검사")]
    d = 1 if p[0].kind == "L" else -1
    x = [v.price * d for v in p]
    checks = [rule("I00", all(p[i].kind != p[i + 1].kind and p[i].time < p[i + 1].time for i in range(len(p) - 1)), "고점·저점 교대와 시간 순서"),
              rule("I01", x[1] > x[0] and x[0] < x[2] < x[1], "2파는 1파 시작점을 침범하지 않음")]
    if len(p) >= 5:
        checks += [rule("I02", x[3] > x[1], "3파는 1파 끝을 넘어 진행"),
                   rule("I03", x[1] < x[4] < x[3], "4파는 1파 가격 영역과 비중첩")]
    if len(p) == 6:
        lengths = [x[1] - x[0], x[3] - x[2], x[5] - x[4]]
        checks += [rule("I04", lengths[1] >= min(lengths[0], lengths[2]), "3파가 1·3·5 중 최단이면 탈락"),
                   rule("I05", x[5] > x[3], "자동매매는 절단되지 않은 5파만 채택")]
    return checks


def valid(checks):
    return all(c["passed"] for c in checks)


def zigzag_rules(p):
    if len(p) != 4:
        return [rule("Z00", False, "ABC는 연속 4개 피벗 필요")]
    d = 1 if p[0].kind == "L" else -1
    x = [v.price * d for v in p]
    return [rule("Z00", all(p[i].kind != p[i + 1].kind and p[i].time < p[i + 1].time for i in range(3)), "ABC 교대 구조"),
            rule("Z01", x[1] > x[0] and x[0] < x[2] < x[1], "B는 A 시작점 이내"),
            rule("Z02", x[3] > x[1], "C는 A 끝을 넘어 진행")]


def _fit(value, ratios):
    return max(0, 1 - min(abs(value - r) / max(r, .1) for r in ratios))


def _subdivision(start, end, fine, expected, asof):
    inside = [p for p in fine if start.time < p.time < end.time and p.confirmed_at <= asof]
    points = [start] + inside + [end]
    if expected == "5":
        checks = impulse_rules(points) if len(points) == 6 else []
        ok = len(points) == 6 and valid(checks)
    else:
        checks = zigzag_rules(points) if len(points) == 4 else []
        ok = len(points) == 4 and valid(checks)
    return {"start": start.time, "end": end.time, "expected": expected,
            "observed_legs": len(points) - 1, "confirmed": ok,
            "rules": checks, "pivots": points}


def _prove(pivots, lower_candles, scale, asof, parent_duration, adaptive=False, fine_cache=None):
    lower_candles = [c for c in lower_candles if c.end <= asof]
    fine_cache = {} if fine_cache is None else fine_cache
    def fine_at(reversal):
        if reversal not in fine_cache:
            fine_cache[reversal] = confirmed_pivots(lower_candles, reversal)
        return fine_cache[reversal]
    refined = []
    for pivot in pivots:
        matches = [c for c in lower_candles if pivot.time <= c.time < pivot.time + parent_duration
                   and abs((c.high if pivot.kind == "H" else c.low) - pivot.price) <= max(1e-8, pivot.price * 1e-10)]
        if not matches:
            return [{"confirmed": False, "coverage_missing": True, "start": pivot.time, "end": pivot.time,
                     "expected": "subdivision", "observed_legs": 0, "rules": [], "pivots": []}]
        refined.append(Pivot(matches[0].time, pivot.price, pivot.kind, pivot.confirmed_at))
    pivots = refined
    expected = ["5", "3", "5", "3", "5"][:len(pivots) - 1]
    if adaptive:
        # Fixed geometric scale grid, selected by structure only, never future P/L.
        scans = [(max(.0005, scale / factor), fine_at(max(.0005, scale / factor)))
                 for factor in (1.5, 2, 3, 4, 5, 8, 12)]
        best = []
        for a, b, e in zip(pivots, pivots[1:], expected):
            variants = [{**_subdivision(a, b, fine, e, asof), "reversal": reversal,
                         "method": "per_leg_fixed_grid"} for reversal, fine in scans]
            best.append(max(variants, key=lambda s: (s["confirmed"], -abs(s["observed_legs"] - int(e)))))
        if not lower_candles or lower_candles[0].time > pivots[0].time:
            for item in best:
                item.update(confirmed=False, coverage_missing=True)
        return best
    best = []
    for factor in (3, 5, 8):
        fine = fine_at(max(.0005, scale / factor))
        proof = [_subdivision(a, b, fine, e, asof) for a, b, e in zip(pivots, pivots[1:], expected)]
        if not best or sum(s["confirmed"] for s in proof) > sum(s["confirmed"] for s in best):
            best = proof
        if best and all(s["confirmed"] for s in best):
            break
    if not best:
        best = [_subdivision(a, b, [], e, asof) for a, b, e in zip(pivots, pivots[1:], expected)]
    coverage = bool(lower_candles and lower_candles[0].time <= pivots[0].time)
    if not coverage:
        for item in best:
            item["confirmed"] = False
            item["coverage_missing"] = True
    return best


def candidates(timeframe, candles, lower_candles=None, *, scales=None, adaptive=False):
    if len(candles) < 12:
        return [], []
    lower = lower_candles if lower_candles is not None else candles
    asof = candles[-1].end
    all_candidates, rejected = {}, []
    fine_cache = {}
    for scale in SCALES[timeframe] if scales is None else scales:
        pivots = confirmed_pivots(candles, scale)
        for count, pattern in ((3, "WAVE_3"), (5, "WAVE_5"), (6, "IMPULSE_COMPLETE")):
            if len(pivots) < count:
                continue
            p = pivots[-count:]
            checks = impulse_rules(p)
            if not valid(checks):
                rejected.append({"pattern": pattern, "scale": scale, "rules": checks})
                continue
            d = 1 if p[0].kind == "L" else -1
            length1 = abs(p[1].price - p[0].price)
            retrace2 = abs(p[2].price - p[1].price) / length1
            proof = _prove(p, lower, scale, asof, candles[0].end - candles[0].time + 1, adaptive, fine_cache)
            verified = bool(proof and all(s["confirmed"] for s in proof))
            stop = p[-1].price
            if pattern == "WAVE_3":
                target = p[2].price + d * 1.618 * length1
                inv = p[0].price
                score = 50 + 14 * _fit(retrace2, [.5, .618, .786]) + (25 if verified else 0)
            elif pattern == "WAVE_5":
                length3 = abs(p[3].price - p[2].price)
                retrace4 = abs(p[4].price - p[3].price) / length3
                fifth = min(length1, length3) if length3 < length1 else length1
                target = p[4].price + d * fifth
                inv = p[1].price
                score = 51 + 9 * _fit(retrace4, [.236, .382]) + (5 if abs(retrace4 - retrace2) >= .15 else 0) + (25 if verified else 0)
            else:
                d = -d
                inv = stop = p[-1].price
                target = p[4].price
                score = 58 + (25 if verified else 0)
            tail = [b for b in candles if b.time > p[-1].confirmed_at]
            invalid = any(b.low <= stop if d > 0 else b.high >= stop for b in tail)
            if invalid:
                rejected.append({"pattern": pattern, "scale": scale, "rules": [rule("T01", False, "최종 피벗 이후 무효화 가격 침범")]})
                continue
            checks.append(rule("S01", verified, "하위 스윙의 5-3 / 5-3-5-3 구조 검증; 미확인은 주문 제외"))
            c = Candidate(timeframe, pattern, d, scale, p, round(score, 2), inv, stop, target, checks, proof,
                          verified and pattern in {"WAVE_3", "WAVE_5"})
            if c.id not in all_candidates or c.score > all_candidates[c.id].score:
                all_candidates[c.id] = c
        if len(pivots) >= 4:
            p = pivots[-4:]
            checks = zigzag_rules(p)
            if valid(checks):
                d = -1 if p[0].kind == "L" else 1
                a, c_len = abs(p[1].price - p[0].price), abs(p[3].price - p[2].price)
                # A/B/C require 5/3/5, not just three arbitrary swings.
                proof = _prove(p, lower, scale, asof, candles[0].end - candles[0].time + 1, adaptive, fine_cache)
                verified = all(s["confirmed"] for s in proof)
                tail = [b for b in candles if b.time > p[-1].confirmed_at]
                if not any(b.low <= p[-1].price if d > 0 else b.high >= p[-1].price for b in tail):
                    c = Candidate(timeframe, "ZIGZAG_COMPLETE", d, scale, p,
                                  round(55 + 12 * _fit(c_len / a, [.618, 1, 1.618]) + (20 if verified else 0), 2),
                                  p[-1].price, p[-1].price, p[0].price, checks, proof, False)
                    all_candidates[c.id] = c
        # Overlapping motive and sideways structures remain explicit alternatives.
        if len(pivots) >= 6:
            p = pivots[-6:]
            legs = [abs(b.price - a.price) for a, b in zip(p, p[1:])]
            d = 1 if p[0].kind == "L" else -1
            x = [v.price * d for v in p]
            diagonal = x[0] < x[2] < x[1] and x[2] < x[4] < x[1] and x[3] > x[1] and x[5] > x[3] and legs[2] >= min(legs[0], legs[4])
            triangle = all(legs[i + 1] < legs[i] for i in range(4))
            if diagonal or triangle:
                c = Candidate(timeframe, "DIAGONAL_WATCH" if diagonal else "TRIANGLE_WATCH", 0, scale, p,
                              62, 0, 0, 0, [rule("X01", False, "위치·세부 파동의 확정 불가: 신규 주문 제외")])
                all_candidates[c.id] = c
        if len(pivots) >= 4:
            p = pivots[-4:]
            a = abs(p[1].price - p[0].price)
            b = abs(p[2].price - p[1].price)
            if a and b / a >= .9 and not valid(zigzag_rules(p)):
                c = Candidate(timeframe, "FLAT_OR_COMPLEX_WATCH", 0, scale, p, 60, 0, 0, 0,
                              [rule("X02", False, "플랫·복합 조정 대안: 위치·3-3-5 미확정")])
                all_candidates[c.id] = c
    return sorted(all_candidates.values(), key=lambda c: (-c.score, c.id)), rejected[-12:]

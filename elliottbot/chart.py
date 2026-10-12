"""Read-only chart data, isolated from the execution worker and its decisions."""
from copy import deepcopy
from dataclasses import asdict
import threading
import time

from .config import ENDPOINTS, SCALES
from .market import public, validate
from .models import ALL_INTERVALS, Candle
from .strategy import choose
from .waves import candidates, confirmed_pivots


CHART_INTERVALS = ALL_INTERVALS
REFERENCE_SCALES = (.003, .005)
ROLES = {"1w": "상위 방향 거부권", "1d": "방향 정합성", "4h": "3파·5파 진입 구조",
         "1h": "종가 돌파 확인", "30m": "참고 전용 · 주문 판단 제외", "15m": "참고 전용 · 주문 판단 제외"}


class ChartReader:
    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.cache = {}

    def get(self, timeframe):
        if timeframe not in CHART_INTERVALS:
            raise ValueError("지원하지 않는 차트 시간봉입니다.")
        # Network and reference-wave work must never hold the execution lock.
        with self.service.lock:
            s = self.service
            generation, mode, sample = s.generation, s.mode, s.sample
            bars = list(s.frames.get(timeframe, []))
            decision = deepcopy(s.decision or {})
            gap = s.settings.conflict_gap
            active = s.settings.strategy_mode == "active"
            multi = s.settings.strategy_mode in {"multi", "active"}
        key = (generation, timeframe, decision.get("id"))
        with self.lock:
            if key in self.cache and time.monotonic() - self.cache[key][0] < 15:
                return deepcopy(self.cache[key][1])
            step = CHART_INTERVALS[timeframe]
            reference = timeframe == "15m" or (timeframe == "30m" and not active)
            frame = decision.get("frames", {}).get(timeframe)
            forming, warning, server_ms = None, None, None
            fetched_at = None
            if not sample:
                try:
                    base = ENDPOINTS[mode]
                    server_ms = int(public(base, "/fapi/v1/time")["serverTime"])
                    if abs(server_ms - time.time() * 1000) > 5000:
                        raise ValueError("PC와 거래소 시각 차이가 5초를 넘습니다.")
                    raw = public(base, "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": timeframe,
                                                         "limit": 1500 if reference or not bars else 3})
                    received = validate([Candle(int(r[0]), int(r[6]), *map(float, r[1:6])) for r in raw], timeframe, step)
                    if not received or not received[-1].time <= server_ms <= received[-1].end + step:
                        raise ValueError("최신 차트 봉이 지연되었습니다.")
                    merged = {b.time: b for b in bars}
                    merged.update({b.time: b for b in received if b.end < server_ms})
                    bars = validate(sorted(merged.values(), key=lambda b: b.time)[-1500:], timeframe, step)
                    forming = next((b for b in received if b.time <= server_ms <= b.end), None)
                    fetched_at = time.time()
                except Exception as exc:
                    warning = f"참고 차트 갱신 실패: {exc}"
            elif reference and not bars:
                warning = f"합성 모드에는 {step // 60000}분봉 원본이 없습니다. 해당 단기 전략 또는 실제 시세 모드에서 조회 가능합니다."
            bars = bars[-1500:]
            if reference and bars:
                cs, rejects = candidates(timeframe, bars, scales=SCALES[timeframe])
                pick, state = choose(cs, gap)
                frame = {"timeframe": timeframe, "state": state, "bias": pick.direction if pick else 0,
                         "selected": pick.id if pick else None, "candidates": [c.to_json() for c in cs],
                         "rejected": rejects, "closed_at": bars[-1].end, "candle_count": len(bars)}
                for item in frame["candidates"]:
                    item["eligible"] = False
            scales = SCALES[timeframe]
            role = ROLES[timeframe]
            if multi:
                role = {"1w": "상위 파동 맥락", "1d": "4시간봉 상위 맥락", "4h": "진입 후보 / 1시간봉 상위 맥락",
                        "1h": "진입 후보 / 4시간봉 돌파 확인", "30m": "1시간봉 내부 구조·돌파 확인; 독립 주문 없음",
                        "15m": ROLES["15m"]}[timeframe]
            if active and timeframe in {"30m", "15m"}:
                role = "진입 후보 / 1시간봉 돌파 확인" if timeframe == "30m" else "30분봉 내부 구조·돌파 확인; 독립 주문 없음"
            payload = {"mode": mode, "sample": sample, "timeframe": timeframe, "interval": step,
                       "role": role,
                       "reference_only": reference and not (active or (multi and timeframe == "30m")), "candles": [asdict(b) for b in bars],
                       "forming": asdict(forming) if forming else None, "frame": frame,
                       "analysis_asof": bars[-1].end if reference and bars else decision.get("asof"),
                       "closed_at": bars[-1].end if bars else None, "server_time": server_ms,
                       "fetched_at": fetched_at, "warning": warning, "swing_scale": scales[0],
                       "swings": [asdict(p) for p in confirmed_pivots(bars, scales[0])],
                       "source": "합성 재생" if sample else "Binance DEMO 계약 체결가" if mode == "testnet" else "Binance USD-M 계약 체결가"}
            with self.service.lock:
                if self.service.generation != generation:
                    raise ValueError("모드가 변경되었습니다. 차트를 다시 조회하세요.")
            self.cache = {k: v for k, v in self.cache.items() if k[0] == generation and k[1] != timeframe}
            self.cache[key] = (time.monotonic(), deepcopy(payload))
            return payload

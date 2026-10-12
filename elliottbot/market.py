from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import json
import math
from pathlib import Path
import time

from kgridbot.exchange.binance import _request_json
from .models import Candle, INTERVALS, ALL_INTERVALS, MULTI_INTERVALS


def public(base, path, params=None):
    from urllib.parse import urlencode
    return _request_json("GET", base + path + ("?" + urlencode(params) if params else ""), {})


def validate(candles, tf, step=None):
    step = ALL_INTERVALS[tf] if step is None else step
    for i, c in enumerate(candles):
        if not all(math.isfinite(v) and v > 0 for v in (c.open, c.high, c.low, c.close)):
            raise ValueError("유효하지 않은 가격 데이터")
        if not math.isfinite(c.volume) or c.volume < 0:
            raise ValueError("유효하지 않은 거래량 데이터")
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            raise ValueError("OHLC 가격 관계 오류")
        if c.end != c.time + step - 1 or (i and c.time - candles[i - 1].time != step):
            raise ValueError(f"{tf} 캔들 중복·누락 또는 시간 간격 오류")
    return candles


def market_rules(filters):
    filters = {f["filterType"]: f for f in filters}
    lots = [filters["LOT_SIZE"], filters["MARKET_LOT_SIZE"]]
    steps = [Decimal(f["stepSize"]) for f in lots if Decimal(f["stepSize"]) > 0]
    if not steps:
        raise ValueError("거래소 수량 단위가 없습니다.")
    scale = 10 ** max(0, -min(s.as_tuple().exponent for s in steps))
    step = Decimal(math.lcm(*(int(s * scale) for s in steps))) / scale
    minimum = max(Decimal(f["minQty"]) for f in lots)
    maximum = min(Decimal(f["maxQty"]) for f in lots if Decimal(f["maxQty"]) > 0)
    tick = Decimal(filters["PRICE_FILTER"]["tickSize"])
    notional = Decimal(filters["MIN_NOTIONAL"]["notional"])
    if minimum < 0 or maximum < minimum or tick <= 0 or notional <= 0:
        raise ValueError("거래소 주문 필터가 유효하지 않습니다.")
    return {"tick": str(tick), "step": str(step), "min_qty": str(minimum),
            "max_qty": str(maximum), "min_notional": str(notional)}


class Market:
    def __init__(self, base="https://fapi.binance.com", cache=None):
        self.base, self.cache = base, Path(cache) if cache else None
        self.frames = {}
        self.last_fetch = 0
        self.rules = None

    def load(self, force=False, intraday=False, active=False):
        server_ms = int(public(self.base, "/fapi/v1/time")["serverTime"])
        if abs(server_ms - time.time() * 1000) > 5000:
            raise ValueError("PC와 거래소 시각 차이가 5초를 넘습니다. 시간 동기화가 필요합니다.")
        intraday = intraday or active
        refresh_tf = "15m" if active else "30m" if intraday else "1h"
        refresh_step = ALL_INTERVALS[refresh_tf]
        needs = force or refresh_tf not in self.frames or server_ms // refresh_step != self.last_fetch // refresh_step
        if needs:
            new = {}
            periods = [("1w", 350), ("1d", 750), ("4h", 1000), ("1h", 1500)]
            if intraday:
                periods.append(("30m", 1500))
            if active:
                periods.append(("15m", 1500))
            for tf, limit in periods:
                raw = public(self.base, "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": tf, "limit": limit})
                new[tf] = validate([Candle(int(r[0]), int(r[6]), *map(float, r[1:6])) for r in raw if int(r[6]) < server_ms], tf)
            self.frames, self.last_fetch = new, server_ms
            if self.cache:
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache.with_suffix(".tmp")
                tmp.write_text(json.dumps({tf: [asdict(c) for c in cs] for tf, cs in new.items()}))
                tmp.replace(self.cache)
        premium = public(self.base, "/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
        book = public(self.base, "/fapi/v1/ticker/bookTicker", {"symbol": "BTCUSDT"})
        now = int(time.time() * 1000)
        if not -5000 <= now - int(premium["time"]) <= 30_000:
            raise ValueError("거래소 마크 가격이 지연되었습니다.")
        if book.get("time") is not None and not -5000 <= now - int(book["time"]) <= 30_000:
            raise ValueError("거래소 호가가 지연되었습니다.")
        if self.rules is None or needs:
            info = public(self.base, "/fapi/v1/exchangeInfo")
            symbol = next(s for s in info["symbols"] if s["symbol"] == "BTCUSDT")
            if symbol["status"] != "TRADING" or symbol["marginAsset"] != "USDT":
                raise ValueError("BTCUSDT 거래 불가")
            self.rules = market_rules(symbol["filters"])
        quote = {"price": float(premium["markPrice"]), "bid": float(book["bidPrice"]), "ask": float(book["askPrice"]),
                 "funding_rate": float(premium["lastFundingRate"]), "next_funding": int(premium["nextFundingTime"]),
                 "time": int(premium["time"]), "source": "Binance DEMO 가상 시장" if "demo-" in self.base else "Binance USD-M 공개 실시간", "rules": self.rules}
        if (not all(math.isfinite(quote[k]) and quote[k] > 0 for k in ("price", "bid", "ask"))
                or quote["bid"] > quote["ask"] or not math.isfinite(quote["funding_rate"])):
            raise ValueError("유효하지 않은 거래소 가격·호가·펀딩 데이터")
        return self.frames, quote, server_ms

    def funding(self, start, end):
        rows = public(self.base, "/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "startTime": start, "endTime": end, "limit": 1000})
        if len(rows) == 1000:
            raise ValueError("펀딩 기록이 조회 한도를 넘었습니다. 신규 진입 중단")
        return rows


def aggregate(hours, tf):
    step = ALL_INTERVALS[tf]
    hour = hours[0].end - hours[0].time + 1 if hours else INTERVALS["1h"]
    offset = 345_600_000 if tf == "1w" else 0  # Monday 00:00 UTC.
    groups = {}
    for c in hours:
        bucket = (c.time - offset) // step * step + offset
        groups.setdefault(bucket, []).append(c)
    return [Candle(t, t + step - 1, cs[0].open, max(c.high for c in cs), min(c.low for c in cs), cs[-1].close, sum(c.volume for c in cs))
            for t, cs in sorted(groups.items()) if len(cs) == step // hour and cs[0].time == t and cs[-1].end == t + step - 1]


def sample_frames(count=6000, intraday=False, active=False):
    """Deterministic synthetic history for UI/offline checks, never a live fallback."""
    intraday = intraday or active
    hour = ALL_INTERVALS["15m"] if active else ALL_INTERVALS["30m"] if intraday else INTERVALS["1h"]
    factor = INTERVALS["1h"] // hour
    start = 1_735_689_600_000  # 2025-01-01 UTC.
    candles = []
    previous = 75000
    for i in range(count * factor):
        t = i / factor
        close = 75000 + 9500 * math.sin(t / 610) + 2800 * math.sin(t / 93) + 920 * math.sin(t / 19) + 230 * math.sin(t / 3.4)
        candles.append(Candle(start + i * hour, start + (i + 1) * hour - 1, previous,
                              max(previous, close) + 45, min(previous, close) - 45, close, 100))
        previous = close
    return {tf: aggregate(candles, tf) if ALL_INTERVALS[tf] != hour else candles
            for tf in (ALL_INTERVALS if active else MULTI_INTERVALS if intraday else INTERVALS)}

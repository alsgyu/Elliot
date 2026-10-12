from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json


INTERVALS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
MULTI_INTERVALS = {**INTERVALS, "30m": 1_800_000}
ALL_INTERVALS = {**MULTI_INTERVALS, "15m": 900_000}


def decision_timeframe(mode):
    return {"strict": "1h", "multi": "30m", "active": "15m"}[mode]


def strategy_intervals(mode):
    return {"strict": INTERVALS, "multi": MULTI_INTERVALS, "active": ALL_INTERVALS}[mode]


@dataclass(frozen=True)
class Candle:
    time: int
    end: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


@dataclass(frozen=True)
class Pivot:
    time: int
    price: float
    kind: str
    confirmed_at: int


@dataclass
class Candidate:
    timeframe: str
    pattern: str
    direction: int
    scale: float
    pivots: list[Pivot]
    score: float
    invalidation: float
    stop: float
    target: float
    rules: list[dict] = field(default_factory=list)
    subdivisions: list[dict] = field(default_factory=list)
    eligible: bool = False

    @property
    def id(self):
        return digest({"tf": self.timeframe, "pattern": self.pattern, "pivots": self.pivots})[:20]

    def to_json(self):
        observed = (1 if self.pivots[0].kind == "L" else -1) if self.pivots else 0
        if self.pattern in {"DIAGONAL_WATCH", "TRIANGLE_WATCH", "FLAT_OR_COMPLEX_WATCH"}:
            observed = 0
        return {**asdict(self), "id": self.id,
                "observed_direction": observed, "observed_direction_name": "상승" if observed > 0 else "하락" if observed < 0 else "미확정",
                "direction_name": "LONG" if self.direction > 0 else "SHORT" if self.direction < 0 else "NEUTRAL"}


def encode(value):
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, default=encode, sort_keys=True, allow_nan=False).encode()).hexdigest()

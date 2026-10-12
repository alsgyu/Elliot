from dataclasses import asdict, dataclass, replace
import math


ENDPOINTS = {"paper": "https://fapi.binance.com", "testnet": "https://demo-fapi.binance.com", "live": "https://fapi.binance.com"}
SCALES = {"1w": (0.08, 0.13), "1d": (0.035, 0.055), "4h": (0.012, 0.02), "1h": (0.0045, 0.008),
          "30m": (.003, .005), "15m": (.0015, .0025)}
STRATEGY_VERSION = "ew-causal-1.0"
MULTI_VERSION = "ew-multiscenario-2.0"
ACTIVE_VERSION = "ew-intraday-2.1"
RESEARCH_VERSION = "ew-pivot-research-1.2"
INTRADAY_SCALES = SCALES["30m"]
MIN_LEVERAGE, MAX_LEVERAGE = 1, 10


def validated_leverage(value):
    message = f"레버리지는 {MIN_LEVERAGE}~{MAX_LEVERAGE}배 정수만 허용합니다."
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not MIN_LEVERAGE <= number <= MAX_LEVERAGE:
        raise ValueError(message)
    return int(number)


@dataclass(frozen=True)
class Settings:
    strategy_mode: str = "strict"
    capital: float = 1000
    leverage: int = 1
    risk_percent: float = 0.5
    daily_loss_percent: float = 2
    total_loss_percent: float = 10
    max_notional: float = 900
    min_rr: float = 1.5
    min_score: float = 70
    conflict_gap: float = 12
    allow_short: bool = True
    fee_bps: float = 5
    slippage_bps: float = 5
    max_spread_bps: float = 15
    max_entries_per_day: int = 4
    cooldown_minutes: int = 60
    loss_streak_limit: int = 3
    loss_pause_minutes: int = 360
    peak_drawdown_percent: float = 5
    max_entry_drift_r: float = .25
    max_cost_fraction: float = .25
    max_stop_percent: float = 1.0
    research_mode: bool = False
    research_min_score: float = 60
    research_risk_multiplier: float = .5

    def to_json(self):
        return asdict(self)

    def updated(self, values):
        if not isinstance(values, dict) or set(values) - set(asdict(self)):
            raise ValueError("알 수 없는 설정입니다.")
        converted = {}
        for key, value in values.items():
            if key == "strategy_mode":
                if not isinstance(value, str) or value not in {"strict", "multi", "active"}:
                    raise ValueError("전략은 strict, multi 또는 active여야 합니다.")
                converted[key] = value
            elif key in {"allow_short", "research_mode"}:
                if not isinstance(value, bool):
                    raise ValueError(f"{key}: true/false 값이어야 합니다.")
                converted[key] = value
            else:
                if isinstance(value, bool):
                    raise ValueError("숫자 설정에 true/false를 사용할 수 없습니다.")
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("유한한 숫자를 입력하세요.")
                converted[key] = number
        new = replace(self, **converted)
        if new.research_mode and new.strategy_mode != "active":
            raise ValueError("연구 모드는 단기 전략(active)에서만 사용할 수 있습니다.")
        leverage = validated_leverage(new.leverage)
        limits = {"capital": (50, 10_000_000), "risk_percent": (0.05, 2),
                  "daily_loss_percent": (0.1, 10), "total_loss_percent": (0.1, 30),
                  "max_notional": (50, 50_000_000), "min_rr": (1, 10),
                  "min_score": (60, 100), "conflict_gap": (5, 40),
                  "fee_bps": (0, 100), "slippage_bps": (0, 100), "max_spread_bps": (1, 100),
                  "max_entries_per_day": (1, 20), "cooldown_minutes": (15, 1440),
                  "loss_streak_limit": (1, 10), "loss_pause_minutes": (60, 10080),
                  "peak_drawdown_percent": (.5, 20), "max_entry_drift_r": (.05, .5), "max_cost_fraction": (.05, .4),
                  "max_stop_percent": (.1, 5), "research_min_score": (55, 100), "research_risk_multiplier": (.05, .5)}
        for key, (low, high) in limits.items():
            if not low <= getattr(new, key) <= high:
                raise ValueError(f"{key}: {low}~{high} 범위여야 합니다.")
        if new.daily_loss_percent > new.total_loss_percent:
            raise ValueError("일일 손실 한도는 누적 손실 한도보다 클 수 없습니다.")
        for key in ("max_entries_per_day", "cooldown_minutes", "loss_streak_limit", "loss_pause_minutes"):
            if int(getattr(new, key)) != getattr(new, key):
                raise ValueError(f"{key}: 정수여야 합니다.")
        return replace(new, leverage=leverage)

"""Local-only QuantStats calculations. No ticker downloads or external analytics API."""
from __future__ import annotations

import math
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def availability() -> dict:
    try:
        return {"available": True, "library": "QuantStats", "version": version("quantstats")}
    except PackageNotFoundError:
        return {"available": False, "library": "QuantStats", "version": None,
                "reason": "분석 패키지 미설치: ./start.sh로 실행해 주세요."}


def performance(samples: list[dict], initial_capital: float) -> dict:
    result = {**availability(), "samples": len(samples), "max_drawdown": None,
              "total_return": None, "sharpe": None, "volatility": None, "daily_samples": 0,
              "note": "저장 표본 기준 낙폭입니다. 샤프·변동성은 서울 기준 완료된 일별 수익률 30개부터 계산합니다."}
    if not result["available"] or len(samples) < 2:
        return result
    cache = Path(__file__).resolve().parent.parent / "data" / "matplotlib"
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache.parent / "cache"))
    import numpy as np
    import pandas as pd
    import quantstats as qs

    equity = np.array([float(s["equity"]) for s in samples])
    capital = np.array([float(s.get("capital", initial_capital)) or initial_capital for s in samples])
    if not np.isfinite(equity).all() or np.any(equity <= 0) or initial_capital <= 0:
        return {**result, "note": "자산이 0 이하이거나 유효하지 않아 수익률 지표를 계산하지 않습니다."}
    # Contributions/withdrawals are not investment returns.
    values = np.concatenate(([(equity[0] - capital[0]) / capital[0]],
                             (np.diff(equity) - np.diff(capital)) / equity[:-1]))
    index = pd.to_datetime([s["timestamp"] for s in samples], unit="s", utc=True).tz_convert("Asia/Seoul")
    returns = pd.Series(values, index=index)
    result["total_return"] = _finite(qs.stats.comp(returns))
    result["max_drawdown"] = _finite(qs.stats.max_drawdown(returns))
    wealth = (1 + returns).cumprod()
    daily = wealth.resample("D").last().dropna()
    daily = daily[daily.index < pd.Timestamp.now(tz="Asia/Seoul").normalize()].pct_change().dropna()
    result["daily_samples"] = len(daily)
    if len(daily) >= 30 and daily.std() > 0:
        result["sharpe"] = _finite(qs.stats.sharpe(daily, periods=365, rf=0))
        result["volatility"] = _finite(qs.stats.volatility(daily, periods=365))
    return result


def _finite(value):
    return float(value) if math.isfinite(float(value)) else None

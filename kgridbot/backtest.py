from __future__ import annotations

import argparse
import json
import math
import tempfile
from dataclasses import replace
from decimal import Decimal
from datetime import datetime
from pathlib import Path

from .analytics import performance
from .config import RuntimeConfig, load_config
from .engine import TradingEngine
from .exchange.paper import PaperExchange
from .models import Mode
from .store import Store
from .risk import SEOUL


def run_backtest(config: RuntimeConfig) -> dict:
    mid = (config.grid.lower_price + config.grid.upper_price) / 2
    span = config.grid.upper_price - config.grid.lower_price
    scenarios = {
        "range": [mid + span * Decimal(str(math.sin(i / 6))) * Decimal("0.45") for i in range(96)],
        "crash": [mid - span * Decimal(i) / 55 for i in range(96)],
        "uptrend": [mid + span * Decimal(i) / 55 for i in range(96)],
    }
    return {
        "dataset": "synthetic-validation-v2", "optimized": False,
        "assumptions": [
            "합성 검증 시나리오입니다. 과거 시장 수익률이나 최적화 결과가 아닙니다.",
            "한 단계는 1시간으로 가정합니다. 대기열·호가 깊이·봉 내부 체결 순서는 재현하지 않습니다.",
            "지정가 교차 시 부분 체결, 시장가에는 설정한 슬리피지와 테이커 수수료를 적용합니다.",
            "펀딩은 별도 합성 정산 이벤트(서로 다른 시각과 양수·음수 요율)로 실제 자산에서 가감합니다.",
            "같은 그리드 설정을 모든 구간에 적용하며 이 데이터로 파라미터를 최적화하지 않습니다.",
        ],
        "results": {name: _run_scenario(config, name, prices) for name, prices in scenarios.items()},
    }


def _run_scenario(config: RuntimeConfig, name: str, prices: list[Decimal]) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"kgridbot-{name}-") as td:
        cfg = replace(config, mode=Mode.PAPER, database_path=Path(td) / "scenario.sqlite3")
        store = Store(cfg.database_path)
        try:
            exchange = PaperExchange(store, cfg.market_rules, prices[0], price_script=prices,
                                     slippage_bps=cfg.grid.slippage_bps)
            engine = TradingEngine(cfg, store, exchange)
            samples = [{"timestamp": 1704067200, "price": str(prices[0]),
                        "equity": str(cfg.grid.allocated_margin), "capital": str(cfg.grid.allocated_margin)}]
            funding_events = {8: Decimal("0.0001"), 16: Decimal("0.00015"), 20: Decimal("-0.00005"),
                              28: Decimal("0.0002"), 40: Decimal("0.0001"), 56: Decimal("-0.0001"), 80: Decimal("0.0002")}
            for i, price in enumerate(prices):
                timestamp = 1704067200 + (i + 1) * 3600
                engine.risk.clock = lambda timestamp=timestamp: datetime.fromtimestamp(timestamp, SEOUL)
                engine.tick()
                if i in funding_events:
                    exchange.settle_funding(f"{name}-{i}", funding_events[i], price)
                pnl = engine.risk.pnl_snapshot(exchange.position(), price)
                samples.append({"timestamp": 1704067200 + (i + 1) * 3600, "price": str(price),
                                "equity": str(cfg.grid.allocated_margin + pnl.net_pnl),
                                "capital": str(cfg.grid.allocated_margin)})
            status = engine.status()
            return {"final_price": str(prices[-1]), "flags": status["flags"], "position": status["position"],
                    "pnl": status["pnl"], "fill_count": len(store.list_fills(100000)),
                    "open_order_count": len(exchange.open_orders()), "analytics": performance(samples, float(cfg.grid.allocated_margin)),
                    "equity_curve": samples, "funding_event_count": len(funding_events)}
        finally:
            store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run fixed paper validation scenarios")
    parser.add_argument("--config", default="configs/paper.example.json")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    report = run_backtest(load_config(args.config))
    body = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(body + "\n", encoding="utf-8")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

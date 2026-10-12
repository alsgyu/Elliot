"""Frozen-rule, chronological public-data evaluation; no private API or broker."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elliottbot.backtest import run
from elliottbot.config import RESEARCH_VERSION, Settings
from elliottbot.market import Market, public, validate
from elliottbot.models import ALL_INTERVALS, Candle, digest


LIMITS = {"1w": 350, "1d": 750, "4h": 1000, "1h": 1500, "30m": 1500, "15m": 1500}


def fetch_history(market, days):
    frames, quote, server = market.load(active=True)
    end = frames["15m"][-1].end
    start = end + 1 - (days + 32) * 86400000
    for tf in ("1h", "30m", "15m"):
        step = ALL_INTERVALS[tf]
        cursor, rows = start // step * step, []
        while cursor < end:
            raw = public(market.base, "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": tf,
                         "startTime": cursor, "endTime": end, "limit": 1500})
            if not raw:
                raise ValueError(f"Public history ended early: {tf}")
            page = [Candle(int(r[0]), int(r[6]), *map(float, r[1:6])) for r in raw if int(r[6]) <= end]
            if not page or page[-1].end + 1 <= cursor:
                raise ValueError(f"Non-advancing public history: {tf}")
            rows.extend(page)
            cursor = page[-1].end + 1
        frames[tf] = validate(rows, tf)
    funding = market.funding(end + 1 - days * 86400000, end)
    return {"frames": {tf: [asdict(c) for c in cs] for tf, cs in frames.items()},
            "rules": quote["rules"], "funding": funding, "server_time": server,
            "source": "Binance public BTCUSDT klines, exchangeInfo, fundingRate", "days": days}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("artifacts/elliott-research-20261005"))
    parser.add_argument("--inputs", type=Path, help="Reuse a frozen public dataset without network requests")
    args = parser.parse_args()
    if not 9 <= args.days <= 90 or args.days % 3:
        raise ValueError("Days must be a multiple of three, 9..90")
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / "public-inputs.json"
    if args.inputs or path.exists():
        data = json.loads((args.inputs or path).read_text())
        if data["days"] != args.days:
            raise ValueError("Do not overwrite a frozen dataset with a different period")
    else:
        data = fetch_history(Market(), args.days)
        path.write_text(json.dumps(data))
    frames = {tf: validate([Candle(**c) for c in cs], tf) for tf, cs in data["frames"].items()}
    settings = replace(Settings(), strategy_mode="active", max_entries_per_day=8, cooldown_minutes=15)
    manifest = {"version": RESEARCH_VERSION, "input_hash": digest(data), "settings": settings.to_json(),
                "fixed_grid_divisors": [1.5, 2, 3, 4, 5, 8, 12], "research_risk_cap": settings.research_risk_multiplier,
                "source_hashes": {name: digest((Path(__file__).resolve().parents[1]/"elliottbot"/name).read_text())
                                  for name in ("waves.py", "multiscenario.py", "execution.py", "backtest.py", "config.py")},
                "history_limits": LIMITS, "private_requests": 0, "live_orders": 0,
                "parameter_search": False, "forward_validation_complete": False}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"stage": "frozen_inputs", "hash": manifest["input_hash"], "days": args.days}), flush=True)
    end = frames["15m"][-1].end
    start = end + 1 - args.days * 86400000
    width = args.days // 3 * 86400000
    results = []
    for fold in range(3):
        first, last = start + fold * width, start + (fold + 1) * width - 1
        clipped = {tf: [c for c in cs if c.end <= last] for tf, cs in frames.items()}
        for label, research, cost in (("active", False, 1), ("research", True, 1), ("research_stress", True, 2)):
            config = replace(settings, research_mode=research, fee_bps=settings.fee_bps*cost,
                             slippage_bps=settings.slippage_bps*cost)
            report = run(clipped, config, data["rules"], data["funding"], start_time=first, history_limits=LIMITS)
            name = f"fold-{fold+1}-{label}"
            (args.output / f"{name}.json").write_text(json.dumps(report, ensure_ascii=False))
            item = {"fold": fold+1, "strategy": label, **{k: report[k] for k in
                    ("from", "to", "net", "return", "closed_trades", "open_position", "max_drawdown", "win_rate", "profit_factor", "entry_blocks", "wait_reasons")}}
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    summary = {**manifest, "results": results, "profitability_proven": False,
               "live_promotion_allowed": False,
               "verdict": "모의 연구 전용. 과거 결과와 관계없이 별도 전진 검증 없이는 실거래 전환 금지."}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    lines = ["# Elliott Research Evaluation", "", f"Version: {RESEARCH_VERSION}",
             f"Input SHA256: {manifest['input_hash']}", "", "| Fold | Strategy | From (UTC) | To (UTC) | Trades | Net USDT | Max DD |",
             "| --- | --- | --- | --- | ---: | ---: | ---: |"]
    for r in results:
        dates = [datetime.fromtimestamp(r[k]/1000, timezone.utc).isoformat() for k in ("from", "to")]
        lines.append(f"| {r['fold']} | {r['strategy']} | {dates[0]} | {dates[1]} | {r['closed_trades']} | {float(r['net']):.3f} | {r['max_drawdown']:.3%} |")
    lines += ["", "Each fold resets capital; no parameter selection between folds. Fixed rules before evaluation.",
              "Research risk multiplier <= 0.5; frequency and returns are not a matched-risk causal experiment.",
              "Exploratory development comparison, not an untouched holdout. Do not select parameters on these returns.",
              "Fees/slippage included; stress doubles both. Published funding included. Stop first on ambiguous bars.",
              "Next-bar market fill approximation, trade OHLC instead of mark OHLC, no queue/liquidation model.",
              "Open positions marked at final close, without future exit fee. No forced liquidation at fold boundary.",
              "Profitability NOT proven. No private API calls, live orders, automatic promotion, or live configuration changes."]
    (args.output / "REPORT.md").write_text("\n".join(lines)+"\n")


if __name__ == "__main__":
    main()

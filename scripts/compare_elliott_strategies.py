"""Read-only public-market comparison. Does not load credentials or a broker."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elliottbot.backtest import compare
from elliottbot.config import Settings
from elliottbot.market import Market
from elliottbot.models import digest
from elliottbot.strategy import decide


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=480)
    parser.add_argument("--active", action="store_true", help="Include 30m entries / 15m confirmation; use 15m common fills")
    args = parser.parse_args()
    if not 48 <= args.bars <= 960:
        raise ValueError("48..960 replay bars required")
    root = Path("artifacts/elliott-active-comparison" if args.active else "artifacts/elliott-comparison")
    root.mkdir(parents=True, exist_ok=True)
    market = Market(cache=root / "candles.json")
    frames, quote, server = market.load(intraday=True, active=args.active)
    clock = "15m" if args.active else "30m"
    funding = market.funding(frames[clock][-args.bars].time, frames[clock][-1].end)
    (root / "inputs.json").write_text(json.dumps({"rules": quote["rules"], "funding": funding,
                                                "server_time": server}, indent=2))
    print(json.dumps({"stage": "public_data_loaded", "bars": {k: len(v) for k, v in frames.items()} }), flush=True)
    settings = replace(Settings(), max_entries_per_day=8, cooldown_minutes=15) if args.active else Settings()
    report = compare(frames, settings, quote["rules"], funding, args.bars)
    report.update(server_time=server, data_hash=digest(frames), private_requests=0, live_orders=0,
                  source="Binance USD-M BTCUSDT public klines / fundingRate", funding_events=len(funding),
                  latest=decide(frames, frames[clock][-1].end, replace(settings, strategy_mode="active" if args.active else "multi")))
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"verdict": report["verdict"], "from": report["comparison"]["multi"]["from"],
               "to": report["comparison"]["multi"]["to"], "private_requests": 0, "live_orders": 0,
               "comparison": {k: {key: r[key] for key in ("return", "net", "max_drawdown", "closed_trades", "open_position", "entry_blocks")}
                              for k, r in report["comparison"].items()}}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

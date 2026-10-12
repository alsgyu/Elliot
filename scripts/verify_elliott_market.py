"""Read public prices and replay closed candles. Never uses private API keys."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elliottbot.backtest import run
from elliottbot.config import Settings
from elliottbot.market import Market
from elliottbot.strategy import decide


def main():
    out = Path("artifacts/elliott-public-check")
    out.mkdir(parents=True, exist_ok=True)
    market = Market(cache=out / "candles.json")
    frames, quote, server = market.load()
    settings = Settings()
    latest = decide(frames, frames["1h"][-1].end, settings)
    funding = market.funding(frames["1h"][-240].time, frames["1h"][-1].end)
    result = run(frames, settings, quote["rules"], funding=funding)
    report = {
        "verified_at": datetime.fromtimestamp(server / 1000, timezone.utc).isoformat(),
        "source": quote["source"], "mark_price": quote["price"], "market_rules": quote["rules"],
        "closed_bars": {tf: len(cs) for tf, cs in frames.items()},
        "decision": {key: latest[key] for key in ("action", "reason", "probability", "data_hash")},
        "settings": asdict(settings), "funding_events": len(funding),
        "replay": result, "private_requests": 0, "live_orders": 0,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**{k: v for k, v in report.items() if k != "replay"},
        "replay": {k: v for k, v in result.items() if k not in {"curve", "fills"}}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

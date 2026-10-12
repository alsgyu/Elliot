from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from elliottbot.backtest import compare, run
from elliottbot.chart import ChartReader
from elliottbot.config import ACTIVE_VERSION, SCALES, Settings
from elliottbot.execution import D, size_trade
from elliottbot.market import Market, sample_frames
from elliottbot.models import ALL_INTERVALS, Candle, Candidate, Pivot
from elliottbot.service import Service
from elliottbot.strategy import decide


RULES = {"tick": ".01", "step": ".001", "min_qty": ".001", "min_notional": "1"}


def setup(end, tf="30m", direction=1):
    prices = [117, 121, 119.1]
    if direction < 0:
        prices = [240-p for p in prices]
    step = ALL_INTERVALS[tf]
    pivots = [Pivot(end-(4-i)*step, p, ("L" if i % 2 == 0 else "H") if direction > 0 else
                    ("H" if i % 2 == 0 else "L"), end-step) for i, p in enumerate(prices)]
    return Candidate(tf, "WAVE_3", direction, SCALES[tf][0], pivots, 85, prices[0], prices[-1],
                     prices[-1]+direction*4*1.618, subdivisions=[{"confirmed": True}], eligible=True)


class ActiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = sample_frames(active=True)
        cls.end = raw["15m"][-1].end
        cls.frames = {tf: [replace(c, open=120, high=120.1, low=119.9, close=120) for c in cs] for tf, cs in raw.items()}

    def calculate(self, counts=None, frames=None, settings=None, trigger=True):
        counts = {"30m": [setup(self.end)]} if counts is None else counts
        def scan(tf, bars, lower, **kwargs):
            self.assertTrue(all(c.end <= self.end for c in bars+lower))
            self.assertEqual(kwargs["scales"], SCALES[tf])
            if tf == "30m" and lower:
                self.assertEqual(lower[-1].end-lower[-1].time+1, ALL_INTERVALS["15m"])
            return counts.get(tf, []), []
        def cross(c, bars, tf):
            return {"price": 119.8 if c.direction > 0 else 120.2, "bar_close": 120,
                    "bar_end": bars[-1].end, "timeframe": tf} if trigger else None
        with patch("elliottbot.multiscenario.candidates", side_effect=scan), patch("elliottbot.multiscenario.breakout", side_effect=cross):
            return decide(self.frames if frames is None else frames, self.end,
                          settings or replace(Settings(), strategy_mode="active"))

    def test_thirty_minute_long_short_with_fifteen_minute_confirmation(self):
        for direction in (1, -1):
            result = self.calculate({"30m": [setup(self.end, direction=direction)]})
            self.assertEqual(result["action"], "LONG" if direction > 0 else "SHORT")
            self.assertEqual(result["version"], ACTIVE_VERSION)
            self.assertEqual(result["selection"]["timeframe"], "30m")
            self.assertEqual(result["trigger"]["timeframe"], "15m")
            self.assertEqual(result["signal_expires_at"], self.end+900000)
            self.assertEqual(result["risk_multiplier"], .25)
            self.assertIsNone(result["probability"])

    def test_aligned_parent_caps_thirty_minute_budget_at_half(self):
        result = self.calculate({"30m": [setup(self.end)], "1h": [replace(setup(self.end, "1h"), eligible=False)]})
        self.assertEqual(result["action"], "LONG")
        self.assertEqual(result["risk_multiplier"], .5)

    def test_research_partial_count_is_explicit_and_risk_discounted(self):
        c = replace(setup(self.end), score=63, eligible=False,
                    subdivisions=[{"confirmed": True}, {"confirmed": False}])
        counts = {"30m": [c]}
        self.assertEqual(self.calculate(counts)["action"], "WAIT")
        research = replace(Settings(), strategy_mode="active", research_mode=True)
        result = self.calculate(counts, settings=research)
        self.assertEqual(result["action"], "LONG")
        self.assertEqual(result["proof_status"], "provisional")
        self.assertFalse(result["selection"]["eligible"])
        self.assertEqual(result["risk_multiplier"], .5)
        self.assertIn("잠정", result["reason"])
        self.assertEqual(self.calculate(counts, settings=replace(research,research_min_score=70))["action"], "WAIT")
        c.subdivisions = [{"confirmed": False}]
        self.assertEqual(self.calculate(counts, settings=research)["action"], "WAIT")

    def test_maximum_stop_distance_blocks_without_moving_stop(self):
        c = setup(self.end)
        c.stop = 117
        result = self.calculate({"30m": [c]})
        self.assertEqual(result["action"], "WAIT")
        row = result["scenarios"][0]
        self.assertFalse(next(v["passed"] for v in row["checks"] if v["id"] == "M11"))
        self.assertEqual(row["selection"]["stop"], 117)

    def test_arbitrarily_tight_stop_fails_cost_gate(self):
        c = setup(self.end)
        c.stop = 119.97
        result = self.calculate({"30m": [c]})
        self.assertEqual(result["action"], "WAIT")
        self.assertFalse(next(v["passed"] for v in result["scenarios"][0]["checks"] if v["id"] == "M08"))

    def test_proof_fresh_breakout_parent_and_conflicts_remain_required(self):
        c = setup(self.end)
        variants = [({"30m": [replace(c, subdivisions=[])]}, True),
                    ({"30m": [c]}, False),
                    ({"30m": [c], "1h": [replace(setup(self.end, "1h", -1), eligible=False)]}, True),
                    ({"30m": [c, setup(self.end, direction=-1)]}, True),
                    ({"15m": [setup(self.end, "15m")]}, True)]
        for counts, trigger in variants:
            self.assertEqual(self.calculate(counts, trigger=trigger)["action"], "WAIT")

    def test_missing_or_stale_fifteen_minute_data_blocks(self):
        for bars in ([], self.frames["15m"][:-3]):
            result = self.calculate(frames={**self.frames, "15m": bars})
            self.assertEqual(result["action"], "WAIT")
            self.assertIn("15분봉", result["reason"])

    def test_future_bars_do_not_change_count_or_decision(self):
        frames = {tf: list(cs) for tf, cs in self.frames.items()}
        frames["15m"].append(Candle(self.end+1, self.end+900000, 999, 999, 999, 999))
        self.assertEqual(self.calculate(), self.calculate(frames=frames))

    def test_execution_rechecks_cost_expiry_stop_and_risk_budget(self):
        settings = replace(Settings(), strategy_mode="active", max_notional=100000)
        decision = self.calculate(settings=settings)
        quote = {"bid": 120, "ask": 120, "price": 120, "time": self.end, "rules": RULES}
        plan = size_trade(decision, quote, settings, 1000, risk_cap=D(".8"))
        self.assertLessEqual(D(plan["risk_at_stop"]), D(".8"))
        self.assertEqual(D(plan["stop"]), D("119.1"))
        with self.assertRaisesRegex(ValueError, "유효 시간"):
            size_trade(decision, {**quote, "time": self.end+900000}, settings, 1000)
        changed = deepcopy(decision)
        changed["selection"]["stop"] = 117
        with self.assertRaisesRegex(ValueError, "손절 거리"):
            size_trade(changed, quote, settings, 1000)
        changed["selection"]["stop"] = 119.97
        with self.assertRaisesRegex(ValueError, "비용 비중"):
            size_trade(changed, quote, settings, 1000)
        with self.assertRaisesRegex(ValueError, "추격"):
            size_trade(decision, {**quote, "bid": 120.3, "ask": 120.3, "price": 120.3}, settings, 1000)

    def test_profile_validation_and_legacy_defaults(self):
        settings = Settings().updated({"strategy_mode": "active", "max_stop_percent": .5})
        self.assertEqual(settings.max_stop_percent, .5)
        self.assertEqual(Settings().updated({"capital": 736}).strategy_mode, "strict")
        for value in (0, .09, 5.1, True, float("nan")):
            with self.assertRaises(ValueError):
                Settings().updated({"max_stop_percent": value})

    def test_service_clock_and_chart_share_thirty_minute_engine_count(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root, sample=True)
            try:
                service.update_settings({"strategy_mode": "active"})
                service.tick()
                asof = service.decision["asof"]
                service.tick()
                self.assertEqual(service.decision["asof"]-asof, 900000)
                c = setup(service.decision["asof"])
                service.decision["frames"]["30m"]["candidates"] = [c.to_json()]
                reader = ChartReader(service)
                with patch("elliottbot.chart.candidates") as scan:
                    chart = reader.get("30m")
                scan.assert_not_called()
                self.assertTrue(chart["frame"]["candidates"][0]["eligible"])
                self.assertEqual(chart["frame"], service.decision["frames"]["30m"])
                self.assertFalse(chart["reference_only"])
                self.assertFalse(reader.get("15m")["reference_only"])
                self.assertFalse(service.account["running"])
            finally:
                service.close()

    def test_replay_decisions_only_at_each_profiles_closed_bar(self):
        # The first replay step also assesses the preceding closed higher bar.
        for mode, count in (("strict", 4), ("multi", 7), ("active", 12)):
            with patch("elliottbot.backtest.decide", return_value={"action": "WAIT"}) as decision:
                run(self.frames, replace(Settings(), strategy_mode=mode), RULES, bars=12)
            self.assertEqual(decision.call_count, count)
        with patch("elliottbot.backtest.decide", return_value={"action": "WAIT"}):
            report = compare(self.frames, Settings(), RULES, bars=12)
        self.assertEqual(len(report["comparison"]), 5)
        self.assertEqual(len({v["from"] for v in report["comparison"].values()}), 1)
        self.assertFalse(report["profitability_proven"])
        self.assertEqual(report["comparison"]["active_stress"]["settings"]["fee_bps"], 10)
        with self.assertRaisesRegex(ValueError, "15m"):
            run({tf: cs for tf, cs in self.frames.items() if tf != "15m"}, replace(Settings(), strategy_mode="active"), RULES)

    def test_public_fetch_includes_fifteen_minutes_and_refreshes_each_close(self):
        market, calls = Market(), []
        now = [self.end+1000]
        def get(base, path, params=None):
            if path.endswith("/time"):
                return {"serverTime": now[0]}
            if path.endswith("/klines"):
                tf = params["interval"]
                calls.append(tf)
                step = ALL_INTERVALS[tf]
                start = now[0]//step*step-24*step
                return [[start+i*step, 120, 121, 119, 120, 1, start+(i+1)*step-1] for i in range(25)]
            if path.endswith("/premiumIndex"):
                return {"markPrice": "120", "time": now[0], "lastFundingRate": "0", "nextFundingTime": now[0]+3600000}
            if path.endswith("/bookTicker"):
                return {"bidPrice": "119.99", "askPrice": "120.01", "time": now[0]}
            self.assertTrue(path.endswith("/exchangeInfo"))
            return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "marginAsset": "USDT", "filters": []}]}
        with patch("elliottbot.market.public", side_effect=get), patch("elliottbot.market.market_rules", return_value=RULES), patch("elliottbot.market.time.time", side_effect=lambda: now[0]/1000):
            frames, _, _ = market.load(active=True)
            self.assertEqual(set(frames), set(ALL_INTERVALS))
            self.assertTrue(all(c.end < now[0] for cs in frames.values() for c in cs))
            market.load(active=True)
            self.assertEqual(calls.count("15m"), 1)
            now[0] += 900000
            market.load(active=True)
            self.assertEqual(calls.count("15m"), 2)


if __name__ == "__main__":
    unittest.main()

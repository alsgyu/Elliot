from copy import deepcopy
from dataclasses import asdict
import tempfile
import unittest
from unittest.mock import Mock, patch

from elliottbot.chart import CHART_INTERVALS, ChartReader
from elliottbot.models import Candle, Candidate, INTERVALS, Pivot
from elliottbot.service import Service


def raw(c):
    return [c.time, c.open, c.high, c.low, c.close, c.volume, c.end]


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, sample=True)
        self.service.tick()
        self.reader = ChartReader(self.service)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def exchange(self, rows, now):
        def get(base, path, params=None):
            self.assertFalse(self.service.lock._is_owned())
            if path == "/fapi/v1/time":
                return {"serverTime": now}
            self.assertEqual(path, "/fapi/v1/klines")
            self.assertEqual(params["symbol"], "BTCUSDT")
            return [raw(c) for c in rows]
        return get

    def test_long_history_never_changes_trading_snapshot(self):
        before = self.service.status()
        chart = self.reader.get("4h")
        self.assertGreater(len(chart["candles"]), 180)
        self.assertLessEqual(len(chart["candles"]), 1500)
        self.assertEqual(chart["frame"], before["decision"]["frames"]["4h"])
        self.assertEqual(self.service.status(), before)
        self.assertIsNone(chart["forming"])
        self.assertFalse(chart["reference_only"])

    def test_cache_is_defensive_and_bounded(self):
        first = self.reader.get("1h")
        first["candles"].clear()
        second = self.reader.get("1h")
        self.assertEqual(len(second["candles"]), 1500)
        self.assertEqual(len(self.reader.cache), 1)

    def test_new_decision_invalidates_cache(self):
        first = self.reader.get("1h")
        self.service.tick()
        second = self.reader.get("1h")
        self.assertGreater(second["closed_at"], first["closed_at"])
        self.assertEqual(len(self.reader.cache), 1)

    def test_reference_interval_is_not_added_to_execution(self):
        self.assertIn("30m", CHART_INTERVALS)
        self.assertNotIn("30m", INTERVALS)
        chart = self.reader.get("30m")
        self.assertTrue(chart["reference_only"])
        self.assertEqual(chart["candles"], [])
        self.assertIn("원본이 없습니다", chart["warning"])
        self.assertNotIn("30m", self.service.decision["frames"])

    def test_forming_candle_not_in_wave_analysis_or_engine(self):
        self.service.sample = False
        previous = deepcopy(self.service.decision)
        last = self.service.frames["1h"][-1]
        step = INTERVALS["1h"]
        forming = Candle(last.time + step, last.end + step, last.close, last.close + 20, last.close - 20, last.close, 50)
        now = forming.time + 1000
        with patch("elliottbot.chart.time.time", return_value=now/1000), patch("elliottbot.chart.public", side_effect=self.exchange([last, forming], now)) as public:
            chart = self.reader.get("1h")
            self.reader.get("1h")
        self.assertEqual(public.call_count, 2)
        self.assertEqual(chart["forming"], asdict(forming))
        self.assertEqual(chart["closed_at"], last.end)
        self.assertTrue(all(p["confirmed_at"] <= last.end for p in chart["swings"]))
        self.assertEqual(self.service.decision, previous)
        self.assertEqual(self.service.frames["1h"][-1], last)

    def test_chart_failure_does_not_halt_engine_or_send_orders(self):
        self.service.sample = False
        self.service.account["running"] = True
        broker = self.service.broker = Mock()
        before = deepcopy(self.service.account)
        with patch("elliottbot.chart.public", side_effect=TimeoutError("offline")):
            chart = self.reader.get("4h")
        self.assertIn("offline", chart["warning"])
        self.assertGreater(len(chart["candles"]), 180)
        self.assertIsNone(chart["forming"])
        self.assertEqual(self.service.account, before)
        self.assertIsNone(self.service.error)
        self.assertEqual(broker.mock_calls, [])

    def test_reference_candidates_cannot_become_trade_signals(self):
        self.service.sample = False
        step = CHART_INTERVALS["30m"]
        rows = [Candle(1700001000000+i*step, 1700001000000+(i+1)*step-1, 100, 110, 90, 100, 5) for i in range(20)]
        now = rows[-1].time + 1000
        cs = [Candidate("30m", "WAVE_3", 1, .003, [Pivot(rows[i].time, v, kind, rows[i+1].end) for i,v,kind in [(0,90,"L"),(3,110,"H"),(6,100,"L")]], 85, 90, 100, 132,
                        subdivisions=[{"confirmed": True}], eligible=True)]
        before = deepcopy(self.service.decision)
        with patch("elliottbot.chart.time.time", return_value=now/1000), patch("elliottbot.chart.public", side_effect=self.exchange(rows, now)), patch("elliottbot.chart.candidates", return_value=(cs,[])) as analyze:
            chart = self.reader.get("30m")
        self.assertEqual(len(analyze.call_args.args[1]), 19)
        self.assertFalse(chart["frame"]["candidates"][0]["eligible"])
        self.assertTrue(cs[0].eligible)
        self.assertTrue(chart["reference_only"])
        self.assertEqual(chart["analysis_asof"], rows[-2].end)
        self.assertEqual(self.service.decision, before)

    def test_bad_timeframe_does_not_fetch(self):
        with patch("elliottbot.chart.public") as fetch:
            with self.assertRaises(ValueError):
                self.reader.get("file:///etc/passwd")
            fetch.assert_not_called()

    def test_mode_switch_during_fetch_drops_response(self):
        self.service.sample = False
        def changed(*args):
            self.service.generation += 1
            raise TimeoutError("changed")
        with patch("elliottbot.chart.public", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "모드가 변경"):
                self.reader.get("1h")
        self.assertEqual(self.reader.cache, {})

    def test_clock_skew_and_stale_market_are_visible(self):
        self.service.sample = False
        last = self.service.frames["1h"][-1]
        now = last.end + 3*INTERVALS["1h"]
        with patch("elliottbot.chart.time.time", return_value=now/1000), patch("elliottbot.chart.public", side_effect=self.exchange([last], now)):
            self.assertIn("지연", self.reader.get("1h")["warning"])
        self.reader.cache.clear()
        with patch("elliottbot.chart.time.time", return_value=now/1000+10), patch("elliottbot.chart.public", return_value={"serverTime":now}):
            self.assertIn("시각 차이", self.reader.get("1h")["warning"])

    def test_gaps_fail_without_overwriting_history(self):
        self.service.sample = False
        last = self.service.frames["1h"][-1]
        step = INTERVALS["1h"]
        future = Candle(last.time+4*step,last.end+4*step,100,110,90,100,5)
        now = future.time+1000
        with patch("elliottbot.chart.time.time", return_value=now/1000), patch("elliottbot.chart.public", side_effect=self.exchange([last, future], now)):
            chart = self.reader.get("1h")
        self.assertIn("누락", chart["warning"])
        self.assertEqual(chart["closed_at"], last.end)


if __name__ == "__main__":
    unittest.main()

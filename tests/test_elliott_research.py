from copy import deepcopy
from dataclasses import replace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from elliottbot.config import Settings
from elliottbot.execution import RemoteBroker
from elliottbot.journal import Journal
from elliottbot.models import Candle, Candidate, Pivot
from elliottbot.multiscenario import pivot_confirmation
from elliottbot.service import Service
from elliottbot.waves import _prove, candidates


class ResearchProofTests(unittest.TestCase):
    def test_different_leg_scales_preserve_all_internal_pivots(self):
        anchors = [Pivot(0, 100, "L", 21), Pivot(10, 110, "H", 21), Pivot(20, 104, "L", 21)]
        fine_a = [Pivot(t, p, k, 21) for t, p, k in
                  [(2, 103, "H"), (4, 101, "L"), (6, 108, "H"), (8, 105, "L")]]
        fine_b = [Pivot(12, 106, "L", 21), Pivot(14, 108, "H", 21)]
        bars = [Candle(t, t, p, p, p, p) for t, p in [(0,100),(10,110),(20,104)]]
        def scan(cs, reversal):
            self.assertTrue(all(c.end <= 21 for c in cs))
            return fine_a if abs(reversal-.003) < 1e-10 else fine_b
        with patch("elliottbot.waves.confirmed_pivots", side_effect=scan):
            shared = _prove(anchors, bars, .006, 21, 1)
            adaptive = _prove(anchors, bars, .006, 21, 1, True)
            future = _prove(anchors, bars+[Candle(22,22,999,999,999,999)], .006, 21, 1, True)
        self.assertFalse(all(s["confirmed"] for s in shared))
        self.assertTrue(all(s["confirmed"] for s in adaptive))
        self.assertEqual([s["observed_legs"] for s in adaptive], [5,3])
        self.assertNotEqual(adaptive[0]["reversal"], adaptive[1]["reversal"])
        self.assertEqual(adaptive, future)

    def test_incomplete_proof_retains_observed_legs_instead_of_fabricating_one(self):
        anchors = [Pivot(0,100,"L",20), Pivot(10,110,"H",20)]
        bars = [Candle(0,0,100,100,100,100), Candle(10,10,110,110,110,110)]
        fine = [Pivot(3,105,"H",10), Pivot(6,103,"L",10)]
        with patch("elliottbot.waves.confirmed_pivots", return_value=fine):
            proof = _prove(anchors, bars, .006, 20, 1)
        self.assertFalse(proof[0]["confirmed"])
        self.assertEqual(proof[0]["observed_legs"], 3)

    def test_pivot_trigger_is_closed_recent_directional_and_proven(self):
        for direction in (1,-1):
            c = Candidate("30m", "WAVE_3", direction, .003,
                          [Pivot(0,100,"L",899999)], 80, 100, 100, 110,
                          subdivisions=[{"confirmed":True}], eligible=True)
            bar = Candle(0,899999,100,101,99,100+direction)
            self.assertIsNotNone(pivot_confirmation(c,[bar],"15m"))
            self.assertIsNone(pivot_confirmation(c,[replace(bar,close=100)],"15m"))
            self.assertIsNone(pivot_confirmation(c,[replace(bar,time=900000,end=1799999)],"15m"))
            self.assertIsNone(pivot_confirmation(c,[replace(bar,end=899998)],"15m"))
            c.subdivisions = [{"confirmed":False}]
            self.assertIsNone(pivot_confirmation(c,[bar],"15m"))

    def test_abc_completion_never_promoted(self):
        values=[100,100,100,110,110,110,105,105,105,112,111,111]
        bars=[Candle(i*900000,(i+1)*900000-1,p,p,p,p) for i,p in enumerate(values)]
        points=[Pivot(i*900000,p,k,(i+1)*900000-1) for i,p,k in [(0,100,"L"),(3,110,"H"),(6,105,"L"),(9,112,"H")]]
        with patch("elliottbot.waves.confirmed_pivots",return_value=points),patch("elliottbot.waves._prove",return_value=[{"confirmed":True}]):
            cs,_=candidates("15m",bars,adaptive=True)
        self.assertTrue(any(c.pattern=="ZIGZAG_COMPLETE" for c in cs))
        self.assertTrue(all(not c.eligible for c in cs if c.pattern=="ZIGZAG_COMPLETE"))


class ResearchGateTests(unittest.TestCase):
    def test_configuration_boolean_and_profile_validation(self):
        for value in ("true",1,None):
            with self.assertRaises(ValueError):
                Settings().updated({"research_mode":value})
        with self.assertRaises(ValueError):
            Settings().updated({"research_mode":True})
        self.assertTrue(Settings().updated({"strategy_mode":"active","research_mode":True}).research_mode)

    def test_live_and_demo_block_save_start_and_execution(self):
        for mode in ("live","testnet"):
            with tempfile.TemporaryDirectory() as root:
                s=Service(root,mode=mode)
                try:
                    with self.assertRaisesRegex(ValueError,"모의거래"):
                        s.update_settings({"strategy_mode":"active","research_mode":True})
                    s.settings=replace(s.settings,strategy_mode="active",research_mode=True)
                    with self.assertRaisesRegex(ValueError,"연구 모드"):
                        s.control("start",{})
                    with self.assertRaisesRegex(ValueError,"연구 모드"):
                        s._enter({})
                    self.assertEqual(s.broker.owned_orders(),{})
                finally:
                    s.close()

    def test_persisted_research_cannot_boot_live(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root,"live")
            s.journal.set("settings",replace(Settings(),strategy_mode="active",research_mode=True).to_json())
            s.close()
            with self.assertRaisesRegex(ValueError,"모의거래"):
                Service(root,"live")

    def test_paper_research_cannot_switch_to_live(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root)
            try:
                s.update_settings({"strategy_mode":"active","research_mode":True})
                with self.assertRaisesRegex(ValueError,"연구 모드"):
                    s.switch_mode("live")
            finally:
                s.close()


class TargetProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal=Journal(self.temp.name)
        self.addCleanup(self.journal.close)
        self.broker=RemoteBroker("testnet",("",""),self.journal)
        self.position={"direction":1,"target":"120","opened_at":1000,"target_id":"target"}
        self.raw={"algoStatus":"NEW","symbol":"BTCUSDT","side":"SELL",
                  "orderType":"TAKE_PROFIT_MARKET","workingType":"MARK_PRICE",
                  "closePosition":True,"triggerPrice":"120"}
        self.journal.set("remote_orders",{"target":{"role":"target","algo":True,"created_at":2,"status":"UNKNOWN"}})

    def test_lost_reply_adopts_registered_target_without_duplicate(self):
        self.position.pop("target_id")
        with patch.object(self.broker,"query",return_value=self.raw),patch.object(self.broker,"target") as order:
            self.assertEqual(self.broker.target_state(self.position),("target","NEW"))
        order.assert_not_called()

    def test_unknown_network_or_payload_never_means_missing(self):
        for raw in ({**self.raw,"algoStatus":"UNKNOWN"},{**self.raw,"triggerPrice":"121"},
                    {**self.raw,"side":"BUY"},{**self.raw,"closePosition":False}):
            with patch.object(self.broker,"query",return_value=raw):
                with self.assertRaises(ValueError):
                    self.broker.target_state(self.position)
        with patch.object(self.broker,"query",side_effect=OSError("network")):
            with self.assertRaises(OSError):
                self.broker.target_state(self.position)

    def test_terminal_and_triggered_orders_are_distinguished(self):
        for status in ("NEW","TRIGGERED","FINISHED","CANCELED","EXPIRED","REJECTED"):
            with patch.object(self.broker,"query",return_value={**self.raw,"algoStatus":status}):
                got=self.broker.target_state(self.position)
            self.assertEqual(got,(None,"MISSING") if status in {"CANCELED","EXPIRED","REJECTED"} else ("target",status))

    def test_duplicate_targets_block_repair(self):
        orders=self.broker.owned_orders();orders["second"]=dict(orders["target"])
        self.journal.set("remote_orders",orders)
        with patch.object(self.broker,"query",return_value=self.raw):
            with self.assertRaisesRegex(ValueError,"중복"):
                self.broker.target_state(self.position)

    def test_repair_is_persisted_bounded_and_leaves_stop_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root)
            try:
                s.account.update(position=deepcopy(self.position),running=True)
                broker=Mock()
                broker.target_state.return_value=(None,"MISSING")
                broker.target.return_value="replacement"
                with patch.object(s,"broker",broker):
                    s._ensure_target(s.account["position"])
                    self.assertFalse(s.account["running"])
                    self.assertEqual(s.journal.get("account")["position"]["target_id"],"replacement")
                    with self.assertRaisesRegex(ValueError,"복구 횟수"):
                        s._ensure_target(s.account["position"])
                broker.target.assert_called_once()
                broker.cancel_protection.assert_not_called()
                broker.flatten.assert_not_called()
            finally:
                s.close()

    def test_failed_repair_cannot_blindly_retry(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root)
            try:
                s.account["position"]=deepcopy(self.position)
                broker=Mock();broker.target_state.return_value=(None,"MISSING")
                broker.target.side_effect=ValueError("response unknown")
                with patch.object(s,"broker",broker):
                    with self.assertRaises(ValueError):s._ensure_target(s.account["position"])
                    self.assertEqual(s.journal.get("account")["position"]["target_repairs"],1)
                    broker.target_state.side_effect=ValueError("lookup unknown")
                    with self.assertRaises(ValueError):s._ensure_target(s.account["position"])
                broker.target.assert_called_once()
            finally:s.close()


if __name__ == "__main__":
    unittest.main()

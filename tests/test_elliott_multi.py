from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from elliottbot import risk
from elliottbot.backtest import compare, run
from elliottbot.config import Settings
from elliottbot.execution import D, size_trade
from elliottbot.market import sample_frames, validate
from elliottbot.models import ALL_INTERVALS, Candle, Candidate, Pivot
from elliottbot.multiscenario import breakout, c_wave
from elliottbot.service import Service
from elliottbot.strategy import decide


def setup(tf="1h", direction=1, score=85, end=10**12):
    step = ALL_INTERVALS[tf]
    values = [100, 120, 110] if direction == 1 else [140, 120, 130]
    ps = [Pivot(end - (4-i)*step, v, ("L" if i%2==0 else "H") if direction==1 else ("H" if i%2==0 else "L"), end-step)
          for i,v in enumerate(values)]
    return Candidate(tf,"WAVE_3",direction,.012,ps,score,values[0],values[-1],160 if direction==1 else 80,
                     [],[{"confirmed":True}],True)


def fixture():
    frames = sample_frames(intraday=True)
    end = frames["1h"][-1].end
    frames = {tf:[replace(c, open=120, high=121, low=119, close=120) for c in cs if c.end<=end]
              for tf,cs in frames.items()}
    return frames,end


class MultiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames, cls.end = fixture()

    def decide_with(self, counts, trigger=True, frames=None):
        def scan(tf, candles, lower, **kw):
            self.assertTrue(all(c.end <= self.end for c in candles+lower))
            return counts.get(tf, []), []
        with patch("elliottbot.multiscenario.candidates", side_effect=scan), patch("elliottbot.multiscenario.breakout",
              return_value={"price":119,"bar_close":120,"bar_end":self.end,"timeframe":"30m"} if trigger else None):
            return decide(frames or self.frames,self.end,replace(Settings(),strategy_mode="multi"))

    def test_one_hour_signal_with_unresolved_parent_reduces_risk(self):
        r=self.decide_with({"1h":[setup(end=self.end)]})
        self.assertEqual(r["action"],"LONG")
        self.assertEqual(r["risk_multiplier"],.5)
        self.assertIsNone(r["probability"])

    def test_confirmed_alignment_and_hourly_risk(self):
        r=self.decide_with({"1h":[setup(end=self.end)],"4h":[setup("4h",end=self.end)]})
        self.assertEqual(r["selection"]["timeframe"],"1h")
        self.assertEqual(r["risk_multiplier"],.75)

    def test_weekly_opposition_caps_aligned_risk(self):
        r=self.decide_with({"1h":[setup(end=self.end)],"4h":[setup("4h",end=self.end)],
                            "1w":[setup("1w",-1,end=self.end)]})
        self.assertEqual(r["risk_multiplier"],.5)

    def test_proven_parent_opposition_blocks_child(self):
        r=self.decide_with({"1h":[setup(end=self.end)],"4h":[replace(setup("4h",-1,end=self.end),eligible=False)]})
        self.assertEqual(r["action"],"WAIT")
        child=next(row for row in r["scenarios"] if row["timeframe"]=="1h")
        self.assertFalse(next(c["passed"] for c in child["checks"] if c["id"]=="M05"))

    def test_internal_proof_and_new_cross_are_required(self):
        c=setup(end=self.end)
        for counts,trigger in (({"1h":[replace(c,subdivisions=[{"confirmed":False}])]},True),({"1h":[c]},False)):
            self.assertEqual(self.decide_with(counts,trigger)["action"],"WAIT")

    def test_short_permission(self):
        r=self.decide_with({"1h":[setup(direction=-1,end=self.end)]})
        self.assertEqual(r["action"],"SHORT")

    def test_opposing_counts_no_false_certainty(self):
        r=self.decide_with({"1h":[setup(end=self.end),setup(direction=-1,end=self.end)]})
        self.assertEqual(r["action"],"WAIT")

    def test_missing_intraday_data_fails_closed(self):
        frames={k:v for k,v in self.frames.items() if k!="30m"}
        self.assertEqual(self.decide_with({"1h":[setup(end=self.end)]},frames=frames)["action"],"WAIT")

    def test_future_candles_excluded(self):
        frames={k:list(v) for k,v in self.frames.items()}
        frames["30m"].append(Candle(self.end+1,self.end+1_800_000,999,999,999,999))
        a=self.decide_with({"1h":[setup(end=self.end)]})
        b=self.decide_with({"1h":[setup(end=self.end)]},frames=frames)
        self.assertEqual(a,b)

    def test_abc_requires_parent_origin_and_proof(self):
        c=setup(direction=-1,end=self.end)
        self.assertEqual(c_wave(c,[]),(None,None))
        parent=replace(setup("4h",-1,end=self.end),pattern="IMPULSE_COMPLETE",
                       pivots=[Pivot(c.pivots[0].time,c.pivots[0].price,"H",self.end)])
        converted,evidence=c_wave(c,[parent])
        self.assertEqual(converted.pattern,"WAVE_C")
        self.assertEqual(converted.target,110)
        self.assertIs(evidence,parent)
        self.assertEqual(c_wave(c,[replace(parent,subdivisions=[])]),(None,None))
        self.assertEqual(c_wave(c,[replace(parent,pivots=[replace(parent.pivots[0],price=141)])]),(None,None))

    def test_observed_and_expected_direction_are_distinct(self):
        c=replace(setup(),pattern="IMPULSE_COMPLETE",direction=-1)
        self.assertEqual(c.to_json()["observed_direction_name"],"상승")
        self.assertEqual(c.to_json()["direction_name"],"SHORT")

    def test_actual_breakout_requires_fresh_cross_and_prior_confirmation(self):
        c=setup(end=10_000_000)
        bars=[Candle(i*1_800_000,(i+1)*1_800_000-1,p,p,p,p) for i,p in enumerate([110,119,121])]
        c.pivots[-1]=replace(c.pivots[-1],time=0)
        level=Pivot(1_800_000,120,"H",bars[1].end)
        with patch("elliottbot.multiscenario.confirmed_pivots",return_value=[level]):
            self.assertIsNotNone(breakout(c,bars,"30m"))
            self.assertIsNone(breakout(c,[*bars[:1],replace(bars[1],close=121),bars[2]],"30m"))
        with patch("elliottbot.multiscenario.confirmed_pivots",return_value=[replace(level,confirmed_at=bars[2].end)]):
            self.assertIsNone(breakout(c,bars,"30m"))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.settings=Settings()
        self.now=1_750_000_000_000
        self.ledger=risk.refresh({},self.now,D(1000))

    def test_daily_count_and_reset(self):
        for _ in range(4): risk.entered(self.ledger)
        self.assertIn("횟수",risk.entry_gate(self.ledger,self.settings,self.now,1000))
        risk.refresh(self.ledger,self.now+86_400_000,1000)
        self.assertIsNone(risk.entry_gate(self.ledger,self.settings,self.now+86_400_000,1000))

    def test_cooldown_survives_day_change(self):
        risk.exited(self.ledger,self.settings,self.now,-1)
        self.assertIsNotNone(risk.entry_gate(self.ledger,self.settings,self.now+1,999))
        self.assertIsNone(risk.entry_gate(self.ledger,self.settings,self.now+3_600_000,999))

    def test_loss_streak_pause(self):
        for _ in range(3): risk.exited(self.ledger,self.settings,self.now,-1)
        self.assertEqual(self.ledger["blocked_until"],self.now+360*60_000)

    def test_profit_peak_and_remaining_budget(self):
        self.assertEqual(risk.budget(self.ledger,self.settings,985),D(5))
        risk.refresh(self.ledger,self.now,1100)
        risk.refresh(self.ledger,self.now+86_400_000,1050)
        self.assertEqual(risk.budget(self.ledger,self.settings,1050),D(5))
        self.assertEqual(risk.budget(self.ledger,self.settings,1045),D(0))

    def test_size_and_chase_cost_expiry(self):
        s=replace(self.settings,strategy_mode="multi",fee_bps=0,slippage_bps=0,max_notional=100000,leverage=5)
        d={"action":"LONG","selection":setup().to_json(),"trigger":{"bar_close":120},
           "risk_multiplier":.5,"signal_expires_at":self.now+1000}
        q={"bid":120,"ask":120,"price":120,"time":self.now,
           "rules":{"tick":".1","step":".001","min_qty":".001","min_notional":"1"}}
        p=size_trade(d,q,s,1000,risk_cap=D(1))
        self.assertLessEqual(D(p["risk_at_stop"]),1)
        self.assertEqual(D(p["risk_multiplier"]),D(".5"))
        with self.assertRaisesRegex(ValueError,"유효 시간"):
            size_trade(d,{**q,"time":self.now+1000},s,1000)
        with self.assertRaisesRegex(ValueError,"추격"):
            size_trade(d,{**q,"ask":123,"bid":123,"price":123},s,1000)
        with self.assertRaisesRegex(ValueError,"위험 배수"):
            size_trade({**d,"risk_multiplier":2},q,s,1000)

    def test_ledger_persistence_and_settings_invalidation(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root,sample=True)
            s.tick()
            risk.entered(s.account["risk"])
            risk.exited(s.account["risk"],s.settings,s.quote["time"],-1)
            s.update_settings({"strategy_mode":"multi"})
            self.assertIsNone(s.decision)
            self.assertFalse(s.account["running"])
            s.tick()
            self.assertIn("30m",s.frames)
            self.assertEqual(s.decision["version"],"ew-multiscenario-2.0")
            expected=deepcopy(s.account["risk"])
            s.close()
            reopened=Service(root,sample=True)
            self.assertEqual(expected,reopened.account["risk"])
            self.assertFalse(reopened.account["running"])
            reopened.close()

    def test_settings_invalid_strategy_and_fractional_counts(self):
        for values in ({"strategy_mode":[]},{"max_entries_per_day":1.5},{"max_cost_fraction":1}):
            with self.assertRaises(ValueError): Settings().updated(values)

    def test_service_entry_count_and_cooldown_gate_do_not_disable_protection(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root,sample=True)
            try:
                s.tick()
                d={**s.decision,"action":"LONG","selection":setup().to_json()}
                s.account['running']=True
                s.account['risk']['entries']=4
                with patch('elliottbot.service.decide',return_value=d), patch.object(s,'_enter') as enter, patch.object(s,'_mark_position') as mark:
                    s.tick()
                    enter.assert_not_called()
                    mark.assert_called_once()
                self.assertIn('횟수',s.status()['entry_gate'])
                s.account['risk']['entries']=0
                s.account['risk']['blocked_until']=s.quote['time']+7_200_000
                with patch('elliottbot.service.decide',return_value=d), patch.object(s,'_enter') as enter:
                    s.tick()
                    enter.assert_not_called()
            finally:
                s.close()

    def test_service_sizes_with_remaining_budget_and_stable_signal_key(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root,sample=True)
            try:
                s.tick()
                s.account['running']=True
                s.account['cash']='985'
                d={**s.decision,'action':'LONG','selection':setup().to_json(),'signal_key':'same-extreme'}
                with patch('elliottbot.service.decide',return_value=d), patch('elliottbot.service.size_trade',return_value={'test':True}) as size, patch.object(s,'_enter') as enter:
                    s.tick()
                    self.assertEqual(size.call_args.kwargs['risk_cap'],D(5))
                    enter.assert_called_once()
                    d['id']='new-decision-same-extreme'
                    s.tick()
                    enter.assert_called_once()
            finally:
                s.close()

    def test_trade_md_contains_scenarios_and_risk_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            s=Service(root,sample=True)
            try:
                d={'scenarios':[{'timeframe':'1h','pattern':'WAVE_C','direction':-1,'priority':80,
                                'risk_multiplier':.5,'rr':2,'checks':[{'passed':False,'detail':'test rejection'}]}],
                   'risk_multiplier':.5,'trigger':{'bar_close':120}}
                event=s.journal.record('ENTRY',{'decision':d})
                contents=s.journal.export_event(event).read_text()
                self.assertIn('위험 배수: 0.5',contents)
                self.assertIn('test rejection',contents)
                self.assertIn('WAVE_C',contents)
            finally:
                s.close()

    def test_comparison_common_timeline_and_honest_zero_trades(self):
        frames=sample_frames(count=300,intraday=True)
        rules={"tick":".1","step":".001","min_qty":".001","min_notional":"1"}
        with patch("elliottbot.backtest.decide",return_value={"action":"WAIT"}):
            report=compare(frames,Settings(),rules,bars=12)
        self.assertFalse(report["profitability_proven"])
        self.assertFalse(report["sufficient_trades"])
        self.assertEqual(len({r["from"] for r in report["comparison"].values()}),1)
        self.assertEqual([r["closed_trades"] for r in report["comparison"].values()],[0,0,0])
        self.assertEqual(report["comparison"]["multi_stress"]["settings"]["fee_bps"],10)


if __name__ == "__main__":
    unittest.main()

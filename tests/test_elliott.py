from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from elliottbot.backtest import run
from elliottbot.config import Settings, ENDPOINTS
from elliottbot.execution import D, RemoteBroker, payoff, size_trade
from elliottbot.journal import Journal
from elliottbot.market import aggregate, market_rules, sample_frames, validate
from elliottbot.models import Candle, Candidate, INTERVALS, Pivot
from elliottbot.secrets import SecretStore
from elliottbot.service import Service
from elliottbot.strategy import choose, decide
from elliottbot.waves import candidates, confirmed_pivots, impulse_rules, valid, zigzag_rules


def bars(values, start=0, step=3_600_000):
    return [Candle(start + i * step, start + (i + 1) * step - 1, v, v, v, v) for i, v in enumerate(values)]


def pivots(values, first="L"):
    return [Pivot(i * 3_600_000, v, first if i % 2 == 0 else "H" if first == "L" else "L", (i + 1) * 3_600_000 - 1) for i, v in enumerate(values)]


def candidate(direction=1, score=86, tf="4h", proven=True):
    ps = pivots([70000, 76000, 73600])
    return Candidate(tf, "WAVE_3", direction, .012, ps, score, 70000, 73600, 83308,
                     [{"id": "I01", "passed": True, "detail": "test"}], [{"confirmed": proven}], proven)


def decision(direction=1):
    c = candidate(direction)
    if direction < 0:
        c.stop, c.target = 80400, 71000
    return {"id": "test-decision", "version": "test", "asof": 1, "action": "LONG" if direction > 0 else "SHORT",
            "selection": c.to_json(), "frames": {}, "checks": [], "reason": "엘리엇 파동 단위 시험"}


def quote(price=77000):
    return {"price": price, "bid": price - 1, "ask": price + 1, "time": int(time.time() * 1000),
            "rules": {"tick": "0.1", "step": "0.001", "min_qty": "0.001", "min_notional": "50"}}


class WaveTests(unittest.TestCase):
    def test_three_immutable_rules(self):
        self.assertTrue(valid(impulse_rules(pivots([100, 120, 110, 150, 130, 165]))))
        for values in ([100, 120, 99], [100, 120, 110, 140, 119], [100, 130, 120, 140, 135, 170]):
            self.assertFalse(valid(impulse_rules(pivots(values))))

    def test_wave_three_need_not_be_longest(self):
        self.assertTrue(valid(impulse_rules(pivots([100, 130, 115, 140, 134, 154]))))

    def test_bearish_symmetry(self):
        self.assertTrue(valid(impulse_rules(pivots([200, 180, 190, 150, 170, 135], "H"))))

    def test_truncation_is_excluded_not_faked(self):
        checks = impulse_rules(pivots([100, 120, 110, 150, 130, 145]))
        self.assertFalse(next(c["passed"] for c in checks if c["id"] == "I05"))

    def test_zigzag_geometry(self):
        self.assertTrue(valid(zigzag_rules(pivots([150, 125, 140, 110], "H"))))
        self.assertFalse(valid(zigzag_rules(pivots([150, 125, 151, 110], "H"))))

    def test_confirmed_prefix_never_repainted(self):
        data = bars([100, 102, 110, 108, 105, 103, 109, 120, 115, 111, 118, 112, 100, 99, 103])
        final = confirmed_pivots(data, .03)
        for n in range(1, len(data)):
            now = confirmed_pivots(data[:n], .03)
            self.assertEqual(now, [p for p in final if p.confirmed_at <= data[n - 1].end])
            self.assertTrue(all(p.confirmed_at <= data[n - 1].end for p in now))

    def test_outside_bar_extends_without_same_bar_confirmation(self):
        data = bars([100, 102, 104])
        data.append(Candle(3 * 3600000, 4 * 3600000 - 1, 104, 110, 95, 96))
        ps = confirmed_pivots(data, .03)
        self.assertFalse(any(p.price == 110 for p in ps))

    def test_actual_five_three_subdivision_is_eligible(self):
        values = [100,100,102,102,101.2,101.2,104.8,104.8,103.9,103.9,106,106,104.8,104.8,105.4,105.4,103.6,103.6,104.9,104.9]
        data = bars(values)
        cs, _ = candidates("4h", data, data)
        found = [c for c in cs if c.pattern == "WAVE_3" and c.eligible]
        self.assertTrue(found)
        self.assertEqual([s["observed_legs"] for s in found[0].subdivisions], [5, 3])

    def test_missing_subwave_history_cannot_be_proven(self):
        data = bars([100,100,102,102,101.2,101.2,104.8,104.8,103.9,103.9,106,106,104.8,104.8,105.4,105.4,103.6,103.6,104.9,104.9])
        cs, _ = candidates("4h", data, data[-5:])
        self.assertTrue(cs)
        self.assertFalse(any(c.eligible for c in cs))

    def test_unconfirmed_tail_is_not_a_wave_endpoint(self):
        ps = confirmed_pivots(bars([100, 102, 104, 108, 112]), .03)
        self.assertEqual([p.price for p in ps], [100])

    def test_data_integrity_rejects_gap_and_nonfinite(self):
        with self.assertRaises(ValueError):
            validate(bars([100, 101])[::2] + bars([102], start=3 * 3600000), "1h")
        with self.assertRaises(ValueError):
            validate(bars([float("nan")]), "1h")

    def test_aggregate_drops_partial_candles(self):
        self.assertEqual(len(aggregate(bars([100] * 7), "4h")), 1)
        self.assertEqual(aggregate(bars([100] * 7), "4h")[0].end, 4 * 3600000 - 1)

    def test_weekly_alignment_is_monday_utc(self):
        f = sample_frames(2200)
        from datetime import datetime, timezone
        for c in f["1w"]:
            self.assertEqual(datetime.fromtimestamp(c.time / 1000, timezone.utc).weekday(), 0)


class DecisionTests(unittest.TestCase):
    def test_conflict_does_not_claim_probability(self):
        a, b = candidate(1, 88), candidate(-1, 80)
        chosen, state = choose([a, b], 12)
        self.assertIsNone(chosen)
        self.assertEqual(state, "CONFLICT")

    def test_score_gap_and_proof_precedence(self):
        a, b = candidate(1, 88), candidate(-1, 70)
        self.assertIs(choose([a, b], 12)[0], a)
        self.assertIs(choose([candidate(-1, 99, proven=False), a], 12)[0], a)

    def fixture(self, weekly_direction=1):
        f = sample_frames(3000)
        asof = f["1h"][-1].end
        close = f["1h"][-1].close
        def fake(tf, candles, lower):
            c = candidate(weekly_direction if tf == "1w" else 1, tf=tf)
            c.pivots = [Pivot(asof - 15 * 3600000, close - 4000, "L", asof - 14 * 3600000),
                        Pivot(asof - 10 * 3600000, close - 500, "H", asof - 9 * 3600000),
                        Pivot(asof - 5 * 3600000, close - 2000, "L", asof - 4 * 3600000)]
            c.stop, c.target = close - 2000, close + 7000
            return [c], []
        return f, asof, fake

    def test_positive_decision_all_gates(self):
        f, asof, fake = self.fixture()
        with patch("elliottbot.strategy.candidates", side_effect=fake):
            d = decide(f, asof, Settings())
        self.assertEqual(d["action"], "LONG")
        self.assertIsNone(d["probability"])
        self.assertTrue(all(c["passed"] for c in d["checks"]))

    def test_weekly_veto_cannot_be_outvoted(self):
        f, asof, fake = self.fixture(-1)
        with patch("elliottbot.strategy.candidates", side_effect=fake):
            d = decide(f, asof, Settings())
        self.assertEqual(d["action"], "WAIT")
        self.assertEqual(d["checks"][-1]["id"], "P02")

    def test_future_candles_do_not_change_past_decision(self):
        frames = sample_frames(2500)
        asof = frames["1h"][-40].end
        truncated = {tf: [c for c in cs if c.end <= asof] for tf, cs in frames.items()}
        self.assertEqual(decide(frames, asof, Settings()), decide(truncated, asof, Settings()))

    def test_stale_higher_timeframe_blocks_entry(self):
        f = sample_frames(2500)
        f["1w"] = f["1w"][:-2]
        d = decide(f, f["1h"][-1].end, Settings())
        self.assertEqual(d["checks"][-1]["id"], "P00")
        self.assertEqual(d["action"], "WAIT")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(self.temp.name)
        self.addCleanup(self.journal.close)
        self.broker = RemoteBroker("testnet", ("k" * 64, "s" * 64), self.journal)

    def test_market_quantity_combines_both_filters(self):
        rules = market_rules([
            {"filterType": "LOT_SIZE", "stepSize": "0.003", "minQty": "0.003", "maxQty": "100"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.002", "minQty": "0.006", "maxQty": "2"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            {"filterType": "MIN_NOTIONAL", "notional": "100"},
        ])
        self.assertEqual(D(rules["step"]), D("0.006"))
        self.assertEqual(D(rules["min_qty"]), D("0.006"))
        self.assertEqual(D(rules["max_qty"]), D("2"))

    def settlement_fixture(self):
        self.journal.set("remote_orders", {"entry": {"algo": False, "exchange_id": "1"},
                                            "exit": {"algo": False, "exchange_id": "2"}})
        rows = [{"orderId": 1, "side": "BUY", "qty": "0.002", "commissionAsset": "USDT",
                 "commission": "0.05", "realizedPnl": "0", "time": 1000, "price": "70000"},
                {"orderId": 2, "side": "SELL", "qty": "0.001", "commissionAsset": "USDT",
                 "commission": "0.03", "realizedPnl": "2", "time": 2000, "price": "72000"},
                {"orderId": 2, "side": "SELL", "qty": "0.001", "commissionAsset": "USDT",
                 "commission": "0.03", "realizedPnl": "4", "time": 2001, "price": "74000"}]
        return rows, {"opened_at": 1000, "quantity": "0.002", "direction": 1}

    def test_settlement_checks_full_quantity_and_weighted_exit(self):
        rows, p = self.settlement_fixture()
        with patch.object(self.broker, "history", side_effect=[rows, []]):
            result = self.broker.settlement(p)
        self.assertEqual(D(result["price"]), D("73000"))
        self.assertEqual(D(result["fees"]), D("0.11"))
        self.assertEqual(D(result["exit_fees"]), D("0.06"))

    def test_balanced_but_incomplete_trade_history_blocks_settlement(self):
        rows, p = self.settlement_fixture()
        rows[0]["qty"] = "0.001"
        with patch.object(self.broker, "history", return_value=rows[:2]), self.assertRaises(ValueError):
            self.broker.settlement(p)

    def test_market_maximum_never_exceeded(self):
        q = quote()
        q["rules"]["max_qty"] = "0.001"
        self.assertEqual(D(size_trade(decision(), q, Settings(), 1000)["quantity"]), D("0.001"))

    def test_min_notional_uses_conservative_mark(self):
        q = quote()
        q["price"] = 45000
        q["rules"].update(max_qty="0.001", min_notional="50")
        with self.assertRaises(ValueError):
            size_trade(decision(), q, Settings(), 1000)

    def test_risk_budget_and_notional_cap(self):
        plan = size_trade(decision(), quote(), Settings(), 1000)
        self.assertLessEqual(D(plan["risk_at_stop"]), D(5))
        self.assertLessEqual(D(plan["quantity"]) * D(plan["entry_reference"]), D(900))
        self.assertEqual(D(plan["quantity"]) % D(".001"), 0)

    def test_short_sizing_and_costs(self):
        plan = size_trade(decision(-1), quote(), Settings(), 1000)
        self.assertEqual(plan["direction"], -1)
        self.assertGreater(D(plan["stop"]), D(plan["entry_reference"]))
        self.assertGreater(D(plan["fee_rate"]), 0)

    def test_too_small_capital_does_not_round_up_risk(self):
        with self.assertRaises(ValueError):
            size_trade(decision(), quote(), Settings(), 10)

    def test_spread_and_chase_entry_rejected(self):
        q = quote()
        q["ask"] += 1000
        with self.assertRaises(ValueError):
            size_trade(decision(), q, Settings(), 1000)
        with self.assertRaises(ValueError):
            size_trade(decision(), quote(83200), Settings(), 1000)

    def test_leverage_validation(self):
        for bad in (11, 0, 2.1, True, "NaN", "inf"):
            with self.assertRaises(ValueError):
                Settings().updated({"leverage": bad})
        for good in (1, 5, 6, 10, "10"):
            self.assertEqual(Settings().updated({"leverage": good}).leverage, int(good))

    def test_write_guard_before_signing(self):
        with patch("elliottbot.execution._request_json") as request:
            with self.assertRaises(ValueError):
                self.broker.request("POST", "/fapi/v1/order", {})
            request.assert_not_called()

    def test_five_to_ten_preserves_risk_and_position_with_current_notional_cap(self):
        for direction in (1, -1):
            d = decision(direction)
            d["selection"].update(stop=76900 if direction > 0 else 77100,
                                  target=83308 if direction > 0 else 71000)
            five = size_trade(d, quote(), replace(Settings(), capital=736, leverage=5), 736)
            ten = size_trade(d, quote(), replace(Settings(), capital=736, leverage=10), 736)
            for field in ("quantity", "risk_at_stop", "risk_budget", "stop", "target", "rr", "fee_rate"):
                self.assertEqual(five[field], ten[field])
            self.assertEqual(D(ten["risk_budget"]), D("3.68"))
            self.assertEqual(D(five["estimated_initial_margin"]), D(ten["estimated_initial_margin"]) * 2)
            self.assertLessEqual(D(ten["quantity"]) * D(ten["entry_reference"]), D(900))

    def test_increased_margin_capacity_still_respects_risk_budget(self):
        d = decision()
        d["selection"].update(stop=76999, target=83308)
        settings = replace(Settings(), leverage=10, capital=736, max_notional=100000, fee_bps=0, slippage_bps=0)
        five = size_trade(d, quote(), replace(settings, leverage=5), 736)
        ten = size_trade(d, quote(), settings, 736)
        self.assertGreater(D(ten["quantity"]), D(five["quantity"]))
        self.assertLessEqual(D(ten["risk_at_stop"]), D("3.68"))

    def test_ten_x_configure_request_and_bounds(self):
        def reply(method, path, params=None, purpose=None):
            if path == "/fapi/v1/positionSide/dual":
                return {"dualSidePosition": False}
            return []
        with patch.object(self.broker, "arm"), patch.object(self.broker, "request", side_effect=reply) as request:
            self.broker.configure(10)
            self.assertTrue(any(c.args[:3] == ("POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 10}) for c in request.call_args_list))
            request.reset_mock()
            for invalid in (11, 0, 9.5, True, float("nan")):
                with self.assertRaises(ValueError):
                    self.broker.configure(invalid)
            request.assert_not_called()

    def test_ten_x_entry_reaches_protection_check_without_order(self):
        plan = size_trade(decision(), quote(), replace(Settings(), leverage=10), 1000)
        with patch.object(self.broker, "request", return_value=[{"leverage": 10, "marginType": "ISOLATED"}]), patch.object(self.broker, "position", return_value={"positionAmt": "0"}), patch.object(self.broker, "place", return_value=("stop", {})) as place, patch.object(self.broker, "query", return_value={"algoStatus": "REJECTED"}):
            with self.assertRaisesRegex(ValueError, "손절 활성화"):
                self.broker.enter(plan)
            self.assertEqual([c.args[0] for c in place.call_args_list], ["stop"])

    def test_above_ten_rejected_before_entry_request(self):
        plan = size_trade(decision(), quote(), Settings(), 1000)
        plan["leverage"] = 11
        with patch.object(self.broker, "request") as request:
            with self.assertRaises(ValueError):
                self.broker.enter(plan)
            request.assert_not_called()

    def test_only_test_order_allowed_before_arming(self):
        with patch("elliottbot.execution._request_json", return_value={}) as request:
            self.broker.request("POST", "/fapi/v1/order/test", {"symbol": "BTCUSDT"})
            self.assertIn("/fapi/v1/order/test", request.call_args.args[1])
            self.assertIn(b"signature=", request.call_args.args[3])

    def test_lost_reply_queries_without_resending(self):
        self.broker.armed = True
        def reply(method, path, params=None, purpose=None):
            if method == "POST":
                raise OSError("response lost")
            return {"orderId": 123, "status": "FILLED", "avgPrice": "77500", "executedQty": ".001"}
        with patch.object(self.broker, "request", side_effect=reply) as req:
            client, raw = self.broker.place("entry", {"type": "MARKET"})
        self.assertEqual(raw["status"], "FILLED")
        self.assertEqual(sum(c.args[0] == "POST" for c in req.call_args_list), 1)
        self.assertIn(client, self.broker.owned_orders())

    def test_unknown_reply_keeps_intent(self):
        self.broker.armed = True
        with patch.object(self.broker, "request", side_effect=OSError("down")):
            with self.assertRaises(ValueError):
                self.broker.place("entry", {"type": "MARKET"})
        self.assertTrue(self.broker.owned_orders())
        self.assertEqual(self.journal.events(1)[0]["kind"], "ORDER_UNKNOWN")

    def test_protection_failure_never_submits_entry(self):
        self.broker.armed = True
        with patch.object(self.broker, "request", return_value=[{"leverage": 1, "marginType": "ISOLATED"}]), patch.object(self.broker, "position", return_value={"positionAmt": "0"}), patch.object(self.broker, "place", return_value=("stop", {})) as place, patch.object(self.broker, "query", return_value={"algoStatus": "REJECTED"}):
            with self.assertRaises(ValueError):
                self.broker.enter(size_trade(decision(), quote(), Settings(), 1000))
        self.assertEqual([c.args[0] for c in place.call_args_list], ["stop"])

    def test_stop_is_active_before_market_entry(self):
        self.broker.armed = True
        plan = size_trade(decision(), quote(), Settings(), 1000)
        log = []
        def place(role, params, algo=False):
            log.append(role)
            return role, {"status": "FILLED", "executedQty": plan["quantity"], "avgPrice": "77500"}
        def query(cid):
            log.append("confirmed_stop")
            return {"algoStatus": "NEW"}
        with patch.object(self.broker, "request", return_value=[{"leverage": 1, "marginType": "ISOLATED"}]), patch.object(self.broker, "position", side_effect=[{"positionAmt": "0"}, {"positionAmt": plan["quantity"]}]), patch.object(self.broker, "place", side_effect=place), patch.object(self.broker, "query", side_effect=query):
            self.broker.enter(plan)
        self.assertEqual(log, ["stop", "confirmed_stop", "entry", "confirmed_stop"])

    def test_flatten_does_not_cancel_protection_early(self):
        with patch.object(self.broker, "position", return_value={"positionAmt": ".001"}), patch.object(self.broker, "place") as place, patch.object(self.broker, "cancel_protection") as cancel:
            with self.assertRaises(ValueError):
                self.broker.flatten()
            self.assertEqual(place.call_args.args[1]["reduceOnly"], "true")
            cancel.assert_not_called()

    def test_account_key_lock_across_databases(self):
        self.broker.arm()
        other = RemoteBroker("testnet", ("k" * 64, "s" * 64), self.journal)
        try:
            with self.assertRaises(RuntimeError):
                other.arm()
        finally:
            self.broker.close()
            other.close()

    def test_short_risk_includes_higher_stop_exit_commission(self):
        plan = size_trade(decision(-1), quote(), Settings(), 1000)
        loss, _ = payoff(plan["entry_reference"], plan["stop"], plan["target"], -1, plan["fee_rate"], plan["slippage_rate"])
        self.assertEqual(loss * D(plan["quantity"]), D(plan["risk_at_stop"]))
        self.assertLessEqual(loss * D(plan["quantity"]), D(plan["risk_budget"]))

    def test_exchange_leverage_change_blocks_entry_path(self):
        self.broker.armed = True
        with patch.object(self.broker, "request", return_value=[{"leverage": 20, "marginType": "ISOLATED"}]), patch.object(self.broker, "place") as place:
            with self.assertRaises(ValueError):
                self.broker.enter(size_trade(decision(), quote(), Settings(), 1000))
            place.assert_not_called()

    def test_protection_firing_during_entry_flattens(self):
        plan = size_trade(decision(), quote(), Settings(), 1000)
        with patch.object(self.broker, "request", return_value=[{"leverage": 1, "marginType": "ISOLATED"}]), patch.object(self.broker, "position", side_effect=[{"positionAmt": "0"}, {"positionAmt": plan["quantity"]}]), patch.object(self.broker, "place", return_value=("x", {"status": "FILLED", "executedQty": plan["quantity"]})), patch.object(self.broker, "query", side_effect=[{"algoStatus": "NEW"}, {"algoStatus": "FINISHED"}]), patch.object(self.broker, "flatten") as flatten:
            with self.assertRaises(ValueError):
                self.broker.enter(plan)
            flatten.assert_called_once()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, sample=True)
        self.addCleanup(self.service.close)
        self.service.tick()

    def test_default_is_paused_without_credentials(self):
        self.assertFalse(self.service.status()["account"]["running"])
        self.assertFalse(self.service.status()["credentials_saved"])

    def test_paper_entry_exit_writes_full_elliott_md(self):
        s = self.service
        s.decision, s.quote = decision(), quote()
        s._enter(size_trade(s.decision, s.quote, s.settings, s.equity()))
        self.assertIsNotNone(s.account["position"])
        original = s.account["position"]["decision"]["id"]
        s.decision = {**decision(), "id": "exit-context"}
        s._exit("파동 목표 도달", 83000)
        self.assertIsNone(s.account["position"])
        events = s.journal.events()
        self.assertEqual(events[0]["data"]["entry_decision"]["id"], original)
        self.assertEqual(events[0]["data"]["exit_decision"]["id"], "exit-context")
        files = list((s.journal.root / "trades").glob("*.md"))
        self.assertEqual(len(files), 2)
        self.assertIn("엘리엇", files[0].read_text())

    def test_short_paper_profit_and_fees(self):
        s = self.service
        s.decision, s.quote = decision(-1), quote()
        s._enter(size_trade(s.decision, s.quote, s.settings, s.equity()))
        s._exit("파동 목표 도달", 71200)
        self.assertGreater(D(s.account["cash"]), D(1000))
        self.assertGreater(D(s.account["fees"]), 0)

    def test_settings_reject_open_position(self):
        s = self.service
        s.decision, s.quote = decision(), quote()
        s._enter(size_trade(s.decision, s.quote, s.settings, s.equity()))
        with self.assertRaises(ValueError):
            s.update_settings({"leverage": 2})

    def test_status_snapshot_does_not_mutate_account(self):
        status = self.service.status()
        status["account"]["cash"] = "0"
        self.assertNotEqual(self.service.account["cash"], "0")

    def test_loss_latch_blocks_manual_start(self):
        s = self.service
        s.account["cash"] = "850"
        with self.assertRaises(ValueError):
            s.control("start", {})
        self.assertFalse(s.account["running"])

    def test_bad_settings_are_atomic(self):
        s = self.service
        s.account["cash"] = "10"
        before = json.loads(json.dumps(s.account))
        with self.assertRaises(ValueError):
            s.update_settings({"capital": 50})
        self.assertEqual(s.account, before)

    def test_same_signal_consumed_once(self):
        self.assertTrue(self.service.journal.consume("x"))
        self.assertFalse(self.service.journal.consume("x"))

    def test_sample_cannot_switch_live(self):
        with self.assertRaises(ValueError):
            self.service.switch_mode("live")

    def test_secret_mode_separation_and_permissions(self):
        secret = SecretStore(self.temp.name)
        secret.save("testnet", "a" * 64, "b" * 64)
        self.assertEqual(secret.load("live"), ("", ""))
        self.assertEqual((Path(self.temp.name) / "secrets/testnet.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(secret.load("testnet"), ("a" * 64, "b" * 64))

    def test_secret_symlink_rejected(self):
        secret = SecretStore(Path(self.temp.name) / "other")
        secret.root.parent.mkdir()
        secret.root.symlink_to(self.temp.name)
        with self.assertRaises(ValueError):
            secret.save("live", "a" * 64, "b" * 64)

    def test_lost_exit_keeps_pending_and_protection(self):
        s = self.service
        s.decision, s.quote = decision(), quote()
        s._enter(size_trade(s.decision, s.quote, s.settings, s.equity()))
        from unittest.mock import Mock
        s.broker = Mock()
        s.broker.flatten.side_effect = ValueError("unknown")
        with self.assertRaises(ValueError):
            s._exit("test", 76000)
        self.assertEqual(s.account["pending"]["kind"], "EXIT")
        self.assertEqual(s.journal.get("account")["pending"]["kind"], "EXIT")
        s.broker.cancel_protection.assert_not_called()
        s.broker = None

    def test_live_requires_phrase_even_after_setup(self):
        s = self.service
        from unittest.mock import Mock
        s.mode, s.broker = "live", Mock()
        s.journal.set("settings_confirmed", True)
        with self.assertRaises(ValueError):
            s.control("start", {})
        s.broker.preflight.assert_not_called()
        self.assertFalse(s.account["running"])
        s.mode, s.broker = "paper", None

    def test_failed_account_refresh_discards_previous_passes(self):
        from unittest.mock import Mock
        s = self.service
        s.preflight_result = {"ok": True, "checks": [{"name": "old", "ok": True}]}
        with patch.object(s, "broker", Mock(key="k" * 64)):
            s.broker.preflight.side_effect = ValueError("account lookup failed")
            with self.assertRaises(ValueError):
                s.check_account()
        self.assertFalse(s.preflight_result["ok"])
        self.assertEqual(s.preflight_result["checks"][0]["name"], "계정 조회")
        self.assertNotIn("old", json.dumps(s.preflight_result))

    def test_failed_order_permission_report_cannot_arm_live_trading(self):
        from unittest.mock import Mock
        s = self.service
        s.journal.set("settings_confirmed", True)
        report = {"ok": False, "checks": [{"name": "주문 권한 검사", "ok": False}]}
        with patch.object(s, "mode", "live"), patch.object(s, "broker", Mock(key="k" * 64)):
            s.broker.preflight.return_value = report
            with self.assertRaises(ValueError):
                s.control("start", {"confirmation": "LIVE BTCUSDT", "risk_accepted": True})
            s.broker.arm.assert_not_called()
        self.assertFalse(s.account["running"])
        self.assertEqual(s.preflight_result, report)

    def test_restart_preserves_position_but_never_resumes(self):
        s = self.service
        s.decision, s.quote = decision(), quote()
        s._enter(size_trade(s.decision, s.quote, s.settings, s.equity()))
        s.account["running"] = True
        s.journal.set("account", s.account)
        s.journal.close()
        s._load_mode("paper")
        self.assertIsNotNone(s.account["position"])
        self.assertFalse(s.account["running"])

    def test_remote_close_costs_not_double_counted(self):
        s = self.service
        from unittest.mock import Mock
        s.decision, s.quote = decision(), quote()
        s._enter(size_trade(s.decision, s.quote, s.settings, s.equity()))
        p = s.account["position"]
        p["fee"], p["funding_paid"] = "1", "2"
        s.account.update(cash="997", fees="1", funding="2")
        s.broker = Mock()
        s.broker.settlement.return_value = {"price": "80000", "gross": "10", "fees": "2", "exit_fees": "1", "funding_income": "-3", "trades": [], "funding": []}
        s._exit("test", 80000, already_flat=True)
        self.assertEqual(D(s.account["cash"]), D(1005))
        self.assertEqual(D(s.account["fees"]), D(2))
        self.assertEqual(D(s.account["funding"]), D(3))
        self.assertEqual(s.journal.get("remote_orders"), {})
        s.broker = None


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.addCleanup(self.service.close)

    def test_switch_to_live_is_possible_without_enabling_trades(self):
        self.service.switch_mode("live")
        self.assertEqual(self.service.mode, "live")
        self.assertFalse(self.service.account["running"])
        self.assertFalse(self.service.broker.armed)
        self.assertEqual(self.service.broker.base, ENDPOINTS["live"])

    def test_bad_destination_keeps_previous_database_open(self):
        secret = Path(self.temp.name) / "secrets"
        secret.mkdir()
        (secret / "testnet.json").write_text("bad", encoding="utf8")
        with self.assertRaises(ValueError):
            self.service.switch_mode("testnet")
        self.assertEqual(self.service.mode, "paper")
        self.assertEqual(self.service.status()["account"]["cash"], "1000")

    def test_keys_never_appear_in_status_or_events(self):
        self.service.switch_mode("testnet")
        self.service.save_credentials({"key": "a" * 64, "secret": "b" * 64})
        payload = json.dumps(self.service.status())
        self.assertNotIn("a" * 64, payload)
        self.assertNotIn("b" * 64, payload)
        self.assertTrue(self.service.status()["credentials_saved"])


class ReplayTests(unittest.TestCase):
    def test_replay_only_passes_past_closed_candles(self):
        f = sample_frames(2200)
        observed = []
        def decide_past(frames, asof, settings):
            self.assertTrue(all(c.end <= asof for cs in frames.values() for c in cs))
            observed.append(asof)
            return {"action": "WAIT"}
        with patch("elliottbot.backtest.decide", side_effect=decide_past):
            report = run(f, Settings(), quote()["rules"], bars=12)
        self.assertEqual(len(observed), 12)
        self.assertEqual(report["closed_trades"], 0)
        self.assertIsNone(report["win_rate"])

    def test_next_bar_entry_and_stop_first(self):
        data = bars([77000] * 180)
        data[169] = replace(data[169], high=85000, low=72000)
        f = {"1h": data, "4h": [], "1d": [], "1w": []}
        with patch("elliottbot.backtest.decide", return_value=decision()):
            report = run(f, Settings(), quote()["rules"], bars=12)
        self.assertEqual(report["fills"][0]["time"], data[169].time)
        self.assertIn("손절 우선", report["fills"][1]["reason"])
        self.assertLess(D(report["fills"][1]["net"]), 0)
        self.assertEqual(len([f for f in report["fills"] if f["kind"] == "ENTRY"]), 1)


if __name__ == "__main__":
    unittest.main()

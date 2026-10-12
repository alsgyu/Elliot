import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location("research_costs",Path(__file__).resolve().parents[1]/"scripts/summarize_elliott_research.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SensitivityTests(unittest.TestCase):
    def test_identity_and_larger_costs_both_sides(self):
        for direction in ("LONG","SHORT"):
            report={"settings":{"fee_bps":5,"slippage_bps":5},"fills":[
                {"kind":"ENTRY","price":"100","quantity":"2","decision":{"action":direction}},
                {"kind":"EXIT","price":"110" if direction=="LONG" else "90","net":"18"}]}
            result=module.fixed_trade_costs(report,1)
            self.assertEqual(float(result["fixed_trade_stress_net"]),18)
            self.assertLess(float(module.fixed_trade_costs(report,2)["fixed_trade_stress_net"]),18)

    def test_unclosed_entry_not_included(self):
        report={"settings":{"fee_bps":5,"slippage_bps":5},"fills":[{"kind":"ENTRY"}]}
        self.assertEqual(module.fixed_trade_costs(report)["closed_trades"],0)


if __name__=="__main__":unittest.main()

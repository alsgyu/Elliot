"""Same-trade cost sensitivity, distinct from re-running entry eligibility."""
from decimal import Decimal as D
import json
from pathlib import Path
import sys


def fixed_trade_costs(report, multiple=2):
    fee, slip = D(str(report["settings"]["fee_bps"]))/10000, D(str(report["settings"]["slippage_bps"]))/10000
    entry, net, original, count = None, D(0), D(0), 0
    for fill in report["fills"]:
        if fill["kind"] == "ENTRY":
            entry = fill
        elif fill["kind"] == "EXIT":
            if entry is None:
                raise ValueError("Exit without matching entry")
            d = D(1 if entry["decision"]["action"] == "LONG" else -1)
            qty, opened, closed = D(entry["quantity"]), D(entry["price"]), D(fill["price"])
            funding = (closed-opened)*qty*d - (opened+closed)*qty*fee - D(fill["net"])
            stressed_entry = opened/(1+d*slip)*(1+d*slip*multiple)
            stressed_exit = closed/(1-d*slip)*(1-d*slip*multiple)
            net += (stressed_exit-stressed_entry)*qty*d - (stressed_entry+stressed_exit)*qty*fee*multiple - funding
            original += D(fill["net"])
            count += 1
            entry = None
    return {"closed_trades":count,"closed_net":str(original),"fixed_trade_stress_net":str(net)}


def main():
    root=Path(sys.argv[1])
    summary=json.loads((root/"summary.json").read_text())
    reports=[json.loads((root/f"fold-{i}-research.json").read_text()) for i in (1,2,3)]
    results=[{"fold":i+1,**fixed_trade_costs(r)} for i,r in enumerate(reports)]
    data={"version":summary["version"],"days":30,"capital_per_fold":reports[0]["settings"]["capital"],
          "closed_trades":sum(r["closed_trades"] for r in results),
          "closed_net":str(sum((D(r["closed_net"]) for r in results),D(0))),
          "fixed_trade_stress_net":str(sum((D(r["fixed_trade_stress_net"]) for r in results),D(0))),
          "folds":results,"profitable_strategy_validated":False,
          "method":"Same completed trades, fixed quantities/times/trigger outcomes/funding, doubled fees and adverse fill slippage. Not an executable re-run or untouched holdout. Open trades excluded."}
    (root/"COST_SENSITIVITY.json").write_text(json.dumps(data,indent=2))
    print(json.dumps(data,indent=2))


if __name__=="__main__":main()

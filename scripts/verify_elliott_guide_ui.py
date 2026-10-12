"""Guide regression: intercepted HTTP only, plus parity with execution payoff."""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from elliottbot.execution import D, payoff, rounded
from elliottbot.models import ALL_INTERVALS, Candidate, Pivot
from elliottbot.service import Service


def fixture(tf, pattern, short=False):
    prices = {
        "WAVE_3": [83841.9, 84506.6, 84188.8],
        "WAVE_5": [82000, 83000, 82500, 85000, 84000],
        "WAVE_C": [83841.9, 84506.6, 84188.8],
        "IMPULSE_COMPLETE": [82000, 83000, 82500, 85000, 84000, 86000],
        "ZIGZAG_COMPLETE": [87249.6, 84444.4, 84872.1, 83841.9],
        "TRIANGLE_WATCH": [87000, 83000, 86000, 83500, 85500, 84000],
    }[pattern]
    step = ALL_INTERVALS[tf]
    start = int(time.time()*1000)//step*step - step*150
    down = prices[1] < prices[0]
    pivots = [Pivot(start+i*20*step, 170000-v if short else v,
                    "H" if (bool(i % 2) != down) != short else "L", start+(i*20+2)*step-1)
              for i, v in enumerate(prices)]
    direction = -1 if short else 1
    stop = pivots[-1].price
    invalid = pivots[0].price
    target = stop + direction*1.618*abs(pivots[1].price-pivots[0].price)
    if pattern == "WAVE_5":
        invalid = pivots[1].price
        target = stop + direction*1000
    elif pattern == "WAVE_C":
        target = stop + direction*abs(pivots[1].price-pivots[0].price)
    elif pattern == "IMPULSE_COMPLETE":
        direction = -direction
        invalid = stop
        target = pivots[-2].price
    elif pattern == "ZIGZAG_COMPLETE":
        invalid = stop
        target = pivots[0].price
    elif pattern == "TRIANGLE_WATCH":
        direction = 0
        stop = invalid = target = 0
    return Candidate(tf, pattern, direction, .003, pivots, 63.39, invalid, stop, target).to_json()


def verify_math(page):
    cases = []
    for direction in (1, -1):
        for fee in (0, 5, 20):
            for slip in (0, 2, 15):
                for rr in (1, 1.5, 3):
                    for tick in (.01, .1):
                        cases.append({"candidate": {"direction": direction, "stop": 84000.123,
                                                      "target": 84000+direction*3000.876},
                                      "settings": {"fee_bps": fee, "slippage_bps": slip, "min_rr": rr},
                                      "rules": {"tick": str(tick)}})
    results = page.evaluate("cases => cases.map(x => guideMath(x.candidate,x.settings,x.rules))", cases)
    for case, result in zip(cases, results):
        assert result and result["exists"], (case, result)
        direction = case["candidate"]["direction"]
        tick = D(case["rules"]["tick"])
        stop = rounded(case["candidate"]["stop"], tick, up=direction < 0)
        target = rounded(case["candidate"]["target"], tick, up=direction < 0)
        assert math.isclose(result["stop"], float(stop), abs_tol=1e-8)
        assert math.isclose(result["target"], float(target), abs_tol=1e-8)
        fee = D(case["settings"]["fee_bps"])/10000
        slip = D(case["settings"]["slippage_bps"])/10000
        price = D(result["boundary"])
        loss, reward = payoff(price, stop, target, direction, fee, slip)
        assert reward/loss >= D(case["settings"]["min_rr"]), (case, result)
        loss_next, reward_next = payoff(price+direction*tick, stop, target, direction, fee, slip)
        assert reward_next/loss_next < D(case["settings"]["min_rr"]), (case, result)
        js = page.evaluate("x => guidePayoff(...x)", [float(price), float(stop), float(target), direction, float(fee), float(slip)])
        assert math.isclose(js["loss"], float(loss), abs_tol=1e-7)
        assert math.isclose(js["reward"], float(reward), abs_tol=1e-7)
    assert page.evaluate("guideMath({direction:1,stop:100,target:100.01},{fee_bps:100,slippage_bps:100,min_rr:10},{tick:'0.01'}).exists") is False
    assert page.evaluate("guideMath({direction:0,stop:0,target:0},{},{})") is None
    assert page.evaluate("guideMath({direction:1,stop:100,target:90},{fee_bps:5,slippage_bps:2,min_rr:1.5},{})") is None
    assert page.evaluate("guideMath({direction:1,stop:100,target:120},{fee_bps:NaN,slippage_bps:2,min_rr:1.5},{})") is None
    return len(cases)


def verify_manual_math(page):
    cases=[]
    settings={"fee_bps":5,"slippage_bps":2,"min_rr":1.5}
    for pattern in ("WAVE_3","WAVE_5","WAVE_C","IMPULSE_COMPLETE","ZIGZAG_COMPLETE"):
        for short in (False,True):
            c=fixture("30m",pattern,short)
            entry=c["stop"]+(c["target"]-c["stop"])*.25
            confirmed=c["pivots"][-1]["confirmed_at"]
            bar={"time":confirmed-1799999,"end":confirmed,"open":entry,"high":entry,"low":entry,"close":entry}
            context={"candles":[bar],"interval":1800000,"analysis_asof":confirmed}
            for filled in (False,True):
                for qty in (None,.003):
                    cases.append({"c":c,"entry":entry,"options":{"direction":c["direction"],"filled":filled,"quantity":qty},
                                  "settings":settings,"rules":{"tick":"0.1"},"context":context})
    plans=page.evaluate("cases=>cases.map(x=>manualEvaluate(x.c,x.entry,x.options,x.settings,x.rules,x.context))",cases)
    for case,plan in zip(cases,plans):
        assert not plan.get("error"),plan
        assert plan["direction"]==case["options"]["direction"]
        c=case["c"];d=c["direction"];fee=D('.0005');slip=D('.0002')
        entry=D(case["entry"])
        modeled=entry if case["options"]["filled"] else entry*(1+d*slip)
        stop=rounded(c["stop"],'.1',up=d<0)
        stop_fill=stop*(1-d*slip)
        loss=(modeled-stop_fill)*d+(modeled+stop_fill)*fee
        qty=D(case["options"]["quantity"] or 1)
        assert math.isclose(plan["loss"],float(loss*qty),abs_tol=1e-7),(case,plan)
        for t in plan["targets"]:
            fill=D(t["price"])*(1-d*slip)
            profit=(fill-modeled)*d-(modeled+fill)*fee
            assert math.isclose(t["reward"],float(profit*qty),abs_tol=1e-7)
            assert math.isclose(t["rr"],float(profit/loss),abs_tol=1e-8)
        breakeven_fill=D(plan["breakEven"])*(1-d*slip)
        assert abs((breakeven_fill-modeled)*d-(modeled+breakeven_fill)*fee)<D('.000001')
        assert any(t["primary"] for t in plan["targets"])
    base=deepcopy(cases[0])
    def evaluate(x):
        return page.evaluate("x=>manualEvaluate(x.c,x.entry,x.options,x.settings,x.rules,x.context)",x)
    first=evaluate(base)
    assert [t["price"] for t in first["targets"]]==[84853.5,85264.2,85928.9]
    changed=deepcopy(base);changed["entry"]=84700
    second=evaluate(changed)
    assert first["stop"]==second["stop"] and [t["price"] for t in first["targets"]]==[t["price"] for t in second["targets"]]
    assert not next(t for t in second["targets"] if t["primary"])["pricePass"]
    assert any(t["pricePass"] and not t["primary"] for t in second["targets"])
    assert any("먼 목표" in text for text in second["issues"])
    for field,value in (("direction",-1),("quantity",0),("quantity",-1)):
        bad=deepcopy(base);bad["options"][field]=value
        assert evaluate(bad).get("error")
    for original in (cases[0],cases[4]):
        for filled in (False,True):
            for invalid in (None,"",0,2,"candidate"):
                missing=deepcopy(original);missing["options"].update(direction=invalid,filled=filled)
                assert "직접 선택" in evaluate(missing)["error"]
                assert evaluate(missing)["showLines"] is False
            missing=deepcopy(original);missing["options"].pop("direction")
            missing["options"]["filled"]=filled
            assert "직접 선택" in evaluate(missing)["error"]
    bad=deepcopy(base);bad["entry"]=bad["c"]["stop"]
    assert not evaluate(bad)["showLines"]
    for original in (cases[0],cases[4]):
        protected=deepcopy(original);protected["options"]["filled"]=True
        protected["entry"]=protected["c"]["stop"]-protected["c"]["direction"]*1000
        guarded=evaluate(protected)
        assert guarded["loss"]<0 and guarded["showLines"]
        assert all(t["rr"] is None for t in guarded["targets"])
        assert any("본전·이익 보호" in w for w in guarded["warnings"])
        protected["options"]["filled"]=False
        assert not evaluate(protected)["showLines"]
    stale=deepcopy(base);stale["context"]["stale"]=True
    assert not evaluate(stale)["showLines"] and any("지연" in s for s in evaluate(stale)["issues"])
    no_history=deepcopy(base);no_history["context"]["candles"]=[]
    assert not evaluate(no_history)["showLines"]
    touched=deepcopy(base)
    bar=deepcopy(touched["context"]["candles"][0]);bar.update(time=bar["end"]+1,end=bar["end"]+1800000,high=86000)
    touched["context"]["forming"]=bar
    got=evaluate(touched)
    assert all(t["touchedAt"] and t["touchedForming"] for t in got["targets"])
    assert not any(t["pricePass"] for t in got["targets"])
    breached=deepcopy(touched);breached["context"]["forming"]["low"]=84000
    assert not evaluate(breached)["showLines"] and any("침범" in s for s in evaluate(breached)["issues"])
    for short in (False,True):
        capped=fixture("30m","WAVE_5",short)
        prices=[80000,84000,82000,85000,84500]
        if short:prices=[170000-p for p in prices]
        for pivot,price in zip(capped["pivots"],prices):pivot["price"]=price
        capped.update(stop=prices[-1],target=prices[-1]+capped["direction"]*3000,invalidation=prices[1])
        levels=page.evaluate("c=>manualTargets(c,.1)",capped)
        assert all((t["price"]-capped["stop"])*capped["direction"]<=3000+.000001 for t in levels)
        assert any(t["primary"] for t in levels)
    return len(cases)


def verify_manual_ui(page,out):
    expect(page.locator('[name="manual-direction"]:checked')).to_have_count(0)
    page.locator("#guide-entry").fill("84400")
    expect(page.locator("#manual-summary")).to_contain_text("방향 선택 대기")
    expect(page.locator("#manual-reference-side")).to_have_text("파동 예상 LONG(롱)")
    expect(page.locator("#manual-position-summary")).to_contain_text("방향 미선택")
    assert page.evaluate("manualChartLevels().length")==0
    page.locator('[name="manual-direction"][value="1"]').check()
    expect(page.locator("#manual-targets tr")).to_have_count(3)
    expect(page.locator("#manual-targets")).to_contain_text("1.618")
    expect(page.locator("#manual-targets")).to_contain_text("2.618")
    expect(page.locator("#manual-warnings")).to_contain_text("미확인")
    expect(page.locator("#manual-summary")).to_contain_text("최적값이나 진입 신호가 아니며")
    plan=page.evaluate("manualReview")
    page.locator("#manual-quantity").fill("0.002")
    expect(page.locator("#manual-profit-heading")).to_have_text("목표 순손익 · USDT")
    assert math.isclose(page.evaluate("manualReview.loss"),plan["loss"]*.002,abs_tol=1e-8)
    page.locator('[name="manual-entry-kind"][value="filled"]').check()
    expect(page.locator("#guide-entry-title")).to_have_text("실제 체결가 · USDT")
    expect(page.locator("#guide-entry")).to_have_value("")
    expect(page.locator("#manual-quantity")).to_have_value("")
    expect(page.locator('[name="manual-direction"]:checked')).to_have_count(0)
    assert page.evaluate("manualChartLevels().length")==0
    page.locator("#guide-entry").fill("84400")
    expect(page.locator("#manual-summary")).to_contain_text("방향 선택 대기")
    page.locator("#manual-review").screenshot(path=str(out/"manual-direction-required.png"))
    page.locator('[name="manual-direction"][value="1"]').check()
    page.locator("#manual-quantity").fill("0.002")
    expect(page.locator("#manual-position-summary")).to_contain_text("입력한 체결 포지션: LONG(롱)")
    expect(page.locator("#guide-calculation")).to_contain_text("진입 슬리피지는 재차 더하지")
    assert page.evaluate("manualReview.loss") < plan["loss"]*.002
    # Polling must preserve both the explicit direction and the entered fill.
    page.evaluate("refresh()")
    expect(page.locator('[name="manual-direction"][value="1"]')).to_be_checked()
    expect(page.locator('[name="manual-entry-kind"][value="filled"]')).to_be_checked()
    expect(page.locator("#guide-entry")).to_have_value("84400")
    expect(page.locator("#manual-quantity")).to_have_value("0.002")
    page.locator("#guide-entry").fill("83000")
    expect(page.locator("#guide-calculation")).to_contain_text("본전·이익 보호")
    expect(page.locator("#manual-metrics")).to_contain_text("손절선 청산 예상 순손익")
    assert all(t["rr"] is None for t in page.evaluate("manualReview.targets"))
    page.locator("#guide-entry").fill("")
    expect(page.locator("#guide-calculation")).to_contain_text("실제 체결가 입력이 필요")
    assert page.evaluate("manualChartLevels().length")==0
    page.locator("#guide-entry").fill("84400")
    page.locator('[name="manual-direction"][value="-1"]').check()
    expect(page.locator("#manual-summary")).to_contain_text("반대입니다")
    expect(page.locator("#manual-targets")).to_contain_text("입력 SHORT(숏)과 참고 파동의 예상 LONG(롱)은 반대")
    expect(page.locator("#manual-summary")).to_contain_text("같은 방향 후보 2개")
    expect(page.locator("#manual-position-summary")).to_contain_text("입력한 체결 포지션: SHORT(숏)")
    expect(page.locator("#manual-reference-side")).to_have_text("파동 예상 LONG(롱)")
    expect(page.locator("#manual-metrics")).to_be_empty()
    assert page.evaluate("manualChartLevels().length")==0
    page.locator("#manual-review").screenshot(path=str(out/"manual-direction-conflict.png"))
    page.locator("#guide-entry").fill("")
    expect(page.locator("#manual-summary")).to_contain_text("반대입니다")
    page.locator("#guide-entry").fill("84400")
    page.locator('[name="manual-direction"][value="1"]').check()
    page.locator("#manual-quantity").fill("0")
    expect(page.locator("#manual-summary")).to_contain_text("양수")
    page.locator("#manual-quantity").fill("0.002")
    assert page.evaluate("manualChartLevels().length")==5
    assert page.evaluate("manualChartLevels()[0].name")=="LONG 입력 체결가"
    page.locator("#show-manual-plan").uncheck()
    assert page.evaluate("manualChartLevels().length")==0
    page.locator("#show-manual-plan").check()
    for width,height in ((1440,1100),(390,844),(360,800)):
        page.set_viewport_size({"width":width,"height":height})
        page.locator("#manual-review").screenshot(path=str(out/f"manual-{width}.png"))
        page.locator(".chart").screenshot(path=str(out/f"manual-chart-{width}.png"))
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert not page.locator("#manual-review p,#manual-review dd,.manual-controls input,.manual-controls span,.manual-reference").evaluate_all("els=>els.filter(e=>e.scrollWidth>e.clientWidth+2).map(e=>e.id)")
        pixels=page.locator("#chart").evaluate("c=>{let n=0,d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=4)if(d[i])n++;return n;}")
        assert pixels>1000
    page.locator('[name="manual-entry-kind"][value="planned"]').check()
    expect(page.locator("#guide-entry")).to_have_value("")
    expect(page.locator('[name="manual-direction"]:checked')).to_have_count(0)
    page.locator('[name="manual-direction"][value="1"]').check()
    page.set_viewport_size({"width":1440,"height":1100})


def main():
    with tempfile.TemporaryDirectory() as root:
        service = Service(root, sample=True)
        try:
            service.tick()
            state = service.status()
        finally:
            service.close()
    state.update(csrf="test-only", error=None, entry_gate=None)
    state["settings"].update(strategy_mode="strict", fee_bps=5, slippage_bps=2, min_rr=1.5)
    state["quote"].update(price=84813.9, bid=84813.8, ask=84814.0)
    state["quote"]["rules"]["tick"] = "0.1"
    charts = {}
    for tf in ALL_INTERVALS:
        cs = [fixture(tf, p) for p in ("WAVE_3", "ZIGZAG_COMPLETE", "WAVE_5", "IMPULSE_COMPLETE", "WAVE_C", "TRIANGLE_WATCH")]
        cs.append(fixture(tf, "WAVE_3", short=True))
        step = ALL_INTERVALS[tf]
        start = cs[0]["pivots"][0]["time"]
        bars = [{"time": start+i*step, "end": start+(i+1)*step-1,
                 "open": 84400+200*math.sin(i/5), "close": 84430+200*math.sin(i/5),
                 "high": 84470+200*math.sin(i/5), "low": 84390+200*math.sin(i/5), "volume": 10}
                for i in range(150)]
        frame = {"timeframe": tf, "state": "SELECTED", "selected": cs[0]["id"], "bias": 1,
                 "candidates": cs, "rejected": [], "closed_at": bars[-1]["end"]}
        state["decision"]["frames"][tf] = frame
        state["candles"][tf] = bars
        charts[tf] = {"mode": "paper", "sample": True, "timeframe": tf, "interval": step,
                      "candles": bars, "frame": frame, "swings": [], "fetched_at": time.time(),
                      "analysis_asof": bars[-1]["end"], "forming": None}
    state["decision"].update(action="WAIT", selection=None, trigger=None, scenarios=[], checks=[], reason="검증용 구조 미확인")
    baseline_settings = deepcopy(state["settings"])
    backtest = {"running": False, "error": None, "report": None}
    out = ROOT / "artifacts/elliott-guide-ui"
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def fulfill(route):
            url = urlsplit(route.request.url)
            if route.request.method != "GET" or url.hostname != "elliott.test":
                writes.append(route.request.url)
                route.abort()
                return
            if url.path == "/api/status":
                route.fulfill(json=state)
            elif url.path == "/api/chart":
                route.fulfill(json=charts[parse_qs(url.query)["timeframe"][0]])
            elif url.path == "/api/backtest":
                route.fulfill(json=backtest)
            else:
                file = ROOT / "elliottbot/static" / ("index.html" if url.path == "/" else url.path.lstrip("/"))
                if url.path == "/vendor/lucide.min.js":
                    file = ROOT / "kgridbot/static/vendor/lucide.min.js"
                if not file.is_file():
                    route.fulfill(status=404, body="not found")
                    return
                content_type = "text/html" if url.path == "/" else "text/css" if url.path.endswith(".css") else "application/javascript"
                route.fulfill(content_type=content_type, body=file.read_bytes())

        page.route("**/*", fulfill)
        page.goto("http://elliott.test/")
        page.wait_for_function("chartData?.timeframe === '4h'")
        math_cases = verify_math(page)
        manual_cases = verify_manual_math(page)
        page.locator('[data-tf="30m"]').click()
        page.wait_for_function("chartData?.timeframe === '30m'")
        expect(page.locator("#guide-state")).to_contain_text("참고 전용")
        expect(page.locator("#guide-levels")).to_contain_text("83,841.90")
        expect(page.locator("#guide-levels")).to_contain_text("84,188.80")
        expect(page.locator("#guide-levels")).to_contain_text("85,264.28")
        expect(page.locator("#guide-bounds")).to_contain_text("손익비만 본 상한")
        expect(page.locator("#guide-trigger")).to_contain_text("미확정")
        expect(page.locator("#guide-calculation")).to_contain_text("방향을 먼저 선택")
        verify_manual_ui(page,out)
        page.locator("#guide-entry").fill("84200")
        expect(page.locator("#guide-calculation")).to_contain_text("직접 입력")
        expect(page.locator("#guide-calculation")).to_contain_text("손익비 계산만 충족")
        page.evaluate("render()")
        expect(page.locator("#guide-entry")).to_have_value("84200")
        page.locator("#guide-entry").fill("84188.8")
        expect(page.locator("#guide-calculation")).to_contain_text("잘못된 쪽")
        page.locator("#guide-entry").fill("0")
        expect(page.locator("#guide-calculation")).to_contain_text("양수")
        page.locator("#guide-entry").fill("")
        for index, expected in ((1, "C 연장"), (2, "4파가"), (3, "5파 연장"), (4, "B파가"), (5, "미산정"), (6, "다음 3파가 하락")):
            page.locator("#chart-count").select_option(charts["30m"]["frame"]["candidates"][index]["id"])
            expect(page.locator("#chart-guide")).to_contain_text(expected)
            expect(page.locator("#guide-entry")).to_have_value("")
            expect(page.locator("#manual-quantity")).to_have_value("")
            expect(page.locator('[name="manual-direction"]:checked')).to_have_count(0)
            assert page.evaluate("manualChartLevels().length")==0
            if index == 5:
                expect(page.locator("#guide-entry")).to_be_disabled()
                expect(page.locator("#guide-bounds")).to_contain_text("미산정")
        expect(page.locator("#guide-bounds")).to_contain_text("손익비만 본 하한")
        page.locator('[name="manual-direction"][value="1"]').check()
        page.locator("#guide-entry").fill("85800")
        expect(page.locator("#manual-summary")).to_contain_text("입력 LONG(롱)과 참고 파동의 예상 SHORT(숏)은 반대")
        assert page.evaluate("manualChartLevels().length")==0
        page.locator("#guide-entry").fill("")
        page.locator('[name="manual-direction"][value="-1"]').check()
        expect(page.locator("#guide-calculation")).to_contain_text("매수호가")
        page.locator("#guide-entry").fill("85000")
        expect(page.locator("#guide-calculation")).to_contain_text("손익비 미달")
        page.locator("#guide-entry").fill("85800")
        expect(page.locator("#guide-calculation")).to_contain_text("손익비 계산만 충족")
        assert page.evaluate("manualChartLevels().length")==5
        assert page.evaluate("manualChartLevels()[0].name")=="SHORT 가정 진입"
        page.locator('[name="manual-entry-kind"][value="filled"]').check()
        expect(page.locator('[name="manual-direction"]:checked')).to_have_count(0)
        page.locator('[name="manual-direction"][value="-1"]').check()
        page.locator("#guide-entry").fill("85800")
        expect(page.locator("#manual-position-summary")).to_contain_text("입력한 체결 포지션: SHORT(숏)")
        assert page.evaluate("manualChartLevels()[0].name")=="SHORT 입력 체결가"
        page.locator("#manual-review").screenshot(path=str(out/"manual-short.png"))
        page.locator('[name="manual-entry-kind"][value="planned"]').check()
        page.locator('[name="manual-direction"][value="-1"]').check()
        page.locator("#guide-entry").fill("85800")
        state["settings"]["strategy_mode"] = "multi"
        page.evaluate("refresh()")
        expect(page.locator("#guide-state")).to_contain_text("독립 진입하지")
        expect(page.locator("#guide-calculation")).to_contain_text("비용 비중 초과")
        page.locator('[data-tf="1h"]').click()
        page.wait_for_function("chartData?.timeframe === '1h'")
        expect(page.locator("#guide-entry")).to_have_value("")
        c = deepcopy(charts["1h"]["frame"]["candidates"][0])
        c["eligible"] = True
        charts["1h"]["frame"]["candidates"][0]["eligible"] = True
        state["decision"].update(action="LONG", selection=c, trigger={"price": 84300, "bar_close": 84310, "timeframe": "30m"})
        page.evaluate("refresh()")
        expect(page.locator("#guide-trigger")).to_contain_text("84,300")
        expect(page.locator("#guide-trigger")).to_contain_text("직접 하한이 아닙니다")
        expect(page.locator("#guide-state")).to_contain_text("체결 완료를 뜻하지")
        page.evaluate("latest.decision.trigger.bar_end=1; renderChartGuide()")
        expect(page.locator("#guide-trigger")).to_contain_text("이미 만료")
        page.evaluate("refresh()")
        page.locator("#chart-count").select_option(charts["1h"]["frame"]["candidates"][1]["id"])
        expect(page.locator("#guide-trigger")).to_contain_text("미확정")
        page.locator("#chart-count").select_option(c["id"])
        page.evaluate("latest.sample=false; latest.last_update=1; renderChartGuide()")
        expect(page.locator("#guide-state")).to_contain_text("자료 지연")
        page.evaluate("latest.sample=true; chartCount='auto'; renderChartGuide()")
        page.locator('[name="manual-direction"][value="1"]').check()
        # No quote must not silently fall back to a candle close or mark price.
        page.evaluate("latest.quote.ask=null; renderChartGuide()")
        expect(page.locator("#guide-calculation")).to_contain_text("최근 종가로 대체하지")
        page.evaluate("refresh()")
        screens = []
        for width, height in ((1440, 1100), (390, 844), (360, 800)):
            page.set_viewport_size({"width": width, "height": height})
            page.locator(".guide-details").evaluate("e=>e.open=true")
            page.locator("#chart-guide").screenshot(path=str(out / f"guide-{width}.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not page.locator("#chart-guide p,#chart-guide dd,#chart-guide input").evaluate_all(
                "els=>els.filter(e=>e.scrollWidth>e.clientWidth+2).map(e=>e.id || e.tagName)"
            )
            pixels = page.locator("#chart").evaluate("c=>{let n=0,d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=4)if(d[i])n++;return n;}")
            assert pixels > 1000
            screens.append({"width": width, "canvas_pixels": pixels})
        page.locator('[data-view="settings"]').click()
        expect(page.locator('#active-preset')).to_be_visible()
        page.locator('#active-preset').click()
        expect(page.locator('[name="strategy_mode"]')).to_have_value('active')
        expect(page.locator('[name="max_entries_per_day"]')).to_have_value('8')
        expect(page.locator('[name="cooldown_minutes"]')).to_have_value('15')
        expect(page.locator('[name="max_stop_percent"]')).to_have_value('1')
        payload=page.evaluate("settingsPayload($('settings-form'))")
        for field in ('capital','risk_percent','daily_loss_percent','leverage','max_notional'):
            assert float(payload[field])==state['settings'][field]
        for width,height in ((1440,1100),(390,844),(360,800)):
            page.set_viewport_size({'width':width,'height':height})
            page.locator('#settings-view').screenshot(path=str(out/f'active-settings-{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.locator('#revert').click()
        expect(page.locator('[name="strategy_mode"]')).to_have_value('multi')
        state['settings']['strategy_mode']='active'
        page.evaluate('refresh()')
        page.locator('[data-view="analysis"]').click()
        page.locator('[data-tf="30m"]').click()
        page.wait_for_function("chartData?.timeframe==='30m'")
        expect(page.locator('#guide-state')).to_contain_text('30분봉은 진입 후보')
        expect(page.locator('#guide-range-caution')).to_contain_text('구조 손절 거리 상한은 1.00%')
        page.evaluate('chartData.frame={...chartData.frame,candidates:[]};renderWaveEvidence()')
        assert page.evaluate('chartFrame().candidates.length')>0
        page.locator('[data-tf="15m"]').click()
        page.wait_for_function("chartData?.timeframe==='15m'")
        expect(page.locator('#wave-status')).to_contain_text('15분봉 · 하위 확인 전용')
        expect(page.locator('#guide-state')).to_contain_text('독립 진입하지')
        for width,height in ((1440,1100),(390,844),(360,800)):
            page.set_viewport_size({'width':width,'height':height})
            page.locator('.chart-section').screenshot(path=str(out/f'active-fifteen-{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        report_fixture={'net':'0','return':0,'max_drawdown':0,'closed_trades':0,'win_rate':None,
                        'average_trade':None,'bars':12,'assumptions':[],'curve':[],'wait_reasons':{}}
        backtest['report']={'comparison':{name:{**report_fixture,'net':str(net)}
                                         for name,net in [('baseline',-3),('multi',-2),('active',-1)]},
                            'verdict':'모의 비교 검증','notes':[]}
        page.evaluate('pollBacktest()')
        expect(page.locator('#bt-net')).to_have_text('-1.00')
        expect(page.locator('#comparison-results')).to_contain_text('단기 적극형')
        page.evaluate("latest.settings.strategy_mode='strict';pollBacktest()")
        expect(page.locator('#bt-net')).to_have_text('-3.00')
        page.evaluate("latest.settings.strategy_mode='active'")
        page.locator('[data-tf="1h"]').click()
        page.wait_for_function("chartData?.timeframe==='1h'")
        page.evaluate("latest.decision.frames[activeTf].candidates=[]; chartCount='auto'; renderWaveEvidence()")
        expect(page.locator("#guide-summary")).to_contain_text("현재는 설명할 후보가 없어")
        expect(page.locator("#guide-entry")).to_be_disabled()
        assert not writes, writes
        assert not errors, errors
        assert state["account"]["running"] is False
        assert all(state["settings"][k] == v for k, v in baseline_settings.items() if k != "strategy_mode")
        browser.close()
    print(json.dumps({"ok": True, "payoff_parity_cases": math_cases, "manual_payoff_cases":manual_cases,"screens": screens,
                      "real_network_requests": 0, "order_requests": 0}))


if __name__ == "__main__":
    main()

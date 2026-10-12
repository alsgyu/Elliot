"""Intercepted browser checks: no live server, external traffic, or order requests."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from elliottbot.service import Service
from elliottbot.chart import ChartReader


def main():
    with tempfile.TemporaryDirectory() as root:
        service=Service(root,sample=True)
        try:
            service.update_settings({"strategy_mode":"active","research_mode":True})
            service.tick()
            base=service.status()
            charts={tf:ChartReader(service).get(tf) for tf in ("4h","30m","15m")}
        finally:service.close()
    output=ROOT/"artifacts/elliott-research-ui"
    output.mkdir(parents=True,exist_ok=True)
    errors=[];writes=[];results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (1440,390,360):
            for mode in ("paper","live"):
                state=deepcopy(base);state.update(mode=mode,csrf="test-token")
                if mode=="live":state["settings"]["research_mode"]=False
                page=browser.new_page(viewport={"width":width,"height":1000})
                page.on("pageerror",lambda error:errors.append(str(error)))
                def route(request):
                    url=urlsplit(request.request.url)
                    if request.request.method!="GET":
                        writes.append(url.path);request.fulfill(status=403,json={"error":"all writes disabled"});return
                    if url.path=="/api/status":request.fulfill(json=state)
                    elif url.path=="/api/chart":
                        from urllib.parse import parse_qs
                        tf=parse_qs(url.query).get("timeframe",["4h"])[0]
                        request.fulfill(json=charts[tf])
                    elif url.path=="/api/backtest":request.fulfill(json={"running":False,"report":None})
                    elif url.path.startswith("/api/"):request.fulfill(status=404,json={"error":"blocked"})
                    else:
                        file=ROOT/"elliottbot/static"/("index.html" if url.path=="/" else url.path.lstrip("/"))
                        if url.path=="/vendor/lucide.min.js":file=ROOT/"kgridbot/static/vendor/lucide.min.js"
                        request.fulfill(body=file.read_bytes(),content_type="text/html" if url.path=="/" else "text/css" if url.path.endswith(".css") else "application/javascript")
                page.route("**/*",route)
                page.goto("http://elliott.test/")
                expect(page.locator("#decision-meta")).to_contain_text("모의 연구" if mode=="paper" else "시장가")
                page.locator('[data-view="settings"]').click()
                if mode=="live":
                    expect(page.locator("#research-preset")).to_be_disabled()
                    expect(page.locator('[name="research_mode"]')).to_be_disabled()
                else:
                    before=page.evaluate("['capital','leverage','risk_percent','daily_loss_percent'].map(k=>document.querySelector(`[name=${k}]`).value)")
                    page.locator("#research-preset").click()
                    payload=page.evaluate("settingsPayload(document.getElementById('settings-form'))")
                    assert payload["research_mode"] is True
                    assert payload["strategy_mode"]=="active"
                    assert float(payload["research_risk_multiplier"])==.5
                    after=page.evaluate("['capital','leverage','risk_percent','daily_loss_percent'].map(k=>document.querySelector(`[name=${k}]`).value)")
                    assert before==after
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"),width
                page.screenshot(path=str(output/f"settings-{mode}-{width}.png"),full_page=True)
                page.locator('[data-view="analysis"]').click()
                page.locator('[data-tf="30m"]').click()
                page.screenshot(path=str(output/f"analysis-{mode}-{width}.png"),full_page=True)
                results.append({"width":width,"mode":mode,"protected":True})
                page.close()
        browser.close()
    assert not errors,errors
    assert not writes,writes
    print(json.dumps({"ok":True,"screens":results,"writes":0,"external_requests":0}))


if __name__=="__main__":main()

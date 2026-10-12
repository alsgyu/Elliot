"""Read-only public-data chart QA against an isolated, keyless PAPER server."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    out = Path("artifacts/elliott-chart-ui")
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width":1440, "height":1100})
        before = page.request.get(args.url + "/api/status").json()
        assert before["mode"] == "paper" and not before["sample"]
        assert not before["account"]["running"] and not before["credentials_saved"]
        errors, writes = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: writes.append(r.url) if r.method != "GET" else None)
        page.goto(args.url)
        page.wait_for_function("() => chartData?.timeframe === '4h' && chartData.forming")
        expect(page.locator("#bar-count")).to_contain_text("진행봉 포함")
        expect(page.locator("#chart-warning")).to_be_hidden()
        page.locator("#chart-range").select_option("1500")
        assert page.evaluate("chartSlice().bars.length") > 180
        page.locator("#chart-range").select_option("120")
        page.locator("#chart-window").fill("0")
        page.locator("#chart-window").dispatch_event("input")
        expect(page.locator("#bar-count")).to_contain_text("마감봉만")
        page.locator("#chart-latest").click()
        expect(page.locator("#bar-count")).to_contain_text("진행봉 포함")
        page.locator("#chart").focus()
        page.keyboard.press("Home")
        expect(page.locator("#bar-count")).to_contain_text("마감봉만")
        page.keyboard.press("End")
        expect(page.locator("#bar-count")).to_contain_text("진행봉 포함")
        results = []
        for tf in ("4h", "30m"):
            page.locator(f'[data-tf="{tf}"]').click()
            page.wait_for_function("tf => chartData?.timeframe === tf && chartData.forming", arg=tf)
            frame = page.evaluate("chartFrame()")
            if tf == "30m":
                expect(page.locator("#wave-status")).to_contain_text("하위 확인 전용" if before["settings"].get("strategy_mode")=="multi" else "참고 전용")
                assert all(not c["eligible"] for c in frame["candidates"])
            if frame["candidates"]:
                candidate = frame["candidates"][-1]
                page.locator("#chart-count").select_option(candidate["id"])
                expect(page.locator("#wave-pivots tr")).to_have_count(len(candidate["pivots"]))
                page.locator("#chart-fit").click()
                expect(page.locator("#chart-caption")).not_to_contain_text("범위 밖")
            for selector in ("#show-tail", "#show-levels", "#show-fib", "#show-subwaves"):
                page.locator(selector).check()
            page.locator("#chart").hover()
            expect(page.locator("#chart-ohlc")).to_contain_text("거래량")
            for width,height in ((1440,1100),(390,844),(360,800)):
                page.set_viewport_size({"width":width,"height":height})
                page.screenshot(path=str(out/f"{tf}-{width}.png"),full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (tf,width)
                assert not page.locator("button:visible,select:visible").evaluate_all("els=>els.filter(e=>e.scrollWidth>e.clientWidth+2).map(e=>e.id)"), (tf,width)
                pixels=page.locator("#chart").evaluate("c=>{let n=0,d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=4)if(d[i])n++;return n;}")
                assert pixels>1000,(tf,width,pixels)
                results.append({"timeframe":tf,"width":width,"pixels":pixels})
        # A delayed response for another timeframe must not replace the active chart.
        page.locator('[data-tf="1w"]').click()
        page.locator('[data-tf="30m"]').click()
        page.wait_for_function("() => chartData?.timeframe === '30m'")
        expect(page.locator("#wave-status")).to_contain_text("30분봉")
        after=page.request.get(args.url+"/api/status").json()
        assert after["settings"]==before["settings"]
        assert not after["account"]["running"] and not after["credentials_saved"]
        assert not writes,writes
        assert not errors,errors
        browser.close()
        print(json.dumps({"ok":True,"read_only":True,"screens":results}))


if __name__=="__main__":
    main()

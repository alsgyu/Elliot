"""Browser acceptance check against a disposable paper database."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8765")
        expect(page.locator("#connection")).to_have_text("엔진 연결됨")
        expect(page.locator("#halt")).to_have_text("진입 중단")
        page.get_by_role("button", name="전략 설정", exact=True).click()
        page.locator('input[name="leverage"]').fill("3")
        page.locator('input[name="allocated_margin"]').fill("500")
        page.locator('input[name="grid_count"]').fill("9")
        page.get_by_role("button", name="설정 검증", exact=True).click()
        expect(page.locator("#preview-message")).to_contain_text("검증 통과")
        page.get_by_role("button", name="설정 적용", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("설정을 저장")
        page.reload()
        expect(page.locator("#leverage")).to_have_text("3×")
        page.get_by_role("button", name="전략 설정", exact=True).click()
        expect(page.locator('input[name="grid_count"]')).to_have_value("9")
        page.locator('input[name="lower_price"]').fill("90000")
        page.get_by_role("button", name="설정 검증", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("하한 가격")
        page.get_by_role("button", name="되돌리기", exact=True).click()
        page.screenshot(path=str(output / "settings-desktop.png"), full_page=True)
        page.get_by_role("button", name="운용 현황", exact=True).click()
        page.locator('[data-action="resume"]').click()
        expect(page.locator("#halt")).to_have_text("매매 중")
        expect(page.locator("#order-count")).not_to_have_text("0")
        page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
        page.get_by_role("button", name="시나리오 분석", exact=True).click()
        page.get_by_role("button", name="분석 실행", exact=True).click()
        expect(page.locator("#scenarios tr")).to_have_count(3, timeout=120000)
        expect(page.locator("#analytics-library")).to_contain_text("0.0.86")
        page.screenshot(path=str(output / "research-desktop.png"), full_page=True)
        page.get_by_role("button", name="운용 현황", exact=True).click()
        for width, height in [(390, 844), (360, 800)]:
            page.set_viewport_size({"width": width, "height": height})
            for view in ["dashboard", "settings", "research"]:
                page.locator(f'[data-view="{view}"]').click()
                page.screenshot(path=str(output / f"{view}-{width}.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, view, "horizontal overflow")
                overflows = page.locator("button:visible").evaluate_all("els => els.filter(e => e.scrollWidth > e.clientWidth + 2).map(e => e.textContent)")
                assert not overflows, (width, view, overflows)
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.locator('[data-view="dashboard"]').click()
        pixels = page.locator("#price-chart").evaluate("c => { const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data; let n=0; for(let i=3;i<d.length;i+=4) if(d[i]>0)n++; return n; }")
        assert pixels > 500, pixels
        page.locator('[data-action="pause"]').click()
        expect(page.locator("#halt")).to_have_text("진입 중단")
        page.locator('[data-action="cancel-and-flatten"]').click()
        expect(page.locator("#confirm-dialog")).to_be_visible()
        page.get_by_role("button", name="취소", exact=True).click()
        page.locator('[data-action="cancel-and-flatten"]').click()
        page.get_by_role("button", name="정리 실행", exact=True).click()
        expect(page.locator("#position")).to_have_text("0.000 BTC")
        expect(page.locator("#order-count")).to_have_text("0")
        assert not errors, errors
        browser.close()
        print(json.dumps({"ok": True, "viewports": [1440, 390, 360], "canvas_nonblank_pixels": pixels,
                          "checks": ["persist settings", "invalid range", "start", "pause", "flatten", "cancel confirmation", "three scenarios", "no overflow", "no JS errors"]}))


if __name__ == "__main__":
    main()

"""UI compatibility regression with fully intercepted HTTP; never touches an account."""
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from elliottbot.service import Service

LEGACY_FIELDS = {"capital", "max_notional", "leverage", "risk_percent", "daily_loss_percent",
                 "total_loss_percent", "min_rr", "min_score", "conflict_gap", "fee_bps",
                 "slippage_bps", "max_spread_bps", "allow_short"}


def main():
    results = []
    with tempfile.TemporaryDirectory() as root:
        service = Service(root, sample=True)
        try:
            base = service.status()
        finally:
            service.close()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for legacy in (True, False):
            state = json.loads(json.dumps(base))
            state["csrf"] = "simulated-token"
            if legacy:
                state["settings"] = {k: v for k, v in state["settings"].items() if k in LEGACY_FIELDS}
                state.pop("limits", None)
            state["settings"].update(capital=739, leverage=5 if legacy else 10)
            state["preflight"] = {"ok": False, "checks": [
                {"name": "가용 담보", "ok": False, "detail": "가용 736.91138633 USDT / 배정 739 USDT"}]}
            writes, errors = [], []
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda e: errors.append(str(e)))

            def fulfill(route):
                path = urlsplit(route.request.url).path
                if route.request.method != "GET":
                    if path != "/api/settings":
                        route.fulfill(status=403, json={"error": "No trading endpoints in this test"})
                        return
                    data = route.request.post_data_json
                    writes.append(data)
                    if set(data) - set(state["settings"]):
                        route.fulfill(status=400, json={"error": "알 수 없는 설정입니다."})
                        return
                    for key, value in data.items():
                        state["settings"][key] = value if key in {"allow_short", "strategy_mode", "research_mode"} else float(value)
                    state["preflight"] = None
                    route.fulfill(json={"ok": True, "settings": state["settings"]})
                elif path == "/api/status":
                    route.fulfill(json=state)
                elif path == "/api/chart":
                    route.fulfill(json={"mode": "paper", "sample": True, "timeframe": "4h", "interval": 14400000,
                                        "candles": [], "frame": None, "swings": []})
                elif path.startswith("/api/"):
                    route.fulfill(json={"running": False, "report": None, "error": None})
                else:
                    file = ROOT / "elliottbot/static" / ("index.html" if path == "/" else path.lstrip("/"))
                    if path == "/vendor/lucide.min.js":
                        file = ROOT / "kgridbot/static/vendor/lucide.min.js"
                    if not file.is_file():
                        route.fulfill(status=404, body="not found")
                        return
                    content_type = "text/html" if path == "/" else "text/css" if path.endswith(".css") else "application/javascript"
                    route.fulfill(content_type=content_type, body=file.read_bytes())

            page.route("**/*", fulfill)
            page.goto("http://elliott.test/")
            page.locator('[data-view="settings"]').click()
            expect(page.locator('[name="capital"]')).to_be_enabled()
            expect(page.locator('[name="capital"]')).to_have_value("739")
            expect(page.locator('[name="leverage"]')).to_have_attribute("max", "5" if legacy else "10")
            expect(page.locator('[name="leverage"]')).to_have_value("5" if legacy else "10")
            if legacy:
                expect(page.locator('[name="strategy_mode"]')).to_be_hidden()
                expect(page.locator('[name="max_entries_per_day"]')).to_be_disabled()
            else:
                expect(page.locator('[name="strategy_mode"]')).to_be_enabled()
            page.locator('[name="capital"]').fill("736")
            page.locator('[name="allow_short"]').uncheck()
            page.get_by_role("button", name="설정 저장", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("저장했습니다")
            assert len(writes) == 1 and writes[0]["capital"] == "736"
            assert writes[0]["leverage"] == ("5" if legacy else "10") and writes[0]["allow_short"] is False
            assert set(writes[0]) == set(state["settings"])
            assert ("strategy_mode" not in writes[0]) == legacy
            page.reload()
            page.locator('[data-view="settings"]').click()
            expect(page.locator('[name="capital"]')).to_have_value("736")
            expect(page.locator('[name="leverage"]')).to_have_value("5" if legacy else "10")
            expect(page.locator('[name="allow_short"]')).not_to_be_checked()
            page.locator('[data-view="connect"]').click()
            expect(page.locator("#preflight")).to_contain_text("계정 검사 대기")
            assert not errors, errors
            results.append({"legacy": legacy, "saved_capital": state["settings"]["capital"],
                            "supported_fields": len(writes[0]), "real_network_requests": 0})
            page.close()
        browser.close()
    print(json.dumps({"ok": True, "cases": results}))


if __name__ == "__main__":
    main()

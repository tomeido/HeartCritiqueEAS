"""Isolated collection directory checks: ``python test_sources_ui.py``.

Requires Playwright/Chromium. All API and external requests are intercepted;
CHROMIUM_PATH can select an existing browser. Safe to import during pytest.
"""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit


def directory():
    sources = []
    for number, (name, domain, board, kind, status) in enumerate([
        ("디시인사이드", "dcinside.com", "실시간 베스트", "html", "pending"),
        ("뽐뿌", "ppomppu.co.kr", "자유게시판", "rss", "ok"),
        ("뽐뿌", "ppomppu.co.kr", "뽐뿌게시판", "rss", "empty"),
        ("FM코리아", "fmkorea.com", "인기글", "html", "blocked"),
        ("블라인드", "teamblind.com", "공개 인기글", "html", "disabled"),
        ("오류 사이트", "example.com", "유머", "html", "error"),
    ]):
        checked = None if status in ("pending", "disabled") else "2026-09-26T01:00:00Z"
        sources.append({
            "id": str(number), "name": name, "domain": domain, "board": board,
            "url": f"https://{domain}/board", "kind": kind,
            "enabled": status != "disabled", "status": status,
            "last_checked_at": checked, "http_code": 403 if status == "blocked" else 200,
            "discovered": 8 if status == "ok" else 0,
            "captured": 2 if status == "ok" else 0,
            "note": "접근 제한으로 수집 제외" if status == "disabled" else "",
            "error": "네트워크 오류" if status == "error" else "",
        })
    return {"enabled": False, "interval_sec": 300, "feeds": 5,
            "last_poll_at": None, "next_poll_at": None, "last_result": None,
            "sources": sources}


async def main():
    from playwright.async_api import async_playwright, expect

    html = (Path(__file__).parent / "static/index.html").read_text()
    async with async_playwright() as p:
        launch = {"headless": True}
        if os.environ.get("CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = await p.chromium.launch(**launch)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            requests = []
            state = {"payload": directory(), "failure": False, "delay": None}
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(request):
                url = request.request.url
                path = urlsplit(url).path
                requests.append(path)
                if url == "http://ui.test/":
                    await request.fulfill(content_type="text/html", body=html)
                elif path == "/api/sources":
                    if state["delay"]:
                        await state["delay"].wait()
                    if state["failure"]:
                        await request.fulfill(status=404, json={"detail": "Not Found"})
                    else:
                        await request.fulfill(json=state["payload"])
                elif path == "/api/config":
                    await request.fulfill(json={"auth_mode": "guest", "vote_threshold": 3})
                elif path == "/api/auth/guest":
                    await request.fulfill(json={"token": "test.guest-test.signature"})
                elif path in ("/api/stories", "/api/my/votes"):
                    await request.fulfill(json=[])
                elif path == "/api/wallet":
                    await request.fulfill(json={"network": "devnet", "error": "test wallet"})
                else:
                    await request.fulfill(body="")

            await page.route("**/*", route)
            await page.goto("http://ui.test/")
            assert "/api/sources" not in requests
            await page.locator("#sources-open").click()
            await expect(page.locator("#sources-open")).to_have_attribute("aria-expanded", "true")
            await expect(page.locator("#sources-search")).to_be_focused()
            await expect(page.locator(".source-item")).to_have_count(6)
            await expect(page.locator("#sources-summary-count")).to_have_text("5개 사이트 · 6개 목록")
            await expect(page.locator("#sources-runtime")).to_contain_text("자동 수집 꺼짐")
            await expect(page.locator("#sources-runtime")).not_to_contain_text("보존 대기")
            await expect(page.locator("#sources-enabled")).to_have_text("5")
            await expect(page.locator("#sources-problems")).to_have_text("3")
            await expect(page.locator('[data-source-id="0"]')).to_contain_text("첫 확인 대기")
            await expect(page.locator('[data-source-id="1"]')).to_contain_text("새 링크 8건 · 신규 캡처 2건")
            await expect(page.locator('[data-source-id="2"]')).to_contain_text("링크 미발견")
            await expect(page.locator('[data-source-id="4"]')).to_contain_text("접근 제한으로 수집 제외")
            assert not any(path.startswith("/api/stats") for path in requests)
            print("PASS: source directory is lazy, independent of stats, and separates configured targets from running state")

            for term, expected in [("뽐뿌", 2), ("자유게시판", 1), ("dcinside.com", 1), ("rss", 2), ("없는 사이트", 0)]:
                await page.locator("#sources-search").fill(term)
                await expect(page.locator(".source-item")).to_have_count(expected)
            await expect(page.locator("#sources-list")).to_contain_text("검색어나 필터에 맞는 사이트가 없습니다")
            await page.locator("#sources-search").fill("")
            for key, expected in [("enabled", 5), ("problems", 3), ("disabled", 1), ("all", 6)]:
                button = page.locator(f'[data-source-filter="{key}"]')
                await button.click()
                await expect(button).to_have_attribute("aria-pressed", "true")
                await expect(page.locator(".source-item")).to_have_count(expected)
            assert await page.evaluate("currentFilter") == "all"
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            print("PASS: Korean/board/domain/method search, filters, empty search and mobile layout")

            if os.environ.get("SOURCES_SCREENSHOT_DIR"):
                screenshot_dir = Path(os.environ["SOURCES_SCREENSHOT_DIR"])
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                await page.locator("#sources-search").blur()
                await page.screenshot(path=str(screenshot_dir / "sources-mobile.png"), full_page=True)
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.screenshot(path=str(screenshot_dir / "sources-desktop.png"), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})

            state["failure"] = True
            await page.locator("#sources-refresh").click()
            await expect(page.locator("#sources-message")).to_contain_text("이전 조회 결과")
            await expect(page.locator(".source-item")).to_have_count(6)
            await expect(page.locator("#sources-refresh")).to_be_enabled()
            state["failure"] = False
            state["payload"]["enabled"] = True
            state["payload"]["last_poll_at"] = "2026-09-26T01:00:00Z"
            state["payload"]["next_poll_at"] = "2026-09-26T01:05:00Z"
            state["payload"]["last_result"] = {
                "discovered": 8, "captured": 2, "queue_pending": 37, "queue_retry": 9,
            }
            await page.locator("#sources-refresh").click()
            await expect(page.locator("#sources-runtime")).to_contain_text("자동 수집 켜짐")
            await expect(page.locator("#sources-runtime")).to_contain_text("5분 간격")
            await expect(page.locator("#sources-runtime")).to_contain_text("신규 캡처 2건")
            await expect(page.locator("#sources-runtime")).to_contain_text("보존 대기 37건 · 재시도 9건")
            await expect(page.locator("#sources-message")).to_be_empty()
            print("PASS: fetch failure preserves clearly stale results; refresh recovers live schedule/results and persistent backlog")

            state["payload"]["sources"][0].update({
                "name": '<img src=x onerror="window.injected=true">',
                "url": "javascript:window.injected=true",
                "note": "<script>window.injected=true</script>",
            })
            await page.locator("#sources-refresh").click()
            await expect(page.locator('[data-source-id="0"] h3')).to_contain_text("<img")
            await expect(page.locator('[data-source-id="0"] a')).to_have_count(0)
            await expect(page.locator("#sources-list img, #sources-list script")).to_have_count(0)
            assert not await page.evaluate("Boolean(window.injected)")
            print("PASS: source metadata is escaped and links reject unsafe schemes")

            state["payload"]["sources"] = []
            await page.locator("#sources-refresh").click()
            await expect(page.locator("#sources-count")).to_have_text("등록된 수집 사이트가 없습니다.")
            await expect(page.locator(".source-item")).to_have_count(0)

            state["failure"] = True
            await page.reload()
            await page.locator("#sources-open").click()
            await expect(page.locator("#sources-message")).to_contain_text("목록을 불러오지 못했습니다")
            await expect(page.locator("#sources-refresh")).to_be_enabled()
            state["failure"] = False
            state["payload"] = directory()
            state["delay"] = asyncio.Event()
            await page.locator("#sources-refresh").click()
            await expect(page.locator("#sources-list")).to_have_attribute("aria-busy", "true")
            await expect(page.locator("#sources-refresh")).to_be_disabled()
            state["delay"].set()
            await expect(page.locator(".source-item")).to_have_count(6)
            await expect(page.locator("#sources-list")).to_have_attribute("aria-busy", "false")
            assert not errors, errors
            print("PASS: empty registry, initial 404, visible loading and retry recovery")
            await page.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

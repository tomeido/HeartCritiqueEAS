"""Isolated browser regression checks; run explicitly with ``python test_ui.py``.

Requires Playwright and its Chromium browser. CHROMIUM_PATH can select an
existing browser. Importing this module during pytest collection does not launch
browsers or require the optional Playwright dependency.
"""

import asyncio
import os
from pathlib import Path


async def main():
    from playwright.async_api import async_playwright, expect

    html = (Path(__file__).parent / "static/index.html").read_text()
    # Keep the real UI handlers while preventing startup API/auth requests. Every
    # resource is intercepted below, so this test cannot contact a live service.
    html = html.replace("\ninit();\n", "\n// Isolated browser test.\n")
    async with async_playwright() as p:
        launch = {"headless": True}
        if os.environ.get("CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = await p.chromium.launch(**launch)
        try:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(request):
                if request.request.url == "http://ui.test/":
                    await request.fulfill(content_type="text/html", body=html)
                else:
                    await request.fulfill(body="")

            await page.route("**/*", route)
            await page.goto("http://ui.test/")
            search = page.locator("#story-search")
            icon = page.locator(".list-search-icon")

            await page.keyboard.press("/")
            await expect(search).to_be_focused()
            await search.fill("typing")
            await page.keyboard.press("/")
            await expect(search).to_have_value("typing/")
            await page.locator("#theme-btn").focus()
            await page.keyboard.press("Control+/")
            await expect(page.locator("#theme-btn")).to_be_focused()
            print("PASS: / focuses search while typing and modified shortcuts remain native")

            await page.evaluate("""() => {
                cfg = { vote_threshold: 3 };
                window.pendingRequests = [];
                window.fetch = (url) => new Promise(resolve => pendingRequests.push({url, resolve}));
            }""")

            async def submit(value, count):
                await search.fill(value)
                await search.press("Enter")
                await page.wait_for_function("n => pendingRequests.length === n", arg=count)
                await expect(icon).to_have_class("list-search-icon spin")

            async def respond(index, body, ok=True):
                await page.evaluate("""([index, body, ok]) => {
                    pendingRequests[index].resolve({ok, status: ok ? 200 : 503, json: async () => body});
                }""", [index, body, ok])

            def story(identifier):
                return {"id": identifier, "body": identifier, "category": "kindness",
                        "vote_count": 0, "threshold": 3, "created_at": "2026-07-08T00:00:00Z"}

            await submit("first", 1)
            await submit("second", 2)
            await respond(0, [story("stale-first")])
            await expect(icon).to_have_class("list-search-icon spin")
            await respond(1, [story("current-second")])
            await expect(icon).to_have_class("list-search-icon")
            await expect(page.locator("#story-list")).to_contain_text("current-second")
            await expect(page.locator("#story-list")).not_to_contain_text("stale-first")
            print("PASS: older response cannot stop the current search spinner")

            await submit("third", 3)
            await submit("fourth", 4)
            await respond(3, [story("current-fourth")])
            await expect(icon).to_have_class("list-search-icon")
            await respond(2, [story("stale-third")])
            await expect(page.locator("#story-list")).to_contain_text("current-fourth")
            await expect(page.locator("#story-list")).not_to_contain_text("stale-third")
            print("PASS: out-of-order responses cannot replace the latest results")

            await page.locator("#story-search-clear").click()
            await page.wait_for_function("pendingRequests.length === 5")
            await expect(icon).to_have_class("list-search-icon spin")
            await respond(4, [])
            await expect(icon).to_have_class("list-search-icon")
            await expect(search).to_have_value("")
            await expect(search).to_be_focused()
            print("PASS: clearing search shows and completes loading")

            await submit("failure", 6)
            await respond(5, {}, False)
            await expect(icon).to_have_class("list-search-icon")
            await expect(page.locator("#story-list")).to_contain_text("목록을 불러오지 못했습니다")
            print("PASS: failed search restores the icon and shows the error")
            assert not errors, errors
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Isolated browser regression checks; run explicitly with ``python test_ui.py``.

Requires Playwright and its Chromium browser. CHROMIUM_PATH can select an
existing browser. Importing this module during pytest collection does not launch
browsers or require the optional Playwright dependency.
"""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def story(identifier, body=None):
    return {"id": identifier, "body": body if body is not None else identifier,
            "category": "kindness", "vote_count": 0, "threshold": 3,
            "created_at": "2026-07-08T00:00:00Z"}


async def check_startup_and_cache(browser, html, expect):
    """Run the real init() with API and third-party responses controlled separately."""
    async def open_page(storage_setup="", fragment=""):
        page = await browser.new_page()
        page.set_default_timeout(5000)
        if storage_setup:
            await page.add_init_script(storage_setup)
        state = {"requests": [], "pending": {}, "held": [], "events": {}, "errors": []}
        page.on("pageerror", lambda error: state["errors"].append(str(error)))

        async def route(request):
            url = request.request.url
            path = urlsplit(url).path
            state["requests"].append(url)
            if url == "http://ui.test/":
                await request.fulfill(content_type="text/html", body=html)
                return
            if not url.startswith("http://ui.test/"):
                # Fonts and SDK stay pending; no external network is contacted.
                key = "sdk" if "supabase" in url else "external"
            elif path == "/api/config":
                key = "config"
            elif path == "/api/stories":
                key = "search" if "q=" in url else "stories"
            elif path.startswith("/api/stories/"):
                key = "detail"
            else:
                payload = {
                    "/api/auth/guest": {"token": "test.guest-test.signature"},
                    "/api/my/votes": [],
                    "/api/wallet": {"network": "devnet", "error": "test wallet unavailable"},
                    "/api/stats": {
                        "stories": {"total": 12, "archived": 0},
                        "citations": {}, "votes": {"total": 0},
                    },
                    "/api/stats/timeseries": [],
                }.get(path, {"vote_count": 0, "threshold": 3})
                state["events"].setdefault(path, asyncio.Event()).set()
                await request.fulfill(json=payload)
                return
            state["held"].append(request)
            state["pending"][key] = request
            state["events"].setdefault(key, asyncio.Event()).set()

        async def pending(key):
            await asyncio.wait_for(
                state["events"].setdefault(key, asyncio.Event()).wait(), timeout=5)
            return state["pending"][key]

        await page.route("**/*", route)
        # Waiting for load would intentionally hang on the pending font/SDK.
        await page.goto("http://ui.test/" + fragment, wait_until="commit")
        await pending("stories")
        await pending("config")
        return page, state, pending

    async def close_page(page, state):
        # Complete held routes before closing so Playwright has no abandoned tasks.
        await asyncio.gather(*(request.abort() for request in state["held"]),
                             return_exceptions=True)
        await page.close()

    for mode in ("supabase", "guest"):
        page, state, pending = await open_page()
        try:
            assert not any("supabase" in url for url in state["requests"])
            await (await pending("stories")).fulfill(
                json=[story(f"startup-{n}") for n in range(12)])
            await expect(page.locator("#story-list .list-item")).to_have_count(10)
            await page.locator("#load-more-btn").click()
            await expect(page.locator("#story-list .list-item")).to_have_count(12)
            assert not await page.evaluate("Boolean(session)")
            assert not any("/api/stats" in url for url in state["requests"])

            config = {"auth_mode": mode, "vote_threshold": 3}
            if mode == "supabase":
                config.update(supabase_url="https://auth.test", supabase_anon_key="test")
            await (await pending("config")).fulfill(json=config)
            if mode == "supabase":
                sdk = await pending("sdk")
                await expect(page.locator("#story-list .list-item")).to_have_count(12)
                assert not await page.evaluate("Boolean(sb)")
                await sdk.fulfill(content_type="application/javascript", body="""
                    window.supabase = {createClient: () => ({auth: {
                        onAuthStateChange() {},
                        async getSession() { return {data: {session: null}}; }
                    }})};
                """)
                await expect(page.locator("#btn-login-header")).to_be_visible()
            else:
                await expect(page.locator("#auth-area")).to_contain_text("게스트")
                assert not any("supabase" in url for url in state["requests"])

            assert not any("/api/stats" in url for url in state["requests"])
            await page.locator("#dash-collapse > summary").click()
            await expect(page.locator("#stat-total")).to_have_text("12")
            await asyncio.wait_for(
                state["events"].setdefault("/api/stats/timeseries", asyncio.Event()).wait(),
                timeout=5)
            assert not state["errors"], state["errors"]
            print(f"PASS: {mode} startup shows stories and supports load more before auth; stats load on demand")
        finally:
            await close_page(page, state)

    page, state, pending = await open_page(fragment="#story=archived-mainnet")
    try:
        archived = {**story("archived-mainnet", "인증보다 먼저 읽을 수 있는 박제 전문"),
                    "arweave_tx_id": "test-tx", "arweave_url": "https://devnet.irys.xyz/test-tx"}
        await (await pending("detail")).fulfill(json=archived)
        await expect(page.locator("#story-body")).to_have_text(archived["body"])
        public_key = "02" + "ab" * 32
        await (await pending("config")).fulfill(json={
            "auth_mode": "supabase", "supabase_url": "https://auth.test",
            "supabase_anon_key": "test", "irys_network": "mainnet",
            "agent_public_key": public_key, "vote_threshold": 3,
        })
        await pending("sdk")
        await expect(page.locator("#archive-hero")).to_have_attribute(
            "href", "https://gateway.irys.xyz/test-tx")
        await expect(page.locator("#archive-lbl")).to_have_text("Arweave 영구 박제됨")
        await expect(page.locator("#verify-rows")).to_contain_text("mainnet · 영구")
        await expect(page.locator("#vf-project-key")).to_have_attribute("title", public_key)
        assert not state["errors"], state["errors"]
        print("PASS: early archived deep link updates mainnet URL and verification key before auth finishes")
    finally:
        await close_page(page, state)

    cached_story = story("cached-homepage")
    snapshots = {
        "valid": "JSON.stringify({savedAt: Date.now(), stories: " + json.dumps([cached_story]) + "})",
        "expired": "JSON.stringify({savedAt: Date.now() - 61000, stories: " + json.dumps([cached_story]) + "})",
        "invalid_json": "'{broken'",
        "invalid_entry": "JSON.stringify({savedAt: Date.now(), stories: [null]})",
        "blocked": None,
    }
    for name, snapshot in snapshots.items():
        setup = (
            "Object.defineProperty(window, 'sessionStorage', {get() {"
            "throw new DOMException('Storage blocked', 'SecurityError'); }});"
            if snapshot is None else
            f"sessionStorage.setItem('hc_story_list_v1', {snapshot});"
        )
        page, state, pending = await open_page(setup)
        try:
            listing = page.locator("#story-list")
            if name == "valid":
                await expect(listing).to_contain_text("cached-homepage")
            else:
                await expect(listing).not_to_contain_text("cached-homepage")
            await (await pending("stories")).fulfill(json=[story("fresh-homepage")])
            await expect(listing).to_contain_text("fresh-homepage")
            await expect(listing).not_to_contain_text("cached-homepage")
            if name == "valid":
                saved = await page.evaluate("sessionStorage.getItem('hc_story_list_v1')")
                assert json.loads(saved)["stories"][0]["id"] == "fresh-homepage"
                search = page.locator("#story-search")
                await search.fill("search-only")
                await search.press("Enter")
                await (await pending("search")).fulfill(json=[story("search-only")])
                await expect(listing).to_contain_text("search-only")
                assert await page.evaluate("sessionStorage.getItem('hc_story_list_v1')") == saved
            assert not state["errors"], state["errors"]
            print(f"PASS: {name} session cache preserves immediate/fresh homepage behavior")
        finally:
            await close_page(page, state)


async def main():
    from playwright.async_api import async_playwright, expect

    full_html = (Path(__file__).parent / "static/index.html").read_text()
    # Keep the real UI handlers while preventing startup API/auth requests. Every
    # resource is intercepted below, so this test cannot contact a live service.
    html = full_html.replace("\ninit();\n", "\n// Isolated browser test.\n")
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

            for body in (
                "한글 본문입니다.\n마지막 줄까지 한 번에 보여야 합니다.",
                "💖🙂 이모지로 시작하는 전문입니다.",
                "  \n  들여쓰기와 줄바꿈을 그대로 보존합니다.",
                '<img src=x onerror="window.bodyInjected=true"> & <script>bad()</script>',
            ):
                result = await page.evaluate("""story => {
                    showStory(story);
                    const body = document.getElementById('story-body');
                    return {text: body.textContent, elements: body.children.length,
                            injected: Boolean(window.bodyInjected)};
                }""", story("full-body", body))
                assert result == {"text": body, "elements": 0, "injected": False}
            print("PASS: full text appears synchronously, preserving Korean, emoji, whitespace and safe literal HTML")

            await page.evaluate("""story => {
                pendingRequests = [];
                session = null;
                showStory(story);
                session = {access_token: 'test-token', user: {id: 'test-user'}};
                refreshVoteStatus(story.id);
            }""", story("vote-race"))
            await respond(1, {"vote_count": 1, "threshold": 3, "already_voted": True})
            await expect(page.locator("#btn-vote")).to_have_text("✓ 투표 완료")
            await respond(0, {"vote_count": 0, "threshold": 3, "already_voted": False})
            await page.evaluate("() => new Promise(requestAnimationFrame)")
            await expect(page.locator("#btn-vote")).to_have_text("✓ 투표 완료")
            await expect(page.locator("#vote-num")).to_have_text("1")
            print("PASS: delayed anonymous vote status cannot overwrite the authenticated result")
            assert not errors, errors
            await page.close()
            await check_startup_and_cache(browser, full_html, expect)
        finally:
            await browser.close()


import pytest

@pytest.mark.asyncio
async def test_palette_micro_ux():
    """Verify the micro-UX improvements (focus routing, disabled button feedback, toast on copy)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        # Give permission to write to clipboard
        browser = await p.chromium.launch()
        context = await browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
        page = await context.new_page()

        # We need to stub window.cfg so that the frontend doesn't throw when trying to render
        await page.add_init_script("""
            window.cfg = { vote_threshold: 3 };
        """)

        await page.goto("file:///app/static/index.html")

        # Test 1: Check if .gen-card has the correct attributes for programmatic focus
        print("Checking .gen-card attributes...")
        gen_card = page.locator("#gen-card")
        tabindex = await gen_card.get_attribute("tabindex")
        style = await gen_card.get_attribute("style")
        assert tabindex == "-1", "tabindex should be -1"
        assert "outline: none" in style.lower(), "style should include outline: none"
        print("Test 1 Passed!")

        # Test 2: Check disabled button title attribute
        print("Checking disabled button title attribute...")
        btn_vote = page.locator("#btn-vote")
        title = await btn_vote.get_attribute("title")
        assert title == "로그인이 필요합니다", "title should be 로그인이 필요합니다"
        print("Test 2 Passed!")

        # Test 3: Trigger toast on copy wallet address
        # We need to first make the wallet copy button visible
        await page.evaluate("""
            const walletBox = document.getElementById('wallet-box');
            walletBox.style.display = 'block';
            const copyBtn = document.getElementById('wallet-copy');
            if (copyBtn) {
                copyBtn.dataset.addr = '0x1234567890abcdef';
                copyBtn.style.display = 'block';
            }
        """)

        print("Testing wallet copy toast...")
        copy_btn = page.locator("#wallet-copy")
        await copy_btn.click()

        toast = page.locator(".toast.success").last
        await toast.wait_for(state="visible", timeout=2000)
        toast_text = await toast.inner_text()
        assert "지갑 주소가 복사되었습니다" in toast_text, f"Expected toast text not found, got {toast_text}"
        print("Test 3 Passed!")

        # Verify scrollFocus is defined
        print("Checking scrollFocus exists...")
        is_defined = await page.evaluate("typeof scrollFocus === 'function'")
        assert is_defined, "scrollFocus should be defined as a function"
        print("Test 4 Passed!")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_palette_micro_ux())

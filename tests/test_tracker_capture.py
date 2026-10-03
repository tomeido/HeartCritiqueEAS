"""실제 커뮤니티 HTML 형태의 본문 확보·오탐·스트림 상한 회귀 테스트."""

import asyncio

import httpx

from services import tracker


def observe(monkeypatch, html):
    monkeypatch.setattr(tracker, "_is_safe_url", lambda _: (True, "", "93.184.216.34"))

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, content=html, headers={"content-type": "text/html; charset=utf-8"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await tracker.fetch_observation(
                "https://community.example/post/1", client, capture_text=True)

    return asyncio.run(run())


def test_captures_article_after_large_navigation(monkeypatch):
    # 인벤 뉴스 본문은 94KB 부근에서 시작한다. 옛 80KB 상한은 메뉴만 보관했다.
    html = b"<style>" + b" " * 94_000 + b"</style><article>" + "기사 실제 본문".encode() + b"</article>"
    obs = observe(monkeypatch, html)
    assert obs["text"] == "기사 실제 본문"
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "live"


def test_response_limit_truncates_oversized_chunk_exactly(monkeypatch):
    # 한 aiter_bytes 청크가 상한보다 커도 뒤쪽 텍스트는 캡처되지 않는다.
    monkeypatch.setattr(tracker, "MAX_BODY_BYTES", 120)
    prefix = b"<p>article body</p><script>"
    html = prefix + b" " * (120 - len(prefix)) + b"</script><p>over budget</p>"
    obs = observe(monkeypatch, html)
    assert obs["text"] == "article body"
    assert obs["del_match"] is False
    assert "over budget" not in obs["text"]


def test_pann_truncated_comment_script_is_not_a_deleted_article(monkeypatch):
    html = ("<article>네이트판 정상 본문</article>"
            "<script>switch(code) {case 3: alert('이미 삭제된 댓글입니다.');")
    obs = observe(monkeypatch, html.encode())
    assert obs["text"] == "네이트판 정상 본문"
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "live"


def test_theqoo_favorite_loader_does_not_hide_the_article(monkeypatch):
    html = ("<nav>전체 HOT 로그인 내 즐겨찾기 관리 로딩중</nav>"
            "<article>" + "정상 게시물의 읽을 수 있는 본문입니다. " * 20 + "</article>")
    obs = observe(monkeypatch, html.encode())
    assert obs["bot_challenge"] is False
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "live"


def test_todayhumor_comment_notice_is_not_a_deleted_article(monkeypatch):
    html = ("<article>오늘의유머 게시물 본문</article>"
            "<div id='blind_all_memo_desc' style='display:none'>"
            "댓글 분란 또는 분쟁 때문에 전체 댓글이 블라인드 처리되었습니다.</div>")
    obs = observe(monkeypatch, html.encode())
    assert obs["del_match"] is False
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "live"


def test_short_loader_and_security_challenge_still_block_capture(monkeypatch):
    for html in ("<title>게시판</title><div>로딩 중</div>",
                 "<h1>보안 시스템</h1><p>잠시 기다리면 자동으로 접속됩니다</p>",
                 "<h1>Just a moment...</h1><p>Checking your browser</p>"):
        obs = observe(monkeypatch, html.encode())
        assert obs["bot_challenge"] is True
        verdict = tracker.decide_status(obs, obs["final_url"], None)
        assert verdict["status"] == "error"
        assert verdict["baseline"] is None


def test_capture_excludes_page_chrome_and_article_hash_is_separate(monkeypatch):
    obs = observe(monkeypatch, ("<nav>메뉴 검색 로그인</nav><article>실제 원문입니다.</article>"
                               "<footer>사이트 안내</footer>").encode())
    assert obs["text"] == "실제 원문입니다."
    assert obs["article_hash"] != obs["text_hash"]
    assert "capture" not in obs  # capture_text is still read-only.


def test_capture_rejects_login_and_script_only_deletion(monkeypatch):
    for page, status in [
        ('<h1>로그인이 필요합니다</h1><form><input name="password"></form>', "blocked"),
        ("<script>alert('삭제된 게시물입니다.');history.back();</script><p>오류</p>", "deleted"),
        ('<main>로그인 후에 본문을 열람할 수 있습니다.</main>', "blocked"),
    ]:
        obs = observe(monkeypatch, page.encode())
        assert obs["text"] is None
        assert tracker.decide_status(obs, obs["final_url"], None)["status"] == status


def test_capture_artifacts_include_bounded_raw_headers_and_image_only(monkeypatch):
    monkeypatch.setattr(tracker, "_is_safe_url", lambda _: (True, "", "93.184.216.34"))
    raw = b'<article><img src="https://community.example/photo.png"></article>'
    async def run():
        def handler(request):
            assert not request.headers.get("cookie")
            assert not request.headers.get("authorization")
            return httpx.Response(200, content=raw, headers={"content-type": "text/html", "set-cookie": "private=yes"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), cookies={"session": "secret"},
                                     auth=httpx.BasicAuth("user", "password")) as client:
            return await tracker.fetch_observation("https://community.example/post/1", client,
                                                   capture_text=True, capture_artifacts=True)
    obs = asyncio.run(run())
    assert obs["text"] is None
    assert obs["article_status"] == "live"
    assert obs["capture"]["raw_body"] == raw
    assert obs["capture"]["media_urls"] == ["https://community.example/photo.png"]
    assert obs["capture"]["html_truncated"] is False
    assert "set-cookie" not in obs["capture"]["response_headers"]
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "live"


def test_known_site_without_article_body_is_not_captured(monkeypatch):
    monkeypatch.setattr(tracker, "_is_safe_url", lambda _: (True, "", "93.184.216.34"))
    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200,
            content=b'<nav>menu</nav><div>loading application</div>', headers={"content-type": "text/html"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await tracker.fetch_observation("https://www.inven.co.kr/board/webzine/2097/123", client,
                                                   capture_text=True, capture_artifacts=True)
    obs = asyncio.run(run())
    assert obs["text"] is None and "capture" not in obs
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "error"


def test_valid_body_with_deleted_text_in_navigation_is_not_false_deletion(monkeypatch):
    obs = observe(monkeypatch, ('<nav>삭제된 게시물입니다</nav><article>온전한 실제 원문</article>').encode())
    assert obs["text"] == "온전한 실제 원문"
    assert tracker.decide_status(obs, obs["final_url"], None)["status"] == "live"


def test_recheck_uses_article_template_with_legacy_page_baseline(monkeypatch):
    monkeypatch.setattr(tracker, "_is_safe_url", lambda _: (True, "", "93.184.216.34"))
    url = "https://www.inven.co.kr/board/webzine/2097/123"
    pages = [b'<nav>menu</nav><div id="powerbbsContent">Original article body</div>',
             "<script>alert('삭제된 게시물입니다.');history.back();</script>".encode(),
             "<div>로그인 후에 열람할 수 있습니다.</div>".encode(),
             b'<nav>menu</nav><div id="powerbbsContent">Original article body</div>']
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                200, content=pages.pop(0), headers={"content-type": "text/html"}))) as client:
            live = await tracker.fetch_observation(url, client, capture_text=True)
            res = tracker.decide_status(live, url, None)
            baseline = {**res["baseline"], "captured": True}
            assert baseline["hash"] == live["text_hash"] != live["article_hash"]
            deleted = await tracker.fetch_observation(url, client)
            assert "text" not in deleted and "capture" not in deleted
            verdict = tracker.decide_status(deleted, url, baseline)
            assert verdict["status"] == "deleted" and verdict["http_code"] == 200
            blocked = await tracker.fetch_observation(url, client)
            assert tracker.decide_status(blocked, url, baseline)["status"] == "blocked"
            restored = await tracker.fetch_observation(url, client)
            assert tracker.decide_status(restored, url, baseline)["status"] == "live"
    asyncio.run(run())


def test_supported_live_article_ignores_page_chrome_changes_with_baseline(monkeypatch):
    monkeypatch.setattr(tracker, "_is_safe_url", lambda _: (True, "", "93.184.216.34"))
    url = "https://www.inven.co.kr/board/webzine/2097/123"
    article = '<div id="powerbbsContent">실제 게시물 본문은 온전합니다.</div>'
    pages = ["<nav>" + "긴 메뉴 항목 " * 800 + "</nav>" + article,
             "<nav>삭제된 게시물입니다.</nav>" + article,
             "<nav>로그인 후에 열람할 수 있습니다.</nav>" + article,
             article]
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                200, content=pages.pop(0).encode(), headers={"content-type": "text/html"}))) as client:
            initial = await tracker.fetch_observation(url, client, capture_text=True)
            baseline = {**tracker.decide_status(initial, url, None)["baseline"], "captured": True}
            for _ in range(3):
                obs = await tracker.fetch_observation(url, client)
                assert obs["article_status"] == "live" and obs["article_supported"]
                assert tracker.decide_status(obs, url, baseline)["status"] == "live"
    asyncio.run(run())


def test_supported_live_parser_does_not_override_root_redirect_identity():
    obs = {"net": "ok", "http_code": 200, "final_url": "https://www.inven.co.kr/",
           "article_supported": True, "article_status": "live", "text_len": 3000,
           "text_hash": "identical", "del_match": False, "blk_match": False}
    baseline = {"captured": True, "final_url": "https://www.inven.co.kr/board/webzine/2097/123",
                "len": 3000, "hash": "identical", "del_match": False, "blk_match": False}
    assert tracker.decide_status(obs, baseline["final_url"], baseline)["status"] == "deleted"

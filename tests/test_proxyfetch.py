"""프록시 관측 채널(services/proxyfetch.py) 회귀 테스트.

핵심 계약(soft 전용 — 되돌릴 수 없는 박제 안전):
  · 프록시 판정은 절대 hard(404/410) http_code 를 만들지 않는다.
  · 삭제 표식/메인 리다이렉트 → soft deleted, 실체 본문 → live, 그 외 전부 유보.
  · 비활성/추적 가능 오류/프록시 실패 시 직접 관측 결과를 그대로 유지(안전한 no-op).
  · 프록시 관측 reason 이 붙으면 is_untrackable_source 가 False (추적 불가 라벨 해제).
"""

import asyncio

import services.proxyfetch as pf
from services.tracker import PROXY_OBSERVED_PREFIX, is_untrackable_source


# ── 순수 파서 ─────────────────────────────────────────────────────────────────
def test_parse_proxy_markdown_with_meta():
    raw = (
        "Title: 어떤 글 제목\n"
        "URL Source: https://www.fmkorea.com/best/123\n"
        "Markdown Content:\n"
        "본문 첫 줄입니다.\n둘째 줄."
    )
    p = pf.parse_proxy_markdown(raw)
    assert p["title"] == "어떤 글 제목"
    assert p["url_source"] == "https://www.fmkorea.com/best/123"
    assert p["content"].startswith("본문 첫 줄")


def test_parse_proxy_markdown_plain_fallback():
    p = pf.parse_proxy_markdown("그냥 본문 텍스트")
    assert p["title"] is None and p["url_source"] is None
    assert p["content"] == "그냥 본문 텍스트"


# ── soft 판정 ─────────────────────────────────────────────────────────────────
def test_decide_deletion_marker_soft_deleted():
    raw = "Markdown Content:\n삭제된 게시물입니다. 게시판으로 돌아가세요."
    v = pf.decide_from_proxy("https://www.fmkorea.com/best/1", raw)
    assert v["status"] == "deleted"
    assert v["reason"].startswith(PROXY_OBSERVED_PREFIX)
    assert "http_code" not in v   # 판정 자체는 코드를 부여하지 않는다(soft 보장)


def test_decide_root_redirect_soft_deleted():
    raw = (
        "Title: FM코리아\n"
        "URL Source: https://www.fmkorea.com/\n"
        "Markdown Content:\n" + ("메인 페이지 콘텐츠 " * 50)
    )
    v = pf.decide_from_proxy("https://www.fmkorea.com/best/12345", raw)
    assert v["status"] == "deleted"
    assert "리다이렉트" in v["reason"]


def test_decide_substantial_content_live():
    raw = ("Title: 글\nURL Source: https://www.fmkorea.com/best/9\n"
           "Markdown Content:\n" + ("살아있는 본문 내용입니다. " * 30))
    v = pf.decide_from_proxy("https://www.fmkorea.com/best/9", raw)
    assert v["status"] == "live"
    assert v["reason"].startswith(PROXY_OBSERVED_PREFIX)


def test_decide_challenge_or_short_is_none():
    # 프록시가 받아온 것도 챌린지 페이지 → 유보
    assert pf.decide_from_proxy("https://x.com/1", "Just a moment...") is None
    # 실체 없는 짧은 응답 → 유보
    assert pf.decide_from_proxy("https://x.com/1", "짧음") is None
    assert pf.decide_from_proxy("https://x.com/1", "") is None


# ── maybe_observe_via_proxy 통합(네트워크 없이 stub) ─────────────────────────
def _direct_error(code=430, reason="HTTP 430"):
    return {"status": "error", "http_code": code, "reason": reason, "baseline": None}


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(pf, "PROXY_FETCH_ENABLED", False)
    direct = _direct_error()
    out = asyncio.run(pf.maybe_observe_via_proxy(
        "https://www.fmkorea.com/best/1", None, direct))
    assert out is direct


def test_overrides_untrackable_error_with_proxy_verdict(monkeypatch):
    monkeypatch.setattr(pf, "PROXY_FETCH_ENABLED", True)

    async def fake_fetch(url, client):
        return "Markdown Content:\n삭제된 게시물입니다."
    monkeypatch.setattr(pf, "fetch_proxy_text", fake_fetch)

    out = asyncio.run(pf.maybe_observe_via_proxy(
        "https://www.fmkorea.com/best/1", None, _direct_error(430)))
    assert out["status"] == "deleted"
    assert out["http_code"] == 430          # 직접 관측 코드 유지 — hard(404/410) 불가
    assert out["reason"].startswith(PROXY_OBSERVED_PREFIX)
    assert out["baseline"] is None


def test_does_not_touch_real_deletion_or_trackable_errors(monkeypatch):
    monkeypatch.setattr(pf, "PROXY_FETCH_ENABLED", True)

    async def fake_fetch(url, client):
        raise AssertionError("호출되면 안 됨")
    monkeypatch.setattr(pf, "fetch_proxy_text", fake_fetch)

    # 진짜 hard 삭제(404)는 개입 금지 (is_untrackable_source 가 404 를 배제)
    hard = {"status": "deleted", "http_code": 404, "reason": "HTTP 404", "baseline": None}
    assert asyncio.run(pf.maybe_observe_via_proxy(
        "https://www.fmkorea.com/best/1", None, hard)) is hard

    # 추적 가능한 사이트의 일반 오류(타임아웃 등)도 개입 금지
    normal_err = {"status": "error", "http_code": None, "reason": "timeout", "baseline": None}
    assert asyncio.run(pf.maybe_observe_via_proxy(
        "https://www.clien.net/service/board/park/1", None, normal_err)) is normal_err


def test_proxy_failure_keeps_direct_result(monkeypatch):
    monkeypatch.setattr(pf, "PROXY_FETCH_ENABLED", True)

    async def fake_fetch(url, client):
        return None   # 프록시 자체 실패(레이트리밋 등)
    monkeypatch.setattr(pf, "fetch_proxy_text", fake_fetch)

    direct = _direct_error(403, "HTTP 403 (접근 거부·안티봇 가능)")
    out = asyncio.run(pf.maybe_observe_via_proxy(
        "https://www.fmkorea.com/best/1", None, direct))
    assert out is direct


# ── 추적 불가 라벨 해제 ───────────────────────────────────────────────────────
def test_proxy_observed_reason_clears_untrackable_label():
    url = "https://www.fmkorea.com/best/1"
    # 봇차단 도메인은 기본 추적 불가
    assert is_untrackable_source(url) is True
    # 프록시 관측으로 판정을 확보하면 라벨 해제 (코드가 봇차단 코드여도)
    assert is_untrackable_source(url, 430, f"{PROXY_OBSERVED_PREFIX}: 생존 확인") is False
    assert is_untrackable_source(url, None, f"{PROXY_OBSERVED_PREFIX}: 삭제 표식 — x") is False

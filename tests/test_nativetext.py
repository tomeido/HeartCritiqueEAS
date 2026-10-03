"""services/nativetext.py — Rust 가속/파이썬 폴백 텍스트 파이프라인 패리티 테스트.

두 층을 고정한다:
  1. 파이썬 폴백(PyPipeline)이 기존 tracker 동작(가시텍스트 추출·패턴 스캔)과 동일
  2. hc_native 가 설치된 환경(Docker)에선 Rust 파이프라인이 파이썬과 결과 동일
     (미설치 샌드박스에선 해당 테스트만 건너뜀 — 폴백 계약은 항상 검증)
"""

import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import nativetext  # noqa: E402
from services.tracker import (  # noqa: E402
    BLOCKED_PATTERNS,
    BOT_CHALLENGE_PATTERNS,
    DELETION_PATTERNS,
    _TEXT_PIPELINE,
    _visible_text,
)


def _py_pipe() -> nativetext.PyPipeline:
    return nativetext.PyPipeline(
        DELETION_PATTERNS.pattern, BLOCKED_PATTERNS.pattern, BOT_CHALLENGE_PATTERNS.pattern
    )


def _rust_pipe():
    try:
        import hc_native
    except ImportError:
        return None
    return nativetext.RustPipeline(
        hc_native, DELETION_PATTERNS.pattern, BLOCKED_PATTERNS.pattern,
        BOT_CHALLENGE_PATTERNS.pattern,
    )


# ── 픽스처: 실제 삭제/차단/봇 문구 + 오탐 함정 + 구조 케이스 ─────────────────
FIXTURES = [
    # (html, 기대 del, 기대 blk, 기대 bot) — 기대값은 파이썬 정답과 교차검증
    ("<html><body><p>삭제된 게시물입니다.</p></body></html>", True, False, False),
    ("<div>글쓴이에 의해 삭제된 글입니다</div>", True, False, False),
    ("<div>신고에 의해 삭제되었습니다</div>", True, False, False),
    ("<p>존재하지 않는 게시물입니다</p>", True, False, False),
    ("<p>페이지를 찾을 수 없습니다</p>", True, False, False),
    ("<p>블라인드 처리된 글</p>", True, False, False),
    ("<p>이 게시물이 블라인드 처리되었습니다.</p>", True, False, False),
    ("<p>블라인드 처리되었습니다.</p>", True, False, False),
    ("<p>deleted post</p>", True, False, False),
    ("<p>DELETED   By admin</p>", True, False, False),          # 대소문자 무시
    # 오탐 함정(살아있는 글의 정상 UI) — 매치되면 안 됨
    ("<p>댓글이 없습니다. 첫 댓글을 남겨보세요</p>", False, False, False),
    ("<p>검색 결과를 찾을 수 없습니다 (사이드 위젯)</p>", False, False, False),
    ("<p>존재하지 않는 회원입니다</p>", False, False, False),
    ("<p>차단된 글 보기 설정</p>", False, False, False),
    ("<p>블라인드 처리 안내</p>", False, False, False),
    ("<article>정상 글</article><div>댓글 분란 또는 분쟁 때문에 전체 댓글이 블라인드 처리되었습니다.</div>",
     False, False, False),
    # 차단(로그인 벽)
    ("<p>이 글을 보려면 로그인이 필요합니다</p>", False, True, False),
    ("<p>회원 전용 열람 게시판입니다</p>", False, True, False),
    ("<p>성인 인증 후 이용 가능</p>", False, True, False),
    ("<p>로그인 버튼은 우측 상단에 있습니다</p>", False, False, False),  # 상시 UI — 미발화
    # 봇 챌린지
    ("<p>보안 시스템에 의해 보호되고 있습니다</p>", False, False, True),
    ("<p>Just a moment...</p>", False, False, True),
    ("<p>잠시만 기다리시면 자동으로 접속됩니다</p>", False, False, True),
    ("<div>로딩 중</div>", False, False, True),
    ("<div>사이트 제목 Loading...</div>", False, False, True),
    ("<nav>내 즐겨찾기 관리 로딩중 HOT 카테고리</nav><article>" +
     "살아있는 게시물의 정상 본문입니다. " * 20 + "</article>", False, False, False),
    # 구조 케이스: script/style/주석/중첩 태그/NBSP/EUC-KR 흔한 공백
    ("<script>var x='삭제된 글';</script><p>정상 본문</p>", False, False, False),
    ("<style>.del::after{content:'삭제된 글'}</style><p>본문</p>", False, False, False),
    ("<!-- 삭제된 게시물 --><p>살아있는 본문</p>", False, False, False),
    ("<noscript>삭제된 글</noscript><p>ok</p>", False, False, False),
    ("<template><span>삭제된 글</span></template><p>ok</p>", False, False, False),
    ("<p>공 백　정리   테스트</p>", False, False, False),
    ("<SCRIPT>x</SCRIPT><P>대문자 태그도 제거</P>", False, False, False),
]

# 응답 상한이 숨겨진 블록 중간에서 끝나도 JS/주석을 본문으로 수집하면 안 된다.
TRUNCATED_FIXTURES = [
    ("<p>정상 본문</p><script>alert('이미 삭제된 댓글입니다.');", "정상 본문"),
    ("<p>정상 본문</p><STYLE>.x{content:'Just a moment'}", "정상 본문"),
    ("<p>정상 본문</p><noscript>삭제된 글", "정상 본문"),
    ("<p>정상 본문</p><template><div>삭제된 글</div>", "정상 본문"),
    ("<p>정상 본문</p><!-- 이미 삭제된 게시물", "정상 본문"),
    ("<script>숨김</script><p>정상 본문</p><script>삭제된 글", "정상 본문"),
    ("<script>숨김</script   ><p>정상 본문</p>", "정상 본문"),
    ("<!-- disabled <script> --> <p>정상 본문</p>", "정상 본문"),
    ("<script><!-- legacy wrapper </script><p>정상 본문</p>", "정상 본문"),
]


def test_python_fallback_matches_legacy_visible_text():
    """폴백 visible_text == 기존 tracker 구현(정규식 4종 순차 적용)과 동일."""
    strip = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1>")
    comment = re.compile(r"(?s)<!--.*?-->")
    tag = re.compile(r"<[^>]+>")
    ws = re.compile(r"\s+")

    def legacy(html):
        s = strip.sub(" ", html)
        s = comment.sub(" ", s)
        s = tag.sub(" ", s)
        return ws.sub(" ", s).strip()

    p = _py_pipe()
    for html, *_ in FIXTURES:
        assert p.visible_text(html) == legacy(html), html


def test_python_fallback_scan_expected():
    p = _py_pipe()
    for html, want_del, want_blk, want_bot in FIXTURES:
        text = p.visible_text(html)
        d, b, bot = p.scan(text)
        assert (d is not None) == want_del, f"del: {html!r} → {d!r}"
        assert (b is not None) == want_blk, f"blk: {html!r} → {b!r}"
        assert bot == want_bot, f"bot: {html!r}"
        # extract_and_scan 은 (visible_text, *scan) 과 동일해야 한다
        assert p.extract_and_scan(html) == (text, d, b, bot)


def test_truncated_hidden_blocks_do_not_leak_into_captured_text():
    p = _py_pipe()
    for html, expected in TRUNCATED_FIXTURES:
        assert p.extract_and_scan(html) == (expected, None, None, False)


def test_scan_snippet_is_leftmost_match():
    p = _py_pipe()
    text = p.visible_text("<p>앞부분 … 삭제된 글 입니다 … 뒤에도 이미 삭제 문구</p>")
    d, _, _ = p.scan(text)
    assert d is not None and d.startswith("삭제된")   # leftmost 대안 우선


def test_tracker_wired_to_pipeline():
    """tracker._visible_text 가 파이프라인 위임으로 바뀐 뒤에도 동작 동일."""
    html = "<script>skip</script><p>본문  텍스트</p><!-- c -->"
    assert _visible_text(html) == "본문 텍스트"
    assert _TEXT_PIPELINE.extract_and_scan(html)[0] == "본문 텍스트"


def test_rust_parity_if_available():
    """hc_native 설치 환경(Docker): Rust 결과 == 파이썬 결과 (완전 일치)."""
    r = _rust_pipe()
    if r is None:
        print("  (hc_native 미설치 — Rust 패리티는 Docker 이미지에서 검증)")
        return
    p = _py_pipe()
    for html, *_ in FIXTURES + TRUNCATED_FIXTURES:
        pv, rv = p.visible_text(html), r.visible_text(html)
        assert pv == rv, f"visible_text 불일치: {html!r}\n py={pv!r}\n rs={rv!r}"
        assert p.scan(pv) == r.scan(rv), f"scan 불일치: {html!r}"
        assert p.extract_and_scan(html) == r.extract_and_scan(html), html


def test_rust_parity_fuzz_if_available():
    """무작위 합성 페이지 200개에서 추출/스캔 완전 일치."""
    r = _rust_pipe()
    if r is None:
        return
    p = _py_pipe()
    rng = random.Random(7)
    words = ("삭제된 글 게시물 로그인 열람 회원 전용 보안 시스템 잠시 기다리 자동 "
             "따뜻한 본문 댓글 없습니다 찾을 수 deleted post Just a moment").split()
    tags = ["p", "div", "span", "li", "b"]
    for i in range(200):
        parts = [f"<!doctype html><body data-i='{i}'>"]
        for _ in range(rng.randint(3, 40)):
            roll = rng.random()
            if roll < 0.15:
                parts.append(f"<script>var a='{ ' '.join(rng.choices(words, k=4)) }';</script>")
            elif roll < 0.25:
                parts.append(f"<!-- { ' '.join(rng.choices(words, k=3)) } -->")
            elif roll < 0.3:
                parts.append(f"<style>.x{{content:'{rng.choice(words)}'}}</style>")
            else:
                t = rng.choice(tags)
                parts.append(f"<{t}>{ ' '.join(rng.choices(words, k=rng.randint(2, 12))) }</{t}>")
        html = "".join(parts) + "</body>"
        assert p.extract_and_scan(html) == r.extract_and_scan(html), f"fuzz #{i}"


def test_fallback_on_incompatible_pattern():
    """rust regex 가 못 받는 패턴(역참조)이면 PyPipeline 으로 폴백해야 한다."""
    pipe = nativetext.make_pipeline(r"(글)\1", r"x", r"y")  # \1 역참조 → rust 비호환
    assert isinstance(pipe, nativetext.PyPipeline) or not pipe.native
    d, b, bot = pipe.scan("글글 테스트")
    assert d == "글글" and b is None and bot is False

"""버그 헌트 수정분 회귀 테스트 (순수 함수 위주).

각 테스트는 이전에 고친 구체적 버그가 되살아나지 않게 불변식을 못 박는다.
"""

import services.llm as llm
import services.tracker as tracker
import services.ratelimit as rl
from routers.feed import _attr


# ── NO_FIT 산문 오탐 (문학적 도입부를 거부로 오인) ───────────────────────────
def test_no_fit_detects_true_refusals():
    for t in [
        "적합한 글이 없습니다.",
        "관련된 제보를 찾을 수 없습니다",
        "해당하는 사연 없음",
        "마땅한 미담이 없네요",
        "NO_FIT",
        "**NO_FIT**",
        "- NO_FIT 적합한 글 없음",
    ]:
        assert llm._is_no_fit(t) is True, f"진짜 거부인데 미탐: {t!r}"


def test_no_fit_ignores_literary_openings():
    # 큐레이터 명사 + 부정이 섞인 정상 서사 도입부는 거부로 오인하면 안 된다.
    for t in [
        "마땅한 사연 없을 줄 알았던 골목에서, 한 시민이 노인을 부축했다.",
        "관련된 이야기 찾을 수 없던 늦은 밤, 누군가 우산을 씌워줬다는 글이 올라왔다.",
        "오늘 아침 출근길에 있었던 따뜻한 이야기입니다.",
    ]:
        assert llm._is_no_fit(t) is False, f"정상 본문 오탐: {t!r}"


# ── EUC-KR/CP949 디코딩 (한글 깨짐으로 삭제 미탐) ────────────────────────────
def test_decode_body_euckr_and_utf8():
    msg = "삭제된 글입니다"
    # <meta charset=euc-kr> 만 있고 HTTP charset 없음
    euckr_meta = ("<html><head><meta charset=euc-kr></head><body>"
                  + msg + "</body></html>").encode("cp949")
    assert msg in tracker._decode_body(euckr_meta, None, "")
    # HTTP Content-Type 헤더로 cp949 명시
    assert msg in tracker._decode_body(msg.encode("cp949"), None,
                                       "text/html; charset=EUC-KR")
    # utf-8 정상 경로
    assert msg in tracker._decode_body(msg.encode("utf-8"), "utf-8",
                                       "text/html; charset=utf-8")


# ── 피드 XML 속성 이스케이프 (속성 주입/피드 깨짐) ──────────────────────────
def test_feed_attr_escapes_quotes():
    out = _attr('http://x/?q="onmouseover=alert(1)')
    assert '"' not in out and "&quot;" in out
    assert _attr("a'b") == "a&apos;b"
    assert _attr("정상텍스트") == "정상텍스트"


# ── client_ip: 공백 XFF 가 모든 클라이언트를 한 키로 묶지 않게 ───────────────
class _Req:
    def __init__(self, headers, host="9.9.9.9"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_client_ip_blank_xff_falls_through():
    # 빈/공백 첫 토큰이면 ''(공유키) 대신 폴백으로
    assert rl.client_ip(_Req({"x-forwarded-for": " , 1.2.3.4"})) == "9.9.9.9"
    assert rl.client_ip(_Req({"x-forwarded-for": " ", "x-real-ip": "8.8.8.8"})) == "8.8.8.8"
    # 정상 XFF 는 첫 토큰
    assert rl.client_ip(_Req({"x-forwarded-for": "5.5.5.5, 1.1.1.1"})) == "5.5.5.5"
    # XFF 없으면 client.host
    assert rl.client_ip(_Req({})) == "9.9.9.9"


# ── FM코리아류 봇차단/안티봇 챌린지 = '삭제 추적 불가' ───────────────────────
def test_untrackable_source_domain_and_botblock():
    U = tracker.is_untrackable_source
    # 지정 도메인은 코드/서브도메인 무관 추적 불가
    assert U("https://www.fmkorea.com/123", 200, None) is True
    assert U("https://m.fmkorea.com/123", 430, "HTTP 430") is True
    # 단, 404/410 은 실제 삭제 신호라 도메인과 무관하게 신뢰(가리지 않음)
    assert U("https://www.fmkorea.com/123", 404, "HTTP 404") is False
    # 봇차단 코드는 도메인 무관 추적 불가
    assert U("https://theqoo.net/1", 403, "HTTP 403") is True
    # 정상 도메인/코드는 추적 가능
    assert U("https://theqoo.net/1", 200, None) is False
    # 200 챌린지 판정분의 reason 센티넬도 추적 불가
    assert U("https://x.example/1", 200, tracker.UNTRACKABLE_REASON) is True


# ── 안티봇 챌린지 페이지(200)를 live/baseline 으로 오인하지 않는다 ────────────
def test_challenge_page_not_marked_live():
    P = tracker.BOT_CHALLENGE_PATTERNS
    assert P.search("에펨코리아 보안 시스템 IP 잠시 기다리면 자동으로 접속됩니다")
    assert P.search("Just a moment... Checking your browser")
    assert not P.search("따뜻한 미담 본문입니다.")
    # 챌린지로 관측되면 200 이어도 error + 센티넬 reason, 기준선 미캡처
    obs = {"net": "ok", "http_code": 200, "final_url": "https://www.fmkorea.com/1",
           "text_len": 3380, "del_match": False, "blk_match": False, "bot_challenge": True}
    v = tracker.decide_status(obs, "https://www.fmkorea.com/1", None)
    assert v["status"] == "error" and v["reason"] == tracker.UNTRACKABLE_REASON
    assert v.get("baseline") is None
    # 일반 live 판정은 영향 없음
    ok = {"net": "ok", "http_code": 200, "final_url": "https://theqoo.net/1",
          "text_len": 3000, "del_match": False, "blk_match": False, "bot_challenge": False}
    assert tracker.decide_status(ok, "https://theqoo.net/1", None)["status"] == "live"

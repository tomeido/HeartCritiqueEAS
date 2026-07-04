"""이야기 검색(GET /api/stories?q=) 정제 로직 테스트.

_sanitize_search 는 PostgREST or_/ilike 필터에 삽입되는 유일한 사용자 입력이다 —
구조 문자(* % _ , ( ) \\ ")를 제거해 필터 인젝션·와일드카드 누수를 차단해야 한다.
"""

from routers.stories import _sanitize_search


def test_plain_korean_preserved():
    assert _sanitize_search("택시 기사") == "택시 기사"


def test_structural_chars_stripped():
    # 필터 주입의 핵심인 콤마(새 조건 추가)·와일드카드·이스케이프가 사라져야 한다.
    # 점(.)은 값 내부에선 무해해 보존된다.
    out = _sanitize_search('body.ilike.*x*,vote_count.gt.0')
    assert "," not in out and "*" not in out
    assert out == "body.ilike. x vote count.gt.0"
    assert _sanitize_search("100%") == "100"
    assert _sanitize_search("a_b") == "a b"
    assert _sanitize_search('quo"te') == "quo te"
    assert _sanitize_search("back\\slash") == "back slash"


def test_wildcards_stripped():
    assert _sanitize_search("*") == ""
    assert _sanitize_search("%%%") == ""
    assert _sanitize_search("*택시*") == "택시"


def test_empty_and_none():
    assert _sanitize_search(None) == ""
    assert _sanitize_search("") == ""
    assert _sanitize_search("   ") == ""


def test_length_capped_at_80():
    assert len(_sanitize_search("가" * 200)) == 80


def test_whitespace_collapsed():
    assert _sanitize_search("택시   기사\n미담") == "택시 기사 미담"


def test_list_stories_all_tiers_fail_returns_503(monkeypatch):
    """모든 컬럼 조합 시도가 연쇄 일시 오류로 실패하면 동일 쿼리 재실행 없이 503.
    (리뷰 확정 결함 수정 회귀: 이전 코드는 base 재실행 후 무처리 500 이었다.)"""
    from fastapi.testclient import TestClient

    import main
    import routers.stories as stories_mod

    class BoomDB:
        def table(self, name):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(stories_mod, "get_db", lambda: BoomDB())
    r = TestClient(main.app).get("/api/stories")
    assert r.status_code == 503
    assert "connection" not in r.text   # 내부 오류 내용 비노출

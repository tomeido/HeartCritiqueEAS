"""services/localdb.py — SQLite 로컬 백엔드(supabase-py 호환 서브셋) 단위 테스트.

외부 의존성 없이(:memory:) 실제 코드베이스가 쓰는 쿼리 패턴을 그대로 재현해 검증한다:
representation 반환, APIError 코드(23505/23503/42703/PGRST204/PGRST202),
or_ 중첩 and()/not.in 파싱, PG NULLS 정렬 기본값, count/head, upsert, RPC, 게스트 인증.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GUEST_TOKEN_SECRET", "test-secret-for-localdb")

from services.dberrors import APIError  # noqa: E402
from services.localdb import LocalClient  # noqa: E402


def _db() -> LocalClient:
    return LocalClient(":memory:")


def _mk_story(db, **over):
    row = {
        "category": "kindness",
        "body": "테스트 본문",
        "citations": [{"uri": "https://ex.am/1", "title": "출처"}],
        "search_queries": ["질의"],
        "vote_count": 0,
    }
    row.update(over)
    return db.table("stories").insert(row).execute().data[0]


def test_insert_returns_representation_and_json_roundtrip():
    db = _db()
    s = _mk_story(db)
    assert s["id"] and len(s["id"]) == 36           # 앱 생성 uuid4
    assert s["created_at"].endswith("+00:00")       # 캐노니컬 timestamptz TEXT
    assert s["citations"] == [{"uri": "https://ex.am/1", "title": "출처"}]

    got = db.table("stories").select("*").eq("id", s["id"]).limit(1).execute()
    assert got.data[0]["citations"][0]["uri"] == "https://ex.am/1"
    assert got.data[0]["search_queries"] == ["질의"]
    assert got.data[0]["from_capture"] is False      # bool 역변환


def test_vote_unique_and_fk_error_codes():
    db = _db()
    s = _mk_story(db)
    uid = "11111111-1111-4111-8111-111111111111"
    db.table("votes").insert({"story_id": s["id"], "user_id": uid}).execute()
    try:
        db.table("votes").insert({"story_id": s["id"], "user_id": uid}).execute()
        assert False, "중복 투표가 통과됨"
    except APIError as e:
        assert e.code == "23505"
    try:
        db.table("votes").insert({
            "story_id": "99999999-9999-4999-8999-999999999999", "user_id": uid,
        }).execute()
        assert False, "FK 위반이 통과됨"
    except APIError as e:
        assert e.code == "23503"


def test_conditional_update_and_returning():
    db = _db()
    s = _mk_story(db)
    # votes 라우터의 단조증가 가드: eq + lt
    r = (db.table("stories").update({"vote_count": 3})
         .eq("id", s["id"]).lt("vote_count", 3).execute())
    assert len(r.data) == 1 and r.data[0]["vote_count"] == 3
    # 이미 3인데 더 낮은 값으로 → 매칭 0행 (에러 아님)
    r2 = (db.table("stories").update({"vote_count": 2})
          .eq("id", s["id"]).lt("vote_count", 2).execute())
    assert r2.data == []
    # archive claim 패턴: is_(null) 조건 update
    r3 = (db.table("stories").update({"arweave_tx_id": "__pending__"})
          .eq("id", s["id"]).is_("arweave_tx_id", "null").execute())
    assert len(r3.data) == 1
    r4 = (db.table("stories").update({"arweave_tx_id": "__pending__"})
          .eq("id", s["id"]).is_("arweave_tx_id", "null").execute())
    assert r4.data == []  # 두 번째 claim 은 실패(단일 승자)


def test_count_exact_head_and_with_rows():
    db = _db()
    for _ in range(3):
        _mk_story(db)
    _mk_story(db, category="critique")
    head = db.table("stories").select("*", count="exact", head=True).execute()
    assert head.count == 4 and head.data == []
    kind = (db.table("stories").select("id", count="exact")
            .eq("category", "kindness").execute())
    assert kind.count == 3 and len(kind.data) == 3
    # count 는 limit 과 무관한 전체 매칭 수
    lim = db.table("stories").select("id", count="exact").limit(1).execute()
    assert lim.count == 4 and len(lim.data) == 1


def test_or_filter_nested_and_not_in():
    db = _db()
    s = _mk_story(db)
    rows = [
        {"story_id": s["id"], "url": "https://a", "status": "live", "http_code": 200},
        {"story_id": s["id"], "url": "https://b", "status": "deleted", "http_code": 404},
        {"story_id": s["id"], "url": "https://c", "status": "deleted", "http_code": None},
        {"story_id": s["id"], "url": "https://d", "status": "deleted", "http_code": 200},
    ]
    db.table("citation_checks").insert(rows).execute()
    # tracker RECHECK_QUEUE_FILTER: hard 삭제(404/410)만 큐에서 제외
    q = ("status.neq.deleted,and(status.eq.deleted,http_code.is.null),"
         "and(status.eq.deleted,http_code.not.in.(404,410))")
    r = db.table("citation_checks").select("url").or_(q).execute()
    urls = {row["url"] for row in r.data}
    assert urls == {"https://a", "https://c", "https://d"}
    # 이중 or_() = AND 결합 (_due_filter)
    r2 = (db.table("citation_checks").select("url").or_(q)
          .or_("next_check_at.is.null,next_check_at.lte.2020-01-01T00:00:00+00:00")
          .execute())
    assert {row["url"] for row in r2.data} == {"https://a", "https://c", "https://d"}


def test_hunter_or_pending_filter():
    db = _db()
    _mk_story(db)                                     # arweave_tx_id NULL
    _mk_story(db, arweave_tx_id="__pending__")
    _mk_story(db, arweave_tx_id="realtx", archived_at="2026-07-01T00:00:00+00:00")
    r = (db.table("stories").select("id", count="exact")
         .or_("arweave_tx_id.is.null,arweave_tx_id.eq.__pending__")
         .gte("created_at", "2000-01-01T00:00:00+00:00").execute())
    assert r.count == 2


def test_not_property_is_not_null_and_neq():
    db = _db()
    _mk_story(db, arweave_tx_id="tx1", archived_at="2026-07-01T00:00:00+00:00")
    _mk_story(db, arweave_tx_id="__pending__")
    _mk_story(db)
    r = (db.table("stories").select("id")
         .not_.is_("arweave_tx_id", "null")
         .neq("arweave_tx_id", "__pending__").execute())
    assert len(r.data) == 1
    c = (db.table("stories").select("*", count="exact", head=True)
         .not_.is_("archived_at", "null").execute())
    assert c.count == 1


def test_order_pg_null_defaults_and_nullsfirst():
    db = _db()
    a = _mk_story(db, archived_at=None, arweave_tx_id="t1")
    b = _mk_story(db, archived_at="2026-07-02T00:00:00+00:00", arweave_tx_id="t2")
    c = _mk_story(db, archived_at="2026-07-03T00:00:00+00:00", arweave_tx_id="t3")
    # PG 기본: DESC → NULLS FIRST (feed archived.xml 의존)
    r = db.table("stories").select("id").order("archived_at", desc=True).execute()
    assert [x["id"] for x in r.data] == [a["id"], c["id"], b["id"]]
    # 명시적 nullsfirst=False → NULLS LAST (promoter value_score desc)
    r2 = (db.table("stories").select("id")
          .order("archived_at", desc=True, nullsfirst=False).execute())
    assert [x["id"] for x in r2.data] == [c["id"], b["id"], a["id"]]
    # ASC nullsfirst=True (tracker next_check_at)
    r3 = (db.table("stories").select("id")
          .order("archived_at", desc=False, nullsfirst=True).execute())
    assert [x["id"] for x in r3.data] == [a["id"], b["id"], c["id"]]


def test_search_or_ilike_and_in():
    db = _db()
    _mk_story(db, body="따뜻한 어묵 국물 한 그릇", poetic_reason=None)
    _mk_story(db, body="다른 이야기", poetic_reason="어묵의 온기")
    _mk_story(db, body="무관한 글", poetic_reason=None)
    r = (db.table("stories").select("id,body")
         .or_("body.ilike.*어묵*,poetic_reason.ilike.*어묵*")
         .order("created_at", desc=True).limit(50).execute())
    assert len(r.data) == 2
    _mk_story(db, gap_score="extreme")
    _mk_story(db, gap_score="high")
    r2 = db.table("stories").select("id").in_("gap_score", ["extreme", "high"]).execute()
    assert len(r2.data) == 2
    r3 = db.table("stories").select("id").in_("gap_score", []).execute()
    assert r3.data == []


def test_bool_eq_and_ts_normalization():
    db = _db()
    _mk_story(db, from_capture=True, origin_captured_url="https://dead/1")
    _mk_story(db)
    r = (db.table("stories").select("*", count="exact", head=True)
         .eq("from_capture", True).execute())
    assert r.count == 1
    # 'Z' 표기 필터도 캐노니컬로 정규화되어 매칭
    z = (db.table("stories").select("id")
         .gte("created_at", "2000-01-01T00:00:00Z").execute())
    assert len(z.data) == 2
    # 'Z' 표기로 쓴 값도 캐노니컬로 저장
    s = _mk_story(db, archived_at="2026-07-01T12:00:00Z", arweave_tx_id="t")
    assert s["archived_at"] == "2026-07-01T12:00:00+00:00"


def test_upsert_on_conflict_and_ignore_duplicates():
    db = _db()
    s = _mk_story(db)
    db.table("citation_checks").upsert(
        [{"story_id": s["id"], "url": "https://a", "status": "unchecked"}],
        on_conflict="story_id,url").execute()
    db.table("citation_checks").upsert(
        [{"story_id": s["id"], "url": "https://a", "status": "live"}],
        on_conflict="story_id,url").execute()
    r = db.table("citation_checks").select("status").eq("story_id", s["id"]).execute()
    assert len(r.data) == 1 and r.data[0]["status"] == "live"
    # wayback: ignore_duplicates=True → 기존 행 보존
    db.table("wayback_snapshots").upsert(
        [{"url": "https://a", "status": "queued"}],
        on_conflict="url", ignore_duplicates=True).execute()
    db.table("wayback_snapshots").upsert(
        [{"url": "https://a", "status": "success"}],
        on_conflict="url", ignore_duplicates=True).execute()
    w = db.table("wayback_snapshots").select("status").eq("url", "https://a").execute()
    assert w.data[0]["status"] == "queued"


def test_delete_returns_rows_and_fk_cascade():
    db = _db()
    s = _mk_story(db, created_at="2020-01-01T00:00:00+00:00")
    db.table("votes").insert({
        "story_id": s["id"], "user_id": "11111111-1111-4111-8111-111111111111",
    }).execute()
    db.table("citation_checks").insert({
        "story_id": s["id"], "url": "https://a", "status": "unchecked",
    }).execute()
    r = (db.table("stories").delete().in_("id", [s["id"]])
         .is_("arweave_tx_id", "null").lte("vote_count", 2).execute())
    assert len(r.data) == 1
    assert db.table("votes").select("id").execute().data == []          # cascade
    assert db.table("citation_checks").select("id").execute().data == []


def test_rpc_cleanup_preserves_captures_and_counts_real_votes():
    db = _db()
    old = "2020-01-01T00:00:00+00:00"
    orphan = _mk_story(db, created_at=old)
    capture = _mk_story(db, created_at=old, from_capture=True,
                        origin_captured_url="https://dead/2")
    voted = _mk_story(db, created_at=old, vote_count=0)  # 캐시는 0이지만 실표 3
    for i in range(3):
        db.table("votes").insert({
            "story_id": voted["id"],
            "user_id": f"00000000-0000-4000-8000-00000000000{i}",
        }).execute()
    fresh = _mk_story(db)
    resp = db.rpc("delete_orphan_pending_stories",
                  {"p_cutoff": "2026-01-01T00:00:00+00:00", "p_max_votes": 2}).execute()
    assert resp.data == 1  # orphan 만 삭제
    remain = {r["id"] for r in db.table("stories").select("id").execute().data}
    assert remain == {capture["id"], voted["id"], fresh["id"]}
    assert orphan["id"] not in remain
    # 미지의 RPC → PGRST202 (cleanup 레거시 폴백 계약)
    try:
        db.rpc("nonexistent_fn", {}).execute()
        assert False
    except APIError as e:
        assert e.code == "PGRST202"


def test_missing_column_error_codes():
    db = _db()
    try:
        db.table("stories").select("no_such_col").limit(1).execute()
        assert False
    except APIError as e:
        assert e.code == "42703" and "no_such_col" in str(e)
    try:
        db.table("stories").insert({"category": "kindness", "body": "b",
                                    "bogus_col": 1}).execute()
        assert False
    except APIError as e:
        assert e.code == "PGRST204" and "bogus_col" in str(e)


def test_guest_auth_roundtrip():
    from services.localauth import issue_guest_token, verify_guest_token
    token, uid = issue_guest_token()
    assert verify_guest_token(token) == uid
    assert verify_guest_token(token + "x") is None
    assert verify_guest_token("guest." + uid + ".deadbeef") is None
    assert verify_guest_token("") is None
    db = _db()
    user = db.auth.get_user(token)
    assert user.user.id == uid
    try:
        db.auth.get_user("bogus")
        assert False
    except ValueError:
        pass


def test_votes_flow_like_router():
    """routers/votes.py 의 실제 시퀀스를 로컬 백엔드로 재현."""
    db = _db()
    s = _mk_story(db)
    from services.localauth import issue_guest_token
    token, uid = issue_guest_token()
    user_id = db.auth.get_user(token).user.id
    db.table("votes").insert({"story_id": s["id"], "user_id": user_id}).execute()
    vr = (db.table("votes").select("id", count="exact")
          .eq("story_id", s["id"]).execute())
    assert vr.count == 1
    (db.table("stories").update({"vote_count": vr.count})
     .eq("id", s["id"]).lt("vote_count", vr.count).execute())
    got = db.table("stories").select("vote_count").eq("id", s["id"]).execute()
    assert got.data[0]["vote_count"] == 1
    mine = (db.table("votes").select("story_id,created_at").eq("user_id", uid)
            .order("created_at", desc=True).limit(500).execute())
    assert [r["story_id"] for r in mine.data] == [s["id"]]

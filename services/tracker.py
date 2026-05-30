"""
인용 URL 삭제 추적 (Citation Deletion Tracker).

CONTEXT.md 핵심 가치: 대기업 자본력에 삭제되는 Web2 커뮤니티의 사각지대 박제.

흐름:
  1. 새 스토리 생성 시 register_citations() 가 모든 출처 URL 을 DB 에 등록
  2. 백그라운드 루프가 주기적으로 N 건씩 HTTP GET 재방문
  3. HTTP 상태코드 + 본문의 "삭제됨" 표식 패턴으로 생존 여부 판단
  4. 결과를 citation_checks 테이블에 갱신
  5. API 응답에 status 포함되어 UI 에서 시각화
"""

import asyncio
import os
import re
from datetime import datetime, timezone

import httpx

from services.db import get_db

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))   # 5분 간격
CHECK_BATCH_SIZE   = int(os.environ.get("CHECK_BATCH_SIZE", "15"))
HTTP_TIMEOUT       = 15
TRACKER_ENABLED    = os.environ.get("TRACKER_ENABLED", "true").lower() != "false"

# 커뮤니티 게시판에서 글이 삭제되었을 때 자주 보이는 본문 패턴.
# 주의: FM코리아 등 일부 사이트는 없는 글을 메인 페이지로 리다이렉트 → 본문 패턴으로
# 잡을 수 없음 (알려진 한계). 명확한 표식이 있는 더쿠/클리앙/네이트판 등은 잘 감지.
DELETION_PATTERNS = re.compile(
    r"삭제된\s*(?:글|톡|게시[물글]?|댓글)"
    r"|이미\s*삭제"
    r"|존재하지\s*않는"
    r"|이\s*글을\s*볼\s*권한"
    r"|접근\s*권한이?\s*없"
    r"|신고에?\s*의해\s*삭제"
    r"|차단된\s*(?:글|게시[물글]?)"
    r"|숨김\s*처리된\s*(?:글|게시[물글]?)"
    r"|글쓴이에?\s*의해\s*삭제"
    r"|블라인드\s*처리"
    r"|찾을\s*수\s*없(?:는|습)"
    r"|페이지를?\s*찾을\s*수\s*없"
    r"|글이?\s*없습니다"
    r"|글이?\s*존재하지\s*않"
    r"|deleted\s+(?:post|by)"
    r"|page\s+not\s+found"
    r"|404\s+not\s+found",
    re.IGNORECASE,
)

# 차단 패턴 (가입자 전용/로그인 필요 등 살아는 있지만 우리가 볼 수 없는 상태)
BLOCKED_PATTERNS = re.compile(
    r"로그인이?\s*필요"
    r"|회원만\s*(?:열람|볼)"
    r"|성인\s*인증"
    r"|가입자\s*전용",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


async def check_url(url: str, client: httpx.AsyncClient) -> dict:
    """단일 URL 의 생존 여부 확인. dict 반환: status/http_code/reason."""
    try:
        resp = await client.get(
            url,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"},
        )
    except httpx.TimeoutException:
        return {"status": "error", "http_code": None, "reason": "timeout"}
    except Exception as e:
        return {"status": "error", "http_code": None, "reason": f"net:{type(e).__name__}"}

    code = resp.status_code

    if code in (404, 410):
        return {"status": "deleted", "http_code": code, "reason": f"HTTP {code}"}
    if code == 403:
        return {"status": "blocked", "http_code": code, "reason": "HTTP 403"}
    if code >= 400:
        return {"status": "error", "http_code": code, "reason": f"HTTP {code}"}

    # 본문에서 삭제/차단 표식 검사 (앞 80KB 만)
    try:
        text = resp.text[:80000]
    except Exception:
        text = ""

    if (m := DELETION_PATTERNS.search(text)):
        return {"status": "deleted", "http_code": code, "reason": f"matched:{m.group(0)[:40]}"}
    if (m := BLOCKED_PATTERNS.search(text)):
        return {"status": "blocked", "http_code": code, "reason": f"matched:{m.group(0)[:40]}"}

    return {"status": "live", "http_code": code, "reason": None}


def register_citations(story_id: str, citations: list) -> None:
    """새 스토리의 citations URL 들을 추적 테이블에 등록 (멱등)."""
    if not citations:
        return
    db = get_db()
    rows = []
    for c in citations:
        uri = (c or {}).get("uri")
        if not uri or not isinstance(uri, str):
            continue
        rows.append({
            "story_id": story_id,
            "url": uri,
            "status": "unchecked",
        })
    if not rows:
        return
    try:
        db.table("citation_checks").upsert(rows, on_conflict="story_id,url").execute()
    except Exception as e:
        print(f"[tracker] register failed for {story_id}: {e}")


async def _trigger_auto_archive_if_needed(story_id: str) -> None:
    """삭제·차단 감지 시 effective threshold 만큼 표가 모였는지 확인하고 박제."""
    try:
        from services.threshold import maybe_archive_now
        await maybe_archive_now(story_id)
    except Exception as e:
        print(f"[tracker] auto-archive check failed for {story_id}: {e}")


async def recheck_one_story(story_id: str) -> int:
    """특정 스토리의 모든 citation 즉시 재검사. 검사한 개수 반환."""
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select("id,url,check_count,status")
        .eq("story_id", story_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return 0

    newly_deleted = False
    async with httpx.AsyncClient() as client:
        for row in rows:
            res = await check_url(row["url"], client)
            prev_status = row.get("status")
            if res["status"] in ("deleted", "blocked") and prev_status not in ("deleted", "blocked"):
                newly_deleted = True
            try:
                db.table("citation_checks").update({
                    "status": res["status"],
                    "http_code": res["http_code"],
                    "reason": res["reason"],
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "check_count": (row.get("check_count") or 0) + 1,
                }).eq("id", row["id"]).execute()
            except Exception as e:
                print(f"[tracker] update fail {row['id']}: {e}")

    # 새로 사라진 글이 있으면 자동 박제 검사
    if newly_deleted:
        await _trigger_auto_archive_if_needed(story_id)

    return len(rows)


async def recheck_batch(batch_size: int = CHECK_BATCH_SIZE) -> int:
    """오래된 (또는 한 번도 안 본) 레코드 N 개를 재검사. 검사한 개수 반환.
    한 번 deleted 로 확정된 URL 은 다시 검사하지 않음."""
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select("id,story_id,url,check_count,status")
        .neq("status", "deleted")
        .order("last_checked", desc=False, nullsfirst=True)
        .limit(batch_size)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return 0

    newly_changed_stories: set[str] = set()
    async with httpx.AsyncClient() as client:
        for row in rows:
            res = await check_url(row["url"], client)
            prev_status = row.get("status")
            try:
                db.table("citation_checks").update({
                    "status": res["status"],
                    "http_code": res["http_code"],
                    "reason": res["reason"],
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "check_count": (row.get("check_count") or 0) + 1,
                }).eq("id", row["id"]).execute()
                # 새로 삭제·차단된 글은 자동 박제 검사 대상
                if (res["status"] in ("deleted", "blocked")
                        and prev_status not in ("deleted", "blocked")
                        and row.get("story_id")):
                    newly_changed_stories.add(row["story_id"])
            except Exception as e:
                print(f"[tracker] update fail {row['id']}: {e}")

    # 새로 사라진/차단된 글이 발견된 스토리들에 대해 자동 박제 검사
    for sid in newly_changed_stories:
        await _trigger_auto_archive_if_needed(sid)

    return len(rows)


def get_status_map(story_ids: list) -> dict:
    """여러 스토리의 citation 상태를 한 번에 조회. 반환: {story_id: {url: row}}"""
    if not story_ids:
        return {}
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select("story_id,url,status,http_code,last_checked,reason")
        .in_("story_id", story_ids)
        .execute()
    )
    out: dict = {}
    for r in resp.data or []:
        out.setdefault(r["story_id"], {})[r["url"]] = r
    return out


async def background_loop() -> None:
    """앱 lifespan 동안 도는 추적 루프."""
    print(f"[tracker] started · interval={CHECK_INTERVAL_SEC}s · batch={CHECK_BATCH_SIZE}")
    # 시작 시 약간 지연 (앱 부팅 안정화)
    await asyncio.sleep(15)
    while True:
        try:
            n = await recheck_batch()
            if n:
                print(f"[tracker] rechecked {n} citation(s)")
        except asyncio.CancelledError:
            print("[tracker] cancelled")
            return
        except Exception as e:
            print(f"[tracker] loop error: {e}")

        # 박제 실패/누락 글 재시도 (sweeper): 임계값 넘겼지만 미박제인 글을
        # 지수 백오프로 자동 복구. 일시 장애로 박제가 영영 누락되는 것 방지.
        try:
            from services.archive import reconcile_pending_archives
            r = await reconcile_pending_archives()
            if r:
                print(f"[tracker] reconciled {r} pending archive(s)")
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[tracker] reconcile error: {e}")

        try:
            await asyncio.sleep(CHECK_INTERVAL_SEC)
        except asyncio.CancelledError:
            return

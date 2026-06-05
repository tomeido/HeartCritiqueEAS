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
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from services.db import get_db

import logging
logger = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))   # 5분 간격
CHECK_BATCH_SIZE   = int(os.environ.get("CHECK_BATCH_SIZE", "15"))
HTTP_TIMEOUT       = 15
MAX_BODY_BYTES     = 80000   # 본문은 앞 80KB 만 읽음 (대용량 응답 남용 방지)
MAX_REDIRECTS      = 5
TRACKER_ENABLED    = os.environ.get("TRACKER_ENABLED", "true").lower() != "false"

# 내부 서비스명 차단 목록 (docker 네트워크)
_BLOCKED_HOSTNAMES = {"localhost", "uploader", "app", "db"}


def _is_safe_url(url: str) -> tuple[bool, str]:
    """citation URL 은 LLM/Tavily 가 만든 비신뢰 값이다. 서버가 GET 하기 전에
    스킴(http/https)과 호스트(사설·루프백·링크로컬·내부서비스 아님)를 검증해 SSRF 차단.
    DNS resolve 후 IP 대역까지 본다."""
    try:
        p = urlparse(url)
    except Exception:
        return False, "bad_url"
    if p.scheme not in ("http", "https"):
        return False, f"scheme:{p.scheme or 'none'}"
    host = p.hostname
    if not host:
        return False, "no_host"
    if host.lower() in _BLOCKED_HOSTNAMES:
        return False, "internal_host"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, "dns_fail"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False, f"blocked_ip:{ip}"
    return True, ""

# 커뮤니티 게시판에서 글이 삭제되었을 때 자주 보이는 본문 패턴.
# 주의: FM코리아 등 일부 사이트는 없는 글을 메인 페이지로 리다이렉트 → 본문 패턴으로
# 잡을 수 없음 (알려진 한계). 명확한 표식이 있는 더쿠/클리앙/네이트판 등은 잘 감지.
#
# 오탐 방지(중요): 패턴은 살아있는 페이지의 정상 UI 문구와 충돌하지 않도록 '게시물 단위'
# 대상 명사에 앵커링한다. 과거 무앵커 패턴이 일으킨 치명적 오탐 사례:
#   - '글이? 없습니다'  → '댓글이 없습니다'(댓글 0개인 살아있는 글)에 매치
#   - '찾을 수 없'       → '검색 결과를/관련 상품을 찾을 수 없습니다'(위젯)에 매치
#   - '존재하지 않는'    → '존재하지 않는 회원/상품'(프로필·쇼핑)에 매치
#   - '차단된 글'·'블라인드 처리' → 'X 보기 설정'·'X 안내'(기능 라벨)에 매치
#   - 'page/404 not found' → 살아있는 페이지의 JS·임베드 문구에 매치(HTTP 404/410 으로 이미 커버)
# 영문 404 류와 순수 HTTP 신호는 위 check_url 의 상태코드 분기(404/410)가 담당한다.
DELETION_PATTERNS = re.compile(
    r"삭제된\s*(?:글|톡|게시[물글]?)"
    r"|이미\s*삭제"
    r"|이\s*글을\s*볼\s*권한"
    r"|접근\s*권한이?\s*없"
    r"|신고에?\s*의해\s*삭제"
    r"|숨김\s*처리된\s*(?:글|게시[물글]?)"
    r"|글쓴이에?\s*의해\s*삭제"
    # '존재하지 않는' 은 대상 명사(글/게시물/페이지) 동반 시에만 — 회원/상품 오탐 차단
    r"|존재하지\s*않는\s*(?:글|게시물|게시글|페이지)"
    r"|글이?\s*존재하지\s*않"
    # '찾을 수 없' 은 글/게시물/페이지/주소 대상일 때만 — 검색결과/상품 오탐 차단
    r"|(?:글|게시물|게시글|페이지|주소)[을를이가]?\s*찾을\s*수\s*없(?:는|습)"
    # '차단된 글' 은 신고/운영 맥락 또는 종결형일 때만 — '차단된 글 보기 설정' 오탐 차단
    r"|(?:신고|운영자|운영진|관리자|다수\s*신고)[로은는이가]?\s*(?:에\s*의해\s*)?차단된\s*(?:글|게시[물글]?)"
    r"|차단된\s*(?:글|게시[물글]?)\s*(?:입니다|이에요|예요|이다)"
    # '블라인드 처리' 는 완료형일 때만 — '블라인드 처리 안내/하기' 오탐 차단
    r"|블라인드\s*처리(?:된|됨|되었|됐)"
    r"|deleted\s+(?:post|by)",
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
    """단일 URL 의 생존 여부 확인. dict 반환: status/http_code/reason.

    SSRF 방어: 리다이렉트를 자동 추종하지 않고, 홉마다 _is_safe_url 로 다시 검증한다.
    (안전한 외부 URL 이 30x 로 사설/루프백/메타데이터/내부서비스 IP 로 리다이렉트해
    초기 1회 검증을 우회하는 것을 차단. follow_redirects=True 면 최종 목적지가 재검증되지 않음.)"""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        safe, why = await asyncio.to_thread(_is_safe_url, cur)
        if not safe:
            return {"status": "error", "http_code": None, "reason": f"unsafe_url:{why}"}
        try:
            # 스트리밍으로 헤더 먼저 받고, 본문은 앞 MAX_BODY_BYTES 만 읽는다.
            async with client.stream(
                "GET", cur, timeout=HTTP_TIMEOUT, follow_redirects=False, headers=headers,
            ) as resp:
                code = resp.status_code
                # 리다이렉트는 직접 따라가되 다음 홉 URL 을 루프 상단에서 재검증
                if 300 <= code < 400 and "location" in resp.headers:
                    cur = str(httpx.URL(cur).join(resp.headers["location"]))
                    continue
                if code in (404, 410):
                    return {"status": "deleted", "http_code": code, "reason": f"HTTP {code}"}
                if code == 403:
                    return {"status": "blocked", "http_code": code, "reason": "HTTP 403"}
                if code >= 400:
                    return {"status": "error", "http_code": code, "reason": f"HTTP {code}"}

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_BODY_BYTES:
                        break
                text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
        except httpx.TimeoutException:
            return {"status": "error", "http_code": None, "reason": "timeout"}
        except Exception as e:
            return {"status": "error", "http_code": None, "reason": f"net:{type(e).__name__}"}

        if (m := DELETION_PATTERNS.search(text)):
            return {"status": "deleted", "http_code": code, "reason": f"matched:{m.group(0)[:40]}"}
        if (m := BLOCKED_PATTERNS.search(text)):
            return {"status": "blocked", "http_code": code, "reason": f"matched:{m.group(0)[:40]}"}

        return {"status": "live", "http_code": code, "reason": None}

    return {"status": "error", "http_code": None, "reason": "too_many_redirects"}


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
        logger.warning(f"[tracker] register failed for {story_id}: {e}")


async def _trigger_auto_archive_if_needed(story_id: str) -> None:
    """삭제·차단 감지 시 effective threshold 만큼 표가 모였는지 확인하고 박제."""
    try:
        from services.threshold import maybe_archive_now
        await maybe_archive_now(story_id)
    except Exception as e:
        logger.warning(f"[tracker] auto-archive check failed for {story_id}: {e}")


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
    async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
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
                logger.warning(f"[tracker] update fail {row['id']}: {e}")

    # 새로 사라진 글이 있으면 자동 박제 검사
    if newly_deleted:
        await _trigger_auto_archive_if_needed(story_id)

    return len(rows)


async def recheck_batch(batch_size: int = CHECK_BATCH_SIZE) -> int:
    """오래된 (또는 한 번도 안 본) 레코드 N 개를 재검사. 검사한 개수 반환.

    sticky 정책: HTTP 404/410 으로 확정된 'hard deleted' 만 영구 제외한다(진짜 사라짐).
    본문 패턴으로만 잡힌 'soft deleted'(http_code 가 404/410 이 아님)는 오탐 가능성이 있어
    재검사 대상에 남겨, 실제로 살아있으면 다음 검사에서 live 로 자동 정정되게 한다.
    (last_checked 오름차순 정렬이라 방금 확인한 soft deleted 는 큐 뒤로 밀려 starvation 방지)"""
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select("id,story_id,url,check_count,status,http_code")
        .or_("status.neq.deleted,and(status.eq.deleted,http_code.not.in.(404,410))")
        .order("last_checked", desc=False, nullsfirst=True)
        .limit(batch_size)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return 0

    newly_changed_stories: set[str] = set()
    async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
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
                logger.warning(f"[tracker] update fail {row['id']}: {e}")

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
    logger.info(f"[tracker] started · interval={CHECK_INTERVAL_SEC}s · batch={CHECK_BATCH_SIZE}")
    # 시작 시 약간 지연 (앱 부팅 안정화)
    await asyncio.sleep(15)
    while True:
        try:
            n = await recheck_batch()
            if n:
                logger.info(f"[tracker] rechecked {n} citation(s)")
        except asyncio.CancelledError:
            logger.info("[tracker] cancelled")
            return
        except Exception as e:
            logger.warning(f"[tracker] loop error: {e}")

        # 박제 실패/누락 글 재시도 (sweeper): 임계값 넘겼지만 미박제인 글을
        # 지수 백오프로 자동 복구. 일시 장애로 박제가 영영 누락되는 것 방지.
        try:
            from services.archive import reconcile_pending_archives
            r = await reconcile_pending_archives()
            if r:
                logger.info(f"[tracker] reconciled {r} pending archive(s)")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"[tracker] reconcile error: {e}")

        try:
            await asyncio.sleep(CHECK_INTERVAL_SEC)
        except asyncio.CancelledError:
            return

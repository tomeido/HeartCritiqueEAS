"""커뮤니티 화제글 선제 수집기 (Proactive Collector).

문제: 사냥개는 Tavily 검색으로 글을 '발견'하는데, 이미 삭제된 글은 검색 인덱스에
없어 구조적으로 못 가져온다. 빨리 지워지는 글(=대기업 비위처럼 삭제가 핵심인 글)은
사람이 투표하기 전에 증발한다.

해법(이 모듈): 살아있을 때 미리 화제글을 잡아 비공개로 보관(본문+해시)하고,
tracker 의 감지 엔진을 그대로 재사용해 주기적으로 삭제를 감시한다.

봇탐지 최소화 원칙(직접 긁는 양을 최소화):
  · 공식 RSS 가 살아있는 사이트만 1차 대상(직접 스크래핑 최소화). 실측 확인 목록은
    COMMUNITY_FEEDS 참고. FM코리아는 안티봇 챌린지(430)라 제외(tracker 의 추적불가와 동일).
  · 피드에서 '신규 글 ID' 만 추려, 본문은 이미 본 글을 빼고 '정확히 1회'만 GET.
  · 요청 사이에 지터, 봇차단 코드(403/429/430/503)엔 그 출처를 이번 주기 건너뜀.
  · SSRF 방어·EUC-KR 디코딩·삭제 판정은 services.tracker 의 검증된 함수를 재사용.

⚠️ captured_posts 는 비공개(service_role 전용, migrations/006). 본문 전체를 보관하므로
   공개 API/Arweave 박제로 내보내려면 PII 마스킹·사인 배제 등 법적 가드레일이 선행돼야
   한다(이 MVP 는 '수집 + 삭제 감시'까지만; 스토리 승격·공개는 미구현).
"""

import asyncio
import os
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from services.db import get_db
from services.tracker import (
    BOT_BLOCK_CODES,
    MAX_REDIRECTS,
    RECHECK_QUEUE_FILTER,
    USER_AGENT,
    _baseline_from_row,
    _build_update,
    _visible_text,
    compute_next_check,
    decide_status,
    fetch_observation,
)
from services.wayback import enqueue as wayback_enqueue

import logging
logger = logging.getLogger(__name__)

# 기본 비활성: migrations/006 적용 후 COLLECTOR_ENABLED=true 로 명시적으로 켠다(외부 폴링 시작).
COLLECTOR_ENABLED = os.environ.get("COLLECTOR_ENABLED", "false").lower() == "true"
COLLECTOR_INTERVAL_SEC = int(os.environ.get("COLLECTOR_INTERVAL_SEC", "600"))   # 피드 폴링 주기 10분
COLLECTOR_INITIAL_DELAY_SEC = int(os.environ.get("COLLECTOR_INITIAL_DELAY_SEC", "90"))
# 한 주기당 새 본문 캡처 상한(백프레셔·정중함). 발견이 많아도 본문 GET 은 이 수로 제한.
COLLECTOR_MAX_CAPTURE_PER_CYCLE = int(os.environ.get("COLLECTOR_MAX_CAPTURE_PER_CYCLE", "20"))
COLLECTOR_RECHECK_BATCH = int(os.environ.get("COLLECTOR_RECHECK_BATCH", "15"))
COLLECTOR_FEED_ITEMS = int(os.environ.get("COLLECTOR_FEED_ITEMS", "30"))   # 피드당 상위 N개만
FEED_TIMEOUT = 15
MAX_FEED_BYTES = 2_000_000   # 피드 본문 상한 2MB
# 요청 사이 지터(초) — 사람 브라우징처럼 보이게 + 서버 부하 최소화
_JITTER_LO = float(os.environ.get("COLLECTOR_JITTER_LO", "1.5"))
_JITTER_HI = float(os.environ.get("COLLECTOR_JITTER_HI", "4.0"))

# 실측 확인된 공식 RSS (source_domain, feed_url). 전 피드 가동 재확인: 2026-06-11.
# 제외:
#   · 더쿠(theqoo): 2026-06-11 기준 RSS 를 잠금('피드 기능이 잠겨 있습니다' 빈 응답) → HTML 폴링(2차) 필요.
#   · 디시인사이드·클리앙·보배드림: 공식 RSS 없음 → HTML 목록 폴링(2차 과제) 보류.
#   · FM코리아: 안티봇 챌린지(430) → tracker.UNTRACKABLE_DOMAINS 와 일관되게 제외.
COMMUNITY_FEEDS = [
    ("ppomppu.co.kr",      "http://www.ppomppu.co.kr/rss.php?id=ppomppu"),
    ("ppomppu.co.kr",      "http://www.ppomppu.co.kr/rss.php?id=freeboard"),
    ("ruliweb.com",        "https://bbs.ruliweb.com/news/rss"),
    ("mlbpark.donga.com",  "https://mlbpark.donga.com/mp/rss.php"),
    ("inven.co.kr",        "https://www.inven.co.kr/webzine/news/rss.php"),
]

# 모듈 상태 (대시보드/stats 용)
_last_poll_at: Optional[datetime] = None
_next_poll_at: Optional[datetime] = None
_last_result: Optional[dict] = None


def get_status() -> dict:
    """대시보드용 수집기 상태 스냅샷 (hunter.get_status 와 동형)."""
    return {
        "enabled": COLLECTOR_ENABLED,
        "interval_sec": COLLECTOR_INTERVAL_SEC,
        "feeds": len(COMMUNITY_FEEDS),
        "next_poll_at": _next_poll_at.isoformat() if _next_poll_at else None,
        "last_poll_at": _last_poll_at.isoformat() if _last_poll_at else None,
        "last_result": _last_result,
    }


def _local(tag: str) -> str:
    """네임스페이스를 떼고 로컬 태그명만(소문자). '{ns}item' → 'item'."""
    return tag.rsplit("}", 1)[-1].lower()


def _parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0 / Atom 피드에서 (title, url, guid, summary) 추출. 파싱 실패는 빈 리스트.
    ET.fromstring 은 XML 선언의 encoding(EUC-KR 등)을 존중하므로 bytes 를 그대로 넘긴다."""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    out: list[dict] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = guid = summary = None
        for ch in node:
            t = _local(ch.tag)
            if t == "title" and ch.text:
                title = ch.text.strip()
            elif t == "link":
                href = ch.get("href")          # Atom: <link href="...">
                if href:
                    link = href.strip()
                elif ch.text and ch.text.strip():  # RSS: <link>...</link>
                    link = ch.text.strip()
            elif t in ("guid", "id") and ch.text and not guid:
                guid = ch.text.strip()
            elif t in ("description", "summary", "content") and ch.text and not summary:
                summary = ch.text.strip()
        if not link and guid and guid.startswith("http"):
            link = guid
        if link and link.startswith("http"):
            out.append({
                "title": title,
                "url": link,
                "guid": guid or link,
                # 요약은 HTML 이 섞이므로 가시 텍스트만, 길이 제한.
                "summary": (_visible_text(summary)[:2000] if summary else None) or None,
            })
    return out


async def _sleep_jitter() -> None:
    """요청 사이 랜덤 지연(정중한 폴링)."""
    await asyncio.sleep(random.uniform(_JITTER_LO, _JITTER_HI))


async def _fetch_feed(url: str, client: httpx.AsyncClient) -> tuple[Optional[bytes], Optional[int]]:
    """RSS/Atom 피드 GET. (raw_bytes|None, http_code|None) 반환.
    피드 URL 은 하드코딩된 신뢰 목록이라 SSRF 위험이 낮아 follow_redirects 를 허용한다."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    try:
        async with client.stream("GET", url, timeout=FEED_TIMEOUT,
                                 follow_redirects=True, headers=headers) as resp:
            code = resp.status_code
            if code >= 400:
                return None, code
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_FEED_BYTES:
                    break
            return b"".join(chunks), code
    except Exception as e:
        logger.info(f"[collector] feed fetch 실패 {url}: {type(e).__name__}")
        return None, None


def _existing_urls(db, urls: list[str]) -> set:
    """이미 잡아둔 글 URL 집합. 조회 실패 시 '전부 기존'으로 간주해(빈 신규) 중복 캡처 폭주 방지."""
    if not urls:
        return set()
    try:
        resp = db.table("captured_posts").select("url").in_("url", urls).execute()
        return {r["url"] for r in (resp.data or [])}
    except Exception as e:
        logger.warning(f"[collector] existing 조회 실패: {e}")
        return set(urls)


async def _capture(db, source: str, feed_url: str, item: dict,
                   client: httpx.AsyncClient) -> bool:
    """신규 글 1건의 본문을 1회 GET 해 captured_posts 에 저장(콜드스타트 = 기준선 캡처)."""
    url = item["url"]
    obs = await fetch_observation(url, client, capture_text=True)
    res = decide_status(obs, url, None)   # 콜드스타트: 기준선 없음
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    text = obs.get("text")
    captured_ok = obs.get("net") == "ok" and bool(text)

    row = {
        "source": source,
        "feed": feed_url,
        "url": url,
        "guid": item.get("guid"),
        "title": item.get("title"),
        "rss_summary": item.get("summary"),
        "status": res["status"],
        "http_code": res["http_code"],
        "reason": res["reason"],
        "last_checked": now_iso,
        "check_count": 1,
        "content_hash": obs.get("text_hash"),
        "body_text": text if captured_ok else None,
        "captured_at": now_iso if captured_ok else None,
    }
    if res.get("baseline"):
        b = res["baseline"]
        row.update({
            "baseline_final_url": b["final_url"],
            "baseline_len": b["len"],
            "baseline_hash": b.get("hash"),
            "baseline_del_match": b["del_match"],
            "baseline_blk_match": b["blk_match"],
            "baseline_at": now_iso,
        })
    if res["status"] == "deleted":
        row["deleted_at"] = now_iso
    nxt, ec = compute_next_check(res["status"], 1, 0, now_dt)
    row["next_check_at"] = nxt
    row["error_count"] = ec

    try:
        # 같은 주기에 두 피드가 같은 글을 올려도 멱등(url unique).
        db.table("captured_posts").upsert(row, on_conflict="url").execute()
    except Exception as e:
        logger.warning(f"[collector] capture 저장 실패 {url}: {e}")
        return False
    # 살아있을 때 Wayback 위임 큐에 적재(삭제 대비 외부 스냅샷). 기능 꺼졌으면 no-op.
    try:
        wayback_enqueue(url)
    except Exception as e:
        logger.warning(f"[collector] wayback enqueue 실패 {url}: {e}")
    return True


async def poll_feeds(client: httpx.AsyncClient) -> dict:
    """모든 피드를 정중하게 순회하며 신규 글을 발견·캡처. {discovered, captured} 반환.

    공정 분배: 발견(모든 피드)과 캡처를 분리하고, 캡처는 피드별로 한 건씩 번갈아 가져가는
    라운드로빈으로 주기 예산(COLLECTOR_MAX_CAPTURE_PER_CYCLE)을 소진한다. 한 고volume 피드
    (예: ppomppu)가 예산을 독식해 다른 커뮤니티가 한 번도 수집 안 되는 일을 막는다.
    신규가 적은 피드는 자기 몫만 쓰고, 남은 예산은 다른 피드가 채운다(낭비 없음)."""
    db = get_db()
    discovered = 0

    # 1) 발견: 모든 피드에서 신규 항목만 추린다(피드 본문은 가벼워 전부 폴링).
    per_feed: list[tuple[str, str, list]] = []
    for source, feed_url in COMMUNITY_FEEDS:
        raw, code = await _fetch_feed(feed_url, client)
        await _sleep_jitter()
        new_items: list = []
        if raw is None:
            if code in BOT_BLOCK_CODES:
                logger.info(f"[collector] {source} 봇차단/일시거부({code}) — 이번 주기 건너뜀")
        else:
            items = _parse_feed(raw)[:COLLECTOR_FEED_ITEMS]
            urls = [it["url"] for it in items]
            existing = _existing_urls(db, urls) if urls else set()
            new_items = [it for it in items if it["url"] not in existing]
            discovered += len(new_items)
        per_feed.append((source, feed_url, new_items))

    # 2) 캡처: 라운드로빈(피드당 1건씩 돌아가며) 예산 소진. 본문은 신규 1건당 정확히 1회 GET.
    budget = COLLECTOR_MAX_CAPTURE_PER_CYCLE
    captured = 0
    idx = [0] * len(per_feed)
    progressed = True
    while budget > 0 and progressed:
        progressed = False
        for i, (source, feed_url, new_items) in enumerate(per_feed):
            if budget <= 0:
                break
            if idx[i] >= len(new_items):
                continue
            it = new_items[idx[i]]
            idx[i] += 1
            progressed = True
            if await _capture(db, source, feed_url, it, client):
                captured += 1
            budget -= 1
            await _sleep_jitter()

    return {"discovered": discovered, "captured": captured}


async def recheck_captured_batch(batch_size: int = COLLECTOR_RECHECK_BATCH) -> int:
    """만기 도래한 captured_posts 를 재검사해 삭제/변화를 감지(적응형 due 순). 검사 수 반환.
    tracker 와 동일하게 hard(404/410) deleted 만 큐에서 영구 제외하고, soft 는 정정 위해 유지."""
    db = get_db()
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        resp = (
            db.table("captured_posts")
            .select(
                "id,url,check_count,status,http_code,error_count,"
                "baseline_final_url,baseline_len,baseline_hash,"
                "baseline_del_match,baseline_blk_match,baseline_at"
            )
            .or_(RECHECK_QUEUE_FILTER)
            .lte("next_check_at", now_iso)
            .order("next_check_at", desc=False, nullsfirst=True)
            .limit(batch_size)
            .execute()
        )
    except Exception as e:
        logger.warning(f"[collector] recheck 조회 실패: {e}")
        return 0

    rows = resp.data or []
    if not rows:
        return 0

    newly_deleted = 0
    async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
        for row in rows:
            obs = await fetch_observation(row["url"], client)
            res = decide_status(obs, row["url"], _baseline_from_row(row))
            prev = row.get("status")
            now_dt = datetime.now(timezone.utc)
            ni = now_dt.isoformat()
            # tracker 와 동일한 payload(상태·기준선·적응형 스케줄)를 공용 헬퍼로 생성.
            # captured_posts 는 적응형 컬럼이 항상 있으므로 adaptive=True. deleted_at·
            # newly_deleted 는 _build_update 가 다루지 않으므로 여기서 처리한다.
            upd = _build_update(res, row, ni, adaptive=True, now=now_dt)
            if res["status"] == "deleted" and prev != "deleted":
                upd["deleted_at"] = ni
                newly_deleted += 1
            try:
                db.table("captured_posts").update(upd).eq("id", row["id"]).execute()
            except Exception as e:
                logger.warning(f"[collector] recheck 갱신 실패 {row['id']}: {e}")
            await _sleep_jitter()

    if newly_deleted:
        logger.info(f"[collector] captured {newly_deleted}건 새로 삭제 감지")
    return len(rows)


def _table_exists() -> bool:
    try:
        get_db().table("captured_posts").select("id").limit(1).execute()
        return True
    except Exception:
        return False


async def background_loop() -> None:
    """앱 lifespan 동안 도는 선제 수집 루프 (피드 폴링 → 캡처 → 삭제 재검사)."""
    global _next_poll_at, _last_poll_at, _last_result

    if not COLLECTOR_ENABLED:
        logger.info("[collector] 비활성화 (COLLECTOR_ENABLED=false)")
        return
    if not _table_exists():
        logger.warning("[collector] captured_posts 테이블 없음 — migrations/006 적용 후 "
                       "COLLECTOR_ENABLED=true 로 켜세요. 루프 중단.")
        return

    logger.info(f"[collector] 시작 · feeds={len(COMMUNITY_FEEDS)} · interval={COLLECTOR_INTERVAL_SEC}s")
    _next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=COLLECTOR_INITIAL_DELAY_SEC)
    try:
        await asyncio.sleep(COLLECTOR_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        return

    while True:
        try:
            async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
                poll = await poll_feeds(client)
            rechecked = await recheck_captured_batch()
            _last_poll_at = datetime.now(timezone.utc)
            _last_result = {**poll, "rechecked": rechecked, "at": _last_poll_at.isoformat()}
            if poll["captured"] or rechecked:
                logger.info(f"[collector] 발견 {poll['discovered']} · 캡처 {poll['captured']} "
                            f"· 재검사 {rechecked}")
        except asyncio.CancelledError:
            logger.info("[collector] cancelled")
            return
        except Exception as e:
            logger.warning(f"[collector] loop error: {e}")

        jitter = random.uniform(0.9, 1.1)
        delay = max(60, int(COLLECTOR_INTERVAL_SEC * jitter))
        _next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

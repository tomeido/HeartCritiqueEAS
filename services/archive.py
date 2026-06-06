"""
Arweave 영구 박제 오케스트레이터.

흐름:
  1. Supabase에서 스토리 + 투표 로그 조회
  2. 에이전트 private key로 서명
  3. uploader 서비스(Node.js/Irys)에 업로드 요청
  4. 반환된 Arweave Tx ID를 Supabase에 저장

신뢰성:
  - 동시 트리거/중복 박제 방지: '__pending__' 마커를 조건부 선점(claim)해 한 번에 한
    코루틴만 업로드하도록 보장 (mainnet 에선 ETH 이중 지불 방지).
  - 실패 시 선점 해제 + archive_attempts/last_archive_error 기록 → reconcile sweeper 가
    지수 백오프로 자동 재시도 (임계값 넘긴 글이 일시 장애로 영영 누락되는 것 방지).
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

from services.crypto import has_configured_key, sign_dataset
from services.db import get_db

import logging
logger = logging.getLogger(__name__)

UPLOADER_URL = os.environ.get("UPLOADER_URL", "http://uploader:3000").rstrip("/")

# 업로드 진행 중임을 나타내는 선점 마커. NULL(미박제)도 실제 tx도 아닌 중간 상태.
PENDING_MARKER = "__pending__"
# 선점 후 이 시간 안에 끝나지 않으면 크래시로 간주하고 다른 시도가 회수 가능.
PENDING_TIMEOUT_SEC = int(os.environ.get("ARCHIVE_PENDING_TIMEOUT_SEC", "600"))

# 재시도 백오프 파라미터
RECONCILE_BASE_SEC = int(os.environ.get("ARCHIVE_RETRY_BASE_SEC", "300"))     # 5분
RECONCILE_MAX_SEC = int(os.environ.get("ARCHIVE_RETRY_MAX_SEC", "21600"))     # 6시간
RECONCILE_MAX_ATTEMPTS = int(os.environ.get("ARCHIVE_RETRY_MAX_ATTEMPTS", "10"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _record_archive_failure(db, story_id: str, err: Exception) -> None:
    """박제 실패 영속화: 선점 마커를 해제(NULL 복귀)하고 시도 횟수/에러를 기록."""
    try:
        cur = (
            db.table("stories")
            .select("archive_attempts")
            .eq("id", story_id)
            .limit(1)
            .execute()
        )
        attempts = (cur.data[0].get("archive_attempts") or 0) if cur.data else 0
        # 아직 내 선점(__pending__)을 들고 있을 때만 해제 — 그 사이 다른 코루틴이
        # 실제 박제를 끝냈다면(real tx_id) 덮어쓰지 않게 조건부.
        db.table("stories").update({
            "arweave_tx_id": None,  # 선점 해제 → reconcile 가 재시도 가능
            "archive_attempts": attempts + 1,
            "last_archive_attempt": _now_iso(),
            "last_archive_error": str(err)[:500],
        }).eq("id", story_id).eq("arweave_tx_id", PENDING_MARKER).execute()
    except Exception as e2:
        logger.warning(f"[archive] failure record failed for {story_id}: {e2}")


async def archive_story(story_id: str) -> str | None:
    """박제 실행. 성공 시 Arweave Tx ID 반환, 실패/선점실패 시 None."""
    # ephemeral 키로 박제하면 재시작 후 과거 박제물 전부가 '다른 키로 서명됨'으로
    # 검증 실패한다. 검증 불가한 박제물을 만들지 않도록 키 없으면 박제 스킵.
    if not has_configured_key():
        logger.info(f"[archive] AGENT_PRIVATE_KEY 미설정 — 박제 스킵 (story {story_id})")
        return None

    db = get_db()

    story_resp = db.table("stories").select("*").eq("id", story_id).limit(1).execute()
    if not story_resp.data:
        return None
    story = story_resp.data[0]

    tx = story.get("arweave_tx_id")
    if tx and tx != PENDING_MARKER:
        return tx  # 이미 박제 완료

    # ── 조건부 선점(claim) — 동시 트리거 중 단일 승자 보장 ──────────────────────
    now = datetime.now(timezone.utc)
    claim_q = db.table("stories").update({
        "arweave_tx_id": PENDING_MARKER,
        "last_archive_attempt": now.isoformat(),
    }).eq("id", story_id)
    if tx == PENDING_MARKER:
        # 크래시로 남은 stale pending 만 회수 (timeout 초과한 것만)
        stale_before = (now - timedelta(seconds=PENDING_TIMEOUT_SEC)).isoformat()
        claim = claim_q.eq("arweave_tx_id", PENDING_MARKER).lt(
            "last_archive_attempt", stale_before
        ).execute()
    else:
        claim = claim_q.is_("arweave_tx_id", "null").execute()

    if not claim.data:
        # 다른 코루틴이 이미 선점/완료. 기존 tx 재조회.
        cur = (
            db.table("stories").select("arweave_tx_id").eq("id", story_id).limit(1).execute()
        )
        cur_tx = cur.data[0].get("arweave_tx_id") if cur.data else None
        return cur_tx if cur_tx and cur_tx != PENDING_MARKER else None

    # ── 선점 성공 — 업로드 진행 ───────────────────────────────────────────────
    votes_resp = db.table("votes").select("user_id,created_at").eq("story_id", story_id).execute()

    # 검열 증거 봉인: 박제 시점의 출처 생존 상태 + 언론 보도 격차.
    # 원본이 모두 사라진 뒤에도 '왜 검열 위협을 받았는지', '박제 시점에 출처가 이미
    # 삭제됐는지'를 서명된 박제물만으로 검증 가능하게 한다. (미션의 핵심 주장 증명)
    checks_resp = (
        db.table("citation_checks")
        .select("url,status,http_code,reason,last_checked,check_count")
        .eq("story_id", story_id)
        .execute()
    )
    citation_status = checks_resp.data or []
    deleted_count = sum(1 for c in citation_status if c.get("status") == "deleted")
    blocked_count = sum(1 for c in citation_status if c.get("status") == "blocked")

    dataset = {
        "story": {
            "id": story_id,
            "category": story["category"],
            "body": story["body"],
            "citations": story["citations"],
            "search_queries": story.get("search_queries", []),
            "created_at": story["created_at"],
        },
        "votes": {
            "count": len(votes_resp.data),
            "log": votes_resp.data,
        },
        "evidence": {
            "gap_score": story.get("gap_score"),
            "community_count": story.get("community_count"),
            "news_count": story.get("news_count"),
            "deleted_count": deleted_count,
            "blocked_count": blocked_count,
            "citation_status": citation_status,
        },
        "archived_at": _now_iso(),
        "version": "heart-critique-archive-v2",
    }

    signed = sign_dataset(dataset)

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(f"{UPLOADER_URL}/upload", json=signed)
            resp.raise_for_status()
            up = resp.json()
            tx_id = up["txId"]
            # 업로더가 네트워크에 맞는 조회 URL을 반환(devnet→devnet.irys.xyz,
            # mainnet→gateway.irys.xyz). 구버전/응답 누락 대비 폴백도 network-aware 로:
            # devnet 행에 arweave.net 이 박혀 404 나는 것을 방지(BUG: 링크 미연결).
            net = up.get("network") or os.environ.get("IRYS_NETWORK", "devnet")
            fallback_base = (
                "https://gateway.irys.xyz/" if net == "mainnet"
                else "https://devnet.irys.xyz/"
            )
            arweave_url = up.get("arweaveUrl") or f"{fallback_base}{tx_id}"
            if not up.get("arweaveUrl"):
                logger.warning(
                    f"[archive] uploader 가 arweaveUrl 누락 — net={net} 폴백 사용 (story {story_id})"
                )
    except Exception as e:
        logger.warning(f"[archive] Irys upload failed for story {story_id}: {e}")
        _record_archive_failure(db, story_id, e)  # 선점 해제 + 재시도 대상으로 표시
        return None

    # 내 선점(__pending__)을 아직 들고 있을 때만 확정 기록 — 그 사이 재선점/완료된
    # 경우 승자의 tx_id 를 덮어쓰지 않는다(이중 박제·ETH 이중 지출 방지).
    db.table("stories").update({
        "archived_at": _now_iso(),
        "arweave_tx_id": tx_id,
        "arweave_url": arweave_url,
        "last_archive_error": None,
    }).eq("id", story_id).eq("arweave_tx_id", PENDING_MARKER).execute()

    logger.info(f"[archive] Story {story_id} archived → {arweave_url}")
    return tx_id


def _backoff_ready(attempts: int, last_iso: str | None, now: datetime) -> bool:
    """지수 백오프: 시도 N회 후 base*2^(N-1) 초(상한 MAX)가 지나야 재시도."""
    if not last_iso:
        return True
    last = _parse_iso(last_iso)
    if last is None:
        return True
    delay = min(RECONCILE_BASE_SEC * (2 ** max(0, attempts - 1)), RECONCILE_MAX_SEC)
    return (now - last).total_seconds() >= delay


async def reconcile_pending_archives(limit: int = 5) -> int:
    """임계값을 넘겼지만 박제되지 않은(또는 실패한) 글을 백오프 후 재시도.
    tracker 백그라운드 루프가 주기적으로 호출. 실제 재시도한 글 수 반환.

    '사라지기 전에 박제' 미션의 안전망: 일시 장애(uploader 다운/타임아웃/잔고부족)로
    실패한 박제가 영영 누락되지 않게 한다."""
    from services.threshold import gather_story_signals  # 순환 import 회피

    if not has_configured_key():
        return 0  # 서명키 없으면 박제 자체를 안 하므로 sweeper 도 무의미

    db = get_db()
    now = datetime.now(timezone.utc)

    # 크래시로 '__pending__' 에 멈춘 글을 NULL 로 회수 (timeout 초과한 것만).
    # 이래야 아래 null 조회가 다시 후보로 잡는다.
    stale_before = (now - timedelta(seconds=PENDING_TIMEOUT_SEC)).isoformat()
    try:
        db.table("stories").update({"arweave_tx_id": None}).eq(
            "arweave_tx_id", PENDING_MARKER
        ).lt("last_archive_attempt", stale_before).execute()
    except Exception as e:
        logger.warning(f"[archive] stale-pending recovery failed: {e}")

    try:
        resp = (
            db.table("stories")
            .select("id,vote_count,archive_attempts,last_archive_attempt")
            .is_("arweave_tx_id", "null")
            .order("vote_count", desc=True)
            .limit(60)
            .execute()
        )
    except Exception as e:
        logger.warning(f"[archive] reconcile query failed: {e}")
        return 0

    retried = 0
    for row in resp.data or []:
        if retried >= limit:
            break
        attempts = row.get("archive_attempts") or 0
        if attempts >= RECONCILE_MAX_ATTEMPTS:
            continue  # 영구 실패로 간주 (last_archive_error 로 운영자가 확인)
        if not _backoff_ready(attempts, row.get("last_archive_attempt"), now):
            continue

        sig = await asyncio.to_thread(gather_story_signals, row["id"])
        if not sig or sig.get("archived"):
            continue
        if sig.get("vote_count", 0) < sig.get("threshold", 1):
            continue  # 아직 임계값 미달 → 박제 대상 아님

        logger.info(f"[archive] reconcile retry {row['id'][:8]} (attempt {attempts + 1})")
        tx = await archive_story(row["id"])
        retried += 1
        if tx:
            logger.info(f"[archive] reconcile success {row['id'][:8]} → {tx[:12]}")

    return retried

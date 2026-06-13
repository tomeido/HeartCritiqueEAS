"""Wayback Machine 위임 박제 (Save Page Now 2).

목적: 원본 커뮤니티 글이 삭제돼도 중립 제3자(Internet Archive)의 스냅샷은 남는다.
우리 서버가 직접 스크래핑하는 대신 "이 URL 지금 박제해줘"를 IA 에 위임해 수집 부담·봇탐지
위험을 떠넘기고, 동시에 법정에서도 인정되는 타임스탬프 증거를 확보한다(자체 Irys 서명
번들과 상호 보강). 원본이 404/410 으로 죽으면 Availability API 로 스냅샷 생존을 확인해
'원본 삭제됨 + 아카이브 박제 생존'을 증명할 수 있다.

설계(레이트 한도 준수): '프로듀서(enqueue) → 단일 컨슈머(process_batch)' 큐.
  · enqueue(): 스토리 citation 등록(tracker)·화제글 캡처(collector) 시 url 을 wayback_snapshots
    에 'queued' 로 적재(API 호출 없음). 봇차단 도메인(fmkorea 등)은 IA 도 실패하므로 제외.
  · process_batch(): tracker 루프가 주기 호출. (1) pending 작업 상태 폴링(무료),
    (2) queued 를 capacity(동시 12/익명 6, 일일 한도) 안에서 SPN2 save 제출 → pending,
       save 실패 시 Availability 로 기존 스냅샷이라도 찾아 success 로 승격.

⚠️ 한계: Cloudflare/안티봇(fmkorea)은 Wayback·archive.today 도 동일하게 막혀(403/challenge)
   위임해도 스냅샷이 안 떠진다 → 기존 '🚫 삭제 추적 불가'와 동일. 위임이 추적불가를
   추적가능으로 바꾸지 못한다.

API 레퍼런스(2026-06 확인):
  POST https://web.archive.org/save        (Authorization: LOW {access}:{secret}, form: url=)
       → {"job_id": "..."}
  GET  https://web.archive.org/save/status/{job_id}   → {"status": pending|success|error, ...}
  GET  https://web.archive.org/save/status/user       → {"available": N, "daily_captures": ...}
  GET  https://archive.org/wayback/available?url=...   → {"archived_snapshots": {"closest": {...}}}
"""

import os
from datetime import datetime, timedelta, timezone

import httpx

from services.db import get_db
from services.tracker import is_untrackable_source

import logging
logger = logging.getLogger(__name__)

WAYBACK_ENABLED = os.environ.get("WAYBACK_ENABLED", "false").lower() == "true"
# IA S3 키 (https://archive.org/account/s3.php). 익명 save 는 불안정해 자동화엔 키를 권장.
IA_ACCESS_KEY = os.environ.get("IA_ACCESS_KEY", "").strip()
IA_SECRET_KEY = os.environ.get("IA_SECRET_KEY", "").strip()

WAYBACK_SUBMIT_PER_CYCLE = int(os.environ.get("WAYBACK_SUBMIT_PER_CYCLE", "4"))   # 주기당 save 상한
WAYBACK_POLL_PER_CYCLE = int(os.environ.get("WAYBACK_POLL_PER_CYCLE", "10"))      # 주기당 status 폴링
WAYBACK_MAX_ATTEMPTS = int(os.environ.get("WAYBACK_MAX_ATTEMPTS", "3"))           # save 재시도 한도
WAYBACK_POLL_BACKOFF_SEC = int(os.environ.get("WAYBACK_POLL_BACKOFF_SEC", "120"))  # pending 재폴링 간격
# 최근 이 기간 안에 이미 박제됐으면 IA 가 재캡처를 건너뛰고 기존 스냅샷을 돌려준다(쿼터 절약).
WAYBACK_IF_NOT_ARCHIVED_SEC = int(os.environ.get("WAYBACK_IF_NOT_ARCHIVED_SEC", "2592000"))  # 30일

_SAVE_ENDPOINT = "https://web.archive.org/save"
_STATUS_ENDPOINT = "https://web.archive.org/save/status"
_USER_STATUS_ENDPOINT = "https://web.archive.org/save/status/user"
_AVAILABILITY_ENDPOINT = "https://archive.org/wayback/available"
_HTTP_TIMEOUT = 30


def _can_save() -> bool:
    """새 스냅샷 제출(save) 가능 여부 = 기능 켜짐 + IA 키 보유. 키가 없으면 availability 만."""
    return WAYBACK_ENABLED and bool(IA_ACCESS_KEY and IA_SECRET_KEY)


def _auth_header() -> dict:
    return {"Authorization": f"LOW {IA_ACCESS_KEY}:{IA_SECRET_KEY}",
            "Accept": "application/json"}


# ── 순수 파서/빌더 (외부 의존 없음 — 테스트 용이) ────────────────────────────
def snapshot_url(ts: str, original_url: str) -> str:
    """Wayback 영속 재생 URL. ts=YYYYMMDDhhmmss."""
    return f"https://web.archive.org/web/{ts}/{original_url}"


def _parse_save(data: dict) -> tuple[str | None, str | None]:
    """save 응답에서 (job_id, error_message). job_id 없으면 message 를 사유로."""
    if not isinstance(data, dict):
        return None, "bad_response"
    job_id = data.get("job_id")
    if job_id:
        return str(job_id), None
    return None, str(data.get("message") or data.get("status_ext") or "no_job_id")


def _parse_status(data: dict) -> dict:
    """save/status 응답 → {status, timestamp, snapshot_url, reason}.
    status ∈ pending|success|error (그 외/파싱불가는 pending 으로 보수 처리)."""
    if not isinstance(data, dict):
        return {"status": "pending"}
    st = data.get("status")
    if st == "success":
        ts = data.get("timestamp")
        orig = data.get("original_url")
        return {
            "status": "success",
            "timestamp": ts,
            "snapshot_url": snapshot_url(ts, orig) if (ts and orig) else None,
        }
    if st == "error":
        return {"status": "error",
                "reason": str(data.get("message") or data.get("status_ext") or "error")}
    return {"status": "pending"}


def _parse_user_status(data: dict) -> dict:
    """save/status/user → {available, daily_remaining}. 실패/누락은 None."""
    if not isinstance(data, dict):
        return {"available": None, "daily_remaining": None}
    avail = data.get("available")
    cap = data.get("daily_captures")
    lim = data.get("daily_captures_limit")
    daily_remaining = (lim - cap) if isinstance(cap, int) and isinstance(lim, int) else None
    return {"available": avail if isinstance(avail, int) else None,
            "daily_remaining": daily_remaining}


def _parse_availability(data: dict) -> dict | None:
    """wayback/available → {snapshot_url, timestamp} 또는 None(스냅샷 없음)."""
    if not isinstance(data, dict):
        return None
    closest = ((data.get("archived_snapshots") or {}).get("closest")) or {}
    if not closest.get("available"):
        return None
    url = closest.get("url")
    ts = closest.get("timestamp")
    if not url:
        return None
    # 재생 URL 은 https 로 정규화(IA 가 http 로 줄 때가 있음)
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return {"snapshot_url": url, "timestamp": ts}


# ── 비동기 HTTP 호출 ─────────────────────────────────────────────────────────
async def save_now(url: str, client: httpx.AsyncClient) -> tuple[str | None, str | None]:
    """SPN2 save 제출. (job_id, error). 실패는 (None, 사유)."""
    try:
        resp = await client.post(
            _SAVE_ENDPOINT,
            headers={**_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "url": url,
                "capture_all": "1",                              # 4xx/5xx 도 보존
                "if_not_archived_within": str(WAYBACK_IF_NOT_ARCHIVED_SEC),
                "skip_first_archive": "1",                       # 첫 스캔 단계 생략(빠름)
            },
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 429:
            return None, "rate_limited"
        return _parse_save(resp.json())
    except Exception as e:
        return None, f"net:{type(e).__name__}"


async def check_job(job_id: str, client: httpx.AsyncClient) -> dict:
    """save/status/{job_id} 폴링. 파싱 결과 dict (전송 실패는 pending 으로 보수)."""
    try:
        resp = await client.get(f"{_STATUS_ENDPOINT}/{job_id}",
                                headers=_auth_header(), timeout=_HTTP_TIMEOUT)
        return _parse_status(resp.json())
    except Exception:
        return {"status": "pending"}


async def check_capacity(client: httpx.AsyncClient) -> dict:
    """현재 동시/일일 가용량. 실패 시 {available:None}."""
    try:
        resp = await client.get(_USER_STATUS_ENDPOINT, headers=_auth_header(),
                                timeout=_HTTP_TIMEOUT)
        return _parse_user_status(resp.json())
    except Exception:
        return {"available": None, "daily_remaining": None}


async def availability(url: str, client: httpx.AsyncClient) -> dict | None:
    """이 URL 의 기존 Wayback 스냅샷(가장 가까운 것)을 조회. 인증 불필요."""
    try:
        resp = await client.get(_AVAILABILITY_ENDPOINT, params={"url": url},
                                timeout=_HTTP_TIMEOUT)
        return _parse_availability(resp.json())
    except Exception:
        return None


# ── DB 큐 (프로듀서) ─────────────────────────────────────────────────────────
def _table_exists() -> bool:
    try:
        get_db().table("wayback_snapshots").select("id").limit(1).execute()
        return True
    except Exception:
        return False


def enqueue(urls) -> int:
    """url(들)을 스냅샷 큐에 'queued' 로 적재(멱등, API 호출 없음). 적재 시도 수 반환.
    봇차단 도메인은 IA 도 실패하므로 제외한다. 기능 꺼졌으면 아무것도 안 한다."""
    if not WAYBACK_ENABLED:
        return 0
    if isinstance(urls, str):
        urls = [urls]
    rows = []
    seen = set()
    for u in urls:
        if not u or not isinstance(u, str) or u in seen:
            continue
        if is_untrackable_source(u):     # fmkorea 류 — 위임해도 실패
            continue
        seen.add(u)
        rows.append({"url": u, "status": "queued"})
    if not rows:
        return 0
    try:
        # 이미 큐에 있으면(성공/진행 포함) 덮어쓰지 않는다(DO NOTHING).
        get_db().table("wayback_snapshots").upsert(
            rows, on_conflict="url", ignore_duplicates=True
        ).execute()
        return len(rows)
    except Exception as e:
        logger.warning(f"[wayback] enqueue 실패: {e}")
        return 0


# ── 컨슈머 (process_batch) ───────────────────────────────────────────────────
async def _poll_pending(db, client) -> int:
    """pending 작업의 상태를 폴링해 success/error 로 확정. 갱신 건수 반환."""
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat()
    try:
        resp = (
            db.table("wayback_snapshots")
            .select("id,url,job_id,next_poll_at")
            .eq("status", "pending")
            .lte("next_poll_at", now_iso)
            .order("next_poll_at", desc=False, nullsfirst=True)
            .limit(WAYBACK_POLL_PER_CYCLE)
            .execute()
        )
    except Exception as e:
        logger.warning(f"[wayback] pending 조회 실패: {e}")
        return 0
    rows = resp.data or []
    done = 0
    for row in rows:
        if not row.get("job_id"):
            continue
        res = await check_job(row["job_id"], client)
        upd = {"updated_at": now.isoformat()}
        if res["status"] == "success":
            upd.update({"status": "success", "snapshot_timestamp": res.get("timestamp"),
                        "snapshot_url": res.get("snapshot_url"), "reason": None})
            done += 1
        elif res["status"] == "error":
            upd.update({"status": "error", "reason": res.get("reason")})
            done += 1
        else:  # 아직 pending — 다음 폴링 시각만 미룸
            upd["next_poll_at"] = (now + timedelta(seconds=WAYBACK_POLL_BACKOFF_SEC)).isoformat()
        try:
            db.table("wayback_snapshots").update(upd).eq("id", row["id"]).execute()
        except Exception as e:
            logger.warning(f"[wayback] pending 갱신 실패 {row['id']}: {e}")
    return done


async def _submit_queued(db, client) -> int:
    """queued 를 capacity 안에서 save 제출(→pending). save 실패 시 기존 스냅샷을 찾아 승격.
    제출/승격 건수 반환."""
    cap = await check_capacity(client)
    budget = WAYBACK_SUBMIT_PER_CYCLE
    if isinstance(cap.get("available"), int):
        budget = min(budget, cap["available"])
    if isinstance(cap.get("daily_remaining"), int):
        budget = min(budget, cap["daily_remaining"])
    if budget <= 0:
        return 0
    try:
        resp = (
            db.table("wayback_snapshots")
            .select("id,url,attempts")
            .eq("status", "queued")
            .order("created_at", desc=False, nullsfirst=True)
            .limit(budget)
            .execute()
        )
    except Exception as e:
        logger.warning(f"[wayback] queued 조회 실패: {e}")
        return 0
    rows = resp.data or []
    processed = 0
    for row in rows:
        url = row["url"]
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        attempts = (row.get("attempts") or 0) + 1
        job_id, err = await save_now(url, client)
        if job_id:
            upd = {"status": "pending", "job_id": job_id, "attempts": attempts,
                   "submitted_at": now_iso, "updated_at": now_iso,
                   "next_poll_at": (now + timedelta(seconds=WAYBACK_POLL_BACKOFF_SEC)).isoformat()}
            processed += 1
        else:
            # save 실패 → 이미 떠 있는 스냅샷이라도 있으면 그걸로 승격(타인이 박제했을 수 있음)
            snap = await availability(url, client)
            if snap:
                upd = {"status": "success", "attempts": attempts,
                       "snapshot_url": snap["snapshot_url"],
                       "snapshot_timestamp": snap.get("timestamp"),
                       "updated_at": now_iso, "reason": "기존 스냅샷"}
                processed += 1
            elif attempts >= WAYBACK_MAX_ATTEMPTS:
                upd = {"status": "error", "attempts": attempts,
                       "reason": (err or "save_failed")[:200], "updated_at": now_iso}
            else:
                upd = {"status": "queued", "attempts": attempts,
                       "reason": (err or "save_failed")[:200], "updated_at": now_iso}
        try:
            db.table("wayback_snapshots").update(upd).eq("id", row["id"]).execute()
        except Exception as e:
            logger.warning(f"[wayback] submit 갱신 실패 {row['id']}: {e}")
    return processed


async def process_batch() -> dict:
    """tracker 루프가 주기 호출하는 단일 컨슈머. pending 폴링 + queued 제출."""
    if not WAYBACK_ENABLED:
        return {}
    db = get_db()
    async with httpx.AsyncClient() as client:
        polled = await _poll_pending(db, client)
        submitted = await _submit_queued(db, client) if _can_save() else 0
    if polled or submitted:
        logger.info(f"[wayback] 폴링확정 {polled} · 제출/승격 {submitted}")
    return {"polled": polled, "submitted": submitted}


# ── 조회 (UI/API 직렬화 보조) ────────────────────────────────────────────────
def get_wayback_map(urls: list) -> dict:
    """url 목록의 스냅샷 상태를 한 번에 조회. {url: {status, snapshot_url, timestamp}}."""
    urls = [u for u in (urls or []) if u]
    if not urls or not WAYBACK_ENABLED:
        return {}
    try:
        resp = (
            get_db().table("wayback_snapshots")
            .select("url,status,snapshot_url,snapshot_timestamp")
            .in_("url", urls)
            .execute()
        )
    except Exception:
        return {}
    return {r["url"]: r for r in (resp.data or [])}


def get_status() -> dict:
    """대시보드용 상태(stats 가 사용)."""
    return {
        "enabled": WAYBACK_ENABLED,
        "can_save": _can_save(),       # False = IA 키 없음(availability 만 가능)
        "submit_per_cycle": WAYBACK_SUBMIT_PER_CYCLE,
    }

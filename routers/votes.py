import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from postgrest.exceptions import APIError

from services.archive import archive_story
from services.db import get_anon_db, get_db
from services.threshold import (
    DEFAULT_THRESHOLD,
    compute_effective_threshold,
    gather_story_signals,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def _ensure_uuid(story_id: str) -> None:
    """uuid 컬럼에 잘못된 형식을 넘기면 PostgREST 가 22P02 로 500 을 내므로 선검증."""
    try:
        uuid.UUID(story_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "스토리를 찾을 수 없습니다")


def _verify_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요합니다")
    token = authorization.removeprefix("Bearer ")
    # 토큰 검증은 최소권한 anon 클라이언트로 (service-role 싱글톤의 인증 컨텍스트 오염 방지)
    db = get_anon_db()
    try:
        user_resp = db.auth.get_user(token)
    except Exception:
        raise HTTPException(401, "유효하지 않은 토큰")
    if not user_resp.user:
        raise HTTPException(401, "유효하지 않은 토큰")
    return user_resp.user.id


@router.post("/vote/{story_id}")
async def vote(
    story_id: str,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    _ensure_uuid(story_id)
    user_id = _verify_token(authorization)
    db = get_db()

    # 삽입 실패를 한 묶음으로 409 처리하던 것을 원인별로 구분: 중복(23505)만 '이미 투표',
    # 없는 스토리(FK 23503)는 404, 그 외(일시 DB 오류 등)는 삼키지 말고 5xx 로 노출.
    try:
        db.table("votes").insert({"story_id": story_id, "user_id": user_id}).execute()
    except APIError as e:
        code = getattr(e, "code", None)
        if code == "23505":
            raise HTTPException(409, "이미 투표하셨습니다")
        if code == "23503":
            raise HTTPException(404, "스토리를 찾을 수 없습니다")
        logger.warning(f"[vote] insert 실패 story={story_id} code={code}: {e}")
        raise HTTPException(503, "투표 처리에 실패했습니다. 잠시 후 다시 시도하세요.")
    except Exception as e:
        logger.warning(f"[vote] insert 비APIError story={story_id}: {e}")
        raise HTTPException(503, "투표 처리에 실패했습니다. 잠시 후 다시 시도하세요.")

    # 신호 + 투표수 + effective threshold 한 번에
    sig = await asyncio.to_thread(gather_story_signals, story_id)
    vote_count = sig["vote_count"]
    threshold = sig["threshold"]

    # stories.vote_count 캐시 갱신 — 동시 투표 시 느린 코루틴이 낮은 카운트로 덮어쓰지
    # 않게 단조 증가(.lt)로 가드. (투표는 삽입 전용이라 카운트는 증가만 함)
    db.table("stories").update({"vote_count": vote_count}).eq("id", story_id).lt(
        "vote_count", vote_count
    ).execute()

    if vote_count >= threshold and not sig.get("archived"):
        background_tasks.add_task(archive_story, story_id)

    return {
        "voted": True,
        "vote_count": vote_count,
        "threshold": threshold,
        "default_threshold": DEFAULT_THRESHOLD,
        "urgency": sig["urgency"],
        "urgency_reason": sig.get("reason"),
    }


@router.get("/vote/{story_id}/status")
async def vote_status(
    story_id: str,
    authorization: str | None = Header(default=None),
):
    _ensure_uuid(story_id)
    db = get_db()
    sig = await asyncio.to_thread(gather_story_signals, story_id)

    already_voted = False
    if authorization and authorization.startswith("Bearer "):
        try:
            user_id = _verify_token(authorization)
            v = (
                db.table("votes")
                .select("id")
                .eq("story_id", story_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            already_voted = bool(v.data)
        except HTTPException:
            pass

    return {
        "vote_count": sig.get("vote_count", 0),
        "threshold": sig.get("threshold", DEFAULT_THRESHOLD),
        "default_threshold": DEFAULT_THRESHOLD,
        "urgency": sig.get("urgency", "normal"),
        "urgency_reason": sig.get("reason"),
        "deleted_count": sig.get("deleted_count", 0),
        "blocked_count": sig.get("blocked_count", 0),
        "already_voted": already_voted,
    }

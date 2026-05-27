import asyncio

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from services.archive import archive_story
from services.db import get_db
from services.threshold import (
    DEFAULT_THRESHOLD,
    compute_effective_threshold,
    gather_story_signals,
)

router = APIRouter(prefix="/api")


def _verify_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요합니다")
    token = authorization.removeprefix("Bearer ")
    db = get_db()
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
    user_id = _verify_token(authorization)
    db = get_db()

    # 중복 투표 방지 (unique constraint)
    try:
        db.table("votes").insert({"story_id": story_id, "user_id": user_id}).execute()
    except Exception:
        raise HTTPException(409, "이미 투표하셨습니다")

    # 신호 + 투표수 + effective threshold 한 번에
    sig = await asyncio.to_thread(gather_story_signals, story_id)
    vote_count = sig["vote_count"]
    threshold = sig["threshold"]

    # stories.vote_count 캐시 갱신
    db.table("stories").update({"vote_count": vote_count}).eq("id", story_id).execute()

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

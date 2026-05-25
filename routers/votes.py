import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from services.archive import archive_story
from services.db import get_db

router = APIRouter(prefix="/api")

VOTE_THRESHOLD = int(os.environ.get("VOTE_THRESHOLD", "10"))


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

    # 중복 투표 방지 (unique constraint가 잡음)
    try:
        db.table("votes").insert({"story_id": story_id, "user_id": user_id}).execute()
    except Exception:
        raise HTTPException(409, "이미 투표하셨습니다")

    # 투표수 갱신 (atomic increment via RPC)
    count_resp = (
        db.table("votes").select("id", count="exact").eq("story_id", story_id).execute()
    )
    vote_count = count_resp.count or 0
    db.table("stories").update({"vote_count": vote_count}).eq("id", story_id).execute()

    if vote_count >= VOTE_THRESHOLD:
        background_tasks.add_task(archive_story, story_id)

    return {"voted": True, "vote_count": vote_count, "threshold": VOTE_THRESHOLD}


@router.get("/vote/{story_id}/status")
async def vote_status(
    story_id: str,
    authorization: str | None = Header(default=None),
):
    db = get_db()
    count_resp = (
        db.table("votes").select("id", count="exact").eq("story_id", story_id).execute()
    )
    vote_count = count_resp.count or 0

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
        "vote_count": vote_count,
        "threshold": VOTE_THRESHOLD,
        "already_voted": already_voted,
    }

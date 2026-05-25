import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from services.db import get_db
from services.llm import generate

router = APIRouter(prefix="/api")


@router.post("/story")
async def create_story():
    try:
        result = await asyncio.to_thread(generate)
    except Exception as e:
        raise HTTPException(500, f"LLM 생성 실패: {e}")

    db = get_db()
    resp = db.table("stories").insert({
        "category": result["category"],
        "body": result["body"],
        "citations": result["citations"],
        "search_queries": result["search_queries"],
        "vote_count": 0,
    }).execute()

    story_id = resp.data[0]["id"]

    return {
        "story_id": story_id,
        "category": result["category"],
        "text": result["text"],
        "body": result["body"],
        "citations": result["citations"],
        "provider": result["provider"],
        "model": result["model"],
    }


@router.get("/stories")
async def list_stories(limit: int = 20):
    db = get_db()
    resp = (
        db.table("stories")
        .select("id,category,body,vote_count,archived_at,arweave_tx_id,arweave_url,created_at")
        .order("created_at", desc=True)
        .limit(min(limit, 50))
        .execute()
    )
    return resp.data


@router.get("/stories/{story_id}")
async def get_story(story_id: str):
    db = get_db()
    resp = db.table("stories").select("*").eq("id", story_id).limit(1).execute()
    if not resp.data:
        raise HTTPException(404, "스토리를 찾을 수 없습니다")
    return resp.data[0]

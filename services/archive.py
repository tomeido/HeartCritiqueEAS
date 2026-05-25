"""
Arweave 영구 박제 오케스트레이터.

흐름:
  1. Supabase에서 스토리 + 투표 로그 조회
  2. 에이전트 private key로 서명
  3. uploader 서비스(Node.js/Irys)에 업로드 요청
  4. 반환된 Arweave Tx ID를 Supabase에 저장
"""

import os
from datetime import datetime, timezone

import httpx

from services.crypto import sign_dataset
from services.db import get_db

UPLOADER_URL = os.environ.get("UPLOADER_URL", "http://uploader:3000").rstrip("/")


async def archive_story(story_id: str) -> str | None:
    """박제 실행. 성공 시 Arweave Tx ID 반환, 실패 시 None."""
    db = get_db()

    story_resp = db.table("stories").select("*").eq("id", story_id).maybe_single().execute()
    if not story_resp.data:
        return None
    story = story_resp.data

    if story.get("arweave_tx_id"):
        return story["arweave_tx_id"]

    votes_resp = db.table("votes").select("user_id,created_at").eq("story_id", story_id).execute()

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
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "version": "heart-critique-archive-v1",
    }

    signed = sign_dataset(dataset)

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(f"{UPLOADER_URL}/upload", json=signed)
            resp.raise_for_status()
            tx_id = resp.json()["txId"]
    except Exception as e:
        print(f"[archive] Irys upload failed for story {story_id}: {e}")
        return None

    arweave_url = f"https://arweave.net/{tx_id}"
    db.table("stories").update({
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "arweave_tx_id": tx_id,
        "arweave_url": arweave_url,
    }).eq("id", story_id).execute()

    print(f"[archive] Story {story_id} archived → {arweave_url}")
    return tx_id

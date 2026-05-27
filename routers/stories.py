import asyncio

from fastapi import APIRouter, HTTPException

from services.db import get_db
from services.llm import generate
from services.threshold import (
    DEFAULT_THRESHOLD,
    compute_effective_threshold,
)
from services.tracker import (
    get_status_map,
    recheck_one_story,
    register_citations,
)

router = APIRouter(prefix="/api")


def _augment_with_status(story: dict, status_by_url: dict) -> dict:
    """citations 배열에 track_status / track_last_checked / deleted_count 머지."""
    citations = story.get("citations") or []
    deleted = 0
    blocked = 0
    for c in citations:
        info = status_by_url.get(c.get("uri"))
        if info:
            c["track_status"] = info["status"]
            c["track_last_checked"] = info["last_checked"]
            c["track_http_code"] = info["http_code"]
            if info["status"] == "deleted":
                deleted += 1
            elif info["status"] == "blocked":
                blocked += 1
        else:
            c["track_status"] = "unchecked"
    story["citations"] = citations
    story["deleted_count"] = deleted
    story["blocked_count"] = blocked
    return story


@router.post("/story")
async def create_story(category: str | None = None):
    if category and category not in ("kindness", "critique"):
        raise HTTPException(400, "category는 kindness 또는 critique만 허용")
    try:
        result = await asyncio.to_thread(generate, category)
    except Exception as e:
        raise HTTPException(500, f"LLM 생성 실패: {e}")

    gap = result.get("gap_data") or {}
    db = get_db()
    resp = db.table("stories").insert({
        "category": result["category"],
        "body": result["body"],
        "citations": result["citations"],
        "search_queries": result["search_queries"],
        "vote_count": 0,
        "gap_score": gap.get("gap_score"),
        "community_count": gap.get("community_count"),
        "news_count": gap.get("news_count"),
    }).execute()
    story_id = resp.data[0]["id"]

    # 새 citation 들을 추적 테이블에 등록 (백그라운드 루프가 곧 검사함)
    await asyncio.to_thread(register_citations, story_id, result["citations"])

    return {
        "story_id": story_id,
        "category": result["category"],
        "text": result["text"],
        "body": result["body"],
        "citations": result["citations"],
        "provider": result["provider"],
        "model": result["model"],
        "gap_score": gap.get("gap_score"),
        "community_count": gap.get("community_count"),
        "news_count": gap.get("news_count"),
    }


@router.get("/stories")
async def list_stories(limit: int = 50):
    db = get_db()
    resp = (
        db.table("stories")
        .select("id,category,body,vote_count,archived_at,arweave_tx_id,arweave_url,"
                "created_at,citations,gap_score,community_count,news_count")
        .order("created_at", desc=True)
        .limit(min(limit, 200))
        .execute()
    )
    stories = resp.data or []
    ids = [s["id"] for s in stories]
    status_map = await asyncio.to_thread(get_status_map, ids)
    for s in stories:
        # 목록에서는 본문 외 citations 자체는 빼고 카운트만 노출
        urls_status = status_map.get(s["id"], {})
        deleted = sum(1 for x in urls_status.values() if x["status"] == "deleted")
        blocked = sum(1 for x in urls_status.values() if x["status"] == "blocked")
        s["deleted_count"] = deleted
        s["blocked_count"] = blocked
        s["citation_count"] = len(s.get("citations") or [])
        s.pop("citations", None)  # 무거우니 목록에서는 제거
        # 동적 임계값
        eff = compute_effective_threshold(s.get("gap_score"), deleted, blocked)
        s["effective_threshold"] = eff["threshold"]
        s["urgency"] = eff["urgency"]
        s["default_threshold"] = DEFAULT_THRESHOLD
    return stories


@router.get("/stories/{story_id}")
async def get_story(story_id: str):
    db = get_db()
    resp = db.table("stories").select("*").eq("id", story_id).limit(1).execute()
    if not resp.data:
        raise HTTPException(404, "스토리를 찾을 수 없습니다")
    story = resp.data[0]

    # 추적 정보 머지
    status_map = await asyncio.to_thread(get_status_map, [story_id])
    by_url = status_map.get(story_id, {})

    # 이 스토리에 추적 레코드가 없으면 (옛 데이터) 즉시 등록
    if not by_url and story.get("citations"):
        await asyncio.to_thread(register_citations, story_id, story["citations"])

    out = _augment_with_status(story, by_url)
    # 동적 임계값 머지
    eff = compute_effective_threshold(
        out.get("gap_score"),
        out.get("deleted_count", 0),
        out.get("blocked_count", 0),
    )
    out["effective_threshold"] = eff["threshold"]
    out["urgency"] = eff["urgency"]
    out["urgency_reason"] = eff["reason"]
    out["default_threshold"] = DEFAULT_THRESHOLD
    return out


@router.post("/recheck/{story_id}")
async def manual_recheck(story_id: str):
    """수동 재검사 트리거. 응답에 새 상태 포함."""
    n = await recheck_one_story(story_id)
    if n == 0:
        # 추적 레코드가 없으면 등록 후 한 번 검사
        db = get_db()
        resp = db.table("stories").select("citations").eq("id", story_id).limit(1).execute()
        if not resp.data:
            raise HTTPException(404, "스토리를 찾을 수 없습니다")
        citations = resp.data[0].get("citations") or []
        if not citations:
            return {"checked": 0}
        await asyncio.to_thread(register_citations, story_id, citations)
        n = await recheck_one_story(story_id)

    status_map = await asyncio.to_thread(get_status_map, [story_id])
    return {"checked": n, "statuses": status_map.get(story_id, {})}

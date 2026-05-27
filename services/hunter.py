"""
자동 사냥꾼 (Hunter) — 일정 간격으로 사용자 조작 없이 새 스토리 자동 생성.

CONTEXT.md 정신: AI 사냥개는 24시간 깨어있어야 사라지는 글을 잡는다.

안전장치:
- AUTO_HUNT_MAX_PENDING: 미박제 글 누적 임계 도달 시 일시 정지 (사람이 따라잡을 때까지)
- 카테고리 교대 회전으로 편향 방지
- 간격에 ±10% 지터로 자연스러운 타이밍
- env 로 끌 수 있음 (AUTO_HUNT_ENABLED=false)
"""

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.db import get_db
from services.llm import generate
from services.tracker import register_citations

HUNTER_ENABLED = os.environ.get("AUTO_HUNT_ENABLED", "true").lower() != "false"
HUNTER_INTERVAL_SEC = int(os.environ.get("AUTO_HUNT_INTERVAL_SEC", "21600"))  # 6시간
HUNTER_MAX_PENDING = int(os.environ.get("AUTO_HUNT_MAX_PENDING", "30"))
HUNTER_CATEGORY_ROTATE = (
    os.environ.get("AUTO_HUNT_CATEGORY_ROTATE", "true").lower() != "false"
)
HUNTER_INITIAL_DELAY_SEC = int(os.environ.get("AUTO_HUNT_INITIAL_DELAY_SEC", "60"))

# 모듈 상태
_next_hunt_at: Optional[datetime] = None
_last_category: Optional[str] = None
_last_hunt_at: Optional[datetime] = None
_last_result: Optional[dict] = None


def get_status() -> dict:
    """대시보드용 사냥꾼 상태 스냅샷."""
    return {
        "enabled": HUNTER_ENABLED,
        "interval_sec": HUNTER_INTERVAL_SEC,
        "max_pending": HUNTER_MAX_PENDING,
        "next_hunt_at": _next_hunt_at.isoformat() if _next_hunt_at else None,
        "last_hunt_at": _last_hunt_at.isoformat() if _last_hunt_at else None,
        "last_result": _last_result,
    }


def count_recent_pending() -> int:
    """최근 7일 내 미박제 글 수 (안전장치 판단)."""
    try:
        db = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        resp = (
            db.table("stories")
            .select("id", count="exact")
            .is_("arweave_tx_id", "null")
            .gte("created_at", cutoff)
            .execute()
        )
        return resp.count or 0
    except Exception as e:
        print(f"[hunter] pending count failed: {e}")
        return 0


def pick_category() -> str:
    """카테고리 교대 또는 무작위."""
    global _last_category
    if not HUNTER_CATEGORY_ROTATE:
        return random.choice(["kindness", "critique"])
    if _last_category == "kindness":
        choice = "critique"
    elif _last_category == "critique":
        choice = "kindness"
    else:
        choice = random.choice(["kindness", "critique"])
    _last_category = choice
    return choice


async def hunt_once() -> dict:
    """한 사이클 사냥. 결과 dict 반환 (DB 저장까지)."""
    global _last_hunt_at, _last_result

    pending = await asyncio.to_thread(count_recent_pending)
    if pending >= HUNTER_MAX_PENDING:
        reason = f"미박제 {pending}건 누적 (한계 {HUNTER_MAX_PENDING})"
        print(f"[hunter] skipped: {reason}")
        result = {
            "skipped": True,
            "reason": reason,
            "pending": pending,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _last_result = result
        return result

    category = pick_category()
    print(f"[hunter] 사냥 시작: {category}")

    try:
        gen_result = await asyncio.to_thread(generate, category)
    except Exception as e:
        msg = f"LLM 생성 실패: {e}"
        print(f"[hunter] {msg}")
        result = {"error": msg, "at": datetime.now(timezone.utc).isoformat()}
        _last_result = result
        return result

    gap = gen_result.get("gap_data") or {}
    try:
        db = get_db()
        resp = db.table("stories").insert({
            "category": gen_result["category"],
            "body": gen_result["body"],
            "citations": gen_result["citations"],
            "search_queries": gen_result["search_queries"],
            "vote_count": 0,
            "gap_score": gap.get("gap_score"),
            "community_count": gap.get("community_count"),
            "news_count": gap.get("news_count"),
        }).execute()
        story_id = resp.data[0]["id"]

        await asyncio.to_thread(register_citations, story_id, gen_result["citations"])
        _last_hunt_at = datetime.now(timezone.utc)

        result = {
            "story_id": story_id,
            "category": gen_result["category"],
            "gap_score": gap.get("gap_score"),
            "citation_count": len(gen_result["citations"]),
            "at": _last_hunt_at.isoformat(),
        }
        _last_result = result
        print(f"[hunter] 새 글 {story_id[:8]} category={category} gap={gap.get('gap_score')}")
        return result
    except Exception as e:
        msg = f"DB insert 실패: {e}"
        print(f"[hunter] {msg}")
        result = {"error": msg, "at": datetime.now(timezone.utc).isoformat()}
        _last_result = result
        return result


async def background_loop() -> None:
    """앱 lifespan 동안 도는 자동 사냥 루프."""
    global _next_hunt_at

    if not HUNTER_ENABLED:
        print("[hunter] 비활성화 (AUTO_HUNT_ENABLED=false)")
        return

    print(
        f"[hunter] 시작 · interval={HUNTER_INTERVAL_SEC}s · "
        f"max_pending={HUNTER_MAX_PENDING} · rotate={HUNTER_CATEGORY_ROTATE}"
    )

    # 시작 시 약간 지연 (앱 부팅 안정화 + tracker 와 시간 분산)
    _next_hunt_at = datetime.now(timezone.utc) + timedelta(seconds=HUNTER_INITIAL_DELAY_SEC)
    try:
        await asyncio.sleep(HUNTER_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        return

    while True:
        try:
            await hunt_once()
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[hunter] loop error: {e}")

        # 다음 사냥 예약 (±10% 지터)
        jitter = random.uniform(0.9, 1.1)
        next_delay = max(60, int(HUNTER_INTERVAL_SEC * jitter))
        _next_hunt_at = datetime.now(timezone.utc) + timedelta(seconds=next_delay)

        try:
            await asyncio.sleep(next_delay)
        except asyncio.CancelledError:
            return

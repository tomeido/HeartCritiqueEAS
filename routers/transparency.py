"""투명성·검증 API.

  GET /api/transparency        → 지금 적용 중인 정책·가중치·게이트의 실시간 스냅샷
  GET /api/verify/{story_id}   → 박제 번들 서명 서버측 검증 + 현재 DB 본문 대조

'우릴 믿지 말고 검증하라': 스냅샷은 문서(docs/TRANSPARENCY.md)의 약속과 대조할 수 있는
현재 상태이고, verify 는 서버 없이도 재현 가능한 검증(canonical JSON + ECDSA)의 편의
래퍼다. 로직은 services/transparency.py — 라우터는 HTTP 매핑만 한다.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from services.ratelimit import check_recheck_ratelimit
from services.transparency import build_snapshot, verify_story_archive

router = APIRouter(prefix="/api")


@router.get("/transparency")
async def transparency():
    return await asyncio.to_thread(build_snapshot)


@router.get("/verify/{story_id}")
async def verify_archive(story_id: str, request: Request):
    # 온디맨드 외부 GET 을 유발하므로 재검사와 동일한 per-IP 제한을 공유한다.
    allowed, retry_after, reason = check_recheck_ratelimit(request)
    if not allowed:
        raise HTTPException(
            429, f"{reason}. {retry_after}초 후 다시 시도하세요.",
            headers={"Retry-After": str(retry_after)},
        )
    res = await verify_story_archive(story_id)
    if res.get("reason") == "not_found":
        raise HTTPException(404, "스토리를 찾을 수 없습니다")
    if res.get("reason") == "not_archived":
        raise HTTPException(409, "아직 박제되지 않은 스토리입니다")
    if res.get("reason") == "db_error":
        raise HTTPException(503, "일시적 조회 실패 — 잠시 후 다시 시도하세요")
    return res

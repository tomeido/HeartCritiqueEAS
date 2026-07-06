"""게스트 인증 라우터 — Supabase 미설정(로컬 SQLite 모드) 전용.

POST /api/auth/guest 는 HMAC 서명된 게스트 토큰을 발급한다. 프론트엔드는
/api/config 의 auth_mode == "guest" 일 때 이 엔드포인트로 신원을 만들어
localStorage 에 보관하고, 투표 요청의 Bearer 토큰으로 사용한다.

Supabase 모드에서는 게스트 신원이 OAuth 투표 무결성을 희석하지 않도록
엔드포인트를 숨긴다(404).
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from services.db import is_local_mode
from services.ratelimit import check_guest_ratelimit

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.post("/auth/guest")
async def create_guest_identity(request: Request):
    if not is_local_mode():
        raise HTTPException(404, "Not Found")
    allowed, retry_after, reason = check_guest_ratelimit(request)
    if not allowed:
        raise HTTPException(
            429, f"{reason}. {retry_after}초 후 다시 시도하세요.",
            headers={"Retry-After": str(retry_after)},
        )
    from services.localauth import issue_guest_token
    token, user_id = issue_guest_token()
    return {"token": token, "user_id": user_id, "auth_mode": "guest"}

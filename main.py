"""
Heart & Critique - FastAPI 메인 앱 (Docker 홈서버용)

엔드포인트:
  GET  /                              → 프론트엔드 HTML
  GET  /.well-known/agent-card.json   → A2A 에이전트 카드
  POST /                              → JSON-RPC 2.0 (A2A 호환: message/send)
  GET  /api/config                    → 공개 설정 (Supabase URL/anon key)
  POST /api/story                     → 스토리 생성 + Supabase 저장
  POST /api/vote/{story_id}           → 공론화 찬성 투표 (JWT 필요)
  GET  /api/vote/{story_id}/status    → 투표 현황
  GET  /api/stories                   → 최근 스토리 목록
  GET  /api/stories/{story_id}        → 스토리 상세
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from routers import feed, stats, stories, votes  # noqa: E402
from services.llm import LLM_PROVIDER, GEMINI_MODEL, GROQ_MODEL, generate  # noqa: E402
from services.threshold import DEFAULT_THRESHOLD  # noqa: E402
from services.tracker import TRACKER_ENABLED, background_loop as tracker_loop  # noqa: E402
from services.hunter import HUNTER_ENABLED, background_loop as hunter_loop  # noqa: E402
from services.cleanup import CLEANUP_ENABLED, background_loop as cleanup_loop  # noqa: E402

import logging  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _on_task_done(task: asyncio.Task) -> None:
    """백그라운드 루프가 예외로 죽으면 (조용히 좀비가 되지 않도록) 로깅."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(f"[lifespan] ⚠️ 백그라운드 태스크 '{task.get_name()}' 비정상 종료: {exc!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task] = []
    has_supabase = bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
    if has_supabase and TRACKER_ENABLED:
        tasks.append(asyncio.create_task(tracker_loop(), name="tracker"))
    if has_supabase and HUNTER_ENABLED:
        tasks.append(asyncio.create_task(hunter_loop(), name="hunter"))
    if has_supabase and CLEANUP_ENABLED:
        tasks.append(asyncio.create_task(cleanup_loop(), name="cleanup"))
    for t in tasks:
        t.add_done_callback(_on_task_done)
    app.state.bg_tasks = tasks
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Heart & Critique", version="6.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-PAYMENT-RESPONSE"],
)

app.include_router(stories.router)
app.include_router(votes.router)
app.include_router(stats.router)
app.include_router(feed.router)


@app.get("/health")
async def health(request: Request):
    """헬스체크 + 백그라운드 루프 생존 상태. compose healthcheck 가 사용."""
    tasks = getattr(request.app.state, "bg_tasks", [])
    bg: dict[str, str] = {}
    all_alive = True
    for t in tasks:
        if t.done():
            all_alive = False
            if t.cancelled():
                bg[t.get_name()] = "cancelled"
            else:
                exc = t.exception()
                bg[t.get_name()] = f"dead:{type(exc).__name__}" if exc else "finished"
        else:
            bg[t.get_name()] = "running"
    body = {"ok": all_alive, "background_alive": all_alive, "background": bg}
    # 백그라운드 루프가 죽었으면 503 → compose 가 컨테이너를 unhealthy 로 표시
    return JSONResponse(body, status_code=200 if all_alive else 503)


@app.get("/api/config")
async def get_config():
    from services.crypto import get_public_key_hex, has_configured_key
    try:
        pubkey = get_public_key_hex()
    except Exception:
        pubkey = ""
    return {
        "supabase_url": os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        "vote_threshold": DEFAULT_THRESHOLD,
        "llm_provider": LLM_PROVIDER,
        "agent_public_key": pubkey,
        # 고정 서명키 없음 = 재시작마다 키가 바뀜 → 박제물 검증 신뢰 불가
        "ephemeral_signing_key": not has_configured_key(),
        "irys_network": os.environ.get("IRYS_NETWORK", "devnet"),
    }


# ── A2A 호환 JSON-RPC 엔드포인트 ───────────────────────────────────────────
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _public_url(request: Request) -> str:
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    host = forwarded_host or str(request.base_url.netloc)
    proto = forwarded_proto or request.base_url.scheme
    return f"{proto}://{host}/"


def _build_agent_card(public_url: str) -> dict:
    provider_desc = (
        f"Tavily 뉴스 검색 + {GROQ_MODEL}" if LLM_PROVIDER == "groq"
        else f"{GEMINI_MODEL} + Google Search grounding"
    )
    return {
        "name": "Heart & Critique",
        "description": (
            f"50% 확률로 따뜻한 선행 또는 대기업 비위 사건을 전달하는 A2A 에이전트. "
            f"{provider_desc}. 이야기 본문 무료, 인간 투표({DEFAULT_THRESHOLD}표)로 Arweave 박제."
        ),
        "version": "6.0.0",
        "protocolVersion": "0.2.5",
        "url": public_url,
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "story",
                "name": "Kindness or Critique Story",
                "description": "무료 이야기 생성 (message/send)",
                "tags": ["kindness", "critique", "news", "korean", LLM_PROVIDER, "free"],
                "examples": ["오늘의 이야기", "하나 들려줘"],
            }
        ],
    }


@app.get("/.well-known/agent-card.json")
@app.get("/.well-known/agent.json")
async def agent_card(request: Request):
    return _build_agent_card(_public_url(request))


@app.post("/")
async def jsonrpc_handler(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    # 유효 JSON 이지만 객체가 아닌 경우([], "x", 5, true)에 body.get(...) 가 500 나지 않게.
    if not isinstance(body, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32600, "message": "Invalid Request"}},
            status_code=400,
        )

    rpc_id = body.get("id")
    method = body.get("method", "")

    if method == "agent/getCard":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": _build_agent_card(_public_url(request))}

    if method in ("message/send", "tasks/send"):
        # 레이트리밋: A2A 경로도 동일하게 보호 (무인증 LLM 호출 비용 폭탄 방어)
        from services.ratelimit import check_story_ratelimit
        allowed, retry_after, reason = check_story_ratelimit(request)
        if not allowed:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rpc_id,
                 "error": {"code": -32029,
                           "message": f"Rate limited: {reason}. retry after {retry_after}s"}},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        try:
            result = await asyncio.to_thread(generate)
        except Exception as e:
            task_id = str(uuid.uuid4())
            return {
                "jsonrpc": "2.0", "id": rpc_id,
                "result": {
                    "kind": "task", "id": task_id,
                    "status": {"state": "failed", "timestamp": _now_iso(),
                               "message": {"kind": "message", "role": "agent",
                                           "parts": [{"kind": "text", "text": str(e)}]}},
                    "history": [], "artifacts": [],
                },
            }

        # 적합성 게이트: 적합 글 미발견(no_fit)이면 빈 text 를 'completed'로 위장하지 않고
        # 사람이 읽을 메시지로 알린다(HTTP /api/story 의 503·hunter 스킵과 정합).
        if result.get("no_fit") or not (result.get("text") or "").strip():
            task_id = str(uuid.uuid4())
            return {
                "jsonrpc": "2.0", "id": rpc_id,
                "result": {
                    "kind": "task", "id": task_id,
                    "status": {"state": "completed", "timestamp": _now_iso(),
                               "message": {"kind": "message", "role": "agent",
                                           "parts": [{"kind": "text",
                                                      "text": "지금은 들려드릴 적합한 이야기를 찾지 못했어요. 잠시 후 다시 시도해 주세요."}]}},
                    "history": [], "artifacts": [],
                },
            }

        task_id = str(uuid.uuid4())
        ctx_id = str(uuid.uuid4())
        return {
            "jsonrpc": "2.0", "id": rpc_id,
            "result": {
                "kind": "task", "id": task_id, "contextId": ctx_id,
                "status": {"state": "completed", "timestamp": _now_iso()},
                "history": [
                    {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
                     "parts": [{"kind": "text", "text": result["text"]}],
                     "taskId": task_id, "contextId": ctx_id},
                ],
                "artifacts": [
                    {"artifactId": str(uuid.uuid4()), "name": "reply",
                     "parts": [{"kind": "text", "text": result["text"]},
                                {"kind": "data", "data": {
                                    "category": result["category"],
                                    "citations": result["citations"],
                                    "provider": result["provider"],
                                    "model": result["model"],
                                }}],
                     "metadata": {"category": result["category"]}},
                ],
            },
        }

    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id,
         "error": {"code": -32601, "message": f"Method not found: {method}"}},
    )


# ── 프론트엔드 서빙 ────────────────────────────────────────────────────────
@app.get("/")
async def frontend():
    return FileResponse("static/index.html")

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
from services.tracker import TRACKER_ENABLED, background_loop as tracker_loop  # noqa: E402
from services.hunter import HUNTER_ENABLED, background_loop as hunter_loop  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task] = []
    has_supabase = bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
    if has_supabase and TRACKER_ENABLED:
        tasks.append(asyncio.create_task(tracker_loop(), name="tracker"))
    if has_supabase and HUNTER_ENABLED:
        tasks.append(asyncio.create_task(hunter_loop(), name="hunter"))
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


@app.get("/api/config")
async def get_config():
    from services.crypto import get_public_key_hex
    try:
        pubkey = get_public_key_hex()
    except Exception:
        pubkey = ""
    return {
        "supabase_url": os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        "vote_threshold": int(os.environ.get("VOTE_THRESHOLD", "10")),
        "llm_provider": LLM_PROVIDER,
        "agent_public_key": pubkey,
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
            f"{provider_desc}. 이야기 본문 무료, 인간 투표({os.environ.get('VOTE_THRESHOLD','10')}표)로 Arweave 영구 박제."
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

    rpc_id = body.get("id")
    method = body.get("method", "")

    if method == "agent/getCard":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": _build_agent_card(_public_url(request))}

    if method in ("message/send", "tasks/send"):
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

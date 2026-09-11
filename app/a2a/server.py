"""FastAPI routes for A2A discovery, JSON-RPC and Witness REST reads."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.a2a.agent_card import build_agent_card
from app.a2a.handlers import A2AError, A2AHandlers
from app.chronicle.narrator import daily_chronicle
from app.db.repository import get_repository
from app.memory.engine import MemoryEngine

router = APIRouter()

METHODS = {
    "SendMessage": "send",
    "message/send": "send",
    "tasks/send": "send",
    "SendStreamingMessage": "stream",
    "message/stream": "stream",
    "GetTask": "get",
    "tasks/get": "get",
    "CancelTask": "cancel",
    "tasks/cancel": "cancel",
}


def _public_origin(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


def _error(rpc_id: Any, exc: A2AError) -> dict[str, Any]:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.reason:
        error["data"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": exc.reason,
                "domain": "a2a-protocol.org",
                "metadata": exc.metadata,
            }
        ]
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def _dispatch(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    rpc_id = body.get("id")
    if body.get("jsonrpc") != "2.0" or "id" not in body or not isinstance(body.get("method"), str):
        raise A2AError(-32600, "Request payload validation error")
    operation = METHODS.get(body["method"])
    if not operation:
        raise A2AError(-32601, "Method not found")
    params = body.get("params") or {}
    handlers = A2AHandlers()
    if operation in {"send", "stream"}:
        result = handlers.send_message(params)
    elif operation == "get":
        result = handlers.get_task(params)
    else:
        result = handlers.cancel_task(params)
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}, operation == "stream"


@router.get("/.well-known/agent.json")
async def witness_agent_card(request: Request):
    return build_agent_card(f"{_public_origin(request)}/a2a")


@router.post("/a2a")
async def witness_jsonrpc(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON payload"}},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request payload validation error"}},
            status_code=400,
        )
    try:
        response, streaming = _dispatch(body)
    except A2AError as exc:
        return JSONResponse(_error(body.get("id"), exc), status_code=400)
    except (ValidationError, ValueError) as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32602, "message": str(exc)}},
            status_code=400,
        )
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32603, "message": "Internal error"}},
            status_code=500,
        )
    if streaming:
        async def one_event():
            yield f"data: {json.dumps(response, ensure_ascii=False)}\n\n"
        return StreamingResponse(one_event(), media_type="text/event-stream")
    return JSONResponse(response, media_type="application/json")


@router.post("/api/events", status_code=201)
async def ingest_event(payload: dict[str, Any]):
    try:
        return MemoryEngine().ingest(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@router.get("/api/events")
async def list_events(limit: int = 100):
    return [event for event in get_repository().list_events(limit) if event["visibility"] == "public"]


@router.get("/api/memories")
async def list_memories(agent_id: str | None = None, limit: int = 100):
    memories = get_repository().list_memories(participant_id=agent_id, limit=limit)
    return [memory for memory in memories if memory["visibility"] == "public"]


@router.get("/api/memories/{memory_id}")
async def get_memory(memory_id: str):
    memory = get_repository().get_memory(memory_id)
    if not memory or memory["visibility"] != "public":
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.get("/api/chronicle")
@router.get("/api/chronicle/today")
async def get_chronicle(day: date | None = None):
    return daily_chronicle(day, public_only=True)

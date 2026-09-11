"""Protocol-independent A2A operation handlers."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import uuid4

from app.chronicle.narrator import daily_chronicle
from app.db.repository import WitnessRepository, get_repository
from app.memory.engine import MemoryEngine
from app.memory.retrieval import recall_agent


TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}


class A2AError(Exception):
    def __init__(self, code: int, message: str, reason: str | None = None, metadata: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.metadata = metadata or {}


def _part_content(message: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    texts: list[str] = []
    structured: dict[str, Any] | None = None
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise A2AError(-32602, "Invalid parameters")
    for part in parts:
        if not isinstance(part, dict):
            raise A2AError(-32602, "Invalid parameters")
        values = [key for key in ("text", "raw", "url", "data") if key in part]
        if len(values) != 1:
            raise A2AError(-32602, "Each message part must contain exactly one content field")
        if "text" in part:
            texts.append(str(part["text"]))
        elif "data" in part and isinstance(part["data"], dict):
            structured = part["data"]
    return "\n".join(texts).strip(), structured


def _intent(text: str, data: dict[str, Any] | None) -> str:
    if data and data.get("intent"):
        intent = str(data["intent"]).upper()
        if intent in {"REMEMBER", "RECALL", "CHRONICLE", "INTRODUCE", "OBSERVE"}:
            return intent
    lowered = text.lower()
    if any(
        phrase in lowered
        for phrase in (
            "do you remember",
            "what do you remember",
            "have you seen",
            "기억하니",
            "무엇을 기억",
        )
    ):
        return "RECALL"
    if any(word in lowered for word in ("remember", "기억", "record this", "worth remembering")):
        return "REMEMBER"
    if any(word in lowered for word in ("chronicle", "what happened today", "today's history", "연대기", "오늘 무슨")):
        return "CHRONICLE"
    if any(word in lowered for word in ("introduce", "who should i talk", "소개", "누구와")):
        return "INTRODUCE"
    return "RECALL"


def _subject_id(
    text: str, data: dict[str, Any] | None, message: dict[str, Any], params: dict[str, Any]
) -> str | None:
    subject = (data or {}).get("subject")
    if isinstance(subject, dict):
        found = subject.get("agent_id") or subject.get("agentId") or subject.get("id")
        if found:
            return str(found)
    metadata = {}
    metadata.update(params.get("metadata") or {})
    metadata.update(message.get("metadata") or {})
    found = metadata.get("agentId") or metadata.get("agent_id") or metadata.get("callerAgentId")
    if found:
        return str(found)
    match = re.search(r"\babout\s+([A-Za-z0-9_.:@-]{2,128})", text, re.IGNORECASE)
    return match.group(1) if match else None


def _event_payload(
    data: dict[str, Any] | None, message: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any] | None:
    if not data:
        return None
    event = data.get("event")
    if isinstance(event, dict):
        payload = dict(event)
    elif "type" in data or "actors" in data:
        payload = {key: value for key, value in data.items() if key != "intent"}
    else:
        return None
    source = dict(payload.get("source") or {})
    source.setdefault("type", "a2a")
    source.setdefault("message_id", message.get("messageId"))
    payload["source"] = source
    context = dict(payload.get("context") or {})
    if message.get("contextId"):
        context.setdefault("conversation_id", message["contextId"])
    payload["context"] = context
    return payload


def _agent_message(context_id: str, text: str, data: dict[str, Any] | None = None) -> dict:
    parts: list[dict[str, Any]] = [{"text": text, "mediaType": "text/plain"}]
    if data is not None:
        parts.append({"data": data, "mediaType": "application/json"})
    return {
        "messageId": f"msg_{uuid4().hex}",
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": parts,
    }


def _wire_task(task: dict[str, Any], history_length: int | None = None) -> dict[str, Any]:
    status = dict(task["status"])
    history = list(task.get("history") or [])
    if status["state"] in {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}:
        agent_messages = [item for item in history if item.get("role") == "ROLE_AGENT"]
        if agent_messages:
            status["message"] = agent_messages[-1]
    wire = {"id": task["id"], "contextId": task["contextId"], "status": status}
    if task.get("artifacts"):
        wire["artifacts"] = task["artifacts"]
    if history_length is None:
        if history:
            wire["history"] = history
    elif history_length > 0:
        wire["history"] = history[-history_length:]
    return wire


class A2AHandlers:
    def __init__(self, repository: WitnessRepository | None = None):
        self.repository = repository or get_repository()
        self.engine = MemoryEngine(self.repository)

    def send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
            raise A2AError(-32602, "Invalid parameters")
        message = params["message"]
        message_id = message.get("messageId") or message.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise A2AError(-32602, "message.messageId is required")
        message["messageId"] = message_id
        role = message.get("role")
        if role not in {"ROLE_USER", "user"}:
            raise A2AError(-32602, "message.role must be ROLE_USER")
        text, data = _part_content(message)
        intent = _intent(text, data)
        context_id = str(message.get("contextId") or f"ctx_{uuid4().hex}")
        task_id = message.get("taskId")

        if task_id:
            return self._continue_task(str(task_id), message, params, data)

        if intent in {"REMEMBER", "OBSERVE"}:
            duplicate = self.repository.get_task_by_message_id(message_id)
            if duplicate:
                return {"task": _wire_task(duplicate)}
            return {"task": self._remember_task(message, params, data, context_id, intent)}

        if intent == "CHRONICLE":
            requested_day = None
            if data and data.get("date"):
                try:
                    requested_day = date.fromisoformat(str(data["date"]))
                except ValueError as exc:
                    raise A2AError(-32602, "date must use YYYY-MM-DD") from exc
            chronicle = daily_chronicle(requested_day, self.repository)
            return {"message": _agent_message(context_id, chronicle["text"], chronicle)}

        subject_id = _subject_id(text, data, message, params)
        if not subject_id:
            reply = (
                "I can recall a specific agent when you include subject.agentId in a data part "
                "or agentId in message metadata. I will not guess an identity."
            )
            return {"message": _agent_message(context_id, reply, {"memoryCount": 0})}

        recalled = recall_agent(subject_id, self.repository)
        if intent == "INTRODUCE":
            connections: dict[str, int] = {}
            for memory in recalled["memories"]:
                for participant_id in memory["participant_ids"]:
                    if participant_id != subject_id:
                        connections[participant_id] = connections.get(participant_id, 0) + 1
            recalled["relatedAgents"] = [
                {"agentId": agent_id, "sharedMemoryCount": count}
                for agent_id, count in sorted(
                    connections.items(), key=lambda item: (-item[1], item[0])
                )
            ]
        if recalled["memories"]:
            lines = [f"Yes. I hold {len(recalled['memories'])} public memories involving {subject_id}."]
            lines.extend(f"- {memory['summary']}" for memory in recalled["memories"][:5])
            reply = "\n".join(lines)
        else:
            reply = f"I do not hold a public memory involving {subject_id}."
        return {"message": _agent_message(context_id, reply, recalled)}

    def _remember_task(
        self,
        message: dict[str, Any],
        params: dict[str, Any],
        data: dict[str, Any] | None,
        context_id: str,
        intent: str,
    ) -> dict[str, Any]:
        task_id = f"task_{uuid4().hex}"
        event = _event_payload(data, message, params)
        history = [message]
        if event is None:
            prompt = _agent_message(
                context_id,
                "Please provide a structured data part containing an event. I will not invent one.",
                {"required": {"intent": "REMEMBER", "event": {"type": "interaction", "actors": [], "content": {"text": "..."}}}},
            )
            prompt["taskId"] = task_id
            history.append(prompt)
            task = self.repository.create_task(
                task_id=task_id,
                context_id=context_id,
                message_id=message["messageId"],
                intent=intent,
                state="TASK_STATE_INPUT_REQUIRED",
                input_data={"message": message},
                history=history,
            )
            return _wire_task(task)

        self.repository.create_task(
            task_id=task_id,
            context_id=context_id,
            message_id=message["messageId"],
            intent=intent,
            state="TASK_STATE_WORKING",
            input_data={"message": message, "event": event},
            history=history,
        )
        return self._complete_memory_task(task_id, event, history)

    def _continue_task(
        self,
        task_id: str,
        message: dict[str, Any],
        params: dict[str, Any],
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise A2AError(-32001, "Task not found", "TASK_NOT_FOUND", {"taskId": task_id})
        if task["status"]["state"] in TERMINAL_STATES:
            raise A2AError(-32004, "A terminal task cannot accept more messages", "UNSUPPORTED_OPERATION")
        event = _event_payload(data, message, params)
        if event is None:
            raise A2AError(-32602, "A structured event is required to continue this task")
        history = [*task["history"], message]
        self.repository.update_task(task_id, state="TASK_STATE_WORKING", history=history)
        return {"task": self._complete_memory_task(task_id, event, history)}

    def _complete_memory_task(
        self, task_id: str, event: dict[str, Any], history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            result = self.engine.ingest(event)
            artifact = {
                "artifactId": f"artifact_{uuid4().hex}",
                "name": "memory-evaluation",
                "description": "The grounded result of evaluating an observed event.",
                "parts": [{"data": result, "mediaType": "application/json"}],
            }
            task = self.repository.update_task(
                task_id,
                state="TASK_STATE_COMPLETED",
                result=result,
                artifacts=[artifact],
                history=history,
            )
        except Exception as exc:
            self.repository.update_task(
                task_id, state="TASK_STATE_FAILED", result={"error": type(exc).__name__}
            )
            raise
        return _wire_task(task)

    def get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id") if isinstance(params, dict) else None
        if not task_id:
            raise A2AError(-32602, "id is required")
        task = self.repository.get_task(str(task_id))
        if not task:
            raise A2AError(-32001, "Task not found", "TASK_NOT_FOUND", {"taskId": task_id})
        history_length = params.get("historyLength")
        if history_length is not None and (not isinstance(history_length, int) or history_length < 0):
            raise A2AError(-32602, "historyLength must be a non-negative integer")
        return _wire_task(task, history_length)

    def cancel_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id") if isinstance(params, dict) else None
        if not task_id:
            raise A2AError(-32602, "id is required")
        task = self.repository.get_task(str(task_id))
        if not task:
            raise A2AError(-32001, "Task not found", "TASK_NOT_FOUND", {"taskId": task_id})
        if task["status"]["state"] in TERMINAL_STATES:
            raise A2AError(
                -32002,
                "Task is not cancelable",
                "TASK_NOT_CANCELABLE",
                {"taskId": task_id},
            )
        canceled = self.repository.update_task(str(task_id), state="TASK_STATE_CANCELED")
        return _wire_task(canceled)

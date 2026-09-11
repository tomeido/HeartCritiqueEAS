"""Memory recall helpers."""

from __future__ import annotations

from typing import Any

from app.db.repository import WitnessRepository, get_repository


def recall_agent(
    agent_id: str,
    repository: WitnessRepository | None = None,
    limit: int = 10,
    public_only: bool = True,
) -> dict[str, Any]:
    repo = repository or get_repository()
    memories = repo.list_memories(participant_id=agent_id, limit=limit)
    if public_only:
        memories = [memory for memory in memories if memory["visibility"] == "public"]
    return {
        "agentId": agent_id,
        "memoryCount": len(memories),
        "firstSeen": memories[-1]["occurred_at"] if memories else None,
        "lastSeen": memories[0]["occurred_at"] if memories else None,
        "memories": memories,
    }

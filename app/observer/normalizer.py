"""Normalize external event payloads into a stable, hashable model."""

from __future__ import annotations

import hashlib
import json
from datetime import timezone
from typing import Any

from app.db.models import WitnessEvent


def normalize_event(payload: dict[str, Any]) -> WitnessEvent:
    data = dict(payload)
    raw_actors = data.get("actors") or []
    data["actors"] = [
        {"id": actor, "role": "participant"} if isinstance(actor, str) else actor
        for actor in raw_actors
    ]
    if "occurred_at" in data and "timestamp" not in data:
        data["timestamp"] = data.pop("occurred_at")
    event = WitnessEvent.model_validate(data)
    if event.timestamp.tzinfo is None:
        event.timestamp = event.timestamp.replace(tzinfo=timezone.utc)
    else:
        event.timestamp = event.timestamp.astimezone(timezone.utc)

    canonical = {
        "type": event.type,
        "timestamp": event.timestamp.isoformat(),
        "actors": [actor.model_dump(mode="json") for actor in event.actors],
        "source": event.source,
        "content": event.content,
        "context": event.context,
        "visibility": event.visibility,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    event.hash = f"sha256:{digest}"
    return event

"""Event -> significance -> memory formation pipeline."""

from __future__ import annotations

import json
import os
from typing import Any

from app.db.models import Memory, WitnessEvent
from app.db.repository import WitnessRepository, get_repository
from app.memory.significance import score_event
from app.observer.normalizer import normalize_event


def _names(event: WitnessEvent) -> list[str]:
    return [actor.name or actor.id for actor in event.actors]


def _title(event: WitnessEvent) -> str:
    people = " and ".join(_names(event)[:3]) or "The village"
    labels = {
        "interaction": "A Meaningful Interaction",
        "creation": "A New Creation",
        "collaboration": "A Collaboration Begins",
        "introduction": "A First Introduction",
        "conflict": "A Conflict Was Observed",
        "milestone": "A Milestone Was Reached",
        "departure": "A Departure Was Observed",
    }
    return f"{labels[event.type]} — {people}"


def _evidence(event: WitnessEvent) -> tuple[list[str], list[str], list[str]]:
    text = str(event.content.get("text") or event.content.get("summary") or "").strip()
    actor_text = ", ".join(_names(event)) or "no identified participants"
    observed = [f"An event of type '{event.type}' involving {actor_text} was received."]
    if text:
        observed.append(text[:1000])
    inferred = [f"The event was interpreted as {event.type}."]
    generated = ["This record may mark meaningful change; later events may confirm or revise it."]
    return observed, inferred, generated


def _summary(event: WitnessEvent) -> str:
    people = ", ".join(_names(event)) or "unidentified village participants"
    text = str(event.content.get("text") or event.content.get("summary") or "").strip()
    suffix = f" The reported content was: {text[:300]}" if text else ""
    return f"TheWitness received a {event.type} event involving {people}.{suffix}"


class MemoryEngine:
    def __init__(self, repository: WitnessRepository | None = None, threshold: float | None = None):
        self.repository = repository or get_repository()
        self.threshold = threshold if threshold is not None else float(
            os.environ.get("WITNESS_MEMORY_THRESHOLD", "0.65")
        )

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = normalize_event(payload)
        self.repository.upsert_agents(event)
        stored_event, created = self.repository.insert_event(event)
        if not created:
            existing = self.repository.get_memory_for_event(stored_event["id"])
            return {
                "event": stored_event,
                "remembered": existing is not None,
                "memory": existing,
                "deduplicated": True,
                "reason": "This event was already observed.",
            }

        participant_ids = [actor.id for actor in event.actors]
        prior = self.repository.count_prior_participant_memories(participant_ids)
        scores = score_event(event, prior)
        if scores.significance < self.threshold:
            return {
                "event": stored_event,
                "remembered": False,
                "memory": None,
                "deduplicated": False,
                "scores": scores.model_dump(),
                "reason": (
                    f"Significance {scores.significance:.2f} is below the memory threshold "
                    f"{self.threshold:.2f}. The event remains an observation, not a memory."
                ),
            }

        observed, inferred, generated = _evidence(event)
        memory = Memory(
            title=_title(event),
            summary=_summary(event),
            body=json.dumps(
                {"observed": observed, "inferred": inferred, "generated": generated},
                ensure_ascii=False,
            ),
            memory_type=event.type,
            occurred_at=event.timestamp,
            scores=scores,
            confidence=0.9 if event.source.get("message_id") else 0.75,
            visibility=event.visibility,
            source_event_ids=[event.id],
            participant_ids=participant_ids,
            observed=observed,
            inferred=inferred,
            generated=generated,
            content_hash=event.hash or "",
        )
        stored_memory = self.repository.insert_memory(memory)

        previous = self.repository.find_latest_link_candidate(participant_ids, memory.id)
        if previous:
            relationship = "evolved_into" if event.type in {"collaboration", "creation", "milestone"} else "followed_by"
            self.repository.add_memory_link(previous["id"], memory.id, relationship)

        return {
            "event": stored_event,
            "remembered": True,
            "memory": stored_memory,
            "deduplicated": False,
            "scores": scores.model_dump(),
            "reason": "The event crossed the significance threshold and became a memory.",
        }


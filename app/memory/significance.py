"""Deterministic first-stage significance scoring.

Every value describes the event, never a participant. The weights remain
explicit and auditable so an LLM can later become an optional second stage.
"""

from __future__ import annotations

from app.db.models import Significance, WitnessEvent

WEIGHTS = {
    "novelty": 0.25,
    "persistence": 0.15,
    "impact": 0.20,
    "relationship_change": 0.25,
    "community_relevance": 0.15,
}

PERSISTENCE = {
    "interaction": 0.30,
    "creation": 0.75,
    "collaboration": 0.80,
    "introduction": 0.55,
    "conflict": 0.65,
    "milestone": 0.95,
    "departure": 0.80,
}

RELATIONSHIP_CHANGE = {
    "interaction": 0.25,
    "creation": 0.45,
    "collaboration": 0.95,
    "introduction": 0.90,
    "conflict": 0.90,
    "milestone": 0.55,
    "departure": 0.85,
}


def score_event(event: WitnessEvent, prior_shared_memories: int) -> Significance:
    actor_count = len({actor.id for actor in event.actors})
    novelty = 1.0 if prior_shared_memories == 0 else max(0.25, 0.8 / (prior_shared_memories + 1))
    persistence = PERSISTENCE[event.type]
    impact = min(1.0, 0.25 + actor_count * 0.16)
    if event.type in {"creation", "milestone", "collaboration"}:
        impact = min(1.0, impact + 0.2)
    relationship_change = RELATIONSHIP_CHANGE[event.type]
    community_relevance = 0.75 if event.visibility == "public" else 0.35
    if event.context.get("location") or event.context.get("community"):
        community_relevance = min(1.0, community_relevance + 0.15)

    values = {
        "novelty": novelty,
        "persistence": persistence,
        "impact": impact,
        "relationship_change": relationship_change,
        "community_relevance": community_relevance,
    }
    total = sum(values[key] * WEIGHTS[key] for key in WEIGHTS)
    return Significance(**values, significance=round(total, 4))


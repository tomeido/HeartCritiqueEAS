"""Pydantic domain models for events and memories."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Actor(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=256)
    role: str = "participant"
    name: str | None = None


class WitnessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    type: Literal[
        "interaction",
        "creation",
        "collaboration",
        "introduction",
        "conflict",
        "milestone",
        "departure",
    ] = "interaction"
    timestamp: datetime = Field(default_factory=utc_now)
    actors: list[Actor] = Field(default_factory=list, max_length=50)
    source: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    visibility: Literal["public", "private", "restricted"] = "public"
    hash: str | None = None


class Significance(BaseModel):
    novelty: float = Field(ge=0, le=1)
    persistence: float = Field(ge=0, le=1)
    impact: float = Field(ge=0, le=1)
    relationship_change: float = Field(ge=0, le=1)
    community_relevance: float = Field(ge=0, le=1)
    significance: float = Field(ge=0, le=1)


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: f"mem_{uuid4().hex}")
    title: str
    summary: str
    body: str | None = None
    memory_type: str
    occurred_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    scores: Significance
    confidence: float = Field(default=0.8, ge=0, le=1)
    visibility: Literal["public", "private", "restricted"] = "public"
    source_event_ids: list[str]
    participant_ids: list[str]
    observed: list[str] = Field(default_factory=list)
    inferred: list[str] = Field(default_factory=list)
    generated: list[str] = Field(default_factory=list)
    content_hash: str
    archived: bool = False
    archive_tx_id: str | None = None


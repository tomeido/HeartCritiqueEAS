"""Small SQLite repository for TheWitness' five MVP tables.

It deliberately uses a separate database from the legacy application. A2A is
the communication layer; this repository is the durable memory layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from functools import lru_cache
from typing import Any

from app.db.models import Memory, WitnessEvent, utc_now


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT,
  agent_card_url TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  actor_ids TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL,
  context TEXT NOT NULL,
  visibility TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_witness_events_occurred ON events(occurred_at DESC);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  body TEXT,
  memory_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  significance REAL NOT NULL,
  novelty REAL NOT NULL,
  persistence REAL NOT NULL,
  impact REAL NOT NULL,
  relationship_change REAL NOT NULL,
  community_relevance REAL NOT NULL,
  confidence REAL NOT NULL,
  visibility TEXT NOT NULL,
  source_event_ids TEXT NOT NULL,
  participant_ids TEXT NOT NULL,
  observed TEXT NOT NULL,
  inferred TEXT NOT NULL,
  generated TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  archive_tx_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_witness_memories_occurred ON memories(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_witness_memories_significance ON memories(significance DESC);

CREATE TABLE IF NOT EXISTS memory_links (
  source_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  target_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  relationship TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  PRIMARY KEY(source_memory_id, target_memory_id, relationship)
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  context_id TEXT NOT NULL,
  message_id TEXT NOT NULL UNIQUE,
  intent TEXT NOT NULL,
  state TEXT NOT NULL,
  input TEXT NOT NULL,
  result TEXT,
  artifacts TEXT NOT NULL DEFAULT '[]',
  history TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_witness_tasks_updated ON tasks(updated_at DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class WitnessRepository:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("WITNESS_DB_PATH", "data/thewitness.db")
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_agents(self, event: WitnessEvent) -> None:
        now = utc_now().isoformat()
        with self._lock:
            for actor in event.actors:
                self._conn.execute(
                    """INSERT INTO agents(id,name,agent_card_url,first_seen,last_seen)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         name=COALESCE(excluded.name, agents.name), last_seen=excluded.last_seen""",
                    (actor.id, actor.name, None, now, now),
                )
            self._conn.commit()

    def insert_event(self, event: WitnessEvent) -> tuple[dict[str, Any], bool]:
        payload = (
            event.id,
            event.type,
            event.timestamp.isoformat(),
            _json([actor.id for actor in event.actors]),
            _json(event.content),
            _json(event.source),
            _json(event.context),
            event.visibility,
            event.hash,
            utc_now().isoformat(),
        )
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO events
                       (id,type,occurred_at,actor_ids,content,source,context,visibility,content_hash,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                self._conn.commit()
                created = True
                row = self._conn.execute("SELECT * FROM events WHERE id=?", (event.id,)).fetchone()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                created = False
                row = self._conn.execute(
                    "SELECT * FROM events WHERE content_hash=?", (event.hash,)
                ).fetchone()
        return self._event_row(row), created

    def get_memory_for_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT 1000"
            ).fetchall()
        for row in rows:
            if event_id in json.loads(row["source_event_ids"]):
                return self._memory_row(row)
        return None

    def insert_memory(self, memory: Memory) -> dict[str, Any]:
        s = memory.scores
        with self._lock:
            self._conn.execute(
                """INSERT INTO memories
                   (id,title,summary,body,memory_type,occurred_at,created_at,significance,
                    novelty,persistence,impact,relationship_change,community_relevance,
                    confidence,visibility,source_event_ids,participant_ids,observed,inferred,
                    generated,content_hash,archived,archive_tx_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory.id,
                    memory.title,
                    memory.summary,
                    memory.body,
                    memory.memory_type,
                    memory.occurred_at.isoformat(),
                    memory.created_at.isoformat(),
                    s.significance,
                    s.novelty,
                    s.persistence,
                    s.impact,
                    s.relationship_change,
                    s.community_relevance,
                    memory.confidence,
                    memory.visibility,
                    _json(memory.source_event_ids),
                    _json(memory.participant_ids),
                    _json(memory.observed),
                    _json(memory.inferred),
                    _json(memory.generated),
                    memory.content_hash,
                    int(memory.archived),
                    memory.archive_tx_id,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM memories WHERE id=?", (memory.id,)).fetchone()
        return self._memory_row(row)

    def list_memories(
        self,
        *,
        participant_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("occurred_at>=?")
            params.append(since)
        if until:
            clauses.append("occurred_at<?")
            params.append(until)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM memories{where} ORDER BY occurred_at DESC LIMIT ?",  # noqa: S608
                (*params, min(max(limit, 1), 500)),
            ).fetchall()
        memories = [self._memory_row(row) for row in rows]
        if participant_id:
            memories = [m for m in memories if participant_id in m["participant_ids"]]
        return memories

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._memory_row(row) if row else None

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def count_prior_participant_memories(self, participant_ids: list[str]) -> int:
        if not participant_ids:
            return 0
        memories = self.list_memories(limit=500)
        wanted = set(participant_ids)
        return sum(1 for m in memories if wanted.issubset(set(m["participant_ids"])))

    def find_latest_link_candidate(
        self, participant_ids: list[str], exclude_id: str
    ) -> dict[str, Any] | None:
        if not participant_ids:
            return None
        wanted = set(participant_ids)
        for memory in self.list_memories(limit=100):
            if memory["id"] != exclude_id and wanted.intersection(memory["participant_ids"]):
                return memory
        return None

    def add_memory_link(
        self, source_id: str, target_id: str, relationship: str, confidence: float = 0.75
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO memory_links
                   (source_memory_id,target_memory_id,relationship,confidence,created_at)
                   VALUES(?,?,?,?,?)""",
                (source_id, target_id, relationship, confidence, utc_now().isoformat()),
            )
            self._conn.commit()

    def create_task(
        self,
        *,
        task_id: str,
        context_id: str,
        message_id: str,
        intent: str,
        state: str,
        input_data: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO tasks
                   (id,context_id,message_id,intent,state,input,result,artifacts,history,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    context_id,
                    message_id,
                    intent,
                    state,
                    _json(input_data),
                    None,
                    "[]",
                    _json(history),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_task(task_id)

    def update_task(
        self,
        task_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        assignments = ["state=?", "updated_at=?"]
        params: list[Any] = [state, utc_now().isoformat()]
        for column, value in (("result", result), ("artifacts", artifacts), ("history", history)):
            if value is not None:
                assignments.append(f"{column}=?")
                params.append(_json(value))
        params.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {','.join(assignments)} WHERE id=?",  # noqa: S608
                params,
            )
            self._conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def get_task_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE message_id=?", (message_id,)
            ).fetchone()
        return self._task_row(row) if row else None

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": row["type"],
            "timestamp": row["occurred_at"],
            "actors": json.loads(row["actor_ids"]),
            "content": json.loads(row["content"]),
            "source": json.loads(row["source"]),
            "context": json.loads(row["context"]),
            "visibility": row["visibility"],
            "hash": row["content_hash"],
        }

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"],
            "body": row["body"],
            "memory_type": row["memory_type"],
            "occurred_at": row["occurred_at"],
            "created_at": row["created_at"],
            "significance": row["significance"],
            "scores": {
                key: row[key]
                for key in (
                    "novelty",
                    "persistence",
                    "impact",
                    "relationship_change",
                    "community_relevance",
                )
            },
            "confidence": row["confidence"],
            "visibility": row["visibility"],
            "source_event_ids": json.loads(row["source_event_ids"]),
            "participant_ids": json.loads(row["participant_ids"]),
            "observed": json.loads(row["observed"]),
            "inferred": json.loads(row["inferred"]),
            "generated": json.loads(row["generated"]),
            "content_hash": row["content_hash"],
            "archived": bool(row["archived"]),
            "archive_tx_id": row["archive_tx_id"],
        }

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "contextId": row["context_id"],
            "messageId": row["message_id"],
            "intent": row["intent"],
            "status": {"state": row["state"], "timestamp": row["updated_at"]},
            "input": json.loads(row["input"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "artifacts": json.loads(row["artifacts"]),
            "history": json.loads(row["history"]),
            "createdAt": row["created_at"],
        }


@lru_cache(maxsize=1)
def get_repository() -> WitnessRepository:
    return WitnessRepository()


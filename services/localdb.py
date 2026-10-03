"""로컬 백엔드 — Supabase(PostgREST) 없이도 앱 전체가 동작하게 하는 호환 클라이언트.

엔진은 기본이 **pyturso(Turso Database)** — SQLite 를 Rust 로 재작성한 인프로세스 DB 로,
SQLite 파일 포맷(핫 WAL 포함)과 양방향 호환이라 기존 DB 파일을 변환 없이 그대로 연다.
미설치 환경은 stdlib sqlite3(C) 으로 자동 폴백하며 동작은 동일하다(hc_native 와 같은 철학).
LOCAL_DB_ENGINE=turso|sqlite 로 강제 가능. 두 엔진의 계약 패리티는
tests/test_localdb.py 가 양쪽 파라미터라이즈로 고정한다.

SUPABASE_* 환경변수가 없을 때 services/db.get_db() 가 이 모듈의 클라이언트를 반환한다.
supabase-py 의 fluent 쿼리 인터페이스 중 이 코드베이스가 실제로 쓰는 서브셋만 구현한다:

  table(t).select(cols, count="exact", head=True|False)
          .insert(row|rows) / .update(d) / .upsert(row|rows, on_conflict=, ignore_duplicates=)
          .delete()
          .eq/.neq/.gt/.gte/.lt/.lte/.like/.ilike/.is_/.in_/.or_/.not_(프로퍼티)
          .order(col, desc=, nullsfirst=).limit(n).execute() → .data / .count
  rpc("delete_orphan_pending_stories", {...}).execute()
  auth.get_user(token)  (로컬 게스트 토큰 검증 — services/localauth.py)

PostgREST 와 의미를 맞춘 부분:
  · insert/update/upsert/delete 는 representation(변경 행)을 .data 로 반환 (RETURNING *)
  · UNIQUE 위반 → APIError code 23505, FK 위반 → 23503 (votes 라우터의 409/404 분기)
  · 없는 컬럼: select/필터 → 42703, payload → PGRST204 (마이그레이션 probe 휴리스틱 호환)
  · 없는 RPC → PGRST202 (cleanup 레거시 폴백 호환)
  · ORDER 의 NULL 위치는 PG 기본(ASC→NULLS LAST, DESC→NULLS FIRST)을 명시적으로 재현
    (SQLite 기본은 정반대라 명시 필수)
  · or_ 필터 문자열의 and(...) 중첩, not. 접두, in.(...) 파싱
  · timestamptz 는 ISO-8601 UTC TEXT 로 저장하고 쓰기/필터 시 캐노니컬
    ('+00:00', microsecond 포함) 형식으로 정규화 → 문자열 비교가 시간 비교와 일치

스레드 안전: 단일 커넥션 + RLock 직렬화(단일 프로세스 전제 — uvicorn --workers 1 과 동일).
"""

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from services.dberrors import APIError

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join("data", "heartcritique.db")

# ── DB 엔진: pyturso(Rust) 기본, stdlib sqlite3(C) 자동 폴백 ─────────────────
# Turso 의 제약 위반 메시지는 sqlite3 과 같은 문구('UNIQUE constraint failed' 등)를
# 포함하므로 _integrity_to_apierror 의 substring 매칭이 두 엔진에서 같은 코드로 떨어진다.
# 단 byte-동일은 아니다(UNIQUE/NOT NULL/CHECK 는 ' (19)' 접미가 붙고 CHECK 는 표현식이
# 재포맷됨, FK 만 완전 동일) — APIError.message 전문(정확 일치) 비교·단언에 기대지 말 것.
try:
    import turso as _turso  # pyturso — Rust 재작성 SQLite (파일 포맷 호환)
except Exception:  # ImportError 외 바인딩 로드 실패도 폴백으로 흡수
    _turso = None

_INTEGRITY_ERRORS: tuple = (
    (sqlite3.IntegrityError, _turso.IntegrityError) if _turso is not None
    else (sqlite3.IntegrityError,))


def resolve_engine(pref: str | None = None) -> str:
    """'turso' | 'sqlite' 결정. 우선순위: 인자 > LOCAL_DB_ENGINE env > 자동(설치 여부)."""
    p = (pref if pref is not None else os.environ.get("LOCAL_DB_ENGINE", ""))
    p = p.strip().lower()
    if p in ("sqlite", "sqlite3", "python", "c"):
        return "sqlite"
    if p in ("turso", "pyturso", "rust"):
        if _turso is None:
            logger.warning(
                "[localdb] LOCAL_DB_ENGINE=%s 지정됐지만 pyturso 미설치 — sqlite3 폴백", p)
            return "sqlite"
        return "turso"
    if p:
        logger.warning("[localdb] LOCAL_DB_ENGINE=%r 미인식 — 자동 선택", p)
    return "turso" if _turso is not None else "sqlite"


class _TursoRow:
    """sqlite3.Row 호환 서브셋 — 이름/인덱스 양쪽 접근."""

    __slots__ = ("_vals", "_idx")

    def __init__(self, vals, idx: dict):
        self._vals = vals
        self._idx = idx

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._vals[self._idx[key]]
        return self._vals[key]


class _TursoCursor:
    """turso Cursor → 이 모듈이 쓰는 sqlite3 커서 서브셋(fetch*/rowcount) 어댑터."""

    __slots__ = ("_cur", "_map")

    def __init__(self, cur):
        self._cur = cur
        # description 은 SELECT/RETURNING 에서만 존재 — 이름 접근용 매핑을 1회 계산
        self._map = {d[0]: i for i, d in enumerate(cur.description or ())}

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        row = self._cur.fetchone()
        return None if row is None else _TursoRow(row, self._map)

    def fetchall(self):
        return [_TursoRow(r, self._map) for r in self._cur.fetchall()]


class _TursoConn:
    """turso.Connection → 이 모듈이 쓰는 sqlite3 인터페이스 서브셋 어댑터."""

    __slots__ = ("_conn",)

    def __init__(self, path: str):
        self._conn = _turso.connect(path)

    def execute(self, sql: str, params=()) -> _TursoCursor:
        return _TursoCursor(self._conn.execute(sql, tuple(params)))

    def executescript(self, script: str):
        self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

# ── 스키마 (supabase_schema.sql + migrations 001~013 통합본의 SQLite 번역) ────
_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stories (
  id                       TEXT PRIMARY KEY,
  category                 TEXT NOT NULL CHECK (category IN ('kindness','critique')),
  body                     TEXT NOT NULL,
  citations                TEXT NOT NULL DEFAULT '[]',
  search_queries           TEXT NOT NULL DEFAULT '[]',
  vote_count               INTEGER NOT NULL DEFAULT 0,
  archived_at              TEXT,
  arweave_tx_id            TEXT,
  arweave_url              TEXT,
  created_at               TEXT NOT NULL,
  gap_score                TEXT,
  community_count          INTEGER,
  news_count               INTEGER,
  archive_attempts         INTEGER NOT NULL DEFAULT 0,
  last_archive_attempt     TEXT,
  last_archive_error       TEXT,
  poetic_reason            TEXT,
  volatility_score         INTEGER,
  from_capture             INTEGER NOT NULL DEFAULT 0 CHECK (from_capture IN (0,1)),
  origin_captured_url      TEXT,
  captured_hard_deleted_at TEXT,
  value_score              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stories_created_at ON stories (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stories_unarchived ON stories (vote_count DESC) WHERE arweave_tx_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_stories_archived   ON stories (archived_at DESC) WHERE arweave_tx_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stories_gap_score  ON stories (gap_score) WHERE gap_score IN ('high','extreme');
CREATE UNIQUE INDEX IF NOT EXISTS uq_stories_origin_captured_url
  ON stories (origin_captured_url) WHERE origin_captured_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stories_from_capture ON stories (from_capture, created_at DESC);

CREATE TABLE IF NOT EXISTS votes (
  id         TEXT PRIMARY KEY,
  story_id   TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (story_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_votes_story_id ON votes (story_id);
CREATE INDEX IF NOT EXISTS idx_votes_user_id  ON votes (user_id);

CREATE TABLE IF NOT EXISTS citation_checks (
  id                 TEXT PRIMARY KEY,
  story_id           TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  url                TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'unchecked'
                     CHECK (status IN ('unchecked','live','deleted','blocked','error')),
  http_code          INTEGER,
  reason             TEXT,
  first_seen         TEXT NOT NULL,
  last_checked       TEXT,
  check_count        INTEGER NOT NULL DEFAULT 0,
  baseline_final_url TEXT,
  baseline_len       INTEGER,
  baseline_hash      TEXT,
  baseline_del_match INTEGER NOT NULL DEFAULT 0 CHECK (baseline_del_match IN (0,1)),
  baseline_blk_match INTEGER NOT NULL DEFAULT 0 CHECK (baseline_blk_match IN (0,1)),
  baseline_at        TEXT,
  deleted_at         TEXT,
  next_check_at      TEXT,
  error_count        INTEGER NOT NULL DEFAULT 0,
  UNIQUE (story_id, url)
);
CREATE INDEX IF NOT EXISTS idx_citation_checks_status       ON citation_checks (status);
CREATE INDEX IF NOT EXISTS idx_citation_checks_last_checked ON citation_checks (last_checked);
CREATE INDEX IF NOT EXISTS idx_citation_checks_story_id     ON citation_checks (story_id);
CREATE INDEX IF NOT EXISTS idx_citation_checks_next_check   ON citation_checks (next_check_at);

CREATE TABLE IF NOT EXISTS captured_posts (
  id                 TEXT PRIMARY KEY,
  source             TEXT NOT NULL,
  feed               TEXT,
  url                TEXT NOT NULL,
  guid               TEXT,
  title              TEXT,
  rss_summary        TEXT,
  body_text          TEXT,
  capture_manifest_path TEXT,
  capture_manifest_sha256 TEXT,
  capture_state      TEXT,
  content_hash       TEXT,
  status             TEXT NOT NULL DEFAULT 'unchecked'
                     CHECK (status IN ('unchecked','live','deleted','blocked','error')),
  http_code          INTEGER,
  reason             TEXT,
  first_seen         TEXT NOT NULL,
  captured_at        TEXT,
  last_checked       TEXT,
  check_count        INTEGER NOT NULL DEFAULT 0,
  error_count        INTEGER NOT NULL DEFAULT 0,
  next_check_at      TEXT,
  deleted_at         TEXT,
  baseline_final_url TEXT,
  baseline_len       INTEGER,
  baseline_hash      TEXT,
  baseline_del_match INTEGER NOT NULL DEFAULT 0 CHECK (baseline_del_match IN (0,1)),
  baseline_blk_match INTEGER NOT NULL DEFAULT 0 CHECK (baseline_blk_match IN (0,1)),
  baseline_at        TEXT,
  volatility_score   INTEGER,
  hard_deleted_at    TEXT,
  promoted_story_id  TEXT,
  promotion_status   TEXT,
  value_score        INTEGER,
  UNIQUE (url)
);
CREATE INDEX IF NOT EXISTS idx_captured_posts_next_check   ON captured_posts (next_check_at);
CREATE INDEX IF NOT EXISTS idx_captured_posts_status       ON captured_posts (status);
CREATE INDEX IF NOT EXISTS idx_captured_posts_source       ON captured_posts (source);
CREATE INDEX IF NOT EXISTS idx_captured_posts_first_seen   ON captured_posts (first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_captured_posts_hard_deleted ON captured_posts (hard_deleted_at) WHERE hard_deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_captured_posts_volatility   ON captured_posts (volatility_score DESC);
CREATE INDEX IF NOT EXISTS idx_captured_posts_promotion    ON captured_posts (promotion_status);
CREATE INDEX IF NOT EXISTS idx_captured_posts_value        ON captured_posts (value_score DESC);

CREATE TABLE IF NOT EXISTS discovery_queue (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  feed TEXT NOT NULL,
  guid TEXT,
  title TEXT,
  summary TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','capturing','retry','captured','deleted')),
  attempts INTEGER NOT NULL DEFAULT 0,
  cooldown INTEGER NOT NULL DEFAULT 1 CHECK (cooldown IN (0,1)),
  first_seen TEXT NOT NULL,
  last_attempt_at TEXT,
  next_attempt_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_discovery_source_due
  ON discovery_queue (source, status, next_attempt_at, first_seen);
CREATE INDEX IF NOT EXISTS idx_discovery_source_attempt
  ON discovery_queue (source, last_attempt_at DESC);

CREATE TABLE IF NOT EXISTS wayback_snapshots (
  id                 TEXT PRIMARY KEY,
  url                TEXT NOT NULL UNIQUE,
  job_id             TEXT,
  snapshot_url       TEXT,
  snapshot_timestamp TEXT,
  status             TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','pending','success','error','skipped')),
  reason             TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  submitted_at       TEXT,
  updated_at         TEXT,
  next_poll_at       TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wayback_status    ON wayback_snapshots (status);
CREATE INDEX IF NOT EXISTS idx_wayback_next_poll ON wayback_snapshots (next_poll_at);
"""

# 컬럼 종류: uuid | text | int | bool | json | ts  (쓰기/필터 값 변환과 읽기 역변환에 사용)
_COLS: dict[str, dict[str, str]] = {
    "stories": {
        "id": "uuid", "category": "text", "body": "text", "citations": "json",
        "search_queries": "json", "vote_count": "int", "archived_at": "ts",
        "arweave_tx_id": "text", "arweave_url": "text", "created_at": "ts",
        "gap_score": "text", "community_count": "int", "news_count": "int",
        "archive_attempts": "int", "last_archive_attempt": "ts",
        "last_archive_error": "text", "poetic_reason": "text",
        "volatility_score": "int", "from_capture": "bool",
        "origin_captured_url": "text", "captured_hard_deleted_at": "ts",
        "value_score": "int",
    },
    "votes": {
        "id": "uuid", "story_id": "uuid", "user_id": "uuid", "created_at": "ts",
    },
    "citation_checks": {
        "id": "uuid", "story_id": "uuid", "url": "text", "status": "text",
        "http_code": "int", "reason": "text", "first_seen": "ts",
        "last_checked": "ts", "check_count": "int", "baseline_final_url": "text",
        "baseline_len": "int", "baseline_hash": "text",
        "baseline_del_match": "bool", "baseline_blk_match": "bool",
        "baseline_at": "ts", "deleted_at": "ts", "next_check_at": "ts",
        "error_count": "int",
    },
    "captured_posts": {
        "id": "uuid", "source": "text", "feed": "text", "url": "text",
        "guid": "text", "title": "text", "rss_summary": "text",
        "body_text": "text", "content_hash": "text", "status": "text",
        "capture_manifest_path": "text", "capture_manifest_sha256": "text",
        "capture_state": "text",
        "http_code": "int", "reason": "text", "first_seen": "ts",
        "captured_at": "ts", "last_checked": "ts", "check_count": "int",
        "error_count": "int", "next_check_at": "ts", "deleted_at": "ts",
        "baseline_final_url": "text", "baseline_len": "int",
        "baseline_hash": "text", "baseline_del_match": "bool",
        "baseline_blk_match": "bool", "baseline_at": "ts",
        "volatility_score": "int", "hard_deleted_at": "ts",
        "promoted_story_id": "uuid", "promotion_status": "text",
        "value_score": "int",
    },
    "discovery_queue": {
        "id": "uuid", "url": "text", "source": "text", "feed": "text",
        "guid": "text", "title": "text", "summary": "text", "priority": "int",
        "status": "text", "attempts": "int", "first_seen": "ts",
        "cooldown": "bool",
        "last_attempt_at": "ts", "next_attempt_at": "ts", "last_error": "text",
    },
    "wayback_snapshots": {
        "id": "uuid", "url": "text", "job_id": "text", "snapshot_url": "text",
        "snapshot_timestamp": "text", "status": "text", "reason": "text",
        "attempts": "int", "submitted_at": "ts", "updated_at": "ts",
        "next_poll_at": "ts", "created_at": "ts",
    },
}

# DB 기본값(now())이 있던 timestamptz — 앱에서 채워 저장 포맷을 단일화한다
_TS_DEFAULTS: dict[str, tuple[str, ...]] = {
    "stories": ("created_at",),
    "votes": ("created_at",),
    "citation_checks": ("first_seen",),
    "captured_posts": ("first_seen",),
    "wayback_snapshots": ("created_at",),
    "discovery_queue": ("first_seen", "next_attempt_at"),
}

_OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_ts(v):
    """ISO 타임스탬프 문자열을 캐노니컬 UTC('+00:00') 형식으로 정규화.
    'Z' 접미·다른 오프셋·date-only 를 흡수해 저장/필터가 같은 문자열 체계를 쓰게 한다
    (문자열 비교 == 시간 비교 보장). 파싱 불가면 원본 유지."""
    if not isinstance(v, str) or not v:
        return v
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return v
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _missing_col_error(table: str, col: str, payload: bool = False) -> APIError:
    if payload:
        # PostgREST 의 PGRST204 메시지 형태('could not find' 휴리스틱 호환)
        return APIError({
            "message": f"Could not find the '{col}' column of '{table}' in the schema cache",
            "code": "PGRST204", "hint": None, "details": None,
        })
    return APIError({
        "message": f"column {table}.{col} does not exist",
        "code": "42703", "hint": None, "details": None,
    })


def _integrity_to_apierror(e: Exception) -> APIError:
    msg = str(e)
    if "UNIQUE constraint failed" in msg:
        code = "23505"
    elif "FOREIGN KEY constraint failed" in msg:
        code = "23503"
    elif "CHECK constraint failed" in msg:
        code = "23514"
    elif "NOT NULL constraint failed" in msg:
        code = "23502"
    else:
        code = "23000"
    return APIError({"message": msg, "code": code, "hint": None, "details": None})


class APIResponse:
    """postgrest APIResponse 호환(.data/.count만 사용됨)."""

    __slots__ = ("data", "count")

    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _LocalQuery:
    def __init__(self, client: "LocalClient", table: str):
        if table not in _COLS:
            raise APIError({
                "message": f"relation \"public.{table}\" does not exist",
                "code": "42P01", "hint": None, "details": None,
            })
        self._c = client
        self._t = table
        self._op = "select"
        self._cols = "*"
        self._count = None
        self._head = False
        self._payload = None
        self._on_conflict = None
        self._ignore_dup = False
        self._wheres: list[tuple[str, list]] = []
        self._orders: list[str] = []
        self._limit: int | None = None
        self._negate = False

    # ── 값/컬럼 변환 ────────────────────────────────────────────────────────
    def _kind(self, col: str, payload: bool = False) -> str:
        kind = _COLS[self._t].get(col)
        if kind is None:
            raise _missing_col_error(self._t, col, payload=payload)
        return kind

    def _cv(self, col: str, v, payload: bool = False):
        """파이썬 값 → SQLite 바인딩 값 (컬럼 종류 기반)."""
        kind = self._kind(col, payload=payload)
        if v is None:
            return None
        if kind == "uuid":
            # PG uuid 타입은 대소문자·표기 변형(braces/urn)을 흡수해 canonical(소문자)로
            # 저장·비교하지만 로컬은 TEXT 정확일치 — 여기서 동일하게 정규화해 의미를 맞춘다.
            if isinstance(v, str):
                try:
                    return str(uuid.UUID(v.strip()))
                except ValueError:
                    return v
            return v
        if kind == "json":
            if isinstance(v, (dict, list)):
                return json.dumps(v, ensure_ascii=False)
            return v
        if kind == "bool":
            if isinstance(v, str):
                return 1 if v.strip().lower() in ("true", "t", "1") else 0
            return 1 if v else 0
        if kind == "int":
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, str):
                try:
                    return int(v)
                except ValueError:
                    return v
            return v
        if kind == "ts":
            return _norm_ts(v)
        return v

    def _row_out(self, row: sqlite3.Row, cols: list[str]) -> dict:
        out = {}
        kinds = _COLS[self._t]
        for col in cols:
            v = row[col]
            kind = kinds.get(col)
            if v is not None:
                if kind == "json" and isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        pass
                elif kind == "bool":
                    v = bool(v)
            out[col] = v
        return out

    # ── 빌더 API (supabase-py 호환 서브셋) ──────────────────────────────────
    def select(self, *columns, count: str | None = None, head: bool = False):
        self._op = "select"
        self._cols = ",".join(columns) if columns else "*"
        self._count = count
        self._head = head
        return self

    def insert(self, payload, **_ignored):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload, **_ignored):
        self._op = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict: str | None = None,
               ignore_duplicates: bool = False, **_ignored):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        self._ignore_dup = ignore_duplicates
        return self

    def delete(self, **_ignored):
        self._op = "delete"
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def _add(self, frag: str, params: list):
        if self._negate:
            frag = f"NOT ({frag})"
            self._negate = False
        self._wheres.append((frag, params))
        return self

    def _cmp(self, col: str, op: str, v):
        return self._add(f'"{col}" {_OPS[op]} ?', [self._cv(col, v)])

    def eq(self, col, v):
        return self._cmp(col, "eq", v)

    def neq(self, col, v):
        return self._cmp(col, "neq", v)

    def gt(self, col, v):
        return self._cmp(col, "gt", v)

    def gte(self, col, v):
        return self._cmp(col, "gte", v)

    def lt(self, col, v):
        return self._cmp(col, "lt", v)

    def lte(self, col, v):
        return self._cmp(col, "lte", v)

    def like(self, col, pattern):
        self._kind(col)
        return self._add(f'"{col}" LIKE ?', [str(pattern).replace("*", "%")])

    ilike = like  # SQLite LIKE 는 기본 대소문자 무시(ASCII) — 한국어 콘텐츠엔 동일

    def is_(self, col, v):
        self._kind(col)
        if v is None or (isinstance(v, str) and v.strip().lower() == "null"):
            return self._add(f'"{col}" IS NULL', [])
        truthy = v is True or (isinstance(v, str) and v.strip().lower() == "true")
        return self._add(f'"{col}" IS {1 if truthy else 0}', [])

    def in_(self, col, values):
        vals = [self._cv(col, v) for v in values]
        if not vals:
            return self._add("0 = 1", [])
        marks = ",".join("?" for _ in vals)
        return self._add(f'"{col}" IN ({marks})', vals)

    def or_(self, spec: str, **_ignored):
        frag, params = self._parse_bool(spec, "OR")
        return self._add(frag, params)

    def order(self, col: str, desc: bool = False, nullsfirst: bool | None = None,
              **_ignored):
        self._kind(col)
        # PostgREST/PG 기본: ASC → NULLS LAST, DESC → NULLS FIRST (SQLite 기본과 반대)
        nf = desc if nullsfirst is None else nullsfirst
        self._orders.append(
            f'"{col}" {"DESC" if desc else "ASC"} NULLS {"FIRST" if nf else "LAST"}')
        return self

    def limit(self, n: int, **_ignored):
        self._limit = int(n)
        return self

    # ── or_ 필터 문자열 파서 (PostgREST 문법 서브셋) ─────────────────────────
    @staticmethod
    def _split_top(s: str) -> list[str]:
        parts, depth, cur = [], 0, []
        for ch in s:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        return [p for p in (p.strip() for p in parts) if p]

    def _parse_bool(self, spec: str, joiner: str) -> tuple[str, list]:
        frags, params = [], []
        for part in self._split_top(spec):
            low = part.lower()
            if low.startswith("and(") and part.endswith(")"):
                f, ps = self._parse_bool(part[4:-1], "AND")
            elif low.startswith("or(") and part.endswith(")"):
                f, ps = self._parse_bool(part[3:-1], "OR")
            else:
                f, ps = self._parse_cond(part)
            frags.append(f)
            params.extend(ps)
        if not frags:
            return "1 = 1", []
        return "(" + f" {joiner} ".join(frags) + ")", params

    def _parse_cond(self, cond: str) -> tuple[str, list]:
        toks = cond.split(".")
        if len(toks) < 2:
            raise APIError({"message": f"unsupported filter: {cond!r}",
                            "code": "PGRST100", "hint": None, "details": None})
        col = toks[0]
        i = 1
        neg = False
        if toks[i] == "not" and len(toks) > i + 1:
            neg = True
            i += 1
        op = toks[i]
        value = ".".join(toks[i + 1:])

        if op == "is":
            self._kind(col)
            if value.lower() == "null":
                frag, ps = f'"{col}" IS NULL', []
            else:
                truthy = value.lower() == "true"
                frag, ps = f'"{col}" IS {1 if truthy else 0}', []
        elif op == "in":
            inner = value.strip()
            if inner.startswith("(") and inner.endswith(")"):
                inner = inner[1:-1]
            vals = [self._cv(col, v.strip().strip('"'))
                    for v in inner.split(",") if v.strip()]
            if not vals:
                frag, ps = "0 = 1", []
            else:
                frag = f'"{col}" IN ({",".join("?" for _ in vals)})'
                ps = vals
            # PostgREST not.in 은 NOT(col IN ...) — NULL 행 제외 의미까지 SQL 과 동일
        elif op in ("like", "ilike"):
            self._kind(col)
            frag, ps = f'"{col}" LIKE ?', [value.replace("*", "%")]
        elif op in _OPS:
            frag, ps = f'"{col}" {_OPS[op]} ?', [self._cv(col, value)]
        else:
            raise APIError({"message": f"unsupported operator: {op!r} in {cond!r}",
                            "code": "PGRST100", "hint": None, "details": None})
        if neg:
            frag = f"NOT ({frag})"
        return frag, ps

    # ── 실행 ────────────────────────────────────────────────────────────────
    def _where_sql(self) -> tuple[str, list]:
        if not self._wheres:
            return "", []
        frags = [w[0] for w in self._wheres]
        params: list = []
        for w in self._wheres:
            params.extend(w[1])
        return " WHERE " + " AND ".join(frags), params

    def _select_cols(self) -> list[str]:
        if self._cols.strip() == "*":
            return list(_COLS[self._t].keys())
        cols = [c.strip() for c in self._cols.split(",") if c.strip()]
        for c in cols:
            self._kind(c)
        return cols

    def _defaults_filled(self, row: dict) -> dict:
        row = dict(row)
        # payload 컬럼 검증 + 값 변환은 호출부에서 _cv(payload=True) 로 수행
        if "id" not in row or row.get("id") is None:
            row["id"] = str(uuid.uuid4())
        for col in _TS_DEFAULTS.get(self._t, ()):
            if not row.get(col):
                row[col] = _now_iso()
        return row

    def _rollback_quietly(self) -> None:
        try:
            self._c._conn.rollback()
        except Exception:
            pass

    def execute(self) -> APIResponse:
        with self._c._lock:
            try:
                return self._execute_locked()
            except _INTEGRITY_ERRORS as e:
                # PostgREST 의 배치 삽입은 원자적 — 부분 삽입 잔여가 열린 트랜잭션에
                # 남아 다음 commit 에 편승하지 않도록 즉시 롤백해 의미를 맞춘다.
                self._rollback_quietly()
                raise _integrity_to_apierror(e) from e
            except BaseException:
                # IntegrityError 외 실패(_cv 의 APIError, OperationalError 등)도 동일하게
                # 잔여를 롤백한다. 트랜잭션이 없을 때의 rollback 은 양쪽 엔진 모두 no-op.
                self._rollback_quietly()
                raise

    def _execute_locked(self) -> APIResponse:
        conn = self._c._conn
        where, wparams = self._where_sql()

        if self._op == "select":
            cols = self._select_cols()
            count = None
            if self._count:
                # PostgREST 의 count 는 limit/range 와 무관한 전체 매칭 행 수
                cur = conn.execute(
                    f'SELECT COUNT(*) FROM "{self._t}"{where}', wparams)
                count = cur.fetchone()[0]
                if self._head:
                    return APIResponse([], count)
            sql = f'SELECT {",".join(f'"{c}"' for c in cols)} FROM "{self._t}"{where}'
            if self._orders:
                sql += " ORDER BY " + ", ".join(self._orders)
            if self._limit is not None:
                sql += f" LIMIT {self._limit}"
            rows = conn.execute(sql, wparams).fetchall()
            return APIResponse([self._row_out(r, cols) for r in rows], count)

        all_cols = list(_COLS[self._t].keys())

        if self._op in ("insert", "upsert"):
            payload = self._payload
            rows = payload if isinstance(payload, list) else [payload]
            out: list[dict] = []
            for raw in rows:
                row = self._defaults_filled(raw)
                cols = list(row.keys())
                vals = [self._cv(c, row[c], payload=True) for c in cols]
                sql = (f'INSERT INTO "{self._t}" ({",".join(f'"{c}"' for c in cols)}) '
                       f'VALUES ({",".join("?" for _ in cols)})')
                if self._op == "upsert":
                    conflict = [c.strip() for c in (self._on_conflict or "id").split(",")]
                    if self._ignore_dup:
                        sql += f' ON CONFLICT ({",".join(conflict)}) DO NOTHING'
                    else:
                        setters = [f'"{c}" = excluded."{c}"' for c in cols
                                   if c not in conflict and c != "id"]
                        if setters:
                            sql += (f' ON CONFLICT ({",".join(conflict)}) '
                                    f'DO UPDATE SET {", ".join(setters)}')
                        else:
                            sql += f' ON CONFLICT ({",".join(conflict)}) DO NOTHING'
                sql += " RETURNING *"
                fetched = conn.execute(sql, vals).fetchall()
                out.extend(self._row_out(r, all_cols) for r in fetched)
            conn.commit()
            return APIResponse(out)

        if self._op == "update":
            payload = dict(self._payload or {})
            if not payload:
                return APIResponse([])
            cols = list(payload.keys())
            vals = [self._cv(c, payload[c], payload=True) for c in cols]
            sql = (f'UPDATE "{self._t}" SET '
                   + ", ".join(f'"{c}" = ?' for c in cols)
                   + where + " RETURNING *")
            fetched = conn.execute(sql, vals + wparams).fetchall()
            conn.commit()
            return APIResponse([self._row_out(r, all_cols) for r in fetched])

        if self._op == "delete":
            sql = f'DELETE FROM "{self._t}"{where} RETURNING *'
            fetched = conn.execute(sql, wparams).fetchall()
            conn.commit()
            return APIResponse([self._row_out(r, all_cols) for r in fetched])

        raise APIError({"message": f"unsupported op: {self._op}",
                        "code": "PGRST100", "hint": None, "details": None})


class _LocalRpc:
    def __init__(self, client: "LocalClient", fn: str, params: dict | None):
        self._c = client
        self._fn = fn
        self._params = params or {}

    def execute(self) -> APIResponse:
        if self._fn == "delete_orphan_pending_stories":
            cutoff = _norm_ts(self._params.get("p_cutoff"))
            max_votes = int(self._params.get("p_max_votes") or 0)
            with self._c._lock:
                cur = self._c._conn.execute(
                    """
                    DELETE FROM stories
                     WHERE arweave_tx_id IS NULL
                       AND created_at < ?
                       AND COALESCE(from_capture, 0) = 0
                       AND (SELECT COUNT(*) FROM votes v
                             WHERE v.story_id = stories.id) <= ?
                    """,
                    (cutoff, max_votes),
                )
                self._c._conn.commit()
                return APIResponse(cur.rowcount)
        # PostgREST 와 동일하게 미지의 함수는 PGRST202 (cleanup 폴백 경로 호환)
        raise APIError({
            "message": f"Could not find the function public.{self._fn} in the schema cache",
            "code": "PGRST202", "hint": None, "details": None,
        })


class _LocalAuth:
    """get_anon_db().auth.get_user(token) 호환 — 로컬 게스트 토큰(HMAC) 검증."""

    def get_user(self, token: str):
        from types import SimpleNamespace

        from services.localauth import verify_guest_token
        user_id = verify_guest_token(token)
        if not user_id:
            raise ValueError("invalid guest token")
        return SimpleNamespace(user=SimpleNamespace(id=user_id))


class LocalClient:
    """supabase Client 호환(사용 서브셋) 로컬 클라이언트 — turso(Rust)/sqlite3 겸용."""

    def __init__(self, path: str | None = None, engine: str | None = None):
        self.path = path or os.environ.get("LOCAL_DB_PATH", DEFAULT_DB_PATH)
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self.engine = resolve_engine(engine)
        if self.engine == "turso":
            self._conn = _TursoConn(self.path)
        else:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_DDL)
        # CREATE TABLE IF NOT EXISTS does not upgrade existing capture tables.
        # PRAGMA + additive ALTER works with both sqlite3 and pyturso.
        capture_columns = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(captured_posts)").fetchall()}
        for column in ("capture_manifest_path", "capture_manifest_sha256", "capture_state"):
            if column not in capture_columns:
                self._conn.execute(f'ALTER TABLE captured_posts ADD COLUMN "{column}" TEXT')
        queue_columns = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(discovery_queue)").fetchall()}
        if "cooldown" not in queue_columns:
            self._conn.execute("ALTER TABLE discovery_queue ADD COLUMN cooldown INTEGER NOT NULL DEFAULT 1")
        self._conn.commit()
        self.auth = _LocalAuth()

    def table(self, name: str) -> _LocalQuery:
        return _LocalQuery(self, name)

    def rpc(self, fn: str, params: dict | None = None) -> _LocalRpc:
        return _LocalRpc(self, fn, params)


_local: LocalClient | None = None
_local_lock = threading.Lock()


def get_local_db() -> LocalClient:
    global _local
    if _local is None:
        with _local_lock:
            if _local is None:
                _local = LocalClient()
                note = ("Rust 엔진(pyturso)" if _local.engine == "turso"
                        else "sqlite3 폴백(pyturso 미설치)")
                logger.info(
                    f"[localdb] 로컬 백엔드 사용 (engine={_local.engine}, "
                    f"path={_local.path}) — {note}, Supabase 없이 동작")
    return _local

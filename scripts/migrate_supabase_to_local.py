#!/usr/bin/env python3
"""Supabase(PostgREST) → 로컬 DB(기본 pyturso/Rust 엔진, SQLite 파일 포맷) 전체 마이그레이션.

Supabase 에는 **읽기(GET)로만** 접근한다 — 원본은 어떤 경우에도 변경되지 않는다.

사용법 (컨테이너 안 — .env 의 SUPABASE_* 와 ./data 볼륨을 그대로 씀):
  docker compose build app   # 스크립트가 든 최신 이미지 먼저 빌드
  docker compose run --rm --no-deps app python scripts/migrate_supabase_to_local.py

동작:
  · 5개 테이블(stories → votes → citation_checks → captured_posts → wayback_snapshots)을
    FK 의존 순서로, id 키셋 페이지네이션(order=id.asc & id=gt.<last>)으로 전량 읽기
  · services/localdb.LocalClient 를 통해 upsert — 타임스탬프 캐노니컬화·JSON 직렬화·
    bool 변환·uuid 정규화가 로컬 계약과 정확히 일치
  · conflict 키는 테이블별 자연키(CONFLICT_KEYS): '같은 자연키, 다른 id' 로컬 행과
    충돌하지 않아 재실행·델타 재실행이 안전하다. stories 만 id(다른 테이블이 FK 로
    참조)를 유지하고, 부분 유니크(origin_captured_url) 충돌 행은 사전 감지해 스킵
  · 로컬 스키마(_COLS)에 없는 컬럼은 건너뛰고 경고(스키마 드리프트 가시화)
  · 예기치 못한 유니크 충돌(23505)은 행 단위 폴백으로 해당 행만 스킵하고 계속 진행
  · 종료 시 테이블별 원본/로컬 행 수 대조 — 불일치가 있으면 exit 1 + 재실행 안내

한계(정확히 알 것):
  · 델타 재실행은 원본의 '추가/수정'만 흡수한다. 원본에서 '삭제'된 행은 로컬에서
    지우지 않는다(유령 행 잔존) — 그래서 권장 절차는 앱을 멈춘 뒤 1회 실행이다.
  · votes 의 user_id 는 Supabase OAuth 신원 그대로 옮겨진다. 게스트 인증 전환 후
    같은 사람이 새 게스트 신원으로 다시 투표하는 것은 막을 수 없다(신원 매핑 부재).
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from services.dberrors import APIError  # noqa: E402
from services.localdb import _COLS, LocalClient  # noqa: E402

# FK 의존 순서: votes/citation_checks 가 stories 를 참조한다
TABLES = ["stories", "votes", "citation_checks", "captured_posts", "wayback_snapshots"]

# 테이블별 upsert conflict 키 — 자연키를 써서 '같은 자연키, 다른 id' 인 기존 로컬 행과의
# 보조 UNIQUE 충돌을 구조적으로 제거한다(localdb upsert 는 setter 에서 id 를 항상 제외
# → 기존 로컬 행의 id 보존). votes/citation_checks/captured_posts/wayback_snapshots 의
# id 는 어떤 FK 도 참조하지 않아 안전하다. stories 는 votes·citation_checks 가 id 를
# FK 로 참조하므로 id 를 유지하고, origin_captured_url 부분 유니크만 사전 스킵한다.
CONFLICT_KEYS = {
    "stories": "id",
    "votes": "story_id,user_id",
    "citation_checks": "story_id,url",
    "captured_posts": "url",
    "wayback_snapshots": "url",
}

# body_text 등 큰 컬럼이 있는 테이블은 페이지를 줄여 응답 크기를 제한
PAGE_SIZES = {"captured_posts": 200}
DEFAULT_PAGE_SIZE = 500
RETRIES = 3


def _client() -> tuple[httpx.Client, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 가 필요합니다 (.env 확인)")
    return httpx.Client(
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(60.0, connect=15.0),
    ), f"{url}/rest/v1"


def _get_with_retry(http: httpx.Client, url: str, headers: dict | None = None) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = http.get(url, headers=headers)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            last = e
            wait = 2.0 * (attempt + 1)
            print(f"    ! 요청 실패({e}) — {wait:.0f}s 후 재시도 {attempt + 1}/{RETRIES}")
            time.sleep(wait)
    raise SystemExit(f"원본 읽기 실패(재시도 소진): {url} — {last}")


def remote_count(http: httpx.Client, base: str, table: str) -> int:
    r = _get_with_retry(
        http, f"{base}/{table}?select=id&limit=1",
        headers={"Prefer": "count=exact", "Range": "0-0"})
    return int(r.headers.get("Content-Range", "/0").split("/")[-1])


def _skip_origin_conflicts(db: LocalClient, page: list[dict]) -> tuple[list[dict], int]:
    """stories 전용: 부분 유니크 uq_stories_origin_captured_url 과 충돌하는 행
    ('같은 origin_captured_url, 다른 id' 인 로컬 행 존재)을 사전 스킵한다."""
    urls = [r["origin_captured_url"] for r in page if r.get("origin_captured_url")]
    if not urls:
        return page, 0
    local = (db.table("stories").select("id,origin_captured_url")
             .in_("origin_captured_url", urls).execute().data)
    owner = {r["origin_captured_url"]: r["id"] for r in local}
    kept, skipped = [], 0
    for row in page:
        u = row.get("origin_captured_url")
        if u and owner.get(u) not in (None, row.get("id")):
            print(f"    ⚠ 스킵(origin_captured_url 선점): 원본 {row.get('id', '?')[:8]}… "
                  f"↔ 로컬 {owner[u][:8]}… ({u})")
            skipped += 1
        else:
            kept.append(row)
    return kept, skipped


def _upsert_page(db: LocalClient, table: str, page: list[dict]) -> int:
    """페이지 upsert. 예기치 못한 유니크 충돌(23505)은 행 단위 폴백으로 해당 행만
    스킵하고 계속 진행한다(localdb 의 롤백이 배치 원자성을 보장하므로 재시도 안전).
    반환: 스킵된 행 수."""
    conflict = CONFLICT_KEYS[table]
    try:
        db.table(table).upsert(page, on_conflict=conflict).execute()
        return 0
    except APIError as e:
        if e.code != "23505":
            raise
    skipped = 0
    for row in page:
        try:
            db.table(table).upsert([row], on_conflict=conflict).execute()
        except APIError as e:
            if e.code != "23505":
                raise
            print(f"    ⚠ 스킵(유니크 충돌): {table} 행 {row.get('id', '?')[:8]}… — {e.message}")
            skipped += 1
    return skipped


def migrate_table(http: httpx.Client, base: str, db: LocalClient, table: str,
                  page_size: int) -> tuple[int, int, set]:
    """반환: (적재 행 수, 스킵 행 수, 건너뛴 미지 컬럼 집합)."""
    known = set(_COLS[table])
    dropped: set = set()
    total = 0
    skipped = 0
    last_id = None
    while True:
        q = f"{base}/{table}?select=*&order=id.asc&limit={page_size}"
        if last_id is not None:
            q += f"&id=gt.{last_id}"
        rows = _get_with_retry(http, q).json()
        if not rows:
            break
        page = []
        for row in rows:
            extra = set(row) - known
            if extra:
                dropped |= extra
            page.append({k: v for k, v in row.items() if k in known})
        if table == "stories":
            page, s = _skip_origin_conflicts(db, page)
            skipped += s
        skipped += _upsert_page(db, table, page)
        total += len(page)
        last_id = rows[-1]["id"]
        print(f"    {table}: {total}행 적재 (last_id={last_id[:8]}…)")
    return total, skipped, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", default=None,
                    help="로컬 DB 파일 경로(기본: $LOCAL_DB_PATH 또는 data/heartcritique.db)")
    ap.add_argument("--engine", default=None, choices=["turso", "sqlite"],
                    help="로컬 엔진 강제(기본: 자동 — pyturso 설치 시 turso)")
    ap.add_argument("--tables", nargs="*", default=TABLES,
                    help=f"마이그레이션할 테이블 부분집합(기본: 전체 {TABLES})")
    args = ap.parse_args()

    unknown = set(args.tables) - set(TABLES)
    if unknown:
        sys.exit(f"알 수 없는 테이블: {sorted(unknown)} — 가능: {TABLES}")
    selected = [t for t in TABLES if t in args.tables]
    if not selected:
        sys.exit(f"--tables 선택이 비었습니다 — 가능: {TABLES}")

    http, base = _client()
    db = LocalClient(args.db_path, engine=args.engine)
    print(f"로컬 DB: path={db.path} engine={db.engine}")

    started = time.time()
    failures = []
    for table in selected:
        expect = remote_count(http, base, table)
        print(f"▶ {table}: 원본 {expect}행")
        try:
            got, skipped, dropped = migrate_table(
                http, base, db, table, PAGE_SIZES.get(table, DEFAULT_PAGE_SIZE))
        except APIError as e:
            # FK 델타(23503) 등 — 원본이 마이그레이션 중에 변한 경우. 통제된 실패로
            # 기록하고 다음 테이블 진행(재실행하면 stories 부터 다시 훑어 자가 치유).
            print(f"  ❌ {table}: upsert 실패({e.code}: {e.message}) — "
                  "마이그레이션 중 원본 델타일 수 있음, 재실행으로 복구 가능")
            failures.append(table)
            continue
        if dropped:
            print(f"    ⚠ 로컬 스키마에 없어 건너뛴 컬럼: {sorted(dropped)}")
        # 원본 행 수를 적재 후 재측정 — 마이그레이션 중 원본 증감으로 인한 오탐 최소화
        expect_after = remote_count(http, base, table)
        local = (db.table(table).select("*", count="exact", head=True)
                 .execute().count or 0)
        ok = (local + skipped) >= expect_after
        mark = "✅" if ok else "❌"
        note = f" (스킵 {skipped})" if skipped else ""
        if local > expect_after:
            note += f" — 로컬이 원본보다 많음(기존 로컬 행 존재 추정)"
        print(f"  {mark} {table}: 원본 {expect}→{expect_after} / 적재 {got}{note} / 로컬 {local}")
        if not ok:
            failures.append(table)

    print(f"\n총 소요 {time.time() - started:.1f}s")
    if failures:
        sys.exit(f"❌ 미완료 테이블: {failures} — 재실행하면 이어서 채워집니다")
    print("✅ 마이그레이션 완료 — 모든 테이블 행 수 일치")


if __name__ == "__main__":
    main()

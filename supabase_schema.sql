-- Heart & Critique - Supabase 스키마 (운영 안전 / 멱등)
-- Supabase 대시보드 → SQL Editor 에 붙여넣고 실행.
--
-- ⚠️ 이 파일은 데이터를 삭제하지 않는다. 재실행해도 안전(create ... if not exists,
--    policy 는 drop-then-create). 기존 stories/votes/votes/박제 데이터는 보존된다.
--    완전 초기화(전체 삭제 후 재생성)가 필요하면 supabase_reset.sql 을 사용할 것.
--    컬럼 추가 등 스키마 변경은 migrations/ 의 alter ... if not exists 로 관리한다.

-- 스토리 테이블
create table if not exists public.stories (
  id              uuid        primary key default gen_random_uuid(),
  category        text        not null check (category in ('kindness', 'critique')),
  body            text        not null,
  citations       jsonb       not null default '[]'::jsonb,
  search_queries  jsonb       not null default '[]'::jsonb,
  vote_count      int         not null default 0,
  archived_at     timestamptz,
  arweave_tx_id   text,
  arweave_url     text,
  created_at      timestamptz not null default now(),
  -- 격차 탐지: 커뮤니티 vs 언론 보도 격차 (검열 신호)
  gap_score       text,                -- none/medium/high/extreme (calculate_gap_score 가 산출)
  community_count int,
  news_count      int,
  -- 박제 재시도 추적 (sweeper 가 실패한 박제를 지수 백오프로 자동 재시도)
  archive_attempts     int not null default 0,
  last_archive_attempt timestamptz,
  last_archive_error   text
);

-- 기존 설치본을 위한 컬럼 보강 (이미 있으면 무시)
alter table public.stories
  add column if not exists gap_score            text,
  add column if not exists community_count      int,
  add column if not exists news_count           int,
  add column if not exists archive_attempts     int not null default 0,
  add column if not exists last_archive_attempt timestamptz,
  add column if not exists last_archive_error   text;

-- 투표 테이블 (유저당 스토리 1표)
create table if not exists public.votes (
  id          uuid        primary key default gen_random_uuid(),
  story_id    uuid        not null references public.stories(id) on delete cascade,
  user_id     uuid        not null,
  created_at  timestamptz not null default now(),
  unique (story_id, user_id)
);

-- 인덱스
create index if not exists idx_stories_created_at on public.stories (created_at desc);
create index if not exists idx_stories_unarchived on public.stories (vote_count desc)
  where arweave_tx_id is null;
create index if not exists idx_votes_story_id     on public.votes (story_id);
create index if not exists idx_votes_user_id      on public.votes (user_id);

-- RLS 활성화 (멱등)
alter table public.stories enable row level security;
alter table public.votes   enable row level security;

-- 스토리: 누구나 읽기, 서비스 롤만 쓰기
drop policy if exists "stories_read_all"  on public.stories;
drop policy if exists "stories_write_svc" on public.stories;
create policy "stories_read_all"  on public.stories for select using (true);
create policy "stories_write_svc" on public.stories for all
  using (auth.role() = 'service_role');

-- 투표: 누구나 읽기, 서비스 롤만 쓰기
drop policy if exists "votes_read_all"  on public.votes;
drop policy if exists "votes_write_svc" on public.votes;
create policy "votes_read_all"  on public.votes for select using (true);
create policy "votes_write_svc" on public.votes for all
  using (auth.role() = 'service_role');

-- ── 출처 삭제 추적 ─────────────────────────────────────────────────────────
-- CONTEXT.md 정신: 대기업 자본력에 삭제되는 글을 박제. URL 별 생존 여부 추적.

create table if not exists public.citation_checks (
  id            uuid        primary key default gen_random_uuid(),
  story_id      uuid        not null references public.stories(id) on delete cascade,
  url           text        not null,
  status        text        not null default 'unchecked'
                check (status in ('unchecked', 'live', 'deleted', 'blocked', 'error')),
  http_code     int,
  reason        text,
  first_seen    timestamptz not null default now(),
  last_checked  timestamptz,
  check_count   int         not null default 0,
  unique (story_id, url)
);

create index if not exists idx_citation_checks_status
  on public.citation_checks (status);
create index if not exists idx_citation_checks_last_checked
  on public.citation_checks (last_checked nulls first);
create index if not exists idx_citation_checks_story_id
  on public.citation_checks (story_id);

alter table public.citation_checks enable row level security;
drop policy if exists "citation_checks_read"  on public.citation_checks;
drop policy if exists "citation_checks_write" on public.citation_checks;
create policy "citation_checks_read"  on public.citation_checks
  for select using (true);
create policy "citation_checks_write" on public.citation_checks
  for all using (auth.role() = 'service_role');

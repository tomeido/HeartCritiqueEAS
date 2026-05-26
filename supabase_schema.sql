-- Heart & Critique - Supabase 스키마
-- Supabase 대시보드 → SQL Editor 에 붙여넣고 실행
-- 기존 테이블이 있어도 안전하게 재생성 (FK 순서 주의)

drop table if exists public.votes   cascade;
drop table if exists public.stories cascade;

-- 스토리 테이블
create table public.stories (
  id              uuid        primary key default gen_random_uuid(),
  category        text        not null check (category in ('kindness', 'critique')),
  body            text        not null,
  citations       jsonb       not null default '[]'::jsonb,
  search_queries  jsonb       not null default '[]'::jsonb,
  vote_count      int         not null default 0,
  archived_at     timestamptz,
  arweave_tx_id   text,
  arweave_url     text,
  created_at      timestamptz not null default now()
);

-- 투표 테이블 (유저당 스토리 1표)
create table public.votes (
  id          uuid        primary key default gen_random_uuid(),
  story_id    uuid        not null references public.stories(id) on delete cascade,
  user_id     uuid        not null,
  created_at  timestamptz not null default now(),
  unique (story_id, user_id)
);

-- 인덱스
create index idx_stories_created_at on public.stories (created_at desc);
create index idx_votes_story_id     on public.votes (story_id);
create index idx_votes_user_id      on public.votes (user_id);

-- RLS 활성화
alter table public.stories enable row level security;
alter table public.votes   enable row level security;

-- 스토리: 누구나 읽기, 서비스 롤만 쓰기
create policy "stories_read_all"  on public.stories for select using (true);
create policy "stories_write_svc" on public.stories for all
  using (auth.role() = 'service_role');

-- 투표: 누구나 읽기, 서비스 롤만 쓰기
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
create policy "citation_checks_read"  on public.citation_checks
  for select using (true);
create policy "citation_checks_write" on public.citation_checks
  for all using (auth.role() = 'service_role');

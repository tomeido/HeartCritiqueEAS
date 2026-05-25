-- Heart & Critique - Supabase 스키마
-- Supabase 대시보드 → SQL Editor 에 붙여넣고 실행

-- 스토리 테이블
create table if not exists public.stories (
  id              uuid primary key default gen_random_uuid(),
  category        text not null check (category in ('kindness', 'critique')),
  body            text not null,
  citations       jsonb not null default '[]'::jsonb,
  search_queries  jsonb not null default '[]'::jsonb,
  vote_count      int  not null default 0,
  archived_at     timestamptz,
  arweave_tx_id   text,
  arweave_url     text,
  created_at      timestamptz not null default now()
);

-- 투표 테이블 (유저당 스토리 1표)
create table if not exists public.votes (
  id          uuid primary key default gen_random_uuid(),
  story_id    uuid not null references public.stories(id) on delete cascade,
  user_id     uuid not null,
  created_at  timestamptz not null default now(),
  unique (story_id, user_id)
);

-- 인덱스
create index if not exists idx_stories_created_at on public.stories (created_at desc);
create index if not exists idx_votes_story_id     on public.votes (story_id);
create index if not exists idx_votes_user_id      on public.votes (user_id);

-- RLS: 스토리는 누구나 읽기 가능, 서비스 롤만 쓰기 가능
alter table public.stories enable row level security;
create policy "stories_read_all"  on public.stories for select using (true);
create policy "stories_write_svc" on public.stories for all
  using (auth.role() = 'service_role');

-- RLS: 투표는 누구나 읽기, 서비스 롤만 쓰기 (서버 사이드에서 검증 후 삽입)
alter table public.votes enable row level security;
create policy "votes_read_all"  on public.votes for select using (true);
create policy "votes_write_svc" on public.votes for all
  using (auth.role() = 'service_role');

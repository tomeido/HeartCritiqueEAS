-- 마이그레이션: citation_checks 테이블 추가 (출처 삭제 추적)
-- Supabase SQL Editor 에 붙여넣고 실행. 기존 stories/votes 데이터는 보존됨.

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

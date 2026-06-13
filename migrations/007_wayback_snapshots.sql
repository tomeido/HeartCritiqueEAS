-- 마이그레이션 007: Wayback Machine 위임 박제 큐 (Save Page Now 2).
--
-- 원본이 삭제돼도 중립 제3자(Internet Archive)의 스냅샷은 남는다. 우리가 직접 긁는 대신
-- "이 URL 지금 박제해줘"를 IA 에 위임(services/wayback.py)해 수집 부담·봇탐지를 떠넘기고
-- 법정 인정 타임스탬프 증거를 확보한다. citation/captured URL 을 'queued' 로 적재 → 단일
-- 컨슈머가 capacity(동시 12/익명 6, 일일 한도) 안에서 save 제출 → pending → success.
--
-- idempotent. Supabase SQL Editor 에서 1회 실행.

create table if not exists public.wayback_snapshots (
  id                 uuid        primary key default gen_random_uuid(),
  url                text        not null unique,   -- 원본 글 URL (citation/captured 공용 키)
  job_id             text,                          -- SPN2 작업 id (pending 동안)
  snapshot_url       text,                          -- web.archive.org/web/{ts}/{url} 영속 링크
  snapshot_timestamp text,                          -- YYYYMMDDhhmmss ('timestamp' 예약어 회피)
  status             text        not null default 'queued'
                     check (status in ('queued', 'pending', 'success', 'error', 'skipped')),
  reason             text,
  attempts           int         not null default 0,
  submitted_at       timestamptz,
  updated_at         timestamptz,
  next_poll_at       timestamptz,
  created_at         timestamptz not null default now()
);

create index if not exists idx_wayback_status
  on public.wayback_snapshots (status);
create index if not exists idx_wayback_next_poll
  on public.wayback_snapshots (next_poll_at nulls first);

alter table public.wayback_snapshots enable row level security;
-- 스냅샷 링크는 공개 archive.org URL 이라 읽기 허용(원본 url 도 이미 citations 에 공개).
drop policy if exists "wayback_read_all" on public.wayback_snapshots;
drop policy if exists "wayback_write_svc" on public.wayback_snapshots;
create policy "wayback_read_all"  on public.wayback_snapshots for select using (true);
create policy "wayback_write_svc" on public.wayback_snapshots for all
  using (auth.role() = 'service_role');

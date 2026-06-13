-- Heart & Critique — 2026-06 마이그레이션
-- Supabase SQL Editor 에서 한 번 실행하세요. 모두 idempotent(여러 번 실행해도 안전).
--
-- 해결하는 버그:
--   1) stories.archive_attempts/last_archive_attempt/last_archive_error 컬럼 누락
--      → 박제 재시도 sweeper 가 5분마다 400(42703), 실패한 박제가 자동 재시도되지 않음.
--   2) cleanup 삭제 vote-TOCTOU → 14일+ 0표 글에 삭제 직전 투표가 들어오면 그 투표까지
--      cascade 로 소실(데이터 손실). votes 테이블을 한 스냅샷에서 NOT EXISTS 로 직접
--      확인하는 원자적 삭제 함수로 그 경쟁 창을 닫는다.

-- ── 1) 박제 재시도 추적 컬럼 ────────────────────────────────────────────────
alter table public.stories
  add column if not exists archive_attempts     int not null default 0,
  add column if not exists last_archive_attempt timestamptz,
  add column if not exists last_archive_error   text;

-- ── 1b) 출처 '최초 삭제 감지' 시각 ──────────────────────────────────────────
-- 시계열 삭제 그래프가 매 재검사마다 갱신되는 last_checked 로 드리프트하지 않게,
-- status 가 처음 'deleted' 로 바뀐 시각을 한 번만 기록한다.
alter table public.citation_checks
  add column if not exists deleted_at timestamptz;
-- 기존 'deleted' 행 1회 백필(최초감지 시각 미상 → last_checked 로 근사).
update public.citation_checks
  set deleted_at = last_checked
  where status = 'deleted' and deleted_at is null;

-- ── 2) 미박제 글 원자적 정리 함수 ───────────────────────────────────────────
-- PostgREST 의 캐시 vote_count·.in_() 기반 삭제는 select 와 delete 사이 들어온 투표를
-- 놓쳐 투표받은 글(과 votes 행)을 cascade 로 날릴 수 있다. 이 함수는 votes 테이블을
-- 같은 문장(스냅샷)에서 직접 집계해 그 창을 닫는다.
create or replace function public.delete_orphan_pending_stories(
  p_cutoff    timestamptz,
  p_max_votes int
) returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  n integer;
begin
  with del as (
    delete from public.stories s
    where s.arweave_tx_id is null
      and s.created_at < p_cutoff
      and (select count(*) from public.votes v where v.story_id = s.id) <= p_max_votes
    returning 1
  )
  select count(*) into n from del;
  return n;
end;
$$;

-- service_role(서버 키)만 호출. anon/authenticated 에는 노출하지 않는다.
revoke all on function public.delete_orphan_pending_stories(timestamptz, int) from public, anon, authenticated;
grant execute on function public.delete_orphan_pending_stories(timestamptz, int) to service_role;

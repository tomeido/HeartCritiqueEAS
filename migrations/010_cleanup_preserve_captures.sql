-- 마이그레이션 010: 오래된 미박제 글 정리 시 캡처 승격글(from_capture) 보존.
--
-- 배경: cleanup 은 오래된 미박제(arweave_tx_id IS NULL) 글을 정리한다. 그런데 캡처에서
--   승격된 글(stories.from_capture=true)은 '실제로 삭제된 원본'의 공개 기록이라 미션의 핵심
--   자산이고, 게다가 이를 삭제하면 captured_posts.promoted_story_id 가 사라진 스토리를 가리키는
--   dangling 참조가 되어 재승격도 막힌다(find_promotable 이 promoted_story_id IS NOT NULL 제외).
--   따라서 정리 대상에서 from_capture 글을 영구 제외한다.
--
-- delete_orphan_pending_stories 를 CREATE OR REPLACE 로 갱신(시그니처 동일 → 호출부 무변경).
-- 멱등. Supabase SQL Editor 에서 1회 실행(009 이후).

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
      and coalesce(s.from_capture, false) = false   -- 캡처 승격글(삭제된 원본 기록)은 보존
      and (select count(*) from public.votes v where v.story_id = s.id) <= p_max_votes
    returning 1
  )
  select count(*) into n from del;
  return n;
end;
$$;

revoke all on function public.delete_orphan_pending_stories(timestamptz, int) from public, anon, authenticated;
grant execute on function public.delete_orphan_pending_stories(timestamptz, int) to service_role;

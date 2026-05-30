-- 마이그레이션: 격차 탐지 컬럼 추가 (커뮤니티 vs 언론 보도 격차)

alter table public.stories
  add column if not exists gap_score       text,    -- none/medium/high/extreme
  add column if not exists community_count int,
  add column if not exists news_count      int;

create index if not exists idx_stories_gap_score
  on public.stories (gap_score)
  where gap_score in ('high', 'extreme');

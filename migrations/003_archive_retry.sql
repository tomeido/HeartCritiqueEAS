-- 마이그레이션: 박제 재시도 추적 컬럼 추가.
-- 임계값을 넘겼지만 일시 장애(uploader 다운/타임아웃/잔고부족)로 박제에 실패한 글을
-- 백그라운드 sweeper 가 지수 백오프로 자동 재시도하기 위한 상태 컬럼.
-- 기존 데이터는 보존됨.

alter table public.stories
  add column if not exists archive_attempts     int         not null default 0,
  add column if not exists last_archive_attempt timestamptz,
  add column if not exists last_archive_error   text;

-- sweeper 후보 조회(미박제 글)를 빠르게: arweave_tx_id IS NULL 부분 인덱스
create index if not exists idx_stories_unarchived
  on public.stories (vote_count desc)
  where arweave_tx_id is null;

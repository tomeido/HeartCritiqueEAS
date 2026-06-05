-- 마이그레이션 005: 박제된 글 조회용 부분 인덱스.
-- feed/archived.xml 은 arweave_tx_id IS NOT NULL + order(archived_at desc) 로 조회하고,
-- stats 의 archived count 도 동일 술어를 쓴다. 기존엔 IS NULL(미박제) 부분 인덱스만 있어
-- 박제글 조회는 인덱스를 타지 못했다. 정렬 키(archived_at)에 맞춘 부분 인덱스를 추가한다.
-- 기존 데이터 보존. 재실행 안전(if not exists).

create index if not exists idx_stories_archived
  on public.stories (archived_at desc)
  where arweave_tx_id is not null;

-- 마이그레이션 010: 아카이브 가치 점수(value_score).
--
-- 배경: 캡처 우선순위가 '삭제확률(volatility)' 단일 축이었다. 곧 지워질 글이라도
--   스팸이면 박제할 이유가 없고, 반대로 유일한 1차 증언은 가치가 높다.
--   services/value.py(결정적 가치 스코어러, docs/ARCHIVAL_CRITERIA.md)가 잰
--   '사라지면 아까운 정도'(0~10)를 저장해 캡처·승격 우선순위에 쓴다.
--
-- 원칙(volatility_score 와 동일): 선별·우선순위·UI 배지 전용.
--   생성 게이트·투표 임계값·박제 결정에는 절대 주입하지 않는다.
--
-- idempotent. Supabase SQL Editor 에서 009 이후 1회 실행.

alter table public.captured_posts
  -- services/value.py 의 결정적 아카이브 가치 점수(0~10). 높을수록 사라지면 아까운 글.
  add column if not exists value_score int;

-- 승격 후보 정렬(가치 우선)용 인덱스.
create index if not exists idx_captured_posts_value
  on public.captured_posts (value_score desc nulls last);

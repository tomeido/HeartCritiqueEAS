-- 마이그레이션 012: 승격 스토리에 아카이브 가치 점수(value_score) 승계.
--
-- 배경: collector 가 캡처 시 잰 결정적 아카이브 가치(0~10, services/value.py ·
--   docs/ARCHIVAL_CRITERIA.md)는 captured_posts 에만 있었다(011_value_score). 승격되어 공개된
--   글에서는 '왜 이 글이 남을 가치가 있는가'를 사용자가 볼 수 없다 — 점수를 스토리에
--   복사해 상세 화면의 캡처 기원 알림에 표시한다(가치 체감 + 선별 기준 투명화).
--
-- 원칙(volatility·value 공통): 표시·우선순위 전용.
--   생성 게이트·투표 임계값·박제 결정에는 절대 주입하지 않는다.
--
-- idempotent. Supabase SQL Editor 에서 011_value_score 이후 1회 실행.

alter table public.stories
  -- 승격 시 captured_posts.value_score 를 복사(승격 글 전용, 일반 생성 글은 NULL).
  add column if not exists value_score int;

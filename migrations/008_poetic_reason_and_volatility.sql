-- migrations/008_poetic_reason_and_volatility.sql
-- stories 테이블에 poetic_reason(시적인 요약/박제 사유) 및 volatility_score(휘발성 점수) 컬럼 추가

alter table public.stories
  add column if not exists poetic_reason text,
  add column if not exists volatility_score int;

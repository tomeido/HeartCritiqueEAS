-- 마이그레이션: citation_checks 에 '기준선(baseline)' 컬럼 추가.
-- Supabase SQL Editor 에 붙여넣고 실행. 기존 데이터는 보존됨.
--
-- 목적: 출처 삭제/차단 판정을 '본문 패턴 한 번 매치'(오탐 多)에서
--       '첫 생존 시점 대비 변화'(다른 URL 리다이렉트 / 본문 급감 / 표식 새로 등장)로 전환.
--       기준선은 첫 'live' 확인 때 1회 캡처되고 이후 비교 기준이 된다.
-- 기존 행은 baseline_at 이 NULL → 다음 검사에서 살아있으면 자동으로 기준선을 잡는다.

alter table public.citation_checks
  add column if not exists baseline_final_url text,
  add column if not exists baseline_len       int,
  add column if not exists baseline_del_match boolean not null default false,
  add column if not exists baseline_blk_match boolean not null default false,
  add column if not exists baseline_at        timestamptz;

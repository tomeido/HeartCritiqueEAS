-- Heart & Critique - 완전 초기화 스크립트
--
-- 🚨🚨🚨 경고: 이 파일은 stories / votes / citation_checks 의 모든 데이터를 영구
--    삭제한다(박제 전 글·인간 투표 기록 포함). 최초 설치 또는 의도적 초기화에만 사용.
--    운영 DB 에서는 절대 실행하지 말 것. 실행 전 반드시 백업(Supabase 대시보드의
--    Database > Backups, 또는 pg_dump)을 받을 것.
--
-- 사용법: 이 파일을 실행해 비운 뒤, supabase_schema.sql 을 실행해 재생성한다.

drop table if exists public.citation_checks cascade;
drop table if exists public.votes           cascade;
drop table if exists public.stories         cascade;

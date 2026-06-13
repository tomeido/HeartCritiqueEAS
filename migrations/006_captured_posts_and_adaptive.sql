-- 마이그레이션 006: 선제 수집(captured_posts) + 적응형 삭제 추적 스케줄.
--
-- 배경: 검색(Tavily)으로는 '이미 삭제된 글'을 가져올 수 없다. 그래서 살아있을 때 미리
--   커뮤니티 화제글을 잡아 비공개로 보관(본문+해시)하고, 주기적으로 삭제를 감시한다.
--   (services/collector.py — RSS 발견 → 단발 본문 캡처 → tracker 감지 엔진 재사용)
--
-- 모두 idempotent(여러 번 실행해도 안전). Supabase SQL Editor 에서 1회 실행.

-- ── 1) citation_checks 적응형 스케줄 컬럼 ───────────────────────────────────
-- next_check_at : 다음 재검사 만기. 신규 글은 자주(30분~)·안정적이면 드물게(최대 7일).
-- error_count   : 연속 에러 횟수(지수 백오프 입력).
-- baseline_hash : 첫 live 시 가시 텍스트 sha256 지문(원문 재공개 없는 동일성/존재 증명 +
--                 비트 동일 시 live 확정 단축). compute_next_check()/decide_status() 가 사용.
alter table public.citation_checks
  add column if not exists next_check_at timestamptz,
  add column if not exists error_count   int not null default 0,
  add column if not exists baseline_hash text;

-- 기존 행 백필: 만기를 과거(또는 최초확인 시각)로 둬 다음 루프에서 한 번씩 따라잡게 한다.
update public.citation_checks
  set next_check_at = coalesce(last_checked, first_seen, now())
  where next_check_at is null;

-- due(만기) 우선 조회용 인덱스 (recheck_batch 가 next_check_at 오름차순으로 N건 선택)
create index if not exists idx_citation_checks_next_check
  on public.citation_checks (next_check_at nulls first);

-- ── 2) 선제 수집 글 보관 테이블 (captured_posts) ─────────────────────────────
-- ⚠️ 비공개(service_role 전용). 본문 전체(body_text)를 보관하므로, 공개 API/Arweave 박제
--    로 내보내려면 PII 마스킹·사인(私人) 배제 등 법적 가드레일이 선행돼야 한다(미구현).
--    그래서 stories/citation_checks 와 달리 'read_all' 정책을 두지 않는다.
create table if not exists public.captured_posts (
  id            uuid        primary key default gen_random_uuid(),
  source        text        not null,          -- 출처 도메인 (theqoo.net 등)
  feed          text,                            -- 발견한 RSS 피드 URL
  url           text        not null,            -- 글 원본 URL
  guid          text,                            -- RSS guid (있으면)
  title         text,
  rss_summary   text,                            -- RSS description (정제 스니펫)
  body_text     text,                            -- 첫 캡처 시 가시 텍스트 스냅샷(원본 보존)
  content_hash  text,                            -- sha256(정규화 가시텍스트)
  status        text        not null default 'unchecked'
                check (status in ('unchecked', 'live', 'deleted', 'blocked', 'error')),
  http_code     int,
  reason        text,
  first_seen    timestamptz not null default now(),
  captured_at   timestamptz,                     -- 본문 스냅샷 확보 시각
  last_checked  timestamptz,
  check_count   int         not null default 0,
  error_count   int         not null default 0,
  next_check_at timestamptz,
  deleted_at    timestamptz,                     -- 최초 'deleted' 전환 시각(1회 기록)
  -- 기준선(첫 live 시 캡처): 이후 검사는 이 값 대비 변화로 삭제/차단 판정.
  baseline_final_url text,
  baseline_len       int,
  baseline_hash      text,
  baseline_del_match boolean not null default false,
  baseline_blk_match boolean not null default false,
  baseline_at        timestamptz,
  unique (url)
);

create index if not exists idx_captured_posts_next_check
  on public.captured_posts (next_check_at nulls first);
create index if not exists idx_captured_posts_status
  on public.captured_posts (status);
create index if not exists idx_captured_posts_source
  on public.captured_posts (source);
create index if not exists idx_captured_posts_first_seen
  on public.captured_posts (first_seen desc);

alter table public.captured_posts enable row level security;
-- 읽기 정책 없음 = anon/authenticated 차단. 서버(service_role)만 읽고 쓴다(본문 비공개 보관).
drop policy if exists "captured_posts_read_all" on public.captured_posts;
drop policy if exists "captured_posts_svc"      on public.captured_posts;
create policy "captured_posts_svc" on public.captured_posts for all
  using (auth.role() = 'service_role');

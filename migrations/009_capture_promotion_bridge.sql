-- 마이그레이션 009: 캡처→공개 스토리 승격 다리(promoter) + 삭제확률 점수.
--
-- 배경: collector(006)가 살아있을 때 캡처한 글이 '죽어도' 공개 스토리로 승격되는 경로가
--   없어 dead-end 였다. 이 마이그레이션은 그 다리를 위한 컬럼을 추가한다.
--
-- 안전 원칙(적대적 리뷰 반영):
--   · 자동 승격 트리거는 *hard 삭제*(HTTP 404/410)만. soft(본문패턴/리다이렉트/급감)는
--     오탐 자가정정 가능성이 있어 자동 승격 금지 → hard_deleted_at 로만 구독.
--   · 같은 원본을 두 번 승격하지 않도록 stories.origin_captured_url 에 UNIQUE.
--   · captured_posts 본문(body_text)은 여전히 비공개(006 RLS 유지). 승격은 LLM 익명
--     재작성 + PII 스캐너를 통과한 stories.body 만 공개한다.
--
-- 모두 idempotent. Supabase SQL Editor 에서 006·008 이후 1회 실행.

-- ── 1) captured_posts: 삭제확률 점수 + hard 삭제 시각 + 승격 추적 ───────────────
alter table public.captured_posts
  -- services/volatility.py 의 결정적 삭제확률(0~10). 캡처 우선순위·UI 배지 전용
  -- (생성 게이트·임계값·박제 결정에는 절대 주입하지 않음 — 랭킹/표시용).
  add column if not exists volatility_score int,
  -- HTTP 404/410 로 '확정 삭제'된 최초 시각. 승격 후보 게이트(soft 삭제는 제외).
  add column if not exists hard_deleted_at  timestamptz,
  -- 승격된 공개 스토리 id (1:1). 중복 승격 방지·역참조.
  add column if not exists promoted_story_id uuid,
  -- 승격 파이프라인 상태: null(미처리)/pending_review(수동 검토 대기)/promoted/
  -- blocked_pii(PII 검출로 차단)/skipped(부적합·NO_FIT).
  add column if not exists promotion_status text;

create index if not exists idx_captured_posts_hard_deleted
  on public.captured_posts (hard_deleted_at)
  where hard_deleted_at is not null;
create index if not exists idx_captured_posts_volatility
  on public.captured_posts (volatility_score desc nulls last);
create index if not exists idx_captured_posts_promotion
  on public.captured_posts (promotion_status);

-- ── 2) stories: 캡처 출신 표식 + 원본 URL + 캡처 hard-삭제 시각 ──────────────────
alter table public.stories
  -- 이 스토리가 collector 캡처본에서 승격됐는지(원본은 이미 삭제됐을 수 있음).
  add column if not exists from_capture           boolean not null default false,
  -- 승격의 원본 captured_posts.url (살아있을 때 박아둔 출처, 지금은 죽었을 수 있음).
  add column if not exists origin_captured_url     text,
  -- 캡처 글이 승격 전에 이미 hard 삭제된 시각(박제물의 '삭제 증거' 타임스탬프).
  add column if not exists captured_hard_deleted_at timestamptz;

-- 같은 원본 글을 두 번 공개하지 않도록 멱등 보장(부분 유니크: 일반 스토리는 null 이라 무관).
create unique index if not exists uq_stories_origin_captured_url
  on public.stories (origin_captured_url)
  where origin_captured_url is not null;
create index if not exists idx_stories_from_capture
  on public.stories (from_capture, created_at desc);

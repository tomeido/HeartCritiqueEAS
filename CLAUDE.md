# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Heart & Critique (EAS-free Web2.5 Edition)** — AI 사냥개가 실시간 뉴스를 검색해 따뜻한 선행 또는 대기업 비위 사건을 전달하고, 소셜 로그인한 인간의 투표로 Arweave에 영구 박제하는 Web2.5 타임캡슐 아카이브.

- **LLM**: Groq(Llama+Tavily) 또는 Gemini(Google Search grounding)
- **DB/Auth**: Supabase (OAuth: Google) — Discord 로그인은 제거됨
- **박제**: Irys(Node.js) → Arweave
- **배포**: Docker 홈서버 (FastAPI + uvicorn)

## Development Commands

```bash
# 환경변수 설정
cp .env.example .env
# .env 에서 API 키 입력

# Docker로 실행
docker compose up -d
docker compose logs -f app

# 로컬 개발 (Docker 없이)
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Irys 업로더만 로컬 실행
cd uploader && npm install && node index.js

# API 테스트
curl -X POST http://localhost:8000/api/story
curl http://localhost:8000/api/stories
# A2A JSON-RPC (하위 호환)
curl -X POST http://localhost:8000 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","id":1,"params":{"message":{"parts":[{"text":"하나 들려줘"}]}}}'
```

## Architecture

```
Docker
├── app (Python/FastAPI :8000)
│   ├── main.py               FastAPI 진입점 + JSON-RPC A2A 엔드포인트
│   ├── routers/
│   │   ├── stories.py        POST /api/story, GET /api/stories[/{id}]
│   │   ├── votes.py          POST /api/vote/{id}, GET /api/vote/{id}/status
│   │   └── transparency.py   GET /api/transparency(정책 스냅샷), GET /api/verify/{id}(서명 검증)
│   └── services/
│       ├── llm.py            Groq/Gemini 스토리 생성 파이프라인
│       ├── hunter.py         자동 사냥꾼 — 주기적 스토리 자동 생성 루프
│       ├── collector.py      선제 수집기 — RSS로 화제글 미리 캡처(본문+해시) → 삭제 감시
│       ├── promoter.py       캡처→공개 승격 — hard 삭제된 캡처글을 익명 문학 스토리로 공개(PII 게이트)
│       ├── volatility.py     삭제확률 예측기(결정적) — 캡처 우선순위·UI 배지 랭킹 전용
│       ├── value.py          아카이브 가치 스코어러(결정적) — 캡처/승격 우선순위·스팸 제외(docs/ARCHIVAL_CRITERIA.md)
│       ├── pii.py            구조적 PII 스캐너 — 승격 공개 전 안전 게이트
│       ├── wayback.py        Wayback 위임 박제 — IA Save Page Now 큐(원본 삭제 대비 외부 스냅샷)
│       ├── proxyfetch.py     추적 불가(봇차단) 출처의 프록시 2차 관측 — soft 신호 전용(옵트인)
│       ├── tracker.py        출처/수집글 삭제 추적 + 적응형 재검사 스케줄(compute_next_check)
│       ├── transparency.py   정책·가중치 실시간 스냅샷 + 박제물 서명 검증(docs/TRANSPARENCY.md)
│       ├── db.py             Supabase 클라이언트 싱글톤
│       ├── crypto.py         EC 키 서명·검증 (secp256k1 ECDSA-SHA256)
│       └── archive.py        스토리+투표 번들 → uploader 서비스 호출
├── uploader (Node.js/Irys :3000)
│   └── index.js              POST /upload → Irys → Arweave Tx ID 반환
└── static/index.html         프론트엔드 (Supabase JS + 바닐라 JS)
```

### 주요 흐름

1. **스토리 생성**: `POST /api/story` → `services/llm.generate()` → Supabase `stories` 테이블 저장 → story_id + citations 반환
2. **투표**: `POST /api/vote/{id}` (Bearer JWT 필요) → Supabase `votes` 테이블 insert → vote_count 갱신 → 임계값 도달 시 `services/archive.archive_story()` 백그라운드 실행
3. **박제**: `archive_story()` → EC 서명 → `http://uploader:3000/upload` → Irys → Arweave Tx ID → Supabase 저장
4. **선제 수집(선택)**: `services/collector.py` → 공식 RSS 폴링으로 화제글 발견 → 신규 글만 본문 1회 GET → `captured_posts`(비공개)에 본문+sha256 해시 보관 → `tracker.fetch_observation`/`decide_status` 재사용 + 적응형 주기(`compute_next_check`)로 삭제 감시. 검색이 못 잡는 '삭제된 글'을 살아있을 때 미리 박아두는 경로. `COLLECTOR_ENABLED=false` 기본(외부 폴링이라 `migrations/006` 적용 후 수동 활성화)
5. **캡처→공개 승격(선택, 미션의 핵심)**: `services/promoter.py` → collector 가 살아있을 때 잡아둔 `captured_posts` 가 *실제로 hard 삭제(HTTP 404/410)*되면(`collector.recheck_captured_batch` 가 `hard_deleted_at` 표식) → PII 스캐너(`services/pii.py`)로 본문 검사 → 통과 시 보관해둔 `body_text`를 기존 익명·헤지 프롬프트(`llm.generate_from_text`, 검색 grounding 없이 그 본문만 재작성)로 문학 스토리화 → `stories`에 `from_capture=true`로 INSERT → 죽은 원본 URL을 citation 으로 등록해 tracker 가 'deleted' 표시(단, 임계값 인하는 '목격한' hard — 기준선 캡처 후 사망 — 만 반영하므로 이미 죽은 채 등록되는 승격글 citation 은 표시 전용). **검색은 이미 삭제된 글을 구조적으로 못 주므로, 살아있을 때 잡고(collector)→죽는 걸 감시하고(tracker)→죽은 걸 공개(promoter)하는 경로만이 진짜 사라지는 글을 박제한다.** 안전: 자동 승격은 hard 삭제만(soft 오탐 차단), critique 는 기본 수동 검토(pending_review, 명예훼손 노출 최소화), 원본 raw 본문은 절대 비공개 유지하고 LLM 익명 재작성만 공개. `volatility.py`(결정적 삭제확률)는 캡처 우선순위·UI 배지 랭킹 전용으로만 쓰고 생성 게이트·임계값·박제 결정엔 주입하지 않는다. `PROMOTER_ENABLED=false` 기본(`migrations/009` 적용 + `COLLECTOR_ENABLED=true` 필요). 수동 승격: `POST /api/admin/promote`(`ADMIN_TOKEN` 설정 시).
6. **Wayback 위임(선택)**: citation 등록(tracker)·화제글 캡처(collector) 시 url 을 `wayback_snapshots` 큐에 'queued' 적재 → tracker 루프가 `wayback.process_batch()`로 capacity(IA 동시/일일 한도) 안에서 Save Page Now 제출 → pending → success. 직접 스크래핑 대신 IA 에 위임해 탐지 회피 + 법정 인정 타임스탬프 확보. `/stories/{id}` 응답의 citation 에 `archive_url`(영속 스냅샷) 머지. `WAYBACK_ENABLED=false` 기본(`migrations/007`+IA 키 필요)
7. **가치 선별**: `services/value.py`(결정적 아카이브 가치 0~10, 기준 연구 `docs/ARCHIVAL_CRITERIA.md`) — collector 캡처 우선순위를 `volatility+value` 결합 점수로 정렬하고, 광고·거래 글(hard negative)은 캡처 예산에서 제외. `migrations/011` 적용 시 `captured_posts.value_score` 저장 → promoter 승격 순서(가치 우선)에 반영. volatility 와 동일하게 **박제 결정·임계값엔 절대 미주입**(우선순위·표시 전용).
8. **프록시 관측(선택)**: 봇차단으로 '추적 불가'인 출처(fmkorea 등)를 `services/proxyfetch.py`가 렌더링 프록시(기본 Jina Reader)로 2차 관측. tracker `_process_row` 에서 직접 관측이 error 일 때만 발동. **soft 신호 전용** — 절대 hard(404/410)를 만들지 않아 임계값 인하·자동 승격에 영향 0, 배지·표시만 정확해진다. reason 접두어 `프록시 관측` 이 붙으면 `is_untrackable_source` 가 추적 불가 라벨을 해제. `PROXY_FETCH_ENABLED=false` 기본(출처 URL 이 프록시 사업자에 전달되는 트레이드오프 — `docs/TRANSPARENCY.md`).
9. **투명성·검증**: `GET /api/transparency` — 지금 적용 중인 정책·가중치·게이트 실시간 스냅샷(문서의 '약속' vs 서버의 '현재 상태' 대조용). `GET /api/verify/{story_id}` — Arweave 번들을 게이트웨이(허용 호스트만)에서 받아 ECDSA 서명 검증 + 현재 DB 본문 대조(`crypto.verify_bundle`). citation 응답에 `content_fingerprint`(기준선 sha256) 노출. 신뢰 모델 전체: `docs/TRANSPARENCY.md`.

### A2A JSON-RPC 하위 호환

`POST /` 에서 `message/send` 메서드를 JSON-RPC 2.0으로 처리. 기존 A2A 에이전트와 호환.

## Supabase 설정

1. `supabase_schema.sql` 을 Supabase SQL Editor에서 실행
2. Authentication > Providers 에서 Google OAuth 활성화
3. Authentication > URL Configuration 에서 `http://your-server:8000` 추가

## Environment Variables

| 변수 | 필수 | 설명 |
|---|---|---|
| `SUPABASE_URL` | ✓ | Supabase 프로젝트 URL |
| `SUPABASE_ANON_KEY` | ✓ | 프론트엔드용 anon 키 |
| `SUPABASE_SERVICE_ROLE_KEY` | ✓ | 서버용 서비스 롤 키 |
| `AGENT_PRIVATE_KEY` | ✓ | 에이전트 ETH 개인키 (서명 + Irys 수수료) |
| `GROQ_API_KEY` | Groq 모드 | |
| `TAVILY_API_KEY` | Groq 모드 | |
| `GEMINI_API_KEY` | Gemini 모드 | |
| `IRYS_NETWORK` | | `devnet`(기본/테스트, **약 60일 후 삭제 — 영구 아님**) 또는 `mainnet`(진짜 영구 박제, 소액 ETH 필요). devnet이면 UI가 자동으로 '임시' 라벨 + devnet.irys.xyz 링크 표시 |
| `VOTE_THRESHOLD` | | 박제 트리거 투표수 (기본: 3, `services/threshold.py`의 `DEFAULT_THRESHOLD`가 단일 출처) |
| `STORY_DEDUP_ENABLED` | | 생성 중복 방지 게이트(`services/dedup.py`). **기본 `true`** — 이미 스토리로 만든 출처 URL(citation_checks)을 검색 후보에서 제외해 같은 글의 근사 중복 스토리 방지. DB 조회 실패 시 필터 없이 통과(가용성 우선). 박제 결정·임계값과 무관 |
| `LLM_PROVIDER` | | `groq`(기본) 또는 `gemini` |
| `GEMINI_MAX_ATTEMPTS` / `GEMINI_RETRY_BASE` | | Gemini 일시 오류(5xx/429/네트워크) 지수백오프 재시도 횟수(기본 5)·기준 대기초(기본 1.5). Groq 한도 소진 시 Gemini 가 단일 경로가 되므로 고수요 스파이크를 재시도로 흡수 |
| `COLLECTOR_ENABLED` | | 선제 수집기(`services/collector.py`) 켜기. **기본 `false`** — `migrations/006` 적용 후 `true`. RSS로 화제글을 살아있을 때 미리 잡아 `captured_posts`(비공개)에 본문+해시 보관, 적응형 주기로 삭제 감시 |
| `PROMOTER_ENABLED` | | 캡처→공개 승격(`services/promoter.py`) 켜기. **기본 `false`** — `migrations/009` 적용 + `COLLECTOR_ENABLED=true` 필요. hard 삭제된 캡처글을 PII 게이트·익명 재작성 후 공개 스토리로 승격(검색이 못 잡는 '진짜 사라진 글' 박제). critique 는 기본 수동 검토(`PROMOTER_AUTO_CRITIQUE=true`로 자동화) |
| `PROMOTER_AUTO_CRITIQUE` | | critique(기업 비위) 캡처도 자동 승격할지. **기본 `false`**(명예훼손 노출 최소화 — 수동 검토 큐). |
| `ADMIN_TOKEN` | | 설정 시 `POST /api/admin/promote`(수동 승격, `X-Admin-Token` 헤더) 활성. 미설정이면 엔드포인트 비활성(404). |
| `WAYBACK_ENABLED` | | Wayback 위임 박제(`services/wayback.py`) 켜기. **기본 `false`** — `migrations/007` 적용 + `TRACKER_ENABLED=true` 필요. 원본 삭제 대비 중립 외부 스냅샷을 IA Save Page Now 에 위임 |
| `IA_ACCESS_KEY` / `IA_SECRET_KEY` | Wayback save | IA S3 키(`archive.org/account/s3.php`). 없으면 availability(기존 스냅샷 조회)만 동작, 신규 save 불가 |
| `PROXY_FETCH_ENABLED` | | 추적 불가(봇차단) 출처의 프록시 2차 관측(`services/proxyfetch.py`) 켜기. **기본 `false`**(출처 URL 이 프록시 사업자에 전달됨 — 옵트인). soft 신호 전용이라 임계값·자동 승격엔 영향 없음. `PROXY_FETCH_BASE`(기본 Jina Reader)·`PROXY_FETCH_API_KEY` 로 프록시 교체/인증 |
| `VALUE_W_*` | | 아카이브 가치 스코어러(`services/value.py`) 가중치 튜닝. 현재 적용값은 `GET /api/transparency` 로 공개. `migrations/011` 적용 시 `captured_posts.value_score` 저장, `migrations/012` 적용 시 승격 글(`stories.value_score`)에 승계되어 상세 화면 캡처 기원 알림에 표시(표시 전용 — 박제 결정 미주입) |
| `VERIFY_CACHE_TTL` | | `GET /api/verify/{id}` 결과 프로세스 캐시(초, 기본 600) — 게이트웨이 GET 남용 방지 |
| `STORY_CLEANUP_MAX_VOTES` | | 이 표 수 이하의 오래된 미박제 글만 정리(기본: 임계값-1). 캡처 승격글(`from_capture`)은 어떤 경로로도 정리하지 않음 — RPC 경로는 `migrations/010_cleanup_preserve_captures` 적용 필요 |

## Key Design Decisions

- **한국어 콘텐츠**: LLM 프롬프트, UI, 주석 모두 한국어. 한국 언론 도메인 큐레이션 목록 유지(`services/llm.py`의 `DOMAINS_KINDNESS`, `DOMAINS_CRITIQUE`).
- **소셜 로그인만**: 지갑 연결 불필요. Supabase Auth가 OAuth 처리.
- **sources 무료 공개**: x402 결제 제거. 출처는 생성 즉시 공개. 투표는 "Arweave 영구 박제"를 위한 인간 결단.
- **업로더 분리**: Irys는 공식 Node.js SDK만 지원하므로 별도 컨테이너로 분리.
- `api/index.py`: 기존 Vercel 핸들러 (레거시 보존). 새 기능은 `services/`, `routers/` 에 추가.

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
│   │   └── votes.py          POST /api/vote/{id}, GET /api/vote/{id}/status
│   └── services/
│       ├── llm.py            Groq/Gemini 스토리 생성 파이프라인
│       ├── hunter.py         자동 사냥꾼 — 주기적 스토리 자동 생성 루프
│       ├── collector.py      선제 수집기 — RSS로 화제글 미리 캡처(본문+해시) → 삭제 감시
│       ├── wayback.py        Wayback 위임 박제 — IA Save Page Now 큐(원본 삭제 대비 외부 스냅샷)
│       ├── tracker.py        출처/수집글 삭제 추적 + 적응형 재검사 스케줄(compute_next_check)
│       ├── db.py             Supabase 클라이언트 싱글톤
│       ├── crypto.py         EC 키 서명 (secp256k1 ECDSA-SHA256)
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
5. **Wayback 위임(선택)**: citation 등록(tracker)·화제글 캡처(collector) 시 url 을 `wayback_snapshots` 큐에 'queued' 적재 → tracker 루프가 `wayback.process_batch()`로 capacity(IA 동시/일일 한도) 안에서 Save Page Now 제출 → pending → success. 직접 스크래핑 대신 IA 에 위임해 탐지 회피 + 법정 인정 타임스탬프 확보. `/stories/{id}` 응답의 citation 에 `archive_url`(영속 스냅샷) 머지. `WAYBACK_ENABLED=false` 기본(`migrations/007`+IA 키 필요)

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
| `LLM_PROVIDER` | | `groq`(기본) 또는 `gemini` |
| `COLLECTOR_ENABLED` | | 선제 수집기(`services/collector.py`) 켜기. **기본 `false`** — `migrations/006` 적용 후 `true`. RSS로 화제글을 살아있을 때 미리 잡아 `captured_posts`(비공개)에 본문+해시 보관, 적응형 주기로 삭제 감시 |
| `WAYBACK_ENABLED` | | Wayback 위임 박제(`services/wayback.py`) 켜기. **기본 `false`** — `migrations/007` 적용 + `TRACKER_ENABLED=true` 필요. 원본 삭제 대비 중립 외부 스냅샷을 IA Save Page Now 에 위임 |
| `IA_ACCESS_KEY` / `IA_SECRET_KEY` | Wayback save | IA S3 키(`archive.org/account/s3.php`). 없으면 availability(기존 스냅샷 조회)만 동작, 신규 save 불가 |

## Key Design Decisions

- **한국어 콘텐츠**: LLM 프롬프트, UI, 주석 모두 한국어. 한국 언론 도메인 큐레이션 목록 유지(`services/llm.py`의 `DOMAINS_KINDNESS`, `DOMAINS_CRITIQUE`).
- **소셜 로그인만**: 지갑 연결 불필요. Supabase Auth가 OAuth 처리.
- **sources 무료 공개**: x402 결제 제거. 출처는 생성 즉시 공개. 투표는 "Arweave 영구 박제"를 위한 인간 결단.
- **업로더 분리**: Irys는 공식 Node.js SDK만 지원하므로 별도 컨테이너로 분리.
- `api/index.py`: 기존 Vercel 핸들러 (레거시 보존). 새 기능은 `services/`, `routers/` 에 추가.

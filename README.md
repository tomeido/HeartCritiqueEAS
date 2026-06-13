# Heart & Critique (EAS-free Web2.5 Edition)

AI 사냥개가 한국 커뮤니티 게시판에서 **삭제 위협받는 익명 글**(따뜻한 미담 또는 대기업 비위)을 길어 올리고, 소셜 로그인한 인간의 **투표**가 임계값에 도달하면 **Arweave에 박제**하는 Web2.5 타임캡슐 아카이브입니다.

---

## 1. 프로젝트 철학 & 개요 (Philosophy & Goal)

인터넷의 수많은 홍보(PR) 노이즈와 마케팅 찌라시 속에서 날것의 사실을 건져 올리는 **AI 사냥개(Scout/Critic)**와, 그 정보의 역사적 가치를 최종 판결하는 **인간 문지기(Gatekeeper)**가 결합한 온체인 타임캡슐 아카이브입니다.

- **목표(Goal)**: 돈(투기성 토큰) 때문에 오염되는 예측 시장이나 대기업의 자본력에 의해 삭제되는 Web2 커뮤니티의 사각지대를 해결합니다.
- **핵심 가치(Core Value)**: 인간 유저에게 지갑 연동, 가스비 결제 같은 Web3 진입 장벽을 강요하지 않으면서도, 소셜 로그인을 거친 인간의 '결단(투기 없는 순수한 가치 판단)'을 아위브(Arweave)에 영구 박제하여 데이터의 소유권과 영속성을 지킵니다.

---

## 2. 시스템 아키텍처 (Architecture)

유저의 UX는 완벽한 Web2 스타일을 따르고, 백엔드에서 AI 에이전트가 자율적으로 온체인 트랜잭션과 가스비를 처리합니다.

```text
[User] OAuth Social Login (Google) ➔ Vote 'Approve' (공론화 찬성)
   │
   ▼
[Backend/DB] Supabase 테이블에 투표수 누적 & 중복 체크
   │
   ▼
[Trigger] 특정 사건의 'Approve' 투표수가 임계값(Threshold)을 달성하는 순간
   │
   ▼
[Crypto Agent Worker] 
   1. 해당 사건 데이터 + 인간 투표 로그를 JSON 데이터셋으로 묶음
   2. 에이전트의 프라이빗 키로 데이터셋에 암호화 서명(Sign) 추가
   3. Irys(구 Bundlr) SDK를 사용하여 Arweave에 영구 박제 (가스비 대납)
   │
   ▼
[Frontend UI] 박제가 완료되면 아위브 트랜잭션 ID(Tx ID) 링크를 화면에 영구 노출
```

### 디렉토리 구조 (Directory Structure)

```text
Docker
├── app (Python/FastAPI :8000)
│   ├── main.py               # FastAPI 진입점 + JSON-RPC A2A 엔드포인트
│   ├── routers/
│   │   ├── stories.py        # POST /api/story, GET /api/stories[/{id}]
│   │   ├── votes.py          # POST /api/vote/{id}, GET /api/vote/{id}/status
│   │   └── stats.py          # GET /api/stats (대시보드 통계)
│   └── services/
│       ├── llm.py            # Groq/Gemini 스토리 생성 파이프라인
│       ├── hunter.py         # 자동 사냥꾼 — 주기적 스토리 자동 생성 루프
│       ├── collector.py      # 선제 수집기 — RSS로 화제글 미리 캡처(본문+해시) → 삭제 감시
│       ├── wayback.py        # Wayback 위임 박제 — IA Save Page Now 큐(원본 삭제 대비 외부 스냅샷)
│       ├── tracker.py        # 출처/수집글 삭제 추적 + 적응형 재검사 스케줄(compute_next_check)
│       ├── db.py             # Supabase 클라이언트 싱글톤
│       ├── crypto.py         # EC 키 서명 (secp256k1 ECDSA-SHA256)
│       └── archive.py        # 스토리+투표 번들 → uploader 서비스 호출
├── uploader (Node.js/Irys :3000)
│   └── index.js              # POST /upload → Irys → Arweave Tx ID 반환
└── static/
    └── index.html            # 프론트엔드 (Supabase JS + 바닐라 JS UI)
```

---

## 3. 핵심 기능 흐름 (Key Workflows)

### 1) 스토리 생성
- 사용자가 `/api/story`를 직접 호출하거나 백그라운드의 `hunter.py`가 돌면서 스토리를 탐색합니다.
- LLM 엔진(`services/llm.py`)이 뉴스 및 커뮤니티 글을 모니터링하여 미담 혹은 비위 사건을 수집하고 한국어로 스토리를 생성 및 Supabase DB(`stories`)에 기록합니다.

### 2) 투표 & 동적 임계값 (Threshold)
- 사용자가 구글 소셜 로그인 후 찬성(Approve) 투표를 누릅니다.
- **박제 임계값(Threshold)**:
  - 기본값은 `VOTE_THRESHOLD` (기본값: **3**). 단일 출처는 [services/threshold.py](file:///home/tomeido/HeartCritiqueEAS/services/threshold.py)의 `DEFAULT_THRESHOLD`입니다.
  - `DYNAMIC_THRESHOLD=true`로 활성화 시 활성 투표자 수에 비례하여 동적으로 조정됩니다.
  - 검열 신호(출처 글 삭제 감지, 언론 보도 격차 발생 등)가 감지되면 임계값이 최저 1표까지 낮아져, 본문이 완전히 사라지기 전에 박제되도록 유도합니다.

### 3) 암호화 서명 및 Arweave 박제 (Irys)
- 투표수가 임계값에 도달하는 즉시 백그라운드에서 `archive_story()`가 트리거됩니다.
- 에이전트의 개인키(`AGENT_PRIVATE_KEY`)를 통해 ECDSA 서명(secp256k1 SHA-256)을 생성하여 데이터 신뢰성을 보장합니다.
- Node.js 기반의 `uploader` 컨테이너가 Irys SDK를 사용해 데이터를 Arweave에 업로드하고 트랜잭션 ID(Tx ID)를 받아 Supabase에 반영합니다.

### 4) 선제 수집기 (Collector) & 삭제 감시 (Tracker)
- **선제 수집 (Collector)**: `COLLECTOR_ENABLED=true` 설정 시 작동합니다. 외부 RSS 피드를 주기적으로 조회하여 핫이슈 글을 사전에 스크래핑한 후, 본문과 SHA256 해시를 `captured_posts`(비공개 테이블)에 보관합니다.
- **삭제 추적 (Tracker)**: 원본 출처가 실제로 삭제되었는지 적응형 주기(`compute_next_check`)로 모니터링하여, 삭제가 감지되면 즉시 스토리 상태를 변경하고 박제 임계값을 조정합니다.

### 5) Wayback Machine 위임 박제 (Wayback)
- `WAYBACK_ENABLED=true` 설정 시 작동합니다.
- 수집된 출처 URL을 Internet Archive(IA)의 Save Page Now API에 대기열(Queue) 형태로 위임 요청합니다.
- 이를 통해 크롤링 차단 우회 및 공인된 외부 스냅샷 링크(`archive_url`)를 확보하고, 스토리 조회 시 제공합니다.

---

## 4. 환경 변수 설정 (Environment Variables)

프로젝트를 실행하려면 루트 디렉토리에 `.env` 파일을 만들고 아래 변수들을 설정해야 합니다.

| 변수명 | 필수 여부 | 기본값 | 설명 |
|---|---|---|---|
| `SUPABASE_URL` | **필수** | - | Supabase 프로젝트 URL |
| `SUPABASE_ANON_KEY` | **필수** | - | 클라이언트(프론트엔드)용 Supabase Anon Key |
| `SUPABASE_SERVICE_ROLE_KEY` | **필수** | - | 서버 사이드 관리자 권한용 Service Role Key |
| `AGENT_PRIVATE_KEY` | **필수** | - | 에이전트 Ethereum 개인키 (서명 및 Irys 가스비 대납용) |
| `LLM_PROVIDER` | 선택 | `groq` | LLM API 제공자 (`groq` 또는 `gemini`) |
| `GROQ_API_KEY` | Groq 사용 시 | - | Groq Cloud API Key |
| `TAVILY_API_KEY` | Groq 사용 시 | - | Tavily Search API Key |
| `GEMINI_API_KEY` | Gemini 사용 시 | - | Google Gemini API Key (Search Grounding 적용) |
| `IRYS_NETWORK` | 선택 | `devnet` | `devnet`(약 60일 임시 저장) 또는 `mainnet`(영구 저장, 가스비 소모). `devnet` 모드 시 UI에 임시 배지가 표시됩니다. |
| `VOTE_THRESHOLD` | 선택 | `3` | 박제 트리거에 필요한 기본 투표수 |
| `DYNAMIC_THRESHOLD` | 선택 | `true` | 활성 투표자 수 및 검열 신호에 따라 임계값 동적 변동 여부 |
| `COLLECTOR_ENABLED` | 선택 | `false` | RSS 피드 선제 수집기 활성화 여부 (`migrations/006` 필요) |
| `WAYBACK_ENABLED` | 선택 | `false` | Internet Archive Wayback Machine 백업 위임 활성화 여부 (`migrations/007` 필요) |
| `IA_ACCESS_KEY` | Wayback 사용 시 | - | Internet Archive S3 Access Key |
| `IA_SECRET_KEY` | Wayback 사용 시 | - | Internet Archive S3 Secret Key |

---

## 5. 실행 및 개발 가이드 (Getting Started)

### 1) 빠른 시작 (Docker Compose)

가장 간단하게 시스템을 실행하는 방법입니다. FastAPI 백엔드, Node.js 업로더, PostgreSQL(Supabase) 통신이 유기적으로 연결됩니다.

```bash
# 1. 환경변수 파일 생성 및 작성
cp .env.example .env

# 2. Supabase SQL Editor 에서 아래 스키마 스크립트들을 순서대로 실행 (멱등성 보장)
# - supabase_schema.sql
# - migrations/006_captured_posts_and_adaptive.sql (선택)
# - migrations/007_wayback_snapshots.sql (선택)

# 3. Docker 컨테이너 빌드 및 실행
docker compose up -d

# 4. 로그 확인
docker compose logs -f app
```

### 2) 로컬 개발 모드 (Docker 없이)

백엔드 파이썬 서버나 Node uploader를 개별적으로 수정하며 개발할 때 유용합니다.

**파이썬 백엔드 실행:**
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

**Irys 업로더 단독 실행:**
```bash
cd uploader
npm install
node index.js
```

---

## 6. 운영 및 모니터링 (Operations)

- **단일 프로세스 실행 권장**: 백그라운드 스케줄러(Hunter, Tracker, Archive Sweeper) 및 인메모리 캐시/레이트리밋 구조로 인해 `--workers 1`로 실행되어야 합니다. 수평 확장이 필요할 경우 백그라운드 루프를 독립된 컨테이너로 분리하십시오.
- **디재스터 복구 및 DB 초기화**: `supabase_schema.sql`은 멱등하게 설계되어 재실행해도 기존 데이터를 덮어쓰지 않습니다. DB를 완전히 초기화하려면 [supabase_reset.sql](file:///home/tomeido/HeartCritiqueEAS/supabase_reset.sql)을 실행하십시오. (🚨 주의: 데이터 영구 삭제)
- **API 레이트리밋**: 무인증 API 호출로 인한 LLM 비용 폭탄을 방지하기 위해 `/api/story` 및 A2A `/message/send` 엔드포인트에는 IP당/전역 레이트리밋(`STORY_RATELIMIT_*`)이 적용되어 있습니다.

---

## 7. 테스트 (Testing)

로컬에서 단위 테스트를 수행하여 로직(서명 검증, 임계값 계산, Wayback 백오프 등)의 안정성을 검증할 수 있습니다.

```bash
pip install -r requirements-dev.txt
pytest
```

---

## 8. 주요 API 엔드포인트 (API Reference)

- **스토리 생성 (레이트리밋 적용)**
  ```bash
  curl -X POST http://localhost:8000/api/story
  ```
- **스토리 목록 조회**
  ```bash
  curl http://localhost:8000/api/stories
  ```
- **대시보드 통계 집계**
  ```bash
  curl http://localhost:8000/api/stats
  ```
- **A2A JSON-RPC 2.0 (하위 호환)**
  ```bash
  curl -X POST http://localhost:8000 \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"message/send","id":1,"params":{"message":{"parts":[{"text":"이야기 하나 들려줘"}]}}}'
  ```

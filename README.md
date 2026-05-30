# Heart & Critique (EAS-free Web2.5 Edition)

AI 사냥개가 한국 커뮤니티 게시판에서 **삭제 위협받는 익명 글**(따뜻한 미담 또는 대기업
비위)을 길어 올리고, 소셜 로그인한 인간의 **투표**가 임계값에 도달하면 **Arweave에
박제**하는 Web2.5 타임캡슐 아카이브.

- **LLM/검색**: Groq(Llama)+Tavily 또는 Gemini(Google Search grounding)
- **DB/Auth**: Supabase (OAuth: Google)
- **박제**: Irys → Arweave (devnet=테스트/임시, mainnet=영구)
- **배포**: Docker (FastAPI + uvicorn + Node 업로더)

자세한 아키텍처/설계 결정은 [`CLAUDE.md`](./CLAUDE.md), 철학은 [`CONTEXT.md`](./CONTEXT.md) 참고.

## 빠른 시작

```bash
cp .env.example .env       # API 키·Supabase 값 입력
# Supabase SQL Editor 에서 supabase_schema.sql 실행 (멱등 — 재실행해도 데이터 보존)
docker compose up -d
docker compose logs -f app
```

로컬 개발(Docker 없이):

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

> ⚠️ 레거시 `agent.py` / `api/index.py` 는 사용하지 말 것 — 프로덕션과 다른 코드 경로다.

## 영속성(중요)

`IRYS_NETWORK` 기본값은 **`devnet`(테스트넷)** 이며, 업로드 데이터는 **약 60일 후 삭제되어
영구가 아니다.** devnet 모드에서는 UI가 자동으로 '테스트넷 · 임시' 배지와 `devnet.irys.xyz`
링크를 표시한다. **진짜 영구 박제**가 필요하면 `IRYS_NETWORK=mainnet` 으로 바꾸고 에이전트
지갑에 소액 ETH를 충전해야 한다.

## 박제 임계값

- 기본값은 `VOTE_THRESHOLD`(기본 **3**). 단일 출처는 `services/threshold.py`의
  `DEFAULT_THRESHOLD`.
- `DYNAMIC_THRESHOLD=true`(기본)면 활성 투표자 수에 따라 자동 스케일.
- 검열 신호(출처 삭제 감지·언론 보도 격차)가 강하면 임계값이 내려가 **사라지기 전에**
  박제되도록 한다(최소 1표 유지).

## 운영 메모

- **단일 프로세스 전제**: 백그라운드 루프(hunter/tracker/박제 sweeper)와 인메모리
  캐시·레이트리밋 때문에 `--workers 1` 고정. 수평 확장 시 루프를 별도 컨테이너로 분리.
- **생성 레이트리밋**: `/api/story` 와 A2A `message/send` 는 per-IP + 전역 상한으로 보호
  (`STORY_RATELIMIT_*`, `STORY_MAX_PENDING`). 무인증 호출의 LLM 비용 폭탄 방어.
- **박제 재시도**: 일시 장애로 실패한 박제는 tracker 루프의 sweeper 가 지수 백오프로
  자동 재시도(`ARCHIVE_RETRY_*`).
- **DB 초기화**: `supabase_schema.sql` 은 멱등(데이터 보존). 전체 삭제는
  `supabase_reset.sql`(🚨 데이터 영구 삭제 — 백업 필수).

## 테스트

```bash
pip install -r requirements-dev.txt
pytest            # services/ 의 순수 로직(서명/임계값/sanitize/백오프) 회귀 테스트
```

## API

```bash
curl -X POST http://localhost:8000/api/story          # 스토리 생성(레이트리밋 적용)
curl http://localhost:8000/api/stories                # 목록
curl http://localhost:8000/api/stats                  # 대시보드 집계
# A2A JSON-RPC (하위 호환)
curl -X POST http://localhost:8000 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","id":1,"params":{}}'
```

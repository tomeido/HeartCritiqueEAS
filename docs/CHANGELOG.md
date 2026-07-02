# 변경 로그 (CHANGELOG)

프로젝트의 주요 업데이트를 날짜순(최신 위)으로 기록한다. 각 항목은 "무엇이, 왜, 어떻게"
바뀌었는지와 운영자가 해야 할 조치를 담는다.

---

## 2026-07-03 — 가치 선별 · 프록시 관측 · 투명성 강화

**커밋**: [`c73370d`](https://github.com/tomeido/HeartCritiqueEAS/commit/c73370d) (신규 기능)
→ [`b6e9536`](https://github.com/tomeido/HeartCritiqueEAS/pull/34) (리뷰 하드닝, PR #34)

**목표 4축**: ① 어떤 날것의 글을 박제할지 기준 연구 ② 가치 있는 글 발굴 방법 개선
③ 추적 불가 사이트의 삭제 추적 ④ 프로젝트 전체의 투명성·신뢰성 강화.

### 1단계 — 신규 기능 (`c73370d`)

#### ① 박제 가치 기준 연구
- **`docs/ARCHIVAL_CRITERIA.md` 신설**: 기록학 평가론(셸렌버그의 증거가치/정보가치),
  Archive Team 의 "가치 × 소멸위험" 우선순위, Documenting the Now 윤리 하한을
  한국 커뮤니티 글에 맞게 번역한 기준 문서. value.py 설계의 근거.
- 핵심 결론: **1인칭 직접 경험 + 물증 언급 + 공익 제보**가 1차 사료로서 최고 가치.
  광고·질문·기사 펌글은 사라져도 손실이 아님.

#### ② 가치 있는 글 발굴 (services/value.py)
- **결정적 아카이브 가치 스코어러(0~10) 신설**: 1인칭 경험·물증(녹취/영수증/CCTV)·
  공익 제보(실명+고발)·내부고발·소비자 안전·목격 미담 가산 / 질문·기사 펌글·한 줄 글 감산.
  가중치는 `VALUE_W_*` env 로 튜닝 가능.
- **collector 통합**: 캡처 우선순위를 `삭제확률(volatility) + 가치(value)` 결합 점수로
  정렬. 광고·거래 글(hard negative)은 본문 GET 예산 자체에서 제외(단, "허위 광고에
  당했다" 류 소비자 고발은 피해 정황 교차 검사로 보호).
- **promoter 통합**: hard 삭제 확정 후보는 삭제위험이 이미 실현된 상태이므로
  **가치 우선**으로 승격 순서 변경.
- **`migrations/010_value_score.sql`**: `captured_posts.value_score` 컬럼.
  미적용 환경은 자동 폴백(기존 동작 유지).
- 원칙 유지: 점수는 **우선순위·표시 전용** — 박제 결정·투표 임계값에 절대 미주입.

#### ③ 추적 불가 사이트의 삭제 추적 (services/proxyfetch.py)
- **프록시 2차 관측 채널 신설**: fmkorea 등 봇차단 출처를 렌더링 프록시(기본 Jina
  Reader)로 관측. 직접 관측이 봇차단으로 실패했을 때만 발동.
- **soft 신호 전용 설계**: 절대 hard(404/410)를 만들지 않아 임계값 인하·자동 승격에
  영향 0. 배지·표시만 정확해짐. 프록시 관측 성공 시 "🚫 추적 불가" 라벨 해제.
- 기본 꺼짐(`PROXY_FETCH_ENABLED=false`) — 출처 URL 이 프록시 사업자에 전달되는
  트레이드오프를 문서에 공개(옵트인).

#### ④ 투명성·신뢰성 (services/transparency.py, routers/transparency.py)
- **`GET /api/transparency`**: 서버가 지금 실제 적용 중인 정책·가중치·게이트·활성
  모듈·공개키의 실시간 스냅샷. 문서의 '약속'과 대조해 위반을 드러내는 구조.
- **`GET /api/verify/{story_id}`**: Arweave 번들을 허용 게이트웨이에서 받아 ECDSA
  서명 검증(`crypto.verify_bundle` 신설) + **박제 이후 DB 본문이 몰래 바뀌지 않았는지**
  대조(`body_matches_db`).
- **콘텐츠 지문**: citation 응답에 기준선 sha256(`content_fingerprint`) 노출 —
  원문 재공개 없이 "그 시각에 그 내용이 존재했음"을 제3자가 대조 가능.
- **`docs/TRANSPARENCY.md` 신설**: 신뢰 모델 전체(약속 6개조, 서버 없이 재현하는
  검증 절차, 공개/비공개 데이터 경계, 정직한 한계 5항목).
- 프론트 푸터에 투명성 링크, `/api/stats` 에 프록시 상태 추가.

### 2단계 — 적대적 리뷰 하드닝 (`b6e9536`, PR #34)

멀티에이전트 적대적 리뷰(차원별 파인더 + 발견당 3인 반박 검증)를 발견 0건까지 반복:
**라운드별 7 → 2 → 2 → 0건 수렴**. 확정 결함 13건 수정(전부 실행 재현 후 확정).

#### 정확성
- `RECHECK_QUEUE_FILTER` 에 `http_code.is.null` 분기 — 프록시 soft 삭제(직접 관측
  timeout + 프록시 삭제 판정 = http_code NULL) 행이 재검사 큐에서 영구 이탈해
  **soft 자가정정 불변식**이 깨지던 것 수정.
- `_due_filter` 신설 — 등록 시 스키마 프로브가 일시 실패해 `next_check_at` NULL 로
  저장된 행이 SQL NULL 비교로 due 큐에서 영구 누락(해당 출처의 삭제 추적이 조용히
  꺼짐)되던 것을 "NULL = due" 로 자가 복구.
- `/api/verify` 가 **남의 키로 서명된 번들에 ok=true** 를 주던 구멍 —
  `matches_agent_key` 게이트 + `foreign_signing_key` 사유 추가(위조 번들 차단).
- `_fetch_bundle` 스트리밍 전환 — 5MB 상한이 전체 다운로드 후 검사되어 무의미하던 것.
- value.py: 제목이 `?` 로 끝나는 질문글이 감산을 못 받던 것($ 앵커 문제).

#### 복원력 (스키마 프로브 5곳 통일)
- 일시적 DB 오류를 "마이그레이션 미적용"으로 **영구 캐시하지 않음**
  (`_is_missing_column_error`: 42703/PGRST204 등 컬럼 부재만 영구 캐시).
- `_promotion_cols` 3상화(True/False/None=일시불명) + **불명 시 재검사 주기 skip** —
  프로브 실패 배치에서 감지된 hard 삭제가 `hard_deleted_at` 없이 기록돼 승격 후보가
  비가역 유실되던 것 차단(수집 지연 ~10분 < 영구 유실).
- 적용 지점: `_promotion_cols`·`_value_col`(collector), `_value_col`(promoter),
  `_adaptive_supported`·`_has_deleted_at`(tracker).

#### 보안·기타
- `_redacted_base` 화이트리스트 방식 — 스킴 없는 base(`user:pass@host`)에서 자격증명이
  공개 API(/api/transparency·/api/stats)로 새던 마스킹 우회 수정.
- 프록시 fetch 절대 마감시한(슬로 드립 방어), verify 결과 캐시 상한(500, 메모리 누수 방지).
- `.env.example` 환경변수명 오타(`VALUE_W_P_NEWS_REPOST`), conftest httpx 스텁 보수.

### 검증
- 테스트 **102 → 115개** (회귀 테스트 16개 신규), 역순 실행도 통과(상태 누수 없음).
- 부팅 스모크: FastAPI 라우트 20개 전부 해석 확인.
- postgrest `.or_()` 2회 체이닝(or= 파라미터 추가·AND 결합)과 ISO 타임스탬프 `+` 의
  `%2B` 인코딩을 라이브러리 수준에서 실증.

### 운영자 조치 (배포 시)
1. Supabase SQL Editor 에서 **`migrations/010_value_score.sql` 실행** (미적용이어도
   동작은 하되 가치 우선 정렬이 비활성).
2. 선택: `PROXY_FETCH_ENABLED=true` 로 추적 불가 출처의 프록시 관측 활성화
   (트레이드오프는 `docs/TRANSPARENCY.md` §5 참고).
3. 선택: `VALUE_W_*` 가중치 튜닝 — 현재 적용값은 `GET /api/transparency` 로 확인.
4. `docker compose up -d --build` 재기동.

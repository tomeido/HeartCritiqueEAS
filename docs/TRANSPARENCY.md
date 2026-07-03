# 투명성·신뢰 모델 — 우릴 믿지 말고 검증하라

이 문서는 Heart & Critique 아카이브가 **무엇을 약속하고, 그 약속을 제3자가 어떻게
검증할 수 있는지**를 정의한다. 아카이브의 신뢰는 운영자의 선의가 아니라
(1) 공개된 방법론, (2) 재현 가능한 암호학적 검증, (3) 정직하게 공개된 한계에서 나온다.

## 1. 약속 (What we promise)

| # | 약속 | 강제 지점 |
|---|---|---|
| 1 | **박제는 인간 투표로만 실행된다.** AI 점수(삭제확률·가치)는 우선순위·표시 전용이며 박제 여부를 결정하지 않는다 | `services/threshold.py`, `services/volatility.py`·`value.py` 주석의 미주입 원칙 |
| 2 | **임계값 인하는 '목격한' hard 신호만.** 살아있는 걸 직접 확인(기준선 캡처)한 출처가 그 뒤 HTTP 404/410(삭제)·403(차단)된 경우만 임계값을 낮춘다. 첫 검사부터 죽어 있던 링크·본문 패턴 기반 soft 신호는 배지 표시용 | `threshold.count_citation_signals` (witnessed 게이트) |
| 3 | **자동 승격은 hard 삭제만.** 캡처글의 공개 승격은 404/410 확정 삭제에서만 자동. soft 는 절대 자동화하지 않음 | `promoter.find_promotable` (hard_deleted_at 강제) |
| 4 | **원본 raw 본문은 절대 공개하지 않는다.** 공개되는 것은 LLM 익명·헤지 재작성뿐. 승격 전·후 2회 PII 스캔 | `promoter.promote_one`, `services/pii.py` |
| 5 | **박제물은 서명된다.** 스토리+투표 로그+출처 생존 증거를 canonical JSON 으로 직렬화해 ECDSA-secp256k1-SHA256(low-S)로 서명 후 Arweave 업로드 | `services/crypto.py`, `services/archive.py` |
| 6 | **한계를 숨기지 않는다.** 추적 불가 도메인, devnet 의 비영구성(약 60일), 프록시 관측의 트레이드오프를 UI/API 에 명시 | `/api/transparency`, UI 라벨 |

## 2. 실시간 대조 — `GET /api/transparency`

문서는 '약속'이고 API 스냅샷은 '현재 상태'다. 이 엔드포인트는 서버가 **지금 실제로
적용 중인** 정책·가중치·게이트를 반환한다:

- 임계값 정책(기본값·동적 산출값·활성 투표자 수)
- 활성 모듈(tracker/collector/promoter/wayback/프록시 관측)과 역할
- 승격 게이트(hard-only 트리거, PII 검출 종류, critique 수동 검토 여부)
- 점수 가중치 전체(volatility + value — env 로 튜닝하면 여기 즉시 반영)
- 추적 불가 도메인 목록과 봇차단 코드(정직한 한계 공개)
- 서명 알고리즘·에이전트 공개키·네트워크(devnet 경고 포함)

이 문서의 약속과 스냅샷이 어긋나면 그것이 곧 약속 위반의 증거다.

## 3. 박제물 검증 절차 (재현 가능)

박제 번들 형식(`services/crypto.sign_dataset`):

```json
{
  "payload":   { "story": {...}, "votes": {...}, "evidence": {...}, "archived_at": "...", "version": "heart-critique-archive-v2" },
  "signature": "<hex DER ECDSA>",
  "publicKey": "<hex X9.62 uncompressed>",
  "algorithm": "ECDSA-secp256k1-SHA256"
}
```

### 3-a. 원클릭: 서버측 검증

```
GET /api/verify/{story_id}
```

서버가 Arweave 게이트웨이에서 번들을 받아 서명을 검증하고, **현재 DB 본문과
대조**한다(`body_matches_db`) — 박제 이후 DB 가 몰래 수정되지 않았는지의 양방향
무결성 감사. 결과에 번들/DB 본문의 sha256 이 모두 포함된다.

### 3-b. 서버 없이: 완전 독립 검증

서버를 전혀 신뢰하지 않아도 다음 절차로 재현할 수 있다:

```python
import json, hashlib
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

bundle = json.load(open("bundle.json"))          # Arweave 게이트웨이에서 받은 원본
canonical = json.dumps(bundle["payload"], sort_keys=True,
                       ensure_ascii=False).encode("utf-8")
pub = ec.EllipticCurvePublicKey.from_encoded_point(
    ec.SECP256K1(), bytes.fromhex(bundle["publicKey"]))
pub.verify(bytes.fromhex(bundle["signature"]), canonical,
           ec.ECDSA(hashes.SHA256()))            # 예외 없으면 유효
```

에이전트 공개키는 `GET /api/config`(`agent_public_key`)와 `GET /api/transparency`
에서 확인한다. 프론트엔드의 "🔐 박제 무결성 검증" 카드도 같은 검증을 브라우저에서
수행한다(@noble/curves).

### 3-c. 콘텐츠 지문

`GET /api/stories/{id}` 의 각 출처(citation)에는 `content_fingerprint`(첫 생존 확인
시점 가시 텍스트의 sha256)와 `fingerprint_at` 이 포함된다. 원문을 재공개하지 않고도
"그 시각에 그 내용이 존재했음"을 지문 대조로 증명한다. Wayback 스냅샷(`archive_url`)이
있으면 중립 제3자(Internet Archive)의 타임스탬프 증거로 상호 보강된다.

## 4. 데이터 취급 (공개/비공개 경계)

| 데이터 | 취급 |
|---|---|
| LLM 익명 재작성 스토리, 출처 URL·추적 상태, 콘텐츠 지문 | **공개** |
| 서명된 박제 번들(스토리+투표 로그+출처 생존 증거) | **공개** (Arweave) |
| `captured_posts` 원본 본문(raw) | **비공개** (service_role 전용 RLS) — PII·명예훼손 보호 |
| 수동 검토 큐(pending_review/blocked_pii) 내용 | **비공개** |

## 5. 정직한 한계

- **추적 불가 사이트**: 안티봇(fmkorea 등)은 직접 관측이 불가하며 Wayback 위임도
  동일하게 막힌다. 프록시 관측(`PROXY_FETCH_ENABLED`, 옵트인)은 soft 신호로만
  보강한다 — 프록시 경유 판정은 절대 자동·영구 박제를 앞당기지 않는다.
- **프록시 트레이드오프**: 프록시 사용 시 출처 URL 이 프록시 사업자(기본 Jina)에
  전달된다. 수집 대상이 공개 게시물 URL 뿐이지만, 이 외부 의존을 숨기지 않는다.
- **devnet 은 영구가 아니다**: `IRYS_NETWORK=devnet` 업로드는 약 60일 후 삭제된다.
  UI 가 '임시' 라벨을 표시하고 `/api/transparency` 가 이를 명시한다.
- **점수는 정규식 휴리스틱이다**: volatility/value 점수는 순위용 근사치이며 반어·은어를
  놓친다. 그래서 판정(박제)에 쓰지 않고, 가중치를 공개하며 env 로 교정 가능하게 열어 둔다.
- **투표 로그는 익명이 아니다**: 박제 번들에 투표자 user_id 가 포함된다(인간 합의의
  증거). 투표는 이 공개를 전제로 한 행위다.

## 6. 관련 문서

- [ARCHIVAL_CRITERIA.md](ARCHIVAL_CRITERIA.md) — 무엇을 박제할 가치가 있다고 보는가
- `CLAUDE.md` — 아키텍처·환경변수 전체

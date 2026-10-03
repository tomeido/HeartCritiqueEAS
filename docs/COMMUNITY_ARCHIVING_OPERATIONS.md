# 커뮤니티 원문 보존 운영

현재 대상과 제한 사유는 [수집 사이트](COMMUNITY_SOURCES.md), 설계 근거와 조사 당시 상태는 [조사 문서](KOREAN_COMMUNITY_ARCHIVING_RESEARCH.md)에 있다. 조사 문서는 과거 관측 기록이며 현재 구현 상태는 이 문서를 기준으로 한다.

## 저장 범위

신규 링크는 비공개 `discovery_queue`에 먼저 기록하고 사이트별 요청 예산으로 처리한다. 실패·재시작·목록 이탈에도 URL을 유지한다. `captured_posts`의 최초 성공 본문과 캡처 시각은 덮어쓰지 않는다.

새 캡처에는 본문 텍스트 외에 `data/captures/versions/<URL 해시>/<버전>/`의 HTML 응답 바이트, 지원 이미지, `manifest.json`, 안전하게 읽을 수 있는 `index.html`이 포함된다. 인증 헤더/쿠키를 저장하지 않으며 HTML에서 알려진 인증 필드는 제거하고 그 여부와 원 응답 해시를 manifest에 기록한다. `index.html`은 원문 스크립트를 실행하지 않는 읽기 사본이다. `.bin` 원문을 그대로 서비스하지 않는다.

DB의 `capture_manifest_path`, `capture_manifest_sha256`, `capture_state`가 보존물과 연결된다. 상태 `complete`는 읽은 HTML과 본문에서 발견한 지원 이미지 범위에 해당한다. 이미지 실패·상한 초과·미지원 영상/첨부·HTML 잘림은 `partial`로 남긴다. 기존 텍스트 캡처에 없던 원문 파일과 이미지는 자동 소급 복원하지 않는다.

기본 저장 한도는 10GiB, 디스크 최소 여유는 512MiB, 글당 이미지 12개/총 16MiB, 이미지당 4MiB다. `.env.example`의 `CAPTURE_*` 설정으로 조정한다. 한도에 도달하면 기존 원본을 삭제하지 않고 재시도 오류로 남긴다. 원본을 공개 스토리나 원격 업로드로 자동 내보내지 않는다.

## 검증과 오프라인 읽기

```bash
# 최근 보존물 최대 100개의 원문·이미지·읽기 사본 해시 검증
docker compose exec -T app python scripts/verify_captures.py --limit 100

# DB에 기록된 manifest 해시까지 대조하려면 실제 상대 경로와 해시를 지정
docker compose exec -T app python scripts/verify_captures.py \
  --manifest 'versions/URL_HASH/VERSION/manifest.json' --sha256 'DB_MANIFEST_SHA256'
```

`valid=true`는 저장 무결성 검증이며 글의 사실성 증명이 아니다. `state=partial`은 파일 무결성이 정상이어도 원문 일부를 확보하지 못했음을 뜻한다. 특정 버전 디렉터리를 안전한 로컬 위치에 복사하고 `index.html`을 열면 보존된 텍스트·이미지를 읽을 수 있다. 공개 static 디렉터리로 복사하지 않는다.

## 이미 삭제된 URL의 과거 사본

아래 `https://example.com/post/123`과 시각을 실제 공개 글 URL 및 삭제 관측 UTC 시각으로 바꾼다. 기본 명령은 **검색만** 수행하며 공개·승격·DB 변경은 하지 않는다.

```bash
docker compose exec -T app python scripts/recover_post.py \
  'https://example.com/post/123' --before '2026-09-26T00:00:00Z'

# 표시된 후보 목록의 0번을 다시 조회·검증해 비공개 data/recovered에 저장
docker compose exec -T app python scripts/recover_post.py \
  'https://example.com/post/123' --before '2026-09-26T00:00:00Z' --save 0

docker compose exec -T app python scripts/verify_captures.py --root data/recovered
```

Wayback CDX와 지정 시점에 가까운 Common Crawl 3개 컬렉션을 조회한다. 크롤 주차가 지정 시점 이후인 컬렉션은 제외하고 조회한 이름을 결과에 기록한다. `--commoncrawl-indexes 0`이면 Wayback만, 최대 5개까지 조정할 수 있다. 후보의 URL·시각·HTTP 상태를 검사하고 실제 HTML이 정상 본문으로 판정되어야 저장한다. Common Crawl은 WARC Range·원 URL·원 응답 시각도 검증한다. `errors`에 제공자 장애/거부가 기록되면 ‘원문이 존재하지 않음’으로 단정하지 않는다.

과거 HTML 복구 시 현재 원본 사이트의 이미지를 다운로드해 섞지 않는다. 당시 이미지까지 복구하지 않은 항목은 `partial`이며, snapshot timestamp와 실제 가져온 시각을 따로 남긴다. 과거 HTML 구조가 달라 현재 파서가 본문을 확인하지 못하면 자동 저장을 거부한다. 검색 범위 밖의 더 오래된 사본은 추가 조사 대상이다.

Wayback 자동 큐는 IA 키가 없어도 기존 사본 조회를 처리한다. 새 Save Page Now 요청은 IA 키가 있을 때만 수행한다. 외부 큐의 `success`는 외부 스냅샷 작업/링크 상태이며 로컬 원문 전체 확보를 뜻하지 않는다.

## 스키마와 배포

로컬 SQLite/Turso는 시작 시 기존 DB에 보존물 참조 컬럼을 추가하고 새 대기열을 생성한다. Supabase 사용 시 `migrations/013_discovery_queue.sql`을 먼저 적용한다. 공개 읽기 정책은 추가하지 않는다.

```bash
docker compose build app
docker compose up -d --no-deps app
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/api/sources
```

배포 검증은 health 외에 새 source ID, 실제 첫 폴링 결과, 캡처 파일 검증을 포함해야 한다. `/api/sources` 최근 결과는 메모리이므로 재시작 직후에는 ‘첫 확인 대기’지만 DB 큐는 유지된다.

## 백업과 장기 보존

Turso DB와 WAL을 일관되게 백업하려면 앱을 잠시 중지한 상태에서 **data 디렉터리 전체**를 백업한다. DB만 복사하면 이미지·manifest·게스트 신원이 빠진다. 실행 중 파일을 단순 복사해서 일관된 DB 백업이라고 간주하지 않는다.

원문 파일은 불변 버전이므로 별도 저장장치의 접근 통제된 백업에 복사하고, 복구 위치에서 `verify_captures.py --root ...`를 실행한다. 이번 배포의 사전 백업은 같은 호스트의 롤백용 사본이며 별도 장치 장애 대비 백업과 다르다. 원격 백업 대상은 운영자가 별도로 설정해야 한다.

원문 저장 수명은 로컬 볼륨·백업 유지에 달려 있다. 현재 Irys `devnet` 업로드는 약 60일 테스트 보관이고, 비공개 로컬 원문 저장과 별개다. 이번 수집기 배포는 지갑 입금·유료 mainnet 전환을 포함하지 않는다. Arweave 영구 저장으로 전환할 때는 기존 SDK의 목적 체인과 종료 예정 endpoint를 별도 검증해야 한다.

## 2026-09-26 배포 검증

운영 앱에 배포했고 공개 HTTPS health 및 모바일·데스크톱 수집 목록을 확인했다. 12개 도메인의 15개 목록이 활성화되어 있다. 첫 주기에 246건을 발견해 20건을 보존하고 226건을 영속 대기열에 남겼다. 보존본 20건의 객체 89개가 모두 해시 검증을 통과했으며, 지원 범위를 확보한 `complete` 12건과 미디어 누락·한도가 기록된 `partial` 8건으로 구분된다. 이미지 49개를 저장했다.

별도 비공개 위치로 복사한 두 보존본은 네트워크를 차단한 Chromium에서 이미지 10개가 정상 표시됐고 스크립트·HTTP 요청은 없었다. 배포 이미지 전체 테스트 475개와 추가 과거 복구 회귀 테스트를 통과했다. 기존 캡처 217,072건과 스토리 367건은 교체 직후 유지됐다. 이미지 식별자·중지 상태에서 만든 전체 데이터 백업·검증 집계는 [배포 기록](research/deployment-2026-09-26.json)에 있다.

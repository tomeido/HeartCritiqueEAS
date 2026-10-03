# 한국 커뮤니티 수집 확장과 삭제 글 보존 조사

조사일: 2026-09-26 UTC. 대상: **HeartCritiqueEAS** 작업 폴더와 `127.0.0.1:8000`의 실행 서버. Corporate 프로젝트는 대상에서 제외했다.

**권장 방법은 공개 글을 발견하자마자 원문·이미지를 비공개 보존하고, 이후 삭제 여부를 추적하는 것이다.** 이미 삭제된 글은 기존 캡처, Wayback/Common Crawl의 과거 사본, 작성자가 제공한 자료가 있을 때 복원할 수 있다. 어느 곳에도 사본이 없으면 제목이나 검색 요약으로 원문을 복원했다고 할 수 없다.

이 문서는 현재 코드·운영 상태, 누락 후보의 접근 관측, 구현할 방법을 구분한다. 조사 중 기존 작업 내용을 덮어쓰거나 수집기·DB·배포·유료 업로드 설정을 변경하지 않았다.

## 1. 등록된 사이트와 실제 운영의 차이

| 확인 대상 | 2026-09-26 관측 | 의미 |
|---|---|---|
| 작업 폴더 `services/community_sources.py` | 활성 16개 사이트·17개 목록, 인스티즈 제외 항목 1개 | 이미 작성된 확장 코드가 있다 |
| 실행 컨테이너 `heartcritiqueeas-app-1` | `COMMUNITY_FEEDS` 9개, 8개 사이트 | 작업 폴더의 확장이 실행 서버에 반영되지 않았다 |
| 서버 `/health` | collector/tracker/promoter 등 running | 프로세스가 돌고 있다는 뜻이며 원문 확보 성공률은 아니다 |
| 서버 `/api/sources` | HTTP 404 | 새 사이트 상태 API도 미반영 |
| 서버 `/api/transparency` | collector enabled, feeds=9; Wayback enabled/can_save=true; archive network=devnet | 이미 운영 중인 수집기를 고려해 변경해야 한다 |
| 서버 `/api/stats` | 15초 관측 시간 내 응답 없음 | 실제 사이트별 캡처 성공 건수는 이번에 검증하지 못했다 |

실행 서버의 9개 목록은 뽐뿌 2개, 루리웹 뉴스, MLB파크 불펜, 인벤 뉴스, 클리앙 모두의공원, 보배드림 자유게시판, 더쿠 HOT, 네이트판 랭킹이다. 컨테이너 파일을 AST로 읽어 확인했으며 환경변수나 비밀키를 출력하지 않았다.

작업 폴더에는 여기에 **디시 실시간 베스트·웃긴대학·오늘의유머·SLR클럽·딴지·개드립·82쿡·에펨코리아**가 추가되어 있다. 이들은 새로 처음 구현해야 할 목록과 구별해야 한다. 현재 전체 목록은 [기존 수집 사이트 문서](COMMUNITY_SOURCES.md), 운영/작업 폴더 관측은 [감사 기록 JSON](research/archiving-audit-2026-09-26.json)에 있다.

## 2. 빠져 있는 커뮤니티와 우선순위

아래 순서는 전국 트래픽 순위가 아니라 **기존 수집의 빈틈·프로젝트 목적·접근 가능성**을 함께 고려한 구현 순서다. 사이트 하나의 뉴스/베스트 목록을 읽는 것으로 전체 커뮤니티를 수집한다고 보지 않는다. 카페·대학생 영역도 빠져 있다. 예를 들어 CJ 메조미디어의 2026년 20대 조사(215명)는 네이버 카페·에브리타임 이용을 보여 주지만 전 국민 순위로 일반화할 수 없다. [보고서 원저자 공개본](https://pt.slideshare.net/slideshow/2026_target_report_2029_mezzomeida_20/287237886)

| 우선 | 대상과 발견 URL | 누락/관측 | 추가 방법 |
|---|---|---|---|
| 1 | [인벤 오픈이슈갤러리](https://www.inven.co.kr/board/webzine/2097) | 기존 인벤 항목은 뉴스만. 이 서버에서 공개 목록·표본 글 모두 200, 글 본문 컨테이너 확인 | `inven-openissue` 별도 source, 목록 `a.subject-link`, 본문 `#powerbbsContent`, 제목 `.articleTitle`. URL `/board/webzine/2097/{id}`로 정규화 |
| 1 | [루리웹 유머 베스트](https://bbs.ruliweb.com/best/humor) | 기존 루리웹 항목은 뉴스만. 이 서버에서 목록·표본 글 모두 200, 실제 본문 확인 | `ruliweb-humor` 별도 source. 목록 `#best_body table.board_list_table tr.mode_list td.subject a.subject_link`, 본문 `.board_main_view .view_content`. `/best/board/300143/read/{id}`와 canonical `/community/board/300143/read/{id}`를 같은 글로 통합 |
| 2 | [퀘이사존 자유게시판](https://quasarzone.com/bbs/qb_free), [디미토리 이슈](https://www.dmitory.com/issue), [다모앙 자유게시판](https://damoang.net/free) | 카탈로그 미등록. 웹 열람 도구에서 목록 확인; 운영 서버의 표본 본문까지는 미검증 | 공개 게시판만 후보 등록 후 robots·목록·본문 파서 검증. 퀘이사존의 블라인드 처리 안내를 정상 본문과 구분 |
| 2, 접근 보류 | [아카라이브 공개 베스트 경로](https://arca.live/b/live?mode=best) | 카탈로그 미등록. 이 서버 목록 요청에서 403 챌린지 | 보류 사유 표시. 운영자 허가/제공 피드 등 접근 가능한 경로 확보 후 공개 채널 allowlist 방식으로 추가 |
| 2, 접근 보류 | [인스티즈 인티포털](https://www.instiz.net/pt) | disabled로만 등록. 이번 robots 요청부터 403 챌린지 | 자동 수집 가능하다고 표시하지 않고 보류. 승인된 접근 경로 확보 후 재검증 |
| 2, 조건부 | [이토랜드 유머](https://www.etoland.co.kr/bbs/board.php?bo_table=etohumor06) | 미등록. 이 서버에서는 `/b/etohumor06/list`로 이동해 200 HTML; 다른 웹 관측에서는 보안 안내 | 구형 `wr_id` 파서 가정 금지. robots가 AI/아카이브 봇을 별도로 제한하므로 허용 범위 확인 전 자동 편입 보류 |
| 3, API 발견 | 네이버 카페 공개글 | 카탈로그 미등록. 검색 API는 원문 대신 제목·URL·요약 제공 | 신규 연동은 [NAVER API HUB 카페 검색](https://api.ncloud-docs.com/docs/naver-api-hub-search-cafearticle). 최신순으로 URL 발견 후 공개 원문 별도 캡처 |
| 3, API 발견 | 다음 카페 공개글 | 카탈로그 미등록. `contents`는 글 내용 일부 | [카카오 카페 검색 API](https://developers.kakao.com/docs/ko/daum-search/dev-guide#search-cafe)의 `sort=recency`로 발견하고 공개 원문 접근 가능 여부 별도 판정 |
| 3, 허가 경로 | [블라인드](https://www.teamblind.com/kr/) | 이 서버에서 공개 목록·표본 본문 200. 그러나 [약관 제3조](https://kr.teamblind.com/setting/term)는 명시 허가 없는 크롤링·추출을 제한 | 기술적으로 읽힌다는 이유로 자동 편입하지 않는다. 서비스 허가·제휴 또는 작성자가 제공하는 자기 자료 입력 경로 검토 |
| 별도 입력 | 에브리타임, 회원 전용 카페 게시판 | [에브리타임 공식 앱 설명](https://play.google.com/store/apps/details?hl=ko&id=com.everytime.v2)은 학교별 공간·학교 인증을 명시 | 공개 목록 크롤러와 분리. 작성자 제공본, 허가받은 내보내기 자료를 출처와 함께 수령하는 방식 |

직접 검사는 연구용 User-Agent와 제한된 요청 수로 수행했다. `200`·robots 통과는 운영 허가나 장기 수집 성공률의 보장이 아니다. 차단을 발견한 뒤 프록시·계정·User-Agent 변경으로 재시도하지 않았다. [직접 HTTP 관측 기록](research/community-probe-2026-09-26.json)

**네이버 API 최신 변경:** 기존 개발자센터 검색 API 신규 신청은 2026-07-31 중단되었고 기존 키는 2027-06-30까지 유예된다. 새 연동은 API HUB의 `GET /search/v1/cafearticle`, `X-NCP-APIGW-API-KEY-ID`와 `X-NCP-APIGW-API-KEY` 인증을 기준으로 한다. 예전 무료 쿼터를 신규 서비스 조건으로 가정하지 않는다. [이관 공지](https://developers.naver.com/notice/article/32530), [공식 이관 가이드](https://guide.ncloud-docs.com/docs/apihub-migration)

## 3. 삭제 전에 보존할 구성

```text
공개 RSS·목록·공식 검색 API
  → URL 정규화 → 영속 발견 큐 → 즉시 본문/이미지 캡처
  → 비공개 보존 저장소 + 메타데이터/해시
  → 별도 주기로 삭제 관측
  → 검토·정제 → 공개 스토리 → 인간 투표 → 공개 아카이브

이미 삭제된 URL
  → 내부 캡처 조회 → 과거 외부 스냅샷 조회 → 내용 검증 → 복구본으로 별도 기록
```

### 발견과 첫 캡처

현재 `collector.poll_feeds()`는 전체 목록을 읽은 후 메모리의 후보 중 일부만 캡처한다. 예산에서 밀린 링크는 DB 대기열에 남지 않는다. **발견 즉시 저장하는 큐**를 만들고 캡처 워커가 사이트별 예산으로 소비하도록 바꾼다. 실패한 URL도 목록에서 사라졌다는 이유로 잃지 않게 한다.

권장 큐 필드는 `source_id`, `canonical_url`, `discovered_at`, `priority`, `attempts`, `next_attempt_at`, `last_error`, `state`다. 기존 비공개 `captured_posts`는 실제 본문을 확보한 기록으로 유지한다. SQLite/Supabase 양쪽 스키마와 조회 계약을 함께 반영해야 한다.

기본 설정은 명목 10분 주기당 신규 본문 최대 20건이므로 하루 **약 2,880회 시도 규모**이며, 실제 횟수는 ±10% 주기 지터·처리시간·실패에 따라 달라진다. 17개 목록의 상위 30개를 전부 읽으면 한 번에 최대 510개 후보라 전체 글 수집을 기대할 수 없다. 사이트 추가에 앞서 큐 체류시간과 유입률을 측정한다. 빠른 글은 게시판별 1~3분 목록 확인을 실험할 수 있지만, 사이트별 허용 요청량·`Retry-After`·백오프를 우선하며 모든 사이트에 같은 주기를 적용하지 않는다.

베스트 등재 전에 지워지는 글은 베스트만 봐서는 못 잡는다. 미담/기업 비위/노동·소비자 제보와 관련된 공개 최신글 게시판을 별도 source로 선택하고, 인기 목록과 URL 중복 제거를 공유한다. 번호를 전수 대입하는 방식은 사용하지 않는다.

### 원문·첨부파일 보존

현재 `tracker.fetch_observation()`은 앞 512,000바이트에서 **페이지 전체 가시 텍스트**를 만들며 HTML과 미디어를 버린다. 메뉴·댓글·광고가 본문에 섞일 수 있고 이미지 중심 글은 실질 내용이 빠진다. `body_text` 저장만으로 원본 페이지가 보존됐다고 표시하면 안 된다.

정적 HTML은 기존 SSRF 검증 요청 경로에서 응답을 받아 `warcio`로 WARC 기록을 만들 수 있다. `warcio`는 WARC 읽기/쓰기를 제공하지만 이미지·동영상을 저절로 수집하지는 않는다. 본문 DOM에서 추린 미디어를 크기·시간 예산 안에서 별도 요청하고 성공/누락을 기록한다. [warcio 공식 문서](https://github.com/webrecorder/warcio)

허용된 공개 페이지에 JS 렌더링이 필요한 경우 Browsertrix를 별도 워커로 평가한다. WACZ로 내보낸 후 오프라인 재생을 검사할 수 있고, 작성자 제공 캡처는 ArchiveWeb.page로 만든 WACZ도 입력 후보가 된다. 브라우저가 로그인·챌린지 제한을 해결한다고 가정하지 않는다. [Browsertrix 보존물/내보내기](https://docs.browsertrix.com/user-guide/archived-items/), [ArchiveWeb.page](https://archiveweb.page/)

보존 메타데이터에는 원 URL·최종 URL·발견 시각·실제 캡처 시각·HTTP 코드·본문 추출기 버전·원문 객체의 SHA256·텍스트 SHA256·첨부파일 manifest·잘림/누락 여부를 둔다. 이후 수정본은 새 버전으로 저장하고 최초 원본을 덮어쓰지 않는다. 해시는 저장한 바이트의 동일성 검사용이며 글의 사실성이나 작성자의 진위를 증명하지 않는다.

원본과 미디어는 접근 통제된 파일/객체 저장소에 보관하고 별도 장치에 백업한다. 복원 시 해시 검증과 오프라인 재생을 실제로 확인한다. 인증 쿠키·토큰은 보존물에서 제외하고, HTML 재생은 격리된 환경에서 수행한다. 현재 공개 스토리 생성 경로와 원본 보존 경로는 별개로 유지한다.

### 삭제 관측

기존 `promoter.find_promotable()`은 `hard_deleted_at`이 있는 HTTP 404/410만 자동 승격한다. HTTP 200에서 감지한 삭제 안내·목록 리다이렉트는 soft 신호라 자동 승격되지 않는다. JS alert 삭제 안내는 텍스트 추출에서 유실되어 정상 글로 오인할 수도 있다. 한국 게시판 확장에는 이 차이가 중요하다.

사이트 어댑터에 실제 본문 영역 존재 여부와 삭제/블라인드/로그인 전용 템플릿 판정을 넣는다. 상태를 `live`, `deleted_candidate`, `deleted_confirmed`, `access_blocked`, `login_required`, `temporary_error`처럼 분리하고 관측 이력을 남기는 방식을 권장한다. 403·429·503, CAPTCHA, 검색 결과 누락은 삭제 증거가 아니다. HTTP 200 삭제 템플릿은 시간차 재확인과 기존 원문 대조를 거쳐 검토 큐에 넣으며, 검증 없이 기존 자동 공개 게이트를 완화하지 않는다.

**원문 보존은 삭제 판정과 무관하게 캡처 즉시 완료되어야 한다.** 삭제 감시가 늦어도 이미 보관한 자료는 남는다. 기본 재검사 예산도 10분당 15건이므로, 수집량을 늘릴 때 신규 캡처 예산과 별도로 관리한다.

## 4. 이미 삭제된 글을 찾는 순서

| 순서 | 방법 | 확보한 것으로 인정할 조건 |
|---|---|---|
| 1 | 내부 `captured_posts`·향후 원본 저장소에서 canonical URL 조회 | 실제 본문/객체 존재, 저장 시각·해시 확인 |
| 2 | Wayback Availability로 정확한 URL의 기존 사본 탐색 | 결과의 시각·최종 URL·본문을 확인. `available=true`만으로 원문 복구 성공 처리하지 않음 |
| 3 | Wayback CDX에서 과거 캡처 목록 조회 | 삭제 관측 시점 이전의 후보를 우선하고 원문의 게시판·글 ID·본문을 검증 |
| 4 | Common Crawl URL Index에서 과거 크롤 탐색 | 해당 WARC의 정확한 레코드를 읽고 URL·시각·내용 대조 |
| 5 | 작성자 제공 원본/캡처 또는 출처가 분명한 재게시 자료 | 최초 원본·외부 사본·재게시·스크린샷의 출처를 구별하고 검토 |
| 없음 | 어느 경로에도 원문 사본 없음 | `원문 미확보`로 남김. LLM으로 누락 내용을 만들어 복구본으로 취급하지 않음 |

Wayback Availability는 `GET https://archive.org/wayback/available`에 `url`, 선택적으로 `timestamp`를 전달한다. 가장 가까운 사본이 삭제 이후일 수도 있으므로 시간을 직접 검사해야 한다. [공식 API](https://archive.org/help/wayback_api.php)

여러 시점이 필요하면 `GET https://web.archive.org/cdx/search/cdx`에 `url=<정확한 글 URL>`, `output=json`, `filter=statuscode:200`, `fl=timestamp,original,statuscode,mimetype,digest`, `to=<삭제 관측 UTC 시각>`, `limit=-10`을 전달한다. `200` 필터도 로그인·삭제 안내 HTML을 제거하지 못하므로 실제 내용 검사가 필요하다. [CDX 공식 문서](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server)

Common Crawl은 [컬렉션 목록](https://index.commoncrawl.org/collinfo.json)에서 시기에 맞는 index endpoint를 고르고 정확한 URL을 조회한다. 결과의 `filename`, `offset`, `length`로 `https://data.commoncrawl.org/`의 WARC를 Range 요청한 뒤 WARC 파서로 읽는다. 최근 크롤 하나에 없다고 전체 기록이 없는 것은 아니다. 대규모 도메인 질의는 공개 URL index에 반복 요청하지 말고 별도 배치 작업으로 설계한다. [공식 URL Index](https://index.commoncrawl.org/), [데이터 접근/형식 안내](https://commoncrawl.org/get-started)

**Save Page Now는 현재 시점의 저장 요청이며 삭제 전으로 되돌리는 기능이 아니다.** 원본이 살아 있을 때 보조 백업으로 제출하고, 제출 성공·작업 완료·정상 원문 확보를 구분한다. 현 코드의 `capture_all=1`은 오류 페이지도 저장할 수 있으므로 `success` 상태 하나만 믿으면 안 된다.

이번 요청에는 특정 삭제 URL이 없어 개별 글의 복구 성공 여부나 전체 커뮤니티 복구율은 측정하지 않았다. 위 외부 API 경로는 공식 문서로 확인한 구현 방법이며 모든 커뮤니티 글의 보관을 보장하지 않는다.

## 5. 현재 코드에서 먼저 해결할 문제

| 위치 | 확인한 문제 | 필요한 변경과 검증 |
|---|---|---|
| `collector.recheck_captured_batch()` | 최초 실패 행의 재검사가 live로 바뀌어도 `capture_text=False`라 `body_text`·`captured_at`이 채워지지 않음. 메모리 DB에서 재현 | 본문 없는 행만 재캡처하고 해시·시각·Wayback 큐 갱신. 목록에서 사라진 URL의 재확보 테스트 |
| `collector._capture()` / `tracker.decide_status()` | 첫 관측의 로그인 전용 안내가 live로 인정될 수 있음. script-only 삭제 안내는 텍스트 제거 후 ‘오류’만 원문으로 저장 가능. 직접 판정으로 재현 | 사이트별 본문 적합성 검사. 실제 글에 로그인 위젯이 있어도 거부하지 않는 회귀 검증 |
| `collector.poll_feeds()` | 예산에서 밀린 후보는 메모리에만 있음 | 영속 발견 큐, 멱등성, 재시작 후 복원, 사이트별 공정성·오래된 작업 방치 방지 |
| `wayback.process_batch()` | IA 키가 없으면 `_submit_queued()` 자체를 생략해 문서상 ‘기존 스냅샷 조회 전용’ 경로가 실행되지 않음 | 조회 전용 큐 처리를 제출과 분리. 현재 서버에는 키가 있다고 표시되어 이 문제의 적용 범위는 다른 설정 |
| `tracker.fetch_observation()` | 원본 HTML/미디어 미보존, 페이지 전체 텍스트, 읽기 상한에 의한 잘림 표시 없음 | 원본 객체 + 본문 영역 추출 + `truncated`/첨부 누락 표기 |
| `archive.archive_story()` | 공개 번들은 LLM 스토리·투표·출처 상태이며 원문 객체와 원문 캡처 manifest가 아님 | 필요한 공개 범위의 manifest/증거 연결 추가. 원문 보존 완료와 공개 스토리 박제를 별도 상태로 표시 |

관련 소스: [collector](../services/collector.py), [tracker](../services/tracker.py), [wayback](../services/wayback.py), [promoter](../services/promoter.py), [archive](../services/archive.py).

**장기 보존 설정도 별도 해결해야 한다.** 실행 서버는 `devnet`이며, Irys 공식 문서상 devnet 데이터는 약 60일 보존 후 삭제된다. 또한 최신 Irys 문서는 `uploader.irys.xyz`를 Irys L1용으로 설명하고, 기존 Arweave 업로드 endpoint를 2026-11-01 종료 예정으로 안내한다. 현재 프로젝트는 `@irys/sdk ^0.2.8`을 사용하므로 단순히 `IRYS_NETWORK=mainnet`으로 바꾸거나 SDK를 교체하면 계속 Arweave에 저장된다고 가정해서는 안 된다. 배포에 설치된 SDK가 사용하는 endpoint·목적 체인·영수증·게이트웨이 조회를 검증하고, Arweave가 요구사항이면 지원되는 Arweave 업로드 경로를 선택해야 한다. 이번 조사에서 업로드/송금은 하지 않았다. [네트워크 문서](https://docs.irys.xyz/onchain-storage/mainnet-devnet), [번들러와 종료 공지](https://docs.irys.xyz/onchain-storage/bundlers)

## 6. 구현 순서와 완료 판단

1. **우선 인벤 커뮤니티·루리웹 유머 어댑터를 추가**하고, 기존 확장 8개 사이트의 실제 본문 파서를 함께 검증한다. 각 사이트의 정상·삭제·로그인·챌린지·인코딩·리다이렉트 표본이 필요하다. HTML 구조가 바뀐 사이트는 실패를 성공으로 숨기지 않는다.
2. **첫 캡처 유실과 안내문 오인 문제를 수정**하고 영속 발견 큐를 도입한다. 링크 발견부터 원문 확보까지의 지연, 미확보 큐 길이, 사이트별 성공률을 기록한다.
3. **비공개 원문/이미지 저장을 붙여 실제 오프라인 복원을 확인**한다. 기존 텍스트 캡처의 누락을 복원했다고 표시하지 않는다. DB 백업과 객체 백업을 함께 검증한다.
4. **삭제 관측과 외부 과거 사본 탐색을 분리**한다. 확인한 삭제와 접근 차단을 구별하고 외부 사본에는 원래 캡처 시각과 가져온 시각을 각각 남긴다.
5. **제한이 있는 커뮤니티는 승인된 경로·공식 API·작성자 제공 방식으로 확대**한다. 전체 커뮤니티 수보다 실제 보존할 수 있는 게시판과 캡처 성공률을 먼저 늘린다.
6. **배포 후 새 상태 API, 사이트별 정상 본문 확보, 원문 재생, 삭제 관측 기록, 장기 저장 목적지를 각각 확인**한다. 현재 작업 폴더의 코드와 실행 이미지가 다른 문제도 이 단계에서 해소한다.

기존 관련 테스트 명령:

```bash
python3 -m pytest -q tests/test_community_sources.py tests/test_source_status.py tests/test_collector.py tests/test_tracker_capture.py tests/test_wayback.py
```

이번 실행은 **108 passed, 2 warnings**였다. 기존 테스트 통과는 목록 파서·일부 수집/상태/Wayback 동작에 대한 근거이며, 위에 발견한 미검증 분기나 운영 커뮤니티의 원문 보존 성공을 증명하지 않는다. 이 문서의 제안 기능은 구현 완료 상태가 아니다.

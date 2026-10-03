# 수집 사이트

화면 상단 **수집 사이트**에서 게시판·도메인 검색, 최근 상태, 수집 제외 사유를 확인할 수 있습니다.

2026-09-26 구현 기준 카탈로그는 **23개 사이트·26개 목록**이며, 자동 수집 대상은 **12개 사이트·15개 목록**입니다. 인벤 오픈이슈갤러리·루리웹 유머 베스트·퀘이사존 자유게시판을 추가했습니다. 기존 뉴스 RSS는 별도 목록으로 유지합니다. 접근 차단·robots 제한·허가가 필요한 목록 11개는 자동 요청에서 제외합니다. 이 수는 전체 커뮤니티 게시판의 수집 범위를 뜻하지 않습니다.

목록과 수집기는 `services/community_sources.py`를 공유합니다.

| 사이트 | 게시판 | 방식 | 상태/제외 사유 |
|---|---|---|---|
| 뽐뿌 | [뽐뿌게시판](https://www.ppomppu.co.kr/rss.php?id=ppomppu) | RSS | 수집 대상 |
| 뽐뿌 | [자유게시판](https://www.ppomppu.co.kr/rss.php?id=freeboard) | RSS | 수집 대상 |
| 루리웹 | [뉴스](https://bbs.ruliweb.com/news/rss) | RSS | 수집 대상 |
| 루리웹 | [유머 베스트](https://bbs.ruliweb.com/best/humor) | 공개 HTML | 수집 대상 |
| 엠엘비파크 | [불펜](https://mlbpark.donga.com/mp/rss.php) | RSS | 2026-09-26 robots.txt가 일반 수집기의 접근을 금지하여 수집을 보류했습니다. |
| 인벤 | [뉴스](https://www.inven.co.kr/webzine/news/rss.php) | RSS | 수집 대상 |
| 인벤 | [오픈이슈갤러리](https://www.inven.co.kr/board/webzine/2097) | 공개 HTML | 수집 대상 |
| 클리앙 | [모두의공원](https://www.clien.net/service/board/park) | 공개 HTML | 수집 대상 |
| 보배드림 | [자유게시판](https://bobaedream.co.kr/list?code=freeb) | 공개 HTML | 수집 대상 |
| 더쿠 | [HOT](https://theqoo.net/hot) | 공개 HTML | 수집 대상 |
| 네이트판 | [톡커들의 선택](https://pann.nate.com/talk/ranking) | 공개 HTML | 2026-09-26 robots.txt가 일반 수집기의 접근을 금지하여 수집을 보류했습니다. |
| 디시인사이드 | [실시간 베스트](https://gall.dcinside.com/board/lists/?id=dcbest) | 공개 HTML | 수집 대상 |
| 웃긴대학 | [웃긴자료 일간 베스트](https://web.humoruniv.com/board/humor/list.html?table=pds&st=day) | 공개 HTML | 수집 대상 |
| 오늘의유머 | [베스트오브베스트](https://www.todayhumor.co.kr/board/list.php?table=bestofbest) | 공개 HTML | 수집 대상 |
| SLR클럽 | [자유게시판](https://www.slrclub.com/bbs/zboard.php?id=free) | 공개 HTML | 2026-09-26 robots.txt가 일반 수집기의 게시판 접근을 금지하여 수집을 보류했습니다. |
| 딴지일보 | [자유게시판](https://www.ddanzi.com/free) | 공개 HTML | 2026-09-26 robots.txt가 일반 수집기의 자유게시판 접근을 금지하여 수집을 보류했습니다. |
| 개드립 | [개드립](https://www.dogdrip.net/dogdrip) | 공개 HTML | 수집 대상 |
| 82쿡 | [자유게시판](https://www.82cook.com/entiz/enti.php?bn=15) | 공개 HTML | 수집 대상 |
| 에펨코리아 | [포텐 터짐](https://www.fmkorea.com/best) | 공개 HTML | 2026-09-26 robots.txt는 /best 목록을 허용하지만 정규 원문 /글번호 경로는 일반 수집기에 금지하여 보류했습니다. |
| 퀘이사존 | [자유게시판](https://quasarzone.com/bbs/qb_free) | 공개 HTML | 수집 대상 |
| 디미토리 | [이슈](https://www.dmitory.com/issue) | 공개 HTML | 2026-09-26 공개 목록 HTTP 403 및 robots의 AI 에이전트 제한으로 수집을 보류했습니다. |
| 다모앙 | [자유게시판](https://damoang.net/free) | 공개 HTML | 2026-09-26 공개 목록 HTTP 403 및 robots의 AI 에이전트·무단 수집기 제한으로 수집을 보류했습니다. |
| 아카라이브 | [공개 베스트](https://arca.live/b/live?mode=best) | 공개 HTML | 공개 목록 HTTP 403 보안 챌린지로 보류했습니다. 승인된 접근 경로가 필요합니다. |
| 이토랜드 | [유머](https://etoland.co.kr/b/etohumor06/list) | 공개 HTML | robots의 AI·아카이브 봇 제한과 변경된 글 주소 체계로 운영자 허용 범위 확인 전 수집을 보류했습니다. |
| 블라인드 | [공개 게시판](https://www.teamblind.com/kr/) | 공개 HTML | 약관에서 명시 허가 없는 크롤링·추출을 제한하므로 운영자 허가 전 수집을 보류했습니다. |
| 인스티즈 | [인티포털](https://www.instiz.net/pt) | 공개 HTML | 공개 목록 요청이 HTTP 403 보안 챌린지를 반환하여 자동 수집을 보류했습니다. |

## 실제 동작

- 기본 명목 10분마다 목록당 상위 30개를 발견하고, 한 주기 본문 시도 예산은 20개입니다. 예산에서 밀린 URL도 비공개 `discovery_queue`에 남아 재시작이나 목록 변경 후 재시도됩니다.
- 사이트별 공정 분배와 실패 백오프를 적용합니다. 광고·거래 후보는 기존 가치 필터로 제외합니다.
- 최초 본문 확보 실패 후 정상 응답으로 돌아온 글도 원문을 확보합니다. 최초 성공한 본문·보존물은 덮어쓰지 않습니다.
- 지원 게시판은 본문 영역을 추출하며 로그인·삭제 안내·챌린지·본문 영역 미발견을 정상 캡처로 세지 않습니다. 이미지뿐인 글은 실제 이미지 확보가 필요합니다.
- 새 캡처는 `data/captures`에 HTML·지원 이미지·manifest·오프라인 읽기 사본을 비공개 저장합니다. `captured_posts`에는 텍스트·해시·manifest 참조가 저장됩니다. 이전 텍스트 캡처에 없던 이미지가 자동 복구되는 것은 아닙니다.
- 이미지 미지원 형식·수집 한도·실패·HTML 잘림이 있으면 `partial`로 기록합니다. `complete`도 본문 HTML과 발견한 지원 이미지 범위의 완료이며 동영상·외부 플레이어 전체 재현을 뜻하지 않습니다.
- 저장소가 가득 차면 기존 자료를 지우지 않고 재시도 대기 상태로 남깁니다. 원본은 공개 API나 Arweave 업로드로 자동 노출되지 않습니다.
- 접근 제한으로 비활성화한 도메인은 신규 수집과 수집글 자동 재검사에서 제외하고, 이미 보관한 증거는 유지합니다. 별도 출처 citation 추적과 공개 승격 정책은 기존 기능입니다.

## 상태 읽기

`GET /api/sources`는 DB 조회·외부 요청 없이 카탈로그와 현재 프로세스의 최근 결과를 반환합니다.

- **첫 확인 대기:** 아직 이 프로세스에서 폴링하지 않았습니다. 서버 재시작 시 최근 결과는 초기화되지만 DB 대기열·캡처는 유지됩니다.
- **목록 확인:** 링크를 읽은 결과입니다. 원문 확보 건수는 신규 캡처 수로 구분합니다.
- **링크 미발견 / 접근 차단 / 확인 오류:** 목록 구조·접근 거부·본문 확인·저장 문제를 나타냅니다.
- **수집 제외:** 카탈로그에 표시된 사유로 자동 요청하지 않습니다.
- 최근 주기의 `queue_pending`, `queue_retry`, `queue_oldest_at`은 영속 대기열의 집계입니다. 일시 중단된 출처의 대기 항목도 집계에 남을 수 있습니다.

## 설치와 검증

로컬 DB는 새 `discovery_queue`와 캡처 참조 컬럼을 자동 생성합니다. Supabase는 기존 수집기 마이그레이션과 `migrations/013_discovery_queue.sql`을 적용해야 합니다. Docker의 `./data:/app/data` 영속 볼륨에 DB와 보존물이 함께 저장됩니다.

보존물 검증·삭제 글 복구·백업·배포 절차는 [운영 안내](COMMUNITY_ARCHIVING_OPERATIONS.md)를 참고하세요. 사이트의 접근 정책과 HTML 구조는 바뀔 수 있으므로 화면의 최근 확인 결과를 기준으로 보세요.

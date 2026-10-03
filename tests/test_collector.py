"""선제 수집기 + 적응형 스케줄 회귀 테스트 (순수 함수 위주).

외부 인프라 없이 도는 부분만: compute_next_check(스케줄), decide_status(해시 단축),
_parse_feed(RSS/Atom 파싱). DB/HTTP 가 필요한 경로는 대상에서 제외한다.
"""

import asyncio
import collections
from datetime import datetime, timedelta, timezone

import services.tracker as tracker
import services.collector as collector
from services.localdb import LocalClient


_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


# ── 적응형 스케줄: 신규일수록 자주, 안정적이면 드물게, 에러는 지수 백오프 ──────
def test_compute_next_check_live_grows_and_caps():
    cn = tracker.compute_next_check
    # 첫 live(check_count=1): 최소 주기
    nxt, ec = cn("live", 1, 0, _NOW)
    assert ec == 0
    assert nxt == (_NOW + timedelta(seconds=tracker.TRACK_LIVE_MIN_SEC)).isoformat()
    # 확인 횟수가 쌓이면 기하급수로 증가 (check_count=5 → min*2^4)
    nxt, _ = cn("live", 5, 0, _NOW)
    assert nxt == (_NOW + timedelta(seconds=tracker.TRACK_LIVE_MIN_SEC * 16)).isoformat()
    # 아주 오래 살아남으면 상한(최대 주기)에서 클램프
    nxt, _ = cn("live", 999, 0, _NOW)
    assert nxt == (_NOW + timedelta(seconds=tracker.TRACK_LIVE_MAX_SEC)).isoformat()


def test_compute_next_check_error_backoff_increments():
    cn = tracker.compute_next_check
    nxt, ec = cn("error", 3, 0, _NOW)            # 첫 에러: count 0 → 1, base*2^0
    assert ec == 1
    assert nxt == (_NOW + timedelta(seconds=tracker.TRACK_ERR_BASE_SEC)).isoformat()
    nxt, ec = cn("error", 3, 3, _NOW)            # 4번째 에러: base*2^3
    assert ec == 4
    assert nxt == (_NOW + timedelta(seconds=tracker.TRACK_ERR_BASE_SEC * 8)).isoformat()
    nxt, ec = cn("error", 3, 99, _NOW)           # 폭주 방지 상한
    assert nxt == (_NOW + timedelta(seconds=tracker.TRACK_ERR_MAX_SEC)).isoformat()


def test_compute_next_check_soft_and_live_resets_errors():
    cn = tracker.compute_next_check
    # soft deleted/blocked 는 정정 여지를 위해 중간 주기로 재확인, 에러 카운트 리셋
    nxt, ec = cn("deleted", 9, 5, _NOW)
    assert ec == 0 and nxt == (_NOW + timedelta(seconds=tracker.TRACK_SOFT_SEC)).isoformat()
    # live 로 돌아오면 누적 에러 카운트도 리셋
    _, ec = cn("live", 2, 7, _NOW)
    assert ec == 0


# ── 해시 단축: 비트 동일이면 변화검사 생략하고 live (오탐 불가, live 유지 전용) ─
def test_decide_status_hash_shortcut_keeps_live():
    base = {"captured": True, "final_url": "https://theqoo.net/1", "len": 3000,
            "hash": "deadbeef", "del_match": False, "blk_match": False}
    # 가시 텍스트 지문이 기준선과 동일 → 무조건 live
    same = {"net": "ok", "http_code": 200, "final_url": "https://theqoo.net/1",
            "text_len": 3000, "text_hash": "deadbeef",
            "del_match": False, "blk_match": False, "bot_challenge": False}
    assert tracker.decide_status(same, "https://theqoo.net/1", base)["status"] == "live"


def test_decide_status_hash_differs_still_detects_deletion():
    # 해시가 다르면(=내용 바뀜) 기존 변화 판정으로 정상 진행 — 삭제 표식 새로 등장 → deleted
    base = {"captured": True, "final_url": "https://theqoo.net/1", "len": 3000,
            "hash": "aaaa", "del_match": False, "blk_match": False}
    gone = {"net": "ok", "http_code": 200, "final_url": "https://theqoo.net/1",
            "text_len": 80, "text_hash": "bbbb",
            "del_match": True, "blk_match": False, "del_snip": "삭제된 글",
            "blk_snip": "", "bot_challenge": False}
    assert tracker.decide_status(gone, "https://theqoo.net/1", base)["status"] == "deleted"


def test_new_baseline_carries_hash():
    # 콜드스타트 live 판정 시 기준선에 해시가 실린다(이후 단축·증명에 사용)
    obs = {"net": "ok", "http_code": 200, "final_url": "https://clien.net/9",
           "text_len": 1200, "text_hash": "cafef00d",
           "del_match": False, "blk_match": False, "bot_challenge": False}
    v = tracker.decide_status(obs, "https://clien.net/9", None)
    assert v["status"] == "live"
    assert v["baseline"]["hash"] == "cafef00d"


# ── RSS 2.0 / Atom 피드 파싱 (신규 글 ID 추출) ───────────────────────────────
def test_parse_feed_rss2():
    raw = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>피드</title>'
        '<item><title>글 하나</title>'
        '<link>https://theqoo.net/hot/123</link>'
        '<guid>https://theqoo.net/hot/123</guid>'
        '<description>요약 &lt;b&gt;본문&lt;/b&gt; 입니다</description></item>'
        '<item><title>글 둘</title><link>https://theqoo.net/hot/124</link></item>'
        '</channel></rss>'
    ).encode("utf-8")
    items = collector._parse_feed(raw)
    assert len(items) == 2
    assert items[0]["url"] == "https://theqoo.net/hot/123"
    assert items[0]["title"] == "글 하나"
    # description 의 HTML 태그는 제거된 가시 텍스트로 저장
    assert "본문" in items[0]["summary"] and "<b>" not in items[0]["summary"]
    assert items[1]["url"] == "https://theqoo.net/hot/124"


def test_parse_feed_atom_link_href():
    raw = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>아톰글</title>'
        '<link href="https://bbs.ruliweb.com/news/1"/>'
        '<id>tag:ruliweb,1</id><summary>요약문</summary></entry>'
        '</feed>'
    ).encode("utf-8")
    items = collector._parse_feed(raw)
    assert len(items) == 1
    assert items[0]["url"] == "https://bbs.ruliweb.com/news/1"
    assert items[0]["title"] == "아톰글"


def test_parse_feed_bad_xml_returns_empty():
    assert collector._parse_feed(b"<not xml") == []
    assert collector._parse_feed(b"") == []


def test_parse_clien_html_success():
    raw = (
        '<div>'
        '<div class="list_title">'
        '  <a class="list_subject" href="/service/board/park/12345?category=0">'
        '    <span class="subject_fixed" title="클리앙 제목">클리앙 제목</span>'
        '  </a>'
        '</div>'
        '</div>'
    ).encode("utf-8")
    items = collector._parse_clien_html(raw)
    assert len(items) == 1
    assert items[0]["url"] == "https://www.clien.net/service/board/park/12345"
    assert items[0]["title"] == "클리앙 제목"
    assert items[0]["guid"] == "https://www.clien.net/service/board/park/12345"


def test_parse_bobaedream_html_success():
    raw = (
        '<table>'
        '<tr>'
        '  <td>'
        '    <a class="bsubject" href="/view?code=freeb&No=67890&rtn=blah" title="보배 제목">보배 제목</a>'
        '  </td>'
        '</tr>'
        '</table>'
    ).encode("utf-8")
    items = collector._parse_bobaedream_html(raw)
    assert len(items) == 1
    assert items[0]["url"] == "https://www.bobaedream.co.kr/view?code=freeb&No=67890"
    assert items[0]["title"] == "보배 제목"
    assert items[0]["guid"] == "https://www.bobaedream.co.kr/view?code=freeb&No=67890"


def test_parse_theqoo_html_success():
    """더쿠 HOT 목록(2026-07-04 실측 구조): 일반 글만 추출, 공지(tr.notice)·
    댓글 링크(#fragment)·중복 제외, 엔티티 디코드."""
    raw = (
        '<table>'
        '<tr class="notice  nofn" data-document_srl="111">'
        '  <td class="title"><a href="/hot/111"><strong><span>[공지] 이용 규칙</span></strong></a></td>'
        '</tr>'
        '<tr>'
        '  <td class="no">157585</td>'
        '  <td class="cate"><span>이슈</span></td>'
        '  <td class="title">'
        '    <a href="/hot/4267400742">홍명보, 측근에 &quot;한국 돌아올 생각 없다&quot;</a>'
        '    <i class="fas fa-images"></i>'
        '    <a href="/hot/4267400742#4267400742_comment" class="replyNum">242</a>'
        '  </td>'
        '  <td class="time">02:16</td>'
        '</tr>'
        '<tr>'
        '  <td class="title"><a href="/hot/4267400742">중복 URL 글</a></td>'
        '</tr>'
        '</table>'
    ).encode("utf-8")
    items = collector._parse_theqoo_html(raw)
    assert len(items) == 1                       # 공지 제외 + 중복 제거
    assert items[0]["url"] == "https://theqoo.net/hot/4267400742"
    assert items[0]["title"] == '홍명보, 측근에 "한국 돌아올 생각 없다"'   # 엔티티 디코드
    assert items[0]["guid"] == items[0]["url"]


def test_parse_theqoo_html_garbage_safe():
    assert collector._parse_theqoo_html(b"<not html") == []
    assert collector._parse_theqoo_html(b"") == []


def test_parse_pann_html_success():
    """네이트판 랭킹(2026-07-04 실측 구조): 제목(title 속성)과 본문 미리보기(dd.txt)를
    함께 추출 — 미리보기는 예비 가치·삭제확률 점수용 summary 로 전달된다."""
    raw = (
        '<ul><li>'
        '<dl>'
        '<dt><h2><a href="/talk/375497362"  onclick="vndr(\'BDW03\');" '
        'title="제가 말 안 듣는 며느리라네요^^;;">제가 말 안 듣는 며느리라네요^^;;</a></h2>'
        '<span class="reple-num">(116)</span></dt>'
        '<dd class="txt"><a href="/talk/375497362"  onclick="vndr(\'BDW03\');">'
        'A. 남편과 저는 동갑부부로 어린 자식이 &quot;한 명&quot; 있습니다.</a></dd>'
        '</dl>'
        '</li><li>'
        '<dl>'
        '<dt><h2><a href="/talk/375497362" title="중복 URL 글">중복 URL 글</a></h2></dt>'
        '</dl>'
        '</li></ul>'
    ).encode("utf-8")
    items = collector._parse_pann_html(raw)
    assert len(items) == 1                       # URL 중복 제거
    assert items[0]["url"] == "https://pann.nate.com/talk/375497362"
    assert items[0]["title"] == "제가 말 안 듣는 며느리라네요^^;;"
    assert items[0]["summary"] == 'A. 남편과 저는 동갑부부로 어린 자식이 "한 명" 있습니다.'
    assert items[0]["guid"] == items[0]["url"]


def test_parse_pann_html_no_summary_ok():
    raw = ('<dt><h2><a href="/talk/99" title="미리보기 없는 글">미리보기 없는 글</a></h2></dt>'
           ).encode("utf-8")
    items = collector._parse_pann_html(raw)
    assert len(items) == 1
    assert items[0]["summary"] is None


def test_parse_pann_html_garbage_safe():
    assert collector._parse_pann_html(b"<not html") == []
    assert collector._parse_pann_html(b"") == []


# ── poll_feeds 공정 분배: 한 피드가 주기 예산을 독식하지 못하고 라운드로빈으로 분산 ──
def _setup_poll(monkeypatch, feeds, items_per_feed, budget):
    """poll_feeds 의 IO(피드 fetch·파싱·중복조회·캡처·지터)를 메모리 stub 으로 격리."""
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", feeds)
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", budget)
    db = LocalClient(":memory:", engine="sqlite")
    monkeypatch.setattr(collector, "get_db", lambda: db)

    async def _no_sleep():
        return None
    monkeypatch.setattr(collector, "_sleep_jitter", _no_sleep)

    async def _fetch(url, client):
        return (url.encode(), 200)            # raw = 피드 url (피드별 구분자)
    monkeypatch.setattr(collector, "_fetch_feed", _fetch)

    def _parse(raw):
        base = raw.decode()
        n = items_per_feed[base]
        return [{"url": f"{base}/p{i}", "title": "t", "guid": f"{base}/p{i}", "summary": None}
                for i in range(n)]
    monkeypatch.setattr(collector, "_parse_feed", _parse)
    monkeypatch.setattr(collector, "_existing_urls", lambda db, urls: set())

    got = collections.Counter()

    async def _cap(db, source, feed_url, item, client):
        got[feed_url] += 1
        return True
    monkeypatch.setattr(collector, "_capture", _cap)
    return got


def test_poll_feeds_round_robin_even(monkeypatch):
    feeds = [("a", "fa"), ("b", "fb"), ("c", "fc")]
    got = _setup_poll(monkeypatch, feeds, {"fa": 10, "fb": 10, "fc": 10}, budget=6)
    res = asyncio.run(collector.poll_feeds(client=None))
    assert res["captured"] == 6 and res["discovered"] == 30
    # 한 피드 독식 없이 균등 분배(6/3 = 각 2)
    assert dict(got) == {"fa": 2, "fb": 2, "fc": 2}


def test_poll_feeds_redistributes_leftover(monkeypatch):
    # 신규가 적은 피드(a=1)는 자기 몫만 쓰고, 남은 예산은 다른 피드가 채운다(낭비 없음).
    feeds = [("a", "fa"), ("b", "fb"), ("c", "fc")]
    got = _setup_poll(monkeypatch, feeds, {"fa": 1, "fb": 10, "fc": 10}, budget=6)
    res = asyncio.run(collector.poll_feeds(client=None))
    assert res["captured"] == 6
    assert got["fa"] == 1 and got["fb"] + got["fc"] == 5 and abs(got["fb"] - got["fc"]) <= 1


def test_poll_feeds_skips_hard_negative_ads(monkeypatch):
    """광고·거래 글(hard negative)은 본문 GET 예산 자체를 쓰지 않는다
    (docs/ARCHIVAL_CRITERIA.md §3). discovered 에는 잡히되 캡처에서 제외."""
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", [("a", "fa")])
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", 10)
    db = LocalClient(":memory:", engine="sqlite")
    monkeypatch.setattr(collector, "get_db", lambda: db)

    async def _no_sleep():
        return None
    monkeypatch.setattr(collector, "_sleep_jitter", _no_sleep)

    async def _fetch(url, client):
        return (b"x", 200)
    monkeypatch.setattr(collector, "_fetch_feed", _fetch)

    items = [
        {"url": "u-ad", "title": "노트북 팝니다 (쿠폰 드려요, 문의는 카톡)",
         "guid": "u-ad", "summary": None},
        {"url": "u-keep", "title": "대기업 갑질 제보합니다", "guid": "u-keep", "summary": None},
    ]
    monkeypatch.setattr(collector, "_parse_feed", lambda raw: items)
    monkeypatch.setattr(collector, "_existing_urls", lambda db, urls: set())

    captured = []

    async def _cap(db, source, feed_url, item, client):
        captured.append(item["url"])
        return True
    monkeypatch.setattr(collector, "_capture", _cap)

    res = asyncio.run(collector.poll_feeds(client=None))
    assert res["discovered"] == 2
    assert res["skipped_ads"] == 1
    assert captured == ["u-keep"]   # 광고는 캡처 예산을 쓰지 않는다


class _ProbeDB:
    """스키마 프로브(select 1행) 스텁 — err 가 있으면 그 예외를 던진다."""
    def __init__(self, err=None):
        self.err = err

    def table(self, name):
        return self

    def select(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.err:
            raise self.err
        return type("R", (), {"data": []})()


def test_promotion_cols_transient_error_not_cached(monkeypatch):
    """일시적 연결오류를 '009 미설치'로 영구 캐시하면 그 동안 hard 삭제가 감지돼도
    hard_deleted_at 미기록 → 그 행은 큐에서 영구 제외 → 승격 후보 조용한 유실.
    일시 오류는 캐시 없이 다음 호출이 재판별해 자가 복구돼야 한다."""
    monkeypatch.setattr(collector, "_promo_cols_supported", None)
    assert collector._promotion_cols(_ProbeDB(ConnectionError("reset"))) is False
    assert collector._promo_cols_supported is None      # 캐시 안 됨
    assert collector._promotion_cols(_ProbeDB()) is True  # DB 복구 시 즉시 재활성


def test_promotion_cols_missing_column_cached(monkeypatch):
    monkeypatch.setattr(collector, "_promo_cols_supported", None)
    err = Exception("column captured_posts.promotion_status does not exist (42703)")
    assert collector._promotion_cols(_ProbeDB(err)) is False
    assert collector._promo_cols_supported is False     # 컬럼 부재는 영구 캐시(정상)


def test_value_col_transient_error_not_cached(monkeypatch):
    monkeypatch.setattr(collector, "_value_col_supported", None)
    assert collector._value_col(_ProbeDB(ConnectionError("reset"))) is False
    assert collector._value_col_supported is None
    assert collector._value_col(_ProbeDB()) is True


def test_promotion_cols_state_tri_state(monkeypatch):
    """일시 오류는 None(미확정), 컬럼 부재는 False(확정 캐시), 성공은 True."""
    monkeypatch.setattr(collector, "_promo_cols_supported", None)
    assert collector._promotion_cols_state(_ProbeDB(ConnectionError("reset"))) is None
    assert collector._promo_cols_supported is None
    assert collector._promotion_cols_state(_ProbeDB()) is True


def test_recheck_skips_cycle_on_transient_probe_failure(monkeypatch):
    """프로브 일시 불명 상태로 배치를 진행하면 그 배치의 hard 삭제가 hard_deleted_at 없이
    기록돼 승격 후보가 비가역 유실된다 — 이번 주기를 통째로 건너뛰어야 한다."""
    monkeypatch.setattr(collector, "_promo_cols_supported", None)
    monkeypatch.setattr(collector, "get_db", lambda: _ProbeDB(ConnectionError("reset")))
    assert asyncio.run(collector.recheck_captured_batch()) == 0


# ── 공용 _build_update 계약: collector.recheck_captured_batch 가 이걸 재사용한다 ──
def test_build_update_adaptive_contract():
    res = {
        "status": "live", "http_code": 200, "reason": None,
        "baseline": {"final_url": "https://clien.net/9", "len": 1200,
                     "hash": "abc", "del_match": False, "blk_match": False},
    }
    row = {"check_count": 0, "error_count": 0}
    upd = tracker._build_update(res, row, _NOW.isoformat(), adaptive=True, now=_NOW)
    # 기본 + 기준선 + 적응형 스케줄이 한 payload 로 생성된다(collector 가 의존하는 필드들)
    assert upd["status"] == "live" and upd["check_count"] == 1
    assert upd["baseline_final_url"] == "https://clien.net/9"
    assert upd["baseline_hash"] == "abc"
    assert upd["error_count"] == 0
    assert upd["next_check_at"] == (_NOW + timedelta(seconds=tracker.TRACK_LIVE_MIN_SEC)).isoformat()
    # deleted_at·newly_deleted 는 _build_update 가 다루지 않는다(호출부 책임)
    assert "deleted_at" not in upd
    # adaptive=False(migrations/006 미적용 폴백)면 스케줄/해시 컬럼은 빠진다
    upd2 = tracker._build_update(res, row, _NOW.isoformat(), adaptive=False)
    assert "next_check_at" not in upd2 and "baseline_hash" not in upd2
    assert upd2["baseline_final_url"] == "https://clien.net/9"

"""선제 수집기 + 적응형 스케줄 회귀 테스트 (순수 함수 위주).

외부 인프라 없이 도는 부분만: compute_next_check(스케줄), decide_status(해시 단축),
_parse_feed(RSS/Atom 파싱). DB/HTTP 가 필요한 경로는 대상에서 제외한다.
"""

import asyncio
import collections
from datetime import datetime, timedelta, timezone

import services.tracker as tracker
import services.collector as collector


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


# ── poll_feeds 공정 분배: 한 피드가 주기 예산을 독식하지 못하고 라운드로빈으로 분산 ──
def _setup_poll(monkeypatch, feeds, items_per_feed, budget):
    """poll_feeds 의 IO(피드 fetch·파싱·중복조회·캡처·지터)를 메모리 stub 으로 격리."""
    monkeypatch.setattr(collector, "COMMUNITY_FEEDS", feeds)
    monkeypatch.setattr(collector, "COLLECTOR_MAX_CAPTURE_PER_CYCLE", budget)
    monkeypatch.setattr(collector, "get_db", lambda: object())

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

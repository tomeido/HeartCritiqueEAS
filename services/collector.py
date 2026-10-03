"""커뮤니티 화제글 선제 수집기 (Proactive Collector).

문제: 사냥개는 Tavily 검색으로 글을 '발견'하는데, 이미 삭제된 글은 검색 인덱스에
없어 구조적으로 못 가져온다. 빨리 지워지는 글(=대기업 비위처럼 삭제가 핵심인 글)은
사람이 투표하기 전에 증발한다.

해법(이 모듈): 살아있을 때 미리 화제글을 잡아 비공개로 보관(본문+해시)하고,
tracker 의 감지 엔진을 그대로 재사용해 주기적으로 삭제를 감시한다.

봇탐지 최소화 원칙(직접 긁는 양을 최소화):
  · 공식 RSS 또는 공개 HTML 목록을 폴링한다. 수집 대상·보류 사유는
    services.community_sources 의 카탈로그에서 한 번에 관리한다.
  · 신규 URL 을 DB 대기열에 먼저 저장하고, 실패는 지수 백오프로 재시도한다.
  · 첫 정상 본문·HTML·미디어를 보존하며 기존 원본은 덮어쓰지 않는다.
  · 요청 사이에 지터, 봇차단 코드(403/429/430/503)엔 그 출처를 이번 주기 건너뜀.
  · SSRF 방어·EUC-KR 디코딩·삭제 판정은 services.tracker 의 검증된 함수를 재사용.

⚠️ captured_posts 는 비공개(service_role 전용, migrations/006). 본문 전체를 보관하므로
   공개 API/Arweave 박제로 내보내려면 PII 마스킹·사인 배제 등 법적 가드레일이 선행돼야
   한다(공개 승격은 services.promoter 의 별도 가드레일을 통과해야 한다).
"""

import asyncio
import html as html_mod
import os
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from services.community_sources import COMMUNITY_SOURCES, parse_html_source
from services.db import get_db
from services import discovery
from services.tracker import (
    BOT_BLOCK_CODES,
    MAX_REDIRECTS,
    RECHECK_QUEUE_FILTER,
    USER_AGENT,
    _baseline_from_row,
    _build_update,
    _due_filter,
    _is_missing_column_error,
    _visible_text,
    compute_next_check,
    decide_status,
    fetch_observation,
)
from services.value import assess_value
from services.volatility import predict_volatility
from services.wayback import enqueue as wayback_enqueue

import logging
logger = logging.getLogger(__name__)


# migrations/009(승격 다리) 컬럼 지원 여부 — volatility_score·hard_deleted_at·
# promotion_status. 미설치(009 미적용) 환경에서는 이 컬럼들을 payload 에서 빼 400 을 피한다.
_promo_cols_supported: Optional[bool] = None


def _promotion_cols_state(db) -> Optional[bool]:
    """009 승격 컬럼 지원 3상 판별: True/False = 확정(캐시), None = 일시 오류로 미확정.

    컬럼 부재(009 미적용)일 때만 영구 캐시. 일시적 연결오류를 False 로 굳히면 그 동안
    hard 삭제(404/410)를 감지해도 hard_deleted_at 을 못 박고, 그 행은 큐에서 영구
    제외되므로 승격 후보가 조용히 유실된다 — 미션의 핵심 경로라 일시 오류는 None 으로
    보고해 호출부(recheck_captured_batch)가 비가역 기록을 미루고 주기를 건너뛰게 한다."""
    global _promo_cols_supported
    if _promo_cols_supported is None:
        try:
            (db.table("captured_posts")
             .select("volatility_score,hard_deleted_at,promotion_status").limit(1).execute())
            _promo_cols_supported = True
        except Exception as e:
            if _is_missing_column_error(e, "promotion_status"):
                _promo_cols_supported = False
                logger.info("[collector] captured_posts 승격 컬럼(009) 미설치 — 삭제확률/hard삭제 "
                            "기록 생략. migrations/009 적용 후 promoter 가능.")
            else:
                logger.warning(f"[collector] 승격 컬럼 판별 일시 실패(캐시 안 함): {e!r}")
                return None
    return _promo_cols_supported


def _promotion_cols(db) -> bool:
    """불리언 간편 래퍼(_capture 용 — 일시 불명은 '이번엔 기록 생략'과 동치).
    _capture 경로는 hard 삭제 시 본문도 없어(404) 승격 자체가 불가하므로 비가역 유실 없음."""
    return bool(_promotion_cols_state(db))


# migrations/011(가치 점수) 컬럼 지원 여부 — value_score. 미설치 환경에서는 payload 에서
# 빼 400 을 피한다(_promotion_cols 와 동일 패턴).
_value_col_supported: Optional[bool] = None


def _value_col(db) -> bool:
    global _value_col_supported
    if _value_col_supported is None:
        try:
            db.table("captured_posts").select("value_score").limit(1).execute()
            _value_col_supported = True
        except Exception as e:
            # 컬럼 부재(010 미적용)일 때만 영구 캐시. 일시적 연결오류를 False 로 굳히면
            # 재시작 전까지 value_score 기록이 조용히 꺼진다(routers/stories 와 동일 교훈)
            # — 일시 오류는 캐시하지 않고 이번 호출만 생략, 다음 호출이 재판별한다.
            if _is_missing_column_error(e, "value_score"):
                _value_col_supported = False
                logger.info("[collector] captured_posts.value_score(010) 미설치 — 가치 점수 "
                            "기록 생략. migrations/011 적용 시 승격 우선순위에 반영.")
            else:
                logger.warning(f"[collector] value_score 판별 일시 실패(캐시 안 함): {e!r}")
                return False
    return _value_col_supported

# 기본 비활성: migrations/006 적용 후 COLLECTOR_ENABLED=true 로 명시적으로 켠다(외부 폴링 시작).
COLLECTOR_ENABLED = os.environ.get("COLLECTOR_ENABLED", "false").lower() == "true"
COLLECTOR_INTERVAL_SEC = int(os.environ.get("COLLECTOR_INTERVAL_SEC", "600"))   # 피드 폴링 주기 10분
COLLECTOR_INITIAL_DELAY_SEC = int(os.environ.get("COLLECTOR_INITIAL_DELAY_SEC", "90"))
# 한 주기당 새 본문 캡처 상한(백프레셔·정중함). 발견이 많아도 본문 GET 은 이 수로 제한.
COLLECTOR_MAX_CAPTURE_PER_CYCLE = int(os.environ.get("COLLECTOR_MAX_CAPTURE_PER_CYCLE", "20"))
COLLECTOR_RECHECK_BATCH = int(os.environ.get("COLLECTOR_RECHECK_BATCH", "15"))
COLLECTOR_FEED_ITEMS = int(os.environ.get("COLLECTOR_FEED_ITEMS", "30"))   # 피드당 상위 N개만
FEED_TIMEOUT = 15
MAX_FEED_BYTES = 2_000_000   # 피드 본문 상한 2MB
# 요청 사이 지터(초) — 사람 브라우징처럼 보이게 + 서버 부하 최소화
_JITTER_LO = float(os.environ.get("COLLECTOR_JITTER_LO", "1.5"))
_JITTER_HI = float(os.environ.get("COLLECTOR_JITTER_HI", "4.0"))

# 수집 루프와 공개 목록은 같은 카탈로그를 사용한다. 기존 (domain, url) 계약은 유지.
COMMUNITY_FEEDS = [
    (source["domain"], source["url"])
    for source in COMMUNITY_SOURCES if source["enabled"]
]
_SOURCE_BY_URL = {source["url"]: source for source in COMMUNITY_SOURCES}
_source_results: dict[str, dict] = {}
_feed_cursor = 0  # 예산이 피드 수보다 작아도 매 주기 시작점을 돌려 모든 출처에 기회를 준다.

# 모듈 상태 (대시보드/stats 용)
_last_poll_at: Optional[datetime] = None
_next_poll_at: Optional[datetime] = None
_last_result: Optional[dict] = None


def get_status() -> dict:
    """대시보드용 수집기 상태 스냅샷 (hunter.get_status 와 동형)."""
    return {
        "enabled": COLLECTOR_ENABLED,
        "interval_sec": COLLECTOR_INTERVAL_SEC,
        "feeds": len(COMMUNITY_FEEDS),
        "next_poll_at": _next_poll_at.isoformat() if _next_poll_at else None,
        "last_poll_at": _last_poll_at.isoformat() if _last_poll_at else None,
        "last_result": _last_result,
    }


def get_sources_status() -> dict:
    """DB 조회 없이 공개 카탈로그와 이 프로세스의 최근 폴링 결과만 반환한다.

    본문·제목·원문 작성자·DB 오류 메시지는 포함하지 않는다. 재시작 뒤에는 미확인으로
    돌아간다. enabled 는 수집 대상 설정이며 실제 작동 여부는 최상위 enabled 와 구분한다.
    """
    sources = []
    for source in COMMUNITY_SOURCES:
        result = {
            "status": "pending" if source["enabled"] else "disabled",
            "last_checked_at": None, "http_code": None, "discovered": 0,
            "captured": 0, "attempted": 0, "capture_errors": 0, "error": None,
        }
        if source["enabled"]:
            result.update(_source_results.get(source["url"], {}))
        sources.append({**source, **result})
    return {**get_status(), "sources": sources}


def _parse_source(source: str, feed_url: str, raw: bytes) -> list[dict]:
    parser = _SOURCE_BY_URL.get(feed_url, {}).get("parser", "rss")
    parsers = {
        "rss": _parse_feed, "clien": _parse_clien_html,
        "bobaedream": _parse_bobaedream_html, "theqoo": _parse_theqoo_html,
        "pann": _parse_pann_html,
    }
    if parser in parsers:
        return parsers[parser](raw)
    return parse_html_source(parser, raw)


def _local(tag: str) -> str:
    """네임스페이스를 떼고 로컬 태그명만(소문자). '{ns}item' → 'item'."""
    return tag.rsplit("}", 1)[-1].lower()


def _parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0 / Atom 피드에서 (title, url, guid, summary) 추출. 파싱 실패는 빈 리스트.
    ET.fromstring 은 XML 선언의 encoding(EUC-KR 등)을 존중하므로 bytes 를 그대로 넘긴다."""
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    out: list[dict] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = guid = summary = None
        for ch in node:
            t = _local(ch.tag)
            if t == "title" and ch.text:
                title = ch.text.strip()
            elif t == "link":
                href = ch.get("href")          # Atom: <link href="...">
                if href:
                    link = href.strip()
                elif ch.text and ch.text.strip():  # RSS: <link>...</link>
                    link = ch.text.strip()
            elif t in ("guid", "id") and ch.text and not guid:
                guid = ch.text.strip()
            elif t in ("description", "summary", "content") and ch.text and not summary:
                summary = ch.text.strip()
        if not link and guid and guid.startswith("http"):
            link = guid
        if link and link.startswith("http"):
            # 공식 뽐뿌 RSS 는 HTTP 글 주소를 주지만 해당 페이지는 JS 로 HTTPS 이동한다.
            # JS 를 실행하지 않는 수집기도 실제 본문을 받을 수 있도록 정규화한다.
            parsed = urlsplit(link)
            if (parsed.scheme == "http" and parsed.hostname
                    and (parsed.hostname == "ppomppu.co.kr"
                         or parsed.hostname.endswith(".ppomppu.co.kr"))):
                link = urlunsplit(parsed._replace(scheme="https"))
            out.append({
                "title": title,
                "url": link,
                "guid": guid or link,
                # 요약은 HTML 이 섞이므로 가시 텍스트만, 길이 제한.
                "summary": (_visible_text(summary)[:2000] if summary else None) or None,
            })
    return out


def _parse_clien_html(raw: bytes) -> list[dict]:
    """클리앙 모두의공원 HTML 목록에서 (title, url, guid, summary) 추출."""
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    
    out = []
    pattern = re.compile(
        r'href="(/service/board/park/\d+[^"]*)"[^>]*>.*?title="([^"]+)"',
        re.DOTALL
    )
    for match in pattern.finditer(text):
        path, title = match.groups()
        clean_path = path.split("?")[0]
        url = f"https://www.clien.net{clean_path}"
        out.append({
            "title": title.strip(),
            "url": url,
            "guid": url,
            "summary": None
        })
    return out


def _parse_bobaedream_html(raw: bytes) -> list[dict]:
    """보배드림 자유게시판 HTML 목록에서 (title, url, guid, summary) 추출."""
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    
    out = []
    pattern = re.compile(
        r'<a\s+class="bsubject"[^>]*href="(/view\?[^"]*No=(\d+)[^"]*)"[^>]*>(.*?)</a>',
        re.DOTALL
    )
    for match in pattern.finditer(text):
        full_tag = match.group(0)
        path, no_val, content = match.groups()
        title_text = re.sub(r'<[^>]+>', '', content).strip()
        title_attr = re.search(r'title="([^"]+)"', full_tag)
        title = title_attr.group(1).strip() if title_attr else title_text
        
        url = f"https://www.bobaedream.co.kr/view?code=freeb&No={no_val}"
        out.append({
            "title": title,
            "url": url,
            "guid": url,
            "summary": None
        })
    return out


def _parse_theqoo_html(raw: bytes) -> list[dict]:
    """더쿠 HOT 게시판 HTML 목록에서 (title, url, guid, summary) 추출.
    구조(2026-07-04 실측): 일반 글은 <td class="title"><a href="/hot/<id>">제목</a>,
    공지는 <tr class="notice ...">, 댓글 수는 별도 앵커(#fragment) — 공지 행을 통째로
    걷어낸 뒤 td.title 의 첫 앵커만 취해 댓글 링크·장식 태그를 배제한다."""
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []

    # 운영 공지 행 제거 — 캡처 예산이 공지에 낭비되지 않게.
    text = re.sub(r'<tr class="notice[^"]*".*?</tr>', '', text, flags=re.DOTALL)

    out = []
    seen = set()
    pattern = re.compile(
        r'<td class="title">\s*<a href="(/hot/(\d+))">(.*?)</a>',
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        path, _no, content = match.groups()
        # 태그 제거 후 엔티티 디코드(&quot; 등) — 제목은 가치 점수·승격 citation 에 쓰인다.
        title = html_mod.unescape(re.sub(r'<[^>]+>', '', content)).strip()
        if not title:
            continue
        url = f"https://theqoo.net{path}"
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "title": title,
            "url": url,
            "guid": url,
            "summary": None
        })
    return out


def _parse_pann_html(raw: bytes) -> list[dict]:
    """네이트판 랭킹/목록 HTML 에서 (title, url, guid, summary) 추출.
    구조(2026-07-04 실측): 제목은 <dt><h2><a href="/talk/<id>" … title="제목">,
    본문 미리보기는 <dd class="txt"><a href="/talk/<id>">…</a>. 미리보기를 RSS summary
    처럼 넘겨 본문 GET 전 예비 가치·삭제확률 점수의 정확도를 높인다."""
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []

    # 본문 미리보기 맵: /talk/<id> → 요약 텍스트
    summaries: dict = {}
    for m in re.finditer(
            r'<dd class="txt"><a href="(/talk/\d+)"[^>]*>(.*?)</a>', text, re.DOTALL):
        path, snippet = m.groups()
        snippet = html_mod.unescape(re.sub(r'<[^>]+>', '', snippet)).strip()
        if snippet and path not in summaries:
            summaries[path] = snippet

    out = []
    seen = set()
    pattern = re.compile(
        r'<dt><h2><a href="(/talk/(\d+))"[^>]*title="([^"]*)"[^>]*>',
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        path, _no, title = match.groups()
        title = html_mod.unescape(title).strip()
        if not title:
            continue
        url = f"https://pann.nate.com{path}"
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "title": title,
            "url": url,
            "guid": url,
            "summary": summaries.get(path)
        })
    return out


async def _sleep_jitter() -> None:
    """요청 사이 랜덤 지연(정중한 폴링)."""
    await asyncio.sleep(random.uniform(_JITTER_LO, _JITTER_HI))


async def _fetch_feed(url: str, client: httpx.AsyncClient) -> tuple[Optional[bytes], Optional[int]]:
    """RSS/Atom 피드/HTML 목록 GET. (raw_bytes|None, http_code|None) 반환.
    피드 URL 은 하드코딩된 신뢰 목록이라 SSRF 위험이 낮아 follow_redirects 를 허용한다."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with client.stream("GET", url, timeout=FEED_TIMEOUT,
                                 follow_redirects=True, headers=headers) as resp:
            code = resp.status_code
            if code >= 400:
                return None, code
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_FEED_BYTES:
                    break
            return b"".join(chunks), code
    except Exception as e:
        logger.info(f"[collector] feed fetch 실패 {url}: {type(e).__name__}")
        return None, None


def _existing_urls(db, urls: list[str]) -> set:
    """기존 URL 집합. 조회 실패는 호출부에서 해당 피드를 건너뛰고 오류로 표시한다."""
    if not urls:
        return set()
    try:
        resp = (db.table("captured_posts").select("url,captured_at,status,http_code")
                .in_("url", urls).execute())
        # 최초 접근이 일시 차단/오류였던 글은 본문을 아직 확보하지 못했다.
        # 다음 폴링에서 다시 시도하되 이미 보관했거나 hard 삭제된 글은 건너뛴다.
        return {r["url"] for r in (resp.data or [])
                if r.get("captured_at") or (r.get("status") == "deleted"
                                           and r.get("http_code") in (404, 410))}
    except Exception as e:
        logger.warning(f"[collector] existing 조회 실패: {e}")
        raise RuntimeError("captured URL lookup failed") from e


async def _first_capture_payload(obs: dict, res: dict, now_iso: str,
                                 client: httpx.AsyncClient) -> dict:
    """Prepare an immutable first snapshot only after article/artifact validation."""
    if obs.get("net") != "ok" or res["status"] != "live":
        return {}
    text = obs.get("text")
    if not text and not (obs.get("media_urls") and obs.get("capture")):
        return {}
    payload = {
        "body_text": text or None,
        "content_hash": obs.get("article_hash") or obs.get("text_hash"),
        "captured_at": now_iso,
    }
    if obs.get("capture"):
        from services.preservation import preserve_capture
        manifest = await preserve_capture(obs["capture"], client)
        payload.update({
            "capture_manifest_path": manifest["manifest_path"],
            "capture_manifest_sha256": manifest["manifest_sha256"],
            "capture_state": manifest["state"],
        })
        # HTML referencing an unavailable image is insufficient evidence for an
        # image-only article. Keep the partial manifest, but retry first capture.
        if not text and not manifest.get("media_saved"):
            payload.pop("captured_at")
            payload.pop("content_hash")
    return payload


def _enqueue_wayback(url: str) -> None:
    try:
        wayback_enqueue(url)
    except Exception as e:
        logger.warning(f"[collector] wayback enqueue 실패 {url}: {e}")


async def _capture(db, source: str, feed_url: str, item: dict,
                   client: httpx.AsyncClient) -> bool:
    """Capture once; retry failed first requests without overwriting original evidence."""
    url = item["url"]
    # An idempotent metadata insert followed by a conditional update protects
    # earlier evidence even if another worker captures between discovery and GET.
    db.table("captured_posts").upsert({
        "source": source, "feed": feed_url, "url": url,
        "guid": item.get("guid"), "title": item.get("title"),
        "rss_summary": item.get("summary"),
        **({"first_seen": item["first_seen"]} if item.get("first_seen") else {}),
    }, on_conflict="url", ignore_duplicates=True).execute()
    current = db.table("captured_posts").select("*").eq("url", url).limit(1).execute().data[0]
    if current.get("captured_at") or current.get("body_text"):
        return True
    if current.get("status") == "deleted" and current.get("http_code") in (404, 410):
        return False
    obs = await fetch_observation(url, client, capture_text=True, capture_artifacts=True)
    res = decide_status(obs, url, _baseline_from_row(current))
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    try:
        capture = await _first_capture_payload(obs, res, now_iso, client)
        if capture and not capture.get("captured_at"):
            res = {"status": "error", "http_code": obs.get("http_code"),
                   "reason": "image-only article media unavailable", "baseline": None}
    except Exception:
        logger.exception("[collector] artifact 저장 실패: %s", source)
        capture = {}
        res = {"status": "error", "http_code": obs.get("http_code"),
               "reason": "capture artifact storage failed", "baseline": None}
    row = _build_update(res, current, now_iso, adaptive=True, now=now_dt)
    row.update(capture)
    if res["status"] == "deleted" and not current.get("deleted_at"):
        row["deleted_at"] = now_iso
    vsrc = obs.get("text") or item.get("summary") or ""
    if _promotion_cols(db):
        row["volatility_score"] = predict_volatility(item.get("title"), vsrc, url)["score"]
        if res["status"] == "deleted" and res.get("http_code") in (404, 410):
            row["hard_deleted_at"] = current.get("hard_deleted_at") or now_iso
    if _value_col(db):
        row["value_score"] = assess_value(item.get("title"), vsrc)["score"]
    try:
        saved = (db.table("captured_posts").update(row).eq("url", url)
                 .is_("captured_at", "null").is_("body_text", "null").execute().data)
    except Exception as e:
        logger.warning(f"[collector] capture 저장 실패 {url}: {e}")
        return False
    if not capture.get("captured_at") or not saved:
        return False
    _enqueue_wayback(url)
    return True


async def poll_feeds(client: httpx.AsyncClient) -> dict:
    """모든 피드를 정중하게 순회하며 신규 글을 발견·캡처.
    반환: {discovered, captured, skipped_ads}. 광고·거래 글(hard negative)은 캡처하지
    않으므로 DB 에 남지 않고, 피드에 머무는 동안 매 주기 재발견·재스킵된다(HTTP 비용 0).

    모든 발견 URL 은 예산 사용 전에 영속 대기열에 저장한다. 캡처는 도메인별 한 건씩
    돌아가며 주기 예산을 쓴다. 마지막 시도 시각·실패 백오프도 DB 에 저장하므로
    재시작하거나 글이 목록에서 사라져도 재시도와 출처별 공정 분배가 유지된다."""
    global _feed_cursor
    db = get_db()
    discovered = 0
    skipped_ads = 0
    scheduled_urls: set[str] = set()

    # 매 주기 시작점 회전: 신규 출처가 예산보다 많아져도 뒤쪽 피드를 굶기지 않는다.
    feeds = list(COMMUNITY_FEEDS)
    if feeds:
        offset = _feed_cursor % len(feeds)
        feeds = feeds[offset:] + feeds[:offset]
        _feed_cursor = (offset + 1) % len(feeds)

    # 1) 발견: 모든 피드에서 신규 항목만 추린다.
    for source, feed_url in feeds:
        result = {
            "status": "pending", "last_checked_at": datetime.now(timezone.utc).isoformat(),
            "http_code": None, "discovered": 0, "captured": 0,
            "attempted": 0, "capture_errors": 0, "error": None,
        }
        _source_results[feed_url] = result
        new_items: list = []
        try:
            raw, code = await _fetch_feed(feed_url, client)
            result["http_code"] = code
            if raw is None:
                result["status"] = "blocked" if code in BOT_BLOCK_CODES else "error"
                result["error"] = (f"HTTP {code}: 목록을 가져오지 못했습니다."
                                   if code else "목록 요청에 실패했습니다. 다음 주기에 재시도합니다.")
            else:
                items = _parse_source(source, feed_url, raw)[:COLLECTOR_FEED_ITEMS]
                result["status"] = "ok" if items else "empty"
                if not items:
                    result["error"] = "목록에서 글을 찾지 못했습니다. 접근 제한 또는 목록 구조를 확인해 주세요."
                urls = [it["url"] for it in items]
                existing = _existing_urls(db, urls) if urls else set()
                for it in items:
                    if it["url"] in existing or it["url"] in scheduled_urls:
                        continue
                    scheduled_urls.add(it["url"])
                    new_items.append(it)
                result["discovered"] = len(new_items)
                discovered += len(new_items)
                kept = []
                for it in new_items:
                    va = assess_value(it.get("title"), it.get("summary"))
                    if va["hard_negative"]:
                        skipped_ads += 1
                        continue
                    it["_priority"] = va["score"] + predict_volatility(
                        it.get("title"), it.get("summary") or "", it.get("url") or ""
                    )["score"]
                    kept.append(it)
                kept.sort(key=lambda it: it["_priority"], reverse=True)
                new_items = kept
                # Persist every eligible discovery before consuming capture budget.
                # Backlog survives feeds changing, application restart, and failures.
                discovery.enqueue(db, source, feed_url, new_items)
        except Exception:
            # 한 사이트의 구조 변경/파싱 실패가 다른 출처의 수집을 막지 않는다.
            logger.exception("[collector] 목록 처리 실패: %s", source)
            result["status"] = "error"
            result["error"] = "목록 처리에 실패했습니다. 다음 주기에 재시도합니다."
            new_items = []
        await _sleep_jitter()

    # 2) Drain the durable backlog, including posts no longer present in any feed.
    # Source cooldowns and attempt order are persisted, so restarts stay fair.
    budget = max(0, COLLECTOR_MAX_CAPTURE_PER_CYCLE)
    per_source = discovery.ready(db, [source for source, _ in feeds], budget)
    captured = 0
    idx = [0] * len(per_source)
    progressed = True
    while budget > 0 and progressed:
        progressed = False
        for i, (source, candidates) in enumerate(per_source):
            if budget <= 0:
                break
            if idx[i] >= len(candidates):
                continue
            candidate = candidates[idx[i]]
            idx[i] += 1
            progressed = True
            it = discovery.claim(db, candidate)
            if it is None:
                continue
            feed_url = it["feed"]
            result = _source_results.get(feed_url)
            if result is not None:
                result["attempted"] += 1
            try:
                success = await _capture(db, source, feed_url, it, client)
            except Exception:
                logger.exception("[collector] 본문 캡처 실패: %s", source)
                success = False
            discovery.finish(db, it, success)
            if success:
                captured += 1
                if result is not None:
                    result["captured"] += 1
            else:
                if result is not None:
                    result["capture_errors"] += 1
                    result["status"] = "error"
                    result["error"] = f"본문 확보 또는 저장 실패 {result['capture_errors']}건."
                # A domain's blocked request pauses all its feeds for this cycle.
                idx[i] = len(candidates)
            budget -= 1
            await _sleep_jitter()

    return {"discovered": discovered, "captured": captured, "skipped_ads": skipped_ads,
            **discovery.summary(db)}


async def recheck_captured_batch(batch_size: int = COLLECTOR_RECHECK_BATCH) -> int:
    """만기 도래한 captured_posts 를 재검사해 삭제/변화를 감지(적응형 due 순). 검사 수 반환.
    tracker 와 동일하게 hard(404/410) deleted 만 큐에서 영구 제외하고, soft 는 정정 위해 유지."""
    db = get_db()
    promo_state = _promotion_cols_state(db)
    if promo_state is None:
        # 프로브 일시 불명 상태로 배치를 진행하면, 이 배치에서 감지된 hard 삭제가
        # hard_deleted_at 없이 기록되고 그 행은 큐를 영영 떠나 승격 후보가 비가역적으로
        # 유실된다. 이번 주기는 건너뛰고 다음 주기에 재판별한다(수집 지연 << 유실).
        logger.warning("[collector] 승격 컬럼 판별 일시 실패 — hard 삭제 기록 유실 방지를 위해 "
                       "이번 재검사 주기 건너뜀")
        return 0
    promo = promo_state
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cols = (
        "id,url,title,captured_at,body_text,check_count,status,http_code,error_count,"
        "baseline_final_url,baseline_len,baseline_hash,"
        "baseline_del_match,baseline_blk_match,baseline_at"
    )
    if promo:
        cols += ",hard_deleted_at"
    disabled_domains = {
        source["domain"] for source in COMMUNITY_SOURCES if not source["enabled"]
    } - {source["domain"] for source in COMMUNITY_SOURCES if source["enabled"]}
    try:
        query = (
            db.table("captured_posts")
            .select(cols)
            .or_(RECHECK_QUEUE_FILTER)
            # 만기 도래 + NULL(혼합 상태 자가 복구) — tracker.recheck_batch 와 동일 정책.
            .or_(_due_filter(now_iso))
            .order("next_check_at", desc=False, nullsfirst=True)
            .limit(batch_size)
        )
        # A disabled catalog domain must not continue receiving automated GETs
        # through previously captured rows. Existing evidence stays untouched.
        if disabled_domains:
            query = query.not_.in_("source", sorted(disabled_domains))
        resp = query.execute()
    except Exception as e:
        logger.warning(f"[collector] recheck 조회 실패: {e}")
        return 0

    rows = resp.data or []
    if not rows:
        return 0

    newly_deleted = 0
    newly_hard = 0
    async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
        for row in rows:
            needs_capture = not row.get("captured_at") and not row.get("body_text")
            obs = await fetch_observation(row["url"], client, capture_text=needs_capture,
                                          capture_artifacts=needs_capture)
            res = decide_status(obs, row["url"], _baseline_from_row(row))
            prev = row.get("status")
            now_dt = datetime.now(timezone.utc)
            ni = now_dt.isoformat()
            # tracker 와 동일한 payload(상태·기준선·적응형 스케줄)를 공용 헬퍼로 생성.
            # captured_posts 는 적응형 컬럼이 항상 있으므로 adaptive=True. deleted_at·
            # newly_deleted 는 _build_update 가 다루지 않으므로 여기서 처리한다.
            capture = {}
            if needs_capture:
                try:
                    capture = await _first_capture_payload(obs, res, ni, client)
                    if capture and not capture.get("captured_at"):
                        res = {"status": "error", "http_code": obs.get("http_code"),
                               "reason": "image-only article media unavailable", "baseline": None}
                except Exception:
                    logger.exception("[collector] 재검사 artifact 저장 실패")
                    res = {"status": "error", "http_code": obs.get("http_code"),
                           "reason": "capture artifact storage failed", "baseline": None}
            upd = _build_update(res, row, ni, adaptive=True, now=now_dt)
            upd.update(capture)
            if capture.get("captured_at") and _value_col(db):
                upd["value_score"] = assess_value(row.get("title"), obs.get("text"))["score"]
            if res["status"] == "deleted" and prev != "deleted":
                upd["deleted_at"] = ni
                newly_deleted += 1
            # hard 삭제(404/410)는 *확정 삭제* — promoter 자동 승격의 유일한 게이트 신호.
            # soft(본문패턴/리다이렉트/급감)는 오탐 자가정정 가능성이 있어 절대 승격 신호로
            # 쓰지 않는다(tracker/threshold 의 hard-only 원칙과 일관). 최초 1회만 기록.
            if (promo and res["status"] == "deleted"
                    and res.get("http_code") in (404, 410)
                    and not row.get("hard_deleted_at")):
                upd["hard_deleted_at"] = ni
                newly_hard += 1
            try:
                query = db.table("captured_posts").update(upd).eq("id", row["id"])
                if capture:
                    query = query.is_("captured_at", "null").is_("body_text", "null")
                saved = query.execute().data
                if capture.get("captured_at") and saved:
                    _enqueue_wayback(row["url"])
                    discovery.resolve(db, row["url"], "captured")
                elif (saved and res["status"] == "deleted"
                      and res.get("http_code") in (404, 410)):
                    discovery.resolve(db, row["url"], "deleted")
            except Exception as e:
                logger.warning(f"[collector] recheck 갱신 실패 {row['id']}: {e}")
            await _sleep_jitter()

    if newly_deleted or newly_hard:
        logger.info(f"[collector] captured {newly_deleted}건 새로 삭제 감지"
                    f"(hard 확정 {newly_hard}건 → 승격 후보)")
    return len(rows)


def _table_exists() -> bool:
    try:
        db = get_db()
        db.table("captured_posts").select("id,capture_manifest_path").limit(1).execute()
        db.table("discovery_queue").select("id").limit(1).execute()
        return True
    except Exception:
        return False


async def background_loop() -> None:
    """앱 lifespan 동안 도는 선제 수집 루프 (피드 폴링 → 캡처 → 삭제 재검사)."""
    global _next_poll_at, _last_poll_at, _last_result

    if not COLLECTOR_ENABLED:
        logger.info("[collector] 비활성화 (COLLECTOR_ENABLED=false)")
        return
    if not _table_exists():
        logger.warning("[collector] 수집 스키마 없음 — migrations/006 및 013 적용 후 "
                       "COLLECTOR_ENABLED=true 로 켜세요. 루프 중단.")
        return

    logger.info(f"[collector] 시작 · feeds={len(COMMUNITY_FEEDS)} · interval={COLLECTOR_INTERVAL_SEC}s")
    _next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=COLLECTOR_INITIAL_DELAY_SEC)
    try:
        await asyncio.sleep(COLLECTOR_INITIAL_DELAY_SEC)
    except asyncio.CancelledError:
        return

    while True:
        try:
            async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
                poll = await poll_feeds(client)
            rechecked = await recheck_captured_batch()
            _last_poll_at = datetime.now(timezone.utc)
            _last_result = {**poll, "rechecked": rechecked, "at": _last_poll_at.isoformat()}
            if poll["captured"] or rechecked:
                logger.info(f"[collector] 발견 {poll['discovered']} · 캡처 {poll['captured']} "
                            f"· 재검사 {rechecked}")
        except asyncio.CancelledError:
            logger.info("[collector] cancelled")
            return
        except Exception as e:
            logger.warning(f"[collector] loop error: {e}")

        jitter = random.uniform(0.9, 1.1)
        delay = max(60, int(COLLECTOR_INTERVAL_SEC * jitter))
        _next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

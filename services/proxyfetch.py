"""추적 불가(봇차단) 출처의 2차 관측 채널 — 렌더링 프록시 위임 (Proxy Observation).

문제: 직접 GET 이 403/429/430 이거나 HTTP 200 챌린지 페이지면 본문을 읽을 수 없다.
FM코리아 등도 정상 응답은 직접 관측하고 차단 응답일 때만 이 경로를 고려한다.
Wayback 위임(IA)도 같은 안티봇에 막힐 수 있어 2차 관측 채널을 제공한다.

해법: 렌더링 프록시(기본 Jina Reader, https://r.jina.ai/{url})에 관측을 위임한다.
프록시는 헤드리스 브라우저로 페이지를 렌더링해 가시 텍스트를 돌려주므로,
tracker 의 검증된 삭제 표식 패턴(DELETION_PATTERNS)을 그대로 적용할 수 있고,
'URL Source' 메타데이터로 메인 리다이렉트(삭제글 튕김)도 잡을 수 있다.

안전 원칙(오탐이 영구 박제로 이어지지 않게 — tracker/threshold 의 hard-only 원칙과 일관):
  1. 프록시 관측은 *soft 신호 전용*. 절대 http_code 404/410(hard)을 만들지 않는다
     (트리거 조건 자체가 404/410 을 배제 — is_untrackable_source 참고).
     → 임계값 인하(threshold)·자동 승격(promoter)에 영향 0. 배지·표시만 정확해진다.
  2. 프록시 자체 오류(레이트리밋·프록시도 차단당함·챌린지 통과 실패)는 '판단 유보'로
     기존 untrackable 판정을 유지한다(프록시 장애가 '삭제됨'으로 둔갑하지 않게).
  3. 발화 조건: (a) 잘 앵커링된 삭제 표식 매치, (b) 사이트 루트로 리다이렉트,
     (c) 실체 있는 본문 확보(생존 확인). 그 외는 전부 판단 유보.
  4. 기본 비활성(PROXY_FETCH_ENABLED=false) — 제3자 서비스에 출처 URL 을 보내는
     외부 의존이므로 옵트인. 방문 이력이 프록시 사업자에 남는다는 트레이드오프를
     docs/TRANSPARENCY.md 에 공개한다.
"""

import asyncio
import os
import re
from urllib.parse import urlparse

import httpx

from services.tracker import (
    BOT_CHALLENGE_PATTERNS,
    DELETION_PATTERNS,
    PROXY_OBSERVED_PREFIX,
    _is_site_root,
    _url_key,
    is_untrackable_source,
)

import logging
logger = logging.getLogger(__name__)

PROXY_FETCH_ENABLED = os.environ.get("PROXY_FETCH_ENABLED", "false").lower() == "true"
# 렌더링 프록시 베이스. {base}{원본 URL(스킴 포함)} 형태로 GET (Jina Reader 규약).
PROXY_FETCH_BASE = os.environ.get("PROXY_FETCH_BASE", "https://r.jina.ai/").rstrip("/") + "/"
# 키 없이도 동작하나 레이트리밋이 낮다. 키가 있으면 Bearer 로 전달.
PROXY_FETCH_API_KEY = os.environ.get("PROXY_FETCH_API_KEY", "").strip()
PROXY_FETCH_TIMEOUT = int(os.environ.get("PROXY_FETCH_TIMEOUT", "25"))
# 전체 마감시한(모든 청크 합산): per-op 타임아웃만으론 슬로 드립 응답이 tracker 의
# 직렬 재검사 루프를 장시간 붙들 수 있다(tracker.FETCH_DEADLINE_SEC 와 동일 원칙).
_PROXY_DEADLINE_SEC = PROXY_FETCH_TIMEOUT + 15
# 이 길이 이상의 실체 있는 본문이 오면 '생존 확인'. 짧은 응답(에러 안내 등)은 판단 유보.
PROXY_MIN_LIVE_LEN = int(os.environ.get("PROXY_MIN_LIVE_LEN", "200"))
MAX_PROXY_BYTES = 300_000   # 프록시 응답 상한(가시 텍스트라 충분)


def proxy_url(url: str) -> str:
    """원본 URL 의 프록시 관측 URL. 스킴 포함 전체 URL 을 그대로 뒤에 붙인다."""
    return f"{PROXY_FETCH_BASE}{url}"


# Jina Reader 기본(markdown) 응답의 메타 헤더:
#   Title: ...
#   URL Source: https://...
#   Markdown Content:
#   <본문>
_TITLE_RE = re.compile(r"^Title:\s*(.*)$", re.MULTILINE)
_URL_SOURCE_RE = re.compile(r"^URL Source:\s*(\S+)\s*$", re.MULTILINE)
_CONTENT_SPLIT_RE = re.compile(r"^Markdown Content:\s*$", re.MULTILINE)


def parse_proxy_markdown(raw: str) -> dict:
    """프록시 응답에서 (title, url_source, content) 추출. 메타 헤더가 없으면
    전체를 content 로 본다(플레인 텍스트 모드/타 프록시 호환)."""
    raw = raw or ""
    title_m = _TITLE_RE.search(raw[:2000])
    url_m = _URL_SOURCE_RE.search(raw[:2000])
    split = _CONTENT_SPLIT_RE.split(raw, maxsplit=1)
    content = split[1] if len(split) == 2 else raw
    return {
        "title": title_m.group(1).strip() if title_m else None,
        "url_source": url_m.group(1).strip() if url_m else None,
        "content": content.strip(),
    }


def decide_from_proxy(original_url: str, raw_text: str) -> dict | None:
    """프록시 응답 텍스트로 soft 판정. 반환: {status, reason} 또는 None(판단 유보).

    보수적 순서: 프록시도 챌린지에 걸렸으면 유보 → 명시적 삭제 표식 → 메인 리다이렉트
    → 실체 본문(생존) → 그 외 유보. http_code 는 부여하지 않는다(soft 보장)."""
    parsed = parse_proxy_markdown(raw_text)
    content = parsed["content"]
    if not content:
        return None
    # 프록시가 받아온 것도 챌린지/로딩 페이지면 관측 실패 — 유보.
    if BOT_CHALLENGE_PATTERNS.search(content):
        return None
    dm = DELETION_PATTERNS.search(content)
    if dm:
        return {"status": "deleted",
                "reason": f"{PROXY_OBSERVED_PREFIX}: 삭제 표식 — {dm.group(0)[:40]}"}
    # 삭제글이 메인으로 튕겨나간 경우(직접 추적의 moved_to_root 와 동일 신호).
    src = parsed["url_source"]
    if src and _url_key(src) != _url_key(original_url) and _is_site_root(src):
        return {"status": "deleted",
                "reason": f"{PROXY_OBSERVED_PREFIX}: 게시물 사라짐·메인 리다이렉트"}
    if len(content) >= PROXY_MIN_LIVE_LEN:
        return {"status": "live", "reason": f"{PROXY_OBSERVED_PREFIX}: 생존 확인"}
    return None


async def fetch_proxy_text(url: str, client: httpx.AsyncClient) -> str | None:
    """프록시로 원본 URL 의 렌더링 텍스트를 GET. 실패(비 200/네트워크)는 None."""
    headers = {"Accept": "text/plain"}
    if PROXY_FETCH_API_KEY:
        headers["Authorization"] = f"Bearer {PROXY_FETCH_API_KEY}"
    try:
        async with asyncio.timeout(_PROXY_DEADLINE_SEC):
            async with client.stream("GET", proxy_url(url), timeout=PROXY_FETCH_TIMEOUT,
                                     follow_redirects=True, headers=headers) as resp:
                if resp.status_code != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_PROXY_BYTES:
                        break
                return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as e:
        logger.info(f"[proxyfetch] 프록시 관측 실패 {url}: {type(e).__name__}")
        return None


async def maybe_observe_via_proxy(url: str, client: httpx.AsyncClient,
                                  direct_res: dict) -> dict:
    """직접 관측이 '추적 불가(봇차단)'로 끝났을 때만 프록시로 2차 관측을 시도해
    판정을 보강한다. 유보/실패 시 direct_res 를 그대로 돌려준다(안전한 no-op).

    반환 verdict 는 tracker._verdict 와 동형({status, http_code, reason, baseline}).
    http_code 는 직접 관측의 것을 유지 — 봇차단 코드(403/430 등)이므로 hard(404/410)가
    될 수 없다(soft 보장은 트리거 조건이 아니라 이 구조가 지킨다)."""
    if not PROXY_FETCH_ENABLED:
        return direct_res
    # 진짜 삭제(404/410)나 정상 추적 가능한 오류엔 개입하지 않는다.
    if direct_res.get("status") != "error":
        return direct_res
    if not is_untrackable_source(url, direct_res.get("http_code"), direct_res.get("reason")):
        return direct_res

    raw = await fetch_proxy_text(url, client)
    if raw is None:
        return direct_res
    verdict = decide_from_proxy(url, raw)
    if verdict is None:
        return direct_res
    return {
        "status": verdict["status"],
        "http_code": direct_res.get("http_code"),   # 404/410 불가 — soft 유지
        "reason": verdict["reason"],
        "baseline": None,   # 프록시 텍스트는 직접 관측과 상이 — 기준선으로 쓰지 않는다
    }


def _redacted_base() -> str:
    """공개 API 노출용 base — userinfo(자격증명 포함 구성) 제거.

    보수적 화이트리스트: 정상 http(s) URL 로 파싱될 때만 스킴+호스트+경로를 노출하고,
    그 외(스킴 누락 'user:pass@host/' 는 urlparse 가 'user' 를 스킴으로 오파싱해
    비밀이 path 로 새는 케이스 포함)는 전부 '(redacted)'. 최종 문자열에 '@' 가
    남아 있으면 어떤 경로로든 자격증명 잔존 가능성이 있으므로 역시 마스킹한다."""
    try:
        p = urlparse(PROXY_FETCH_BASE)
        netloc = p.netloc.rpartition("@")[2]   # user:pass@ 제거
        if p.scheme not in ("http", "https") or not netloc:
            return "(redacted)"
        out = f"{p.scheme}://{netloc}{p.path or '/'}"
        return out if "@" not in out else "(redacted)"
    except Exception:
        return "(redacted)"


def get_status() -> dict:
    """대시보드/transparency 용 상태. base 는 자격증명을 걷어낸 형태로만 노출한다
    (무인증 공개 엔드포인트 /api/stats·/api/transparency 가 그대로 내보내므로)."""
    return {
        "enabled": PROXY_FETCH_ENABLED,
        "base": _redacted_base() if PROXY_FETCH_ENABLED else None,
        "has_api_key": bool(PROXY_FETCH_API_KEY),
        "min_live_len": PROXY_MIN_LIVE_LEN,
    }

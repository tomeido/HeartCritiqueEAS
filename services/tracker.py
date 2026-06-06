"""
인용 URL 삭제 추적 (Citation Deletion Tracker).

CONTEXT.md 핵심 가치: 대기업 자본력에 삭제되는 Web2 커뮤니티의 사각지대 박제.

흐름:
  1. 새 스토리 생성 시 register_citations() 가 모든 출처 URL 을 DB 에 등록
  2. 백그라운드 루프가 주기적으로 N 건씩 HTTP GET 재방문
  3. 첫 생존 확인 시 '기준선'(최종 URL·본문 길이·표식 유무)을 캡처하고,
     이후 재방문에서는 *기준선 대비 변화*(다른 URL 로 리다이렉트 / 본문 급감 /
     삭제·차단 표식이 새로 등장)로 사라짐을 판단한다. 살아있던 페이지가 그대로면
     로그인 안내 같은 상시 UI 문구가 있어도 'live' 로 둔다(오탐 차단).
  4. 결과를 citation_checks 테이블에 갱신
  5. API 응답에 status 포함되어 UI 에서 시각화
"""

import asyncio
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlparse

import httpx

from services.db import get_db

import logging
logger = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))   # 5분 간격
CHECK_BATCH_SIZE   = int(os.environ.get("CHECK_BATCH_SIZE", "15"))
HTTP_TIMEOUT       = 15
MAX_BODY_BYTES     = 80000   # 본문은 앞 80KB 만 읽음 (대용량 응답 남용 방지)
MAX_REDIRECTS      = 5
FETCH_DEADLINE_SEC = 30      # 한 citation 의 전체 fetch(모든 리다이렉트 홉 합산) 절대 한도
TRACKER_ENABLED    = os.environ.get("TRACKER_ENABLED", "true").lower() != "false"

# 내부 서비스명 차단 목록 (docker 네트워크)
_BLOCKED_HOSTNAMES = {"localhost", "uploader", "app", "db"}


def _is_safe_url(url: str) -> tuple[bool, str, str]:
    """citation URL 은 LLM/Tavily 가 만든 비신뢰 값이다. 서버가 GET 하기 전에
    스킴(http/https)과 호스트(사설·루프백·링크로컬·내부서비스 아님)를 검증해 SSRF 차단.
    DNS resolve 후 IP 대역까지 본다.

    반환: (safe, reason, pinned_ip). pinned_ip 은 검증을 통과한 '실제로 연결할' IP 다.
    호출부는 이 IP 로 직접 연결(핀)해야 DNS rebinding(검증과 연결 사이 재resolve 로
    내부 IP 로 바꿔치기)을 막을 수 있다. 호스트명으로 다시 연결하면 httpx 가 독립적으로
    재resolve 해 우회된다."""
    try:
        p = urlparse(url)
    except Exception:
        return False, "bad_url", ""
    if p.scheme not in ("http", "https"):
        return False, f"scheme:{p.scheme or 'none'}", ""
    host = p.hostname
    if not host:
        return False, "no_host", ""
    if host.lower() in _BLOCKED_HOSTNAMES:
        return False, "internal_host", ""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, "dns_fail", ""
    pinned = ""
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False, f"blocked_ip:{ip}", ""
        if not pinned:
            pinned = ip
    if not pinned:
        return False, "no_ip", ""
    return True, "", pinned


def _host_header(u: "httpx.URL") -> str:
    """원본 URL 의 Host 헤더 값(host[:port], IPv6 는 브래킷)."""
    h = u.host
    if ":" in h:  # IPv6 리터럴
        h = f"[{h}]"
    return h if u.port is None else f"{h}:{u.port}"

# 커뮤니티 게시판에서 글이 삭제되었을 때 자주 보이는 본문 패턴.
# 주의: FM코리아 등 일부 사이트는 없는 글을 메인 페이지로 리다이렉트 → 본문 패턴으로
# 잡을 수 없음 (알려진 한계). 명확한 표식이 있는 더쿠/클리앙/네이트판 등은 잘 감지.
#
# 오탐 방지(중요): 패턴은 살아있는 페이지의 정상 UI 문구와 충돌하지 않도록 '게시물 단위'
# 대상 명사에 앵커링한다. 과거 무앵커 패턴이 일으킨 치명적 오탐 사례:
#   - '글이? 없습니다'  → '댓글이 없습니다'(댓글 0개인 살아있는 글)에 매치
#   - '찾을 수 없'       → '검색 결과를/관련 상품을 찾을 수 없습니다'(위젯)에 매치
#   - '존재하지 않는'    → '존재하지 않는 회원/상품'(프로필·쇼핑)에 매치
#   - '차단된 글'·'블라인드 처리' → 'X 보기 설정'·'X 안내'(기능 라벨)에 매치
#   - 'page/404 not found' → 살아있는 페이지의 JS·임베드 문구에 매치(HTTP 404/410 으로 이미 커버)
# 영문 404 류와 순수 HTTP 신호는 위 check_url 의 상태코드 분기(404/410)가 담당한다.
DELETION_PATTERNS = re.compile(
    r"삭제된\s*(?:글|톡|게시[물글]?)"
    r"|이미\s*삭제"
    r"|이\s*글을\s*볼\s*권한"
    r"|접근\s*권한이?\s*없"
    r"|신고에?\s*의해\s*삭제"
    r"|숨김\s*처리된\s*(?:글|게시[물글]?)"
    r"|글쓴이에?\s*의해\s*삭제"
    # '존재하지 않는' 은 대상 명사(글/게시물/페이지) 동반 시에만 — 회원/상품 오탐 차단
    r"|존재하지\s*않는\s*(?:글|게시물|게시글|페이지)"
    r"|글이?\s*존재하지\s*않"
    # '찾을 수 없' 은 글/게시물/페이지/주소 대상일 때만 — 검색결과/상품 오탐 차단
    r"|(?:글|게시물|게시글|페이지|주소)[을를이가]?\s*찾을\s*수\s*없(?:는|습)"
    # '차단된 글' 은 신고/운영 맥락 또는 종결형일 때만 — '차단된 글 보기 설정' 오탐 차단
    r"|(?:신고|운영자|운영진|관리자|다수\s*신고)[로은는이가]?\s*(?:에\s*의해\s*)?차단된\s*(?:글|게시[물글]?)"
    r"|차단된\s*(?:글|게시[물글]?)\s*(?:입니다|이에요|예요|이다)"
    # '블라인드 처리' 는 완료형일 때만 — '블라인드 처리 안내/하기' 오탐 차단
    r"|블라인드\s*처리(?:된|됨|되었|됐)"
    r"|deleted\s+(?:post|by)",
    re.IGNORECASE,
)

# 차단 패턴 (가입자 전용/로그인 벽 등 살아는 있지만 우리가 볼 수 없는 상태).
#
# 오탐 주의(중요): 단순 '로그인이 필요' 는 살아있는 글의 상시 UI(댓글 작성·좋아요
# 안내, 헤더 로그인 유도)에도 흔히 박혀 있어 과거 '차단됨' 오탐의 주범이었다.
# 그래서 (a) '글/게시물을 보려면 로그인' 처럼 *본문 열람*이 로그인에 걸린 맥락,
# (b) 회원/가입자 전용 열람, (c) 성인 인증 으로만 앵커링한다.
# 게다가 차단 표식은 기준선 대비 *새로 등장*했을 때만 'blocked' 로 승격한다
# (decide_status 참고) — 상시 로그인 안내는 기준선에도 있으므로 절대 발화하지 않는다.
BLOCKED_PATTERNS = re.compile(
    r"(?:글|게시물|게시글|내용|본문)[을를이가]?\s*(?:보|열람|확인)\S{0,4}\s*(?:려면|시려면|기\s*위해|위해서?)?\s*로그인"
    r"|로그인\s*(?:후|하셔야|해야)\S{0,6}\s*(?:열람|볼\s*수|보실\s*수)"
    r"|회원(?:만|\s*전용|\s*등급)\S{0,4}\s*(?:열람|볼\s*수)"
    r"|가입자\s*전용"
    r"|성인\s*인증",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ---- 본문/URL 정규화 헬퍼 (기준선 대비 변화 판정에 사용) -------------------
_STRIP_BLOCK_RE = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1>")
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# 리다이렉트 비교 시 무시할 추적 파라미터 (광고·유입 추적용; 글 정체성과 무관)
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "ref", "ref_src", "spm", "fromrss", "from",
}


_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?\s*([a-zA-Z0-9_\-]+)', re.I)


def _decode_body(body: bytes, resp_encoding: str | None, content_type: str) -> str:
    """한국 구형 게시판은 EUC-KR/CP949 가 흔하다. resp.encoding(없으면 UTF-8 추정)만
    믿고 디코딩하면 한글이 깨져 삭제/차단 패턴이 매칭되지 않는다(오탐 음성).
    Content-Type → <meta charset> → 폴백 체인으로 인코딩을 결정한다."""
    enc = None
    m = re.search(r'charset=([a-zA-Z0-9_\-]+)', content_type or "", re.I)
    if m:
        enc = m.group(1)
    if not enc and (resp_encoding and resp_encoding.lower() not in ("utf-8", "utf8", "ascii")):
        enc = resp_encoding  # httpx 가 헤더에서 명시적으로 잡은 경우만 신뢰
    if not enc:
        mm = _CHARSET_RE.search(body[:2048])
        if mm:
            enc = mm.group(1).decode("ascii", "ignore")
    enc = (enc or "utf-8").lower()
    if enc in ("euc-kr", "euckr", "ks_c_5601-1987", "ksc5601"):
        enc = "cp949"
    for codec in (enc, "cp949", "utf-8"):
        try:
            return body.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _visible_text(html: str) -> str:
    """HTML 에서 스크립트/스타일/태그/주석을 제거한 가시 텍스트만 반환.
    본문 패턴 매칭과 길이 비교를 script 내 JSON 등 비가시 영역과 분리해
    오탐을 줄인다."""
    s = _STRIP_BLOCK_RE.sub(" ", html)
    s = _COMMENT_RE.sub(" ", s)
    s = _ANY_TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _url_key(url: str) -> tuple:
    """리다이렉트 동일성 비교용 키. http/https·www/m 접두·후행 슬래시·추적
    파라미터·프래그먼트 차이를 무시하고 (host, path, 의미있는 query) 로 정규화.
    글 식별이 query 에 있는 구형 게시판(board.php?no=123)도 query 변화를 잡는다."""
    try:
        p = urlparse(url or "")
    except Exception:
        return ("", "", ())
    host = (p.hostname or "").lower()
    for pre in ("www.", "m.", "mobile."):
        if host.startswith(pre):
            host = host[len(pre):]
            break
    path = (p.path or "").rstrip("/") or "/"
    q = tuple(sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ))
    return (host, path, q)


def _is_site_root(url: str) -> bool:
    """최종 URL 이 사이트 루트('/' 또는 빈 경로, 의미있는 query 없음)인지.
    삭제된 글이 메인으로 튕겨나간 경우(FM코리아 류)를 잡는 신호."""
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    if (p.path or "").rstrip("/") not in ("",):
        return False
    meaningful = [
        k for k, _ in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return not meaningful


# 본문이 기준선 대비 이 비율 미만으로 줄면 '본문 급감'(글이 안내문 한 줄로 대체)
_COLLAPSE_RATIO = float(os.environ.get("CITATION_COLLAPSE_RATIO", "0.35"))
# 급감 판정을 적용할 최소 기준선 길이 (짧은 페이지의 비율 노이즈 방지)
_COLLAPSE_MIN_BASELEN = 800


async def fetch_observation(url: str, client: httpx.AsyncClient) -> dict:
    """단일 URL 을 GET 해서 *관측값*만 수집(판정은 decide_status 가 담당).
    반환 dict 키:
      net        : 'ok'(2xx 본문) | 'http'(>=400) | 'unsafe' | 'timeout'
                   | 'neterr' | 'redirect_loop'
      http_code  : 상태코드 (없으면 None)
      final_url  : 리다이렉트를 모두 따라간 최종 URL
      text_len   : 가시 텍스트 길이 (본문 없으면 None)
      del_match / blk_match : 삭제·차단 표식 매치 여부
      del_snip  / blk_snip  : 매치 문자열 일부 (reason 용)
      reason     : 네트워크 오류 등 부가 사유

    SSRF 방어: 리다이렉트를 자동 추종하지 않고, 홉마다 _is_safe_url 로 다시 검증한다.
    (안전한 외부 URL 이 30x 로 사설/루프백/메타데이터/내부서비스 IP 로 리다이렉트해
    초기 1회 검증을 우회하는 것을 차단. follow_redirects=True 면 최종 목적지가 재검증되지 않음.)"""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
    cur = url
    # 인터-청크 간격이 아니라 '전체' 마감시한. 느린 드립/슬로로리스 서버가 직렬 추적
    # 루프를 무한정 붙들지 못하게 모든 홉을 합쳐 절대 한도를 건다.
    try:
        async with asyncio.timeout(FETCH_DEADLINE_SEC):
            for _ in range(MAX_REDIRECTS + 1):
                safe, why, ip = await asyncio.to_thread(_is_safe_url, cur)
                if not safe:
                    return {"net": "unsafe", "http_code": None, "final_url": cur,
                            "reason": f"unsafe_url:{why}"}
                # DNS rebinding 방어: 검증한 그 IP 로 직접 연결(핀)하고, Host 헤더와
                # TLS SNI 는 원래 호스트명으로 유지(인증서 검증 정상). 호스트명으로 다시
                # 연결하면 httpx 가 독립 재resolve → 내부 IP 로 바꿔치기 가능.
                u = httpx.URL(cur)
                connect_url = u.copy_with(host=ip)
                req_headers = {**headers, "Host": _host_header(u)}
                try:
                    # 스트리밍으로 헤더 먼저 받고, 본문은 앞 MAX_BODY_BYTES 만 읽는다.
                    async with client.stream(
                        "GET", connect_url, timeout=HTTP_TIMEOUT, follow_redirects=False,
                        headers=req_headers, extensions={"sni_hostname": u.host},
                    ) as resp:
                        code = resp.status_code
                        # 리다이렉트는 직접 따라가되 다음 홉 URL 을 루프 상단에서 재검증+재핀
                        if 300 <= code < 400 and "location" in resp.headers:
                            cur = str(httpx.URL(cur).join(resp.headers["location"]))
                            continue
                        if code >= 400:
                            return {"net": "http", "http_code": code, "final_url": cur}

                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= MAX_BODY_BYTES:
                                break
                        raw = _decode_body(
                            b"".join(chunks), resp.encoding,
                            resp.headers.get("content-type", ""),
                        )
                except httpx.TimeoutException:
                    return {"net": "timeout", "http_code": None, "final_url": cur}
                except Exception as e:
                    return {"net": "neterr", "http_code": None, "final_url": cur,
                            "reason": f"net:{type(e).__name__}"}

                text = _visible_text(raw)
                dm = DELETION_PATTERNS.search(text)
                bm = BLOCKED_PATTERNS.search(text)
                return {
                    "net": "ok", "http_code": code, "final_url": cur, "text_len": len(text),
                    "del_match": bool(dm), "blk_match": bool(bm),
                    "del_snip": (dm.group(0)[:40] if dm else ""),
                    "blk_snip": (bm.group(0)[:40] if bm else ""),
                }

            return {"net": "redirect_loop", "http_code": None, "final_url": cur}
    except (asyncio.TimeoutError, TimeoutError):
        return {"net": "timeout", "http_code": None, "final_url": cur, "reason": "deadline"}


def _verdict(status: str, http_code, reason, baseline=None) -> dict:
    """판정 결과 dict. baseline 이 있으면 '이번에 캡처할 기준선'(이미 있으면 None)."""
    return {"status": status, "http_code": http_code, "reason": reason, "baseline": baseline}


def decide_status(obs: dict, original_url: str, baseline: dict | None) -> dict:
    """관측값(obs)을 기준선(baseline)과 대조해 status 를 결정.

    핵심 원칙(오탐 차단): 살아있던 글이 *그대로면* 로그인 안내 같은 상시 UI 문구가
    있어도 live. 'deleted/blocked' 는 기준선 대비 명확한 *변화*가 있을 때만 발화한다:
      · 다른 URL(특히 사이트 메인)로 리다이렉트
      · 본문 길이 급감 (글이 안내문으로 대체)
      · 삭제·차단 표식이 *새로* 등장 (기준선엔 없던 문구)

    hard/soft 구분: HTTP 404/410 만 hard(임계값 인하 대상). 변화 기반 판정은 soft 라
    배지·피드엔 뜨지만 자동·영구 박제의 임계값은 낮추지 않는다(되돌릴 수 없는 사고 방지).

    baseline 입력: {captured:bool, final_url, len, del_match, blk_match} 또는 None.
    """
    net = obs.get("net")
    code = obs.get("http_code")

    # --- 네트워크/HTTP 신호 (본문 없음) ---
    if net == "unsafe":
        return _verdict("error", None, obs.get("reason", "unsafe_url"))
    if net == "timeout":
        return _verdict("error", None, "timeout")
    if net == "neterr":
        return _verdict("error", None, obs.get("reason", "net"))
    if net == "redirect_loop":
        return _verdict("error", None, "too_many_redirects")
    if net == "http":
        if code in (404, 410):
            return _verdict("deleted", code, f"HTTP {code}")
        if code == 403:
            # 403 은 안티봇/WAF(Cloudflare 등)일 때가 많아 '차단 확정'으로 보지 않는다.
            # 실제 운영진 차단이면 본문(가입자 전용 등)이나 리다이렉트로 별도 포착된다.
            return _verdict("error", 403, "HTTP 403 (접근 거부·안티봇 가능)")
        return _verdict("error", code, f"HTTP {code}")

    # --- net == 'ok' : 2xx 본문 확보 ---
    final_url = obs.get("final_url") or original_url
    have_base = bool(baseline and baseline.get("captured"))

    if not have_base:
        # 콜드스타트(기준선 없음): 비교 불가. 잘 앵커링된 '삭제' 표식만 신뢰하고,
        # 오탐 주범인 '차단(로그인 벽)' 표식은 무시한 채 다음 검사로 미룬다.
        # 기준선은 '첫 live 확인 시 1회'만 캡처한다. 삭제 상태에서 캡처하면 그 표식이
        # 기준선에 박혀 newly_del 이 영영 False 가 되어 다음 검사에 live 로 뒤집히는
        # 자가오염이 생기므로, 삭제 분기에선 캡처하지 않는다(소프트 삭제는 큐에 남아
        # 매 회 재판정되고, 실제 복구 시 del_match 가 사라지며 그때 기준선을 잡는다).
        if obs.get("del_match"):
            return _verdict("deleted", code, f"삭제 표식: {obs.get('del_snip')}")
        new_base = {
            "final_url": final_url,
            "len": obs.get("text_len"),
            "del_match": obs.get("del_match", False),
            "blk_match": obs.get("blk_match", False),
        }
        return _verdict("live", code, None, baseline=new_base)

    # --- 기준선 보유: 변화 기반 판정 ---
    base_len = baseline.get("len") or 0
    cur_len = obs.get("text_len")
    collapsed = (
        base_len >= _COLLAPSE_MIN_BASELEN
        and cur_len is not None
        and cur_len < base_len * _COLLAPSE_RATIO
    )
    newly_del = obs.get("del_match") and not baseline.get("del_match")
    newly_blk = obs.get("blk_match") and not baseline.get("blk_match")
    # 변화 비교의 기준은 '기준선이 캡처된 시점의 최종 URL'(base_url)이다. 등록 당시
    # 원본 URL(original_url)이 아니라 기준선과 비교해야, 기준선이 이미 root 였던 경우
    # 매 검사가 거짓 '삭제'로 발화하는 것을 막는다.
    base_url = baseline.get("final_url") or original_url
    moved = _url_key(final_url) != _url_key(base_url)
    moved_to_root = moved and _is_site_root(final_url)

    # 우선순위: 명시적 표식(삭제) → 구조적 사라짐(메인 리다이렉트) → 명시적 표식(차단)
    #         → 일반적 본문 급감. 구체 신호를 일반 신호보다 앞세워 라벨 정확도 확보.
    if newly_del:
        return _verdict("deleted", code, f"삭제 표식 새로 등장: {obs.get('del_snip')}")
    if moved_to_root:
        return _verdict("deleted", code, f"게시물 사라짐·메인 리다이렉트: {final_url[:120]}")
    if newly_blk:
        return _verdict("blocked", code, f"차단 표식 새로 등장: {obs.get('blk_snip')}")
    if collapsed:
        return _verdict("deleted", code, f"본문 급감 {base_len}→{cur_len}자")
    # 경로만 바뀐 리다이렉트(moved 이지만 root 도 아니고 근거 없음)는 정규화/슬러그
    # 변경일 수 있어 live 유지(보수적). moved 단독으론 삭제로 판정하지 않는다.
    return _verdict("live", code, None)


def _baseline_from_row(row: dict) -> dict | None:
    """citation_checks 행에서 기준선 dict 구성. 아직 캡처 전이면 None."""
    if not row.get("baseline_at"):
        return None
    return {
        "captured": True,
        "final_url": row.get("baseline_final_url"),
        "len": row.get("baseline_len"),
        "del_match": bool(row.get("baseline_del_match")),
        "blk_match": bool(row.get("baseline_blk_match")),
    }


def _build_update(res: dict, row: dict, now_iso: str) -> dict:
    """판정 결과를 citation_checks update payload 로. 기준선 캡처 시 컬럼 추가."""
    upd = {
        "status": res["status"],
        "http_code": res["http_code"],
        "reason": res["reason"],
        "last_checked": now_iso,
        "check_count": (row.get("check_count") or 0) + 1,
    }
    if res.get("baseline"):
        b = res["baseline"]
        upd.update({
            "baseline_final_url": b["final_url"],
            "baseline_len": b["len"],
            "baseline_del_match": b["del_match"],
            "baseline_blk_match": b["blk_match"],
            "baseline_at": now_iso,
        })
    return upd


def register_citations(story_id: str, citations: list) -> None:
    """새 스토리의 citations URL 들을 추적 테이블에 등록 (멱등)."""
    if not citations:
        return
    db = get_db()
    rows = []
    for c in citations:
        uri = (c or {}).get("uri")
        if not uri or not isinstance(uri, str):
            continue
        rows.append({
            "story_id": story_id,
            "url": uri,
            "status": "unchecked",
        })
    if not rows:
        return
    try:
        db.table("citation_checks").upsert(rows, on_conflict="story_id,url").execute()
    except Exception as e:
        logger.warning(f"[tracker] register failed for {story_id}: {e}")


async def _trigger_auto_archive_if_needed(story_id: str) -> None:
    """삭제·차단 감지 시 effective threshold 만큼 표가 모였는지 확인하고 박제."""
    try:
        from services.threshold import maybe_archive_now
        await maybe_archive_now(story_id)
    except Exception as e:
        logger.warning(f"[tracker] auto-archive check failed for {story_id}: {e}")


def _newly_gone(res: dict, prev_status) -> bool:
    """이번 판정이 '삭제·차단'이고 직전엔 아니었으면(새로 사라짐) True."""
    return (res["status"] in ("deleted", "blocked")
            and prev_status not in ("deleted", "blocked"))


# citation_checks.deleted_at 컬럼 지원 여부(마이그레이션 전이면 None→False 로 1회 탐지).
# 미설치 상태에서 deleted_at 을 update 에 넣으면 400 으로 상태 갱신 자체가 실패하므로 가드.
_deleted_at_supported: bool | None = None


def _has_deleted_at(db) -> bool:
    global _deleted_at_supported
    if _deleted_at_supported is None:
        try:
            db.table("citation_checks").select("deleted_at").limit(1).execute()
            _deleted_at_supported = True
        except Exception:
            _deleted_at_supported = False
            logger.info("[tracker] citation_checks.deleted_at 미설치 — 시계열은 "
                        "last_checked 폴백 (supabase_migration_2026-06.sql 적용 권장)")
    return _deleted_at_supported


async def _process_row(db, row: dict, client: httpx.AsyncClient) -> tuple[dict, object, bool]:
    """citation 한 행을 재검사(관측 → 판정 → DB 갱신).
    반환: (판정 dict, 직전 status, 갱신 성공 여부)."""
    obs = await fetch_observation(row["url"], client)
    res = decide_status(obs, row["url"], _baseline_from_row(row))
    prev_status = row.get("status")
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        upd = _build_update(res, row, now_iso)
        # 처음 'deleted' 로 바뀐 순간에만 최초감지 시각 기록(컬럼 있을 때만).
        if res["status"] == "deleted" and prev_status != "deleted" and _has_deleted_at(db):
            upd["deleted_at"] = now_iso
        db.table("citation_checks").update(upd).eq("id", row["id"]).execute()
        return res, prev_status, True
    except Exception as e:
        logger.warning(f"[tracker] update fail {row['id']}: {e}")
        return res, prev_status, False


async def recheck_one_story(story_id: str) -> int:
    """특정 스토리의 모든 citation 즉시 재검사. 검사한 개수 반환."""
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select(
            "id,url,check_count,status,"
            "baseline_final_url,baseline_len,baseline_del_match,baseline_blk_match,baseline_at"
        )
        .eq("story_id", story_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return 0

    newly_deleted = False
    async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
        for row in rows:
            res, prev_status, _ok = await _process_row(db, row, client)
            if _newly_gone(res, prev_status):
                newly_deleted = True

    # 새로 사라진 글이 있으면 자동 박제 검사
    if newly_deleted:
        await _trigger_auto_archive_if_needed(story_id)

    return len(rows)


async def recheck_batch(batch_size: int = CHECK_BATCH_SIZE) -> int:
    """오래된 (또는 한 번도 안 본) 레코드 N 개를 재검사. 검사한 개수 반환.

    sticky 정책: HTTP 404/410 으로 확정된 'hard deleted' 만 영구 제외한다(진짜 사라짐).
    본문 패턴으로만 잡힌 'soft deleted'(http_code 가 404/410 이 아님)는 오탐 가능성이 있어
    재검사 대상에 남겨, 실제로 살아있으면 다음 검사에서 live 로 자동 정정되게 한다.
    (last_checked 오름차순 정렬이라 방금 확인한 soft deleted 는 큐 뒤로 밀려 starvation 방지)"""
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select(
            "id,story_id,url,check_count,status,http_code,"
            "baseline_final_url,baseline_len,baseline_del_match,baseline_blk_match,baseline_at"
        )
        .or_("status.neq.deleted,and(status.eq.deleted,http_code.not.in.(404,410))")
        .order("last_checked", desc=False, nullsfirst=True)
        .limit(batch_size)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return 0

    newly_changed_stories: set[str] = set()
    async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
        for row in rows:
            res, prev_status, ok = await _process_row(db, row, client)
            # 새로 삭제·차단된 글은 자동 박제 검사 대상 (DB 갱신 성공 시에만)
            if ok and _newly_gone(res, prev_status) and row.get("story_id"):
                newly_changed_stories.add(row["story_id"])

    # 새로 사라진/차단된 글이 발견된 스토리들에 대해 자동 박제 검사
    for sid in newly_changed_stories:
        await _trigger_auto_archive_if_needed(sid)

    return len(rows)


def get_status_map(story_ids: list) -> dict:
    """여러 스토리의 citation 상태를 한 번에 조회. 반환: {story_id: {url: row}}"""
    if not story_ids:
        return {}
    db = get_db()
    resp = (
        db.table("citation_checks")
        .select("story_id,url,status,http_code,last_checked,reason")
        .in_("story_id", story_ids)
        .execute()
    )
    out: dict = {}
    for r in resp.data or []:
        out.setdefault(r["story_id"], {})[r["url"]] = r
    return out


async def background_loop() -> None:
    """앱 lifespan 동안 도는 추적 루프."""
    logger.info(f"[tracker] started · interval={CHECK_INTERVAL_SEC}s · batch={CHECK_BATCH_SIZE}")
    # 시작 시 약간 지연 (앱 부팅 안정화)
    await asyncio.sleep(15)
    while True:
        try:
            n = await recheck_batch()
            if n:
                logger.info(f"[tracker] rechecked {n} citation(s)")
        except asyncio.CancelledError:
            logger.info("[tracker] cancelled")
            return
        except Exception as e:
            logger.warning(f"[tracker] loop error: {e}")

        # 박제 실패/누락 글 재시도 (sweeper): 임계값 넘겼지만 미박제인 글을
        # 지수 백오프로 자동 복구. 일시 장애로 박제가 영영 누락되는 것 방지.
        try:
            from services.archive import reconcile_pending_archives
            r = await reconcile_pending_archives()
            if r:
                logger.info(f"[tracker] reconciled {r} pending archive(s)")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"[tracker] reconcile error: {e}")

        try:
            await asyncio.sleep(CHECK_INTERVAL_SEC)
        except asyncio.CancelledError:
            return

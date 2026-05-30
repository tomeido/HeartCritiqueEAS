"""
⚠️ LEGACY / DEPRECATED — 런타임에서 사용되지 않음 (Docker 이미지에도 미포함).
   프로덕션 진입점은 main.py + routers/ + services/ 다. 이 파일의 프롬프트·도메인
   목록·정규식은 services/llm.py 와 갈라져 있으니 신규 변경은 services/ 에만 할 것.
   참고/이관용으로만 보존.

Heart & Critique - A2A serverless handler with x402-gated sources

매 요청마다 동전을 던져:
  - 50% : Gemini + Google Search 로 실시간 조사한 따뜻한 선행 이야기
  - 50% : 대기업의 '인류애가 흔들릴 만한' 무거운 사건 정리

수익화 모델:
  - 이야기 본문: 무료 (message/send)
  - 참고 출처 링크: x402 (HTTP 402 Payment Required) 로 USDC 최소단위(0.000001) 결제 후
    공개. 무료 응답에 서버가 서명한 sourceToken 이 동봉되며, sources/reveal 호출 시
    이 토큰을 결제와 함께 제출하면 서버가 해독해 돌려준다.

엔드포인트:
  GET  /                            - HTML UI (브라우저) 또는 JSON info (curl/agent)
  GET  /.well-known/agent-card.json - A2A agent card (무료)
  POST /                            - JSON-RPC 2.0
                                       method=message/send     : 무료 (이야기)
                                       method=sources/reveal   : x402 결제 (출처)
"""

import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT = 50

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip()
GROQ_ENDPOINT = os.environ.get(
    "GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions"
).strip()
GROQ_TIMEOUT = 50

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_ENDPOINT = os.environ.get(
    "TAVILY_ENDPOINT", "https://api.tavily.com/search"
).strip()
TAVILY_TIMEOUT = 20
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))

# Optional comma-separated override; if set, applies to both categories.
_tavily_override = os.environ.get("TAVILY_INCLUDE_DOMAINS", "").strip()
TAVILY_INCLUDE_DOMAINS_OVERRIDE = (
    [d.strip() for d in _tavily_override.split(",") if d.strip()]
    if _tavily_override else None
)

DOMAINS_KINDNESS = [
    # 포털 뉴스 + 종합지
    "news.naver.com", "news.daum.net",
    "hani.co.kr", "joongang.co.kr", "chosun.com", "donga.com",
    "khan.co.kr", "ohmynews.com", "kmib.co.kr",
    # 방송 / 통신사
    "sbs.co.kr", "kbs.co.kr", "imbc.com", "ytn.co.kr",
    "yonhapnews.co.kr", "news1.kr", "newsis.com",
    # 한국인 공감 커뮤니티 (미담 자주 회자)
    "theqoo.net", "pann.nate.com",
]

DOMAINS_CRITIQUE = [
    # 탐사 / 대안 매체 (대기업 비판 보도 강함)
    "hani.co.kr", "khan.co.kr", "ohmynews.com",
    "pressian.com", "sisain.co.kr", "newstapa.org", "mediatoday.co.kr",
    # 경제지
    "mk.co.kr", "hankyung.com", "edaily.co.kr", "fnnews.com", "mt.co.kr",
    # 메이저 종합지 / 방송
    "chosun.com", "joongang.co.kr", "donga.com",
    "sbs.co.kr", "kbs.co.kr", "imbc.com", "ytn.co.kr",
    "yonhapnews.co.kr", "news1.kr", "newsis.com",
    # 오너 일가·연예 스캔들 보도
    "dispatch.co.kr", "ilyo.co.kr",
    # 포털 뉴스
    "news.naver.com", "news.daum.net",
]


X402_PAY_TO = os.environ.get("X402_PAY_TO", "").strip()
X402_NETWORK = os.environ.get("X402_NETWORK", "base-sepolia").strip()
X402_AMOUNT = os.environ.get("X402_AMOUNT", "1").strip()
X402_FACILITATOR = os.environ.get(
    "X402_FACILITATOR", "https://www.x402.org/facilitator"
).rstrip("/")
X402_SETTLE = os.environ.get("X402_SETTLE", "true").lower() != "false"

X402_NETWORKS = {
    "base-sepolia": {
        "chainId": 84532,
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "extra": {"name": "USDC", "version": "2"},
    },
    "base": {
        "chainId": 8453,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "extra": {"name": "USD Coin", "version": "2"},
    },
}


PROMPT_KINDNESS = """\
한국어로 쓰는 따뜻한 큐레이터. 아래 검색 결과 중 '평범한 사람의 따뜻한 선행' 한 가지를 골라
5~8문장으로 들려줘. 규칙: 검색 결과 사실 기반(창작·과장 금지), 인물·지역·시점은 결과에
나오는 만큼만 구체적으로, 톤은 담담하고 따뜻하게. 본문 마지막 줄에 "오늘의 한 줄: ..." 형식
한 줄 메시지. URL·출처 표기는 본문에 넣지 마(시스템이 별도로 붙여줌).
응답 맨 끝(오늘의 한 줄 다음)에 별도 한 줄로 정확히 다음 형식의 메타 라인을 적어:
USED_SOURCES: [번호, 번호] — 실제로 본문 근거로 사용한 검색 결과 번호만 (1개여도 대괄호 안에).
이 메타 라인은 시스템이 제거하니 자유롭게 적어. 출력은 본문 + 메타 라인만.
"""

PROMPT_CRITIQUE = """\
한국어로 쓰는 냉정한 탐사 기자. 아래 검색 결과 중 잘 알려진 대기업(국내 재벌·플랫폼·글로벌
메가코퍼) 한 곳의 '인류애가 흔들릴 만한' 무거운 사건 한 가지를 6~9문장으로 사실대로 정리.
선호 사건: 노동자 사망·중대재해 은폐, 결함·유해 제품, 하청·갑질·임금 착취, 내부고발자 보복,
회계·뇌물·담합, 약탈적 마케팅, 정보 유출·기만, 그리고 대표·총수·오너 일가의 도덕적 타락
(갑질·폭언·마약·음주운전·성범죄·횡령·탈세·세습 비리 등) 도 포함. 규칙: 검색 결과 근거, 창작·미확인 의혹 금지.
회사명·시점·장소·피해 규모, 비판 주체(언론·법원·정부·노조 등) 명시. 톤은 차가운 사실 나열,
분노 형용사 금지. 중소·신생기업·단순 광고 논란은 피하고 생명·존엄·생계 영향 사건 우선.
본문 마지막 줄에 정확히:
"※ 비판 관점 요약이며, 해당 기업의 공식 입장과 다를 수 있습니다."
URL·출처 표기는 본문에 넣지 마.
응답 맨 끝(disclaimer 다음)에 별도 한 줄로 정확히 다음 형식의 메타 라인을 적어:
USED_SOURCES: [번호, 번호] — 실제로 본문 근거로 사용한 검색 결과 번호만 (1개여도 대괄호 안에).
이 메타 라인은 시스템이 제거하니 자유롭게 적어. 출력은 본문 + 메타 라인만.
"""


SEARCH_QUERIES_KINDNESS = [
    "최근 따뜻한 선행 미담 시민 뉴스",
    "익명 기부 미담 보도 한국",
    "이웃 도움 선행 뉴스 최근",
    "구조 영웅 시민 따뜻한 뉴스",
    "일상 선행 따뜻한 사연 보도",
]

SEARCH_QUERIES_CRITIQUE = [
    "대기업 노동자 사망 산재 은폐 보도",
    "대기업 갑질 하청 임금 착취 뉴스",
    "대기업 결함 제품 안전사고 보도",
    "대기업 내부고발자 보복 뉴스",
    "대기업 회계 부정 뇌물 사건 보도",
    "대기업 환경 오염 피해 보도",
    "대기업 정보 유출 소비자 기만 뉴스",
    "재벌 총수 오너 갑질 폭언 보도",
    "재벌 2세 3세 마약 음주운전 사건",
    "대기업 회장 대표 횡령 탈세 기소",
    "재벌 오너 일가 성범죄 사건 보도",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def call_gemini(prompt: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY env var is not configured for this deployment."
        )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 1.0,
            "topP": 0.95,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Gemini network error: {e.reason}") from e


def parse_gemini_response(data: dict):
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        raise RuntimeError(f"Gemini returned no candidates: {feedback or data}")
    cand = candidates[0]

    parts = ((cand.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts).strip()

    gm = cand.get("groundingMetadata") or {}
    chunks = gm.get("groundingChunks") or []
    queries = gm.get("webSearchQueries") or []

    citations = []
    seen = set()
    for c in chunks:
        web = c.get("web") or {}
        uri = web.get("uri")
        title = web.get("title")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        citations.append({"title": title or uri, "uri": uri})

    return text, citations, queries


def call_groq(prompt, system: str = None) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY env var is not configured for this deployment."
        )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.8,
        "top_p": 0.9,
        "max_tokens": 1024,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GROQ_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "heart-critique-a2a/4.1 (+https://heart-critique-a2a.vercel.app)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GROQ_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Groq network error: {e.reason}") from e


def parse_groq_chat_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Groq returned no choices: {data}")
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise RuntimeError(f"Groq returned empty content: {choices[0]}")
    return text


def tavily_search(query: str, include_domains=None, max_results: int = None) -> dict:
    if not TAVILY_API_KEY:
        raise RuntimeError(
            "TAVILY_API_KEY env var is not configured for this deployment."
        )
    payload = {
        "query": query,
        "max_results": max_results or TAVILY_MAX_RESULTS,
        "search_depth": "basic",
        "topic": "news",
        "include_answer": False,
    }
    if include_domains:
        payload["include_domains"] = list(include_domains)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TAVILY_API_KEY}",
            "User-Agent": "heart-critique-a2a/5.0 (+https://heart-critique-a2a.vercel.app)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TAVILY_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tavily HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Tavily network error: {e.reason}") from e


def normalize_search_results(data: dict) -> list:
    """Returns list of {title, url, content} dicts from Tavily response."""
    results = data.get("results") or []
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        out.append({
            "title": (r.get("title") or url) if isinstance(r.get("title"), str) else url,
            "url": url,
            "content": (r.get("content") or "")[:600],
        })
    return out


USED_SOURCES_RE = re.compile(
    r'(?im)^[ \t]*USED_SOURCES[ \t]*[:=][ \t]*\[?([0-9,\s]*)\]?[ \t]*$'
)


def extract_used_indices(text: str, total: int) -> tuple:
    """Strip the USED_SOURCES meta line from `text` and return
    (cleaned_text, sorted_unique_indices_in_[1, total]). If the line is
    missing or malformed, returns (text, [])."""
    m = USED_SOURCES_RE.search(text)
    if not m:
        return text.strip(), []
    raw = m.group(1)
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    seen = set()
    indices = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok.isdigit():
            continue
        i = int(tok)
        if 1 <= i <= total and i not in seen:
            seen.add(i)
            indices.append(i)
    return cleaned, sorted(indices)


def build_search_context(results: list) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = r["title"]
        content = r["content"].replace("\n", " ").strip()
        lines.append(f"[{i}] 제목: {title}")
        if content:
            lines.append(f"    내용: {content}")
    return "\n".join(lines) if lines else "(검색 결과 없음)"


def format_story_text(category: str, body: str, citation_count: int) -> str:
    header = (
        "[ 따뜻한 선행 이야기 ]" if category == "kindness"
        else "[ 인류애가 흔들리는 대기업 사건 ]"
    )
    out = [header, "", body.strip()]
    if citation_count > 0:
        out.append("")
        out.append(
            f"※ 참고 링크 {citation_count}개는 잠겨 있습니다. "
            f"x402 결제(USDC 최소단위) 후 sources/reveal 로 확인할 수 있습니다."
        )
    return "\n".join(out)


def format_sources_text(category: str, citations: list) -> str:
    header = (
        "[ 출처 - 따뜻한 선행 이야기 ]" if category == "kindness"
        else "[ 출처 - 인류애가 흔들리는 대기업 사건 ]"
    )
    out = [header, ""]
    if not citations:
        out.append("(공개할 출처가 없습니다.)")
    else:
        out.append("참고 링크")
        for i, c in enumerate(citations, 1):
            out.append(f"  [{i}] {c['title']} - {c['uri']}")
    return "\n".join(out)


def _source_secret() -> bytes:
    base = (
        os.environ.get("X402_SOURCE_KEY")
        or X402_PAY_TO
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or "heart-critique-default-dev-secret"
    )
    return hashlib.sha256(
        ("heart-critique-source-v1:" + base).encode("utf-8")
    ).digest()


def encrypt_source_token(payload: dict) -> str:
    pt = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    secret = _source_secret()
    nonce = os.urandom(16)
    keystream = hashlib.shake_256(secret + nonce).digest(len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, keystream))
    tag = hmac.new(secret, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt_source_token(token: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception as e:
        raise ValueError(f"sourceToken decode failed: {e}") from e
    if len(raw) < 16 + 32:
        raise ValueError("sourceToken too short")
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    secret = _source_secret()
    expected = hmac.new(secret, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("sourceToken signature invalid (다른 서버 키로 발급된 토큰일 수 있음)")
    keystream = hashlib.shake_256(secret + nonce).digest(len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, keystream))
    try:
        return json.loads(pt.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"sourceToken inner payload invalid: {e}") from e


def generate_via_groq_search(category: str) -> tuple:
    """Pre-search architecture: Tavily search → Llama chat. Returns
    (text, citations, queries, model_name)."""
    seeds = (
        SEARCH_QUERIES_KINDNESS if category == "kindness"
        else SEARCH_QUERIES_CRITIQUE
    )
    query = random.choice(seeds)
    domains = TAVILY_INCLUDE_DOMAINS_OVERRIDE or (
        DOMAINS_KINDNESS if category == "kindness" else DOMAINS_CRITIQUE
    )
    search_data = tavily_search(query, include_domains=domains)
    results = normalize_search_results(search_data)
    # Fallback: if the curated domain pool returned nothing, retry unrestricted
    # so the user still gets a story (better than hard-failing).
    if not results:
        search_data = tavily_search(query)
        results = normalize_search_results(search_data)
    context = build_search_context(results)

    system_prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
    user_prompt = (
        f"아래 검색 결과 중 하나를 골라 위 규칙대로 들려줘.\n\n"
        f"검색 결과:\n{context}"
    )

    chat = call_groq(user_prompt, system=system_prompt)
    raw_text = parse_groq_chat_text(chat)

    text, used_indices = extract_used_indices(raw_text, len(results))
    if used_indices:
        chosen = [results[i - 1] for i in used_indices]
    else:
        chosen = results
    citations = [{"title": r["title"], "uri": r["url"]} for r in chosen]
    return text, citations, [query], GROQ_MODEL


def generate() -> dict:
    category = "kindness" if random.random() < 0.5 else "critique"

    if LLM_PROVIDER == "groq":
        text, citations, queries, model_name = generate_via_groq_search(category)
    elif LLM_PROVIDER == "gemini":
        prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
        raw = call_gemini(prompt)
        text, citations, queries = parse_gemini_response(raw)
        model_name = GEMINI_MODEL
    else:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={LLM_PROVIDER!r}; expected 'groq' or 'gemini'."
        )

    source_token = encrypt_source_token({
        "category": category,
        "citations": citations,
        "searchQueries": queries,
        "issuedAt": int(time.time()),
    })
    citation_count = len(citations)
    formatted = format_story_text(category, text, citation_count)
    return {
        "category": category,
        "text": formatted,
        "body": text,
        "citationCount": citation_count,
        "searchQueriesCount": len(queries),
        "sourceToken": source_token,
        "provider": LLM_PROVIDER,
        "model": model_name,
    }


def get_public_url(headers) -> str:
    host = (
        headers.get("x-forwarded-host") or headers.get("X-Forwarded-Host")
        or headers.get("host") or headers.get("Host") or "localhost"
    )
    proto = headers.get("x-forwarded-proto") or headers.get("X-Forwarded-Proto")
    if not proto:
        proto = "https" if "vercel" in host or host.endswith(".app") else "http"
    return f"{proto}://{host}/"


def build_payment_requirements(resource_url: str) -> dict:
    net = X402_NETWORKS.get(X402_NETWORK) or X402_NETWORKS["base-sepolia"]
    return {
        "scheme": "exact",
        "network": X402_NETWORK,
        "maxAmountRequired": X402_AMOUNT,
        "resource": resource_url,
        "description": (
            "Heart & Critique - 이야기 출처(참고 링크) 공개를 위한 USDC 최소단위 결제"
        ),
        "mimeType": "application/json",
        "payTo": X402_PAY_TO,
        "maxTimeoutSeconds": 60,
        "asset": net["asset"],
        "outputSchema": None,
        "extra": net["extra"],
    }


def build_402_body(resource_url: str, error: str = None) -> dict:
    return {
        "x402Version": 1,
        "accepts": [build_payment_requirements(resource_url)],
        "error": error,
    }


def facilitator_call(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{X402_FACILITATOR}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"facilitator {path} HTTP {e.code}: {msg[:400]}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"facilitator {path} network error: {e.reason}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"facilitator {path} unexpected: {e}") from e
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(
            f"facilitator {path} non-JSON response: {raw[:400]}"
        ) from e


def verify_payment(x_payment_b64: str, requirements: dict):
    try:
        decoded = base64.b64decode(x_payment_b64)
        payment = json.loads(decoded)
    except Exception as e:
        return False, f"X-PAYMENT decode failed: {e}", None
    body = {
        "x402Version": 1,
        "paymentPayload": payment,
        "paymentRequirements": requirements,
    }
    try:
        res = facilitator_call("/verify", body)
    except Exception as e:
        return False, str(e), payment
    if res.get("isValid"):
        return True, None, payment
    return False, res.get("invalidReason") or "verification failed", payment


def settle_payment(payment: dict, requirements: dict) -> dict:
    body = {
        "x402Version": 1,
        "paymentPayload": payment,
        "paymentRequirements": requirements,
    }
    return facilitator_call("/settle", body)


def build_agent_card(public_url: str) -> dict:
    paid_url = public_url
    return {
        "name": "Heart & Critique",
        "description": (
            "50% 확률로 최근 보도된 익명/일상의 따뜻한 선행을, "
            "50% 확률로 대기업이 일으킨 '인류애가 흔들릴 만한' 무거운 사건을 들려주는 "
            "A2A 에이전트. " + (
                f"Tavily 뉴스 검색으로 출처 후보를 모은 뒤 {GROQ_MODEL} 로 "
                "한국어 본문을 합성합니다. "
                if LLM_PROVIDER == "groq"
                else f"{GEMINI_MODEL} + Google Search grounding 으로 실시간 조사합니다. "
            ) +
            "이야기 본문은 무료. 매 응답에 동봉된 sourceToken 을 sources/reveal "
            "메서드에 x402 결제(USDC 최소단위)와 함께 제출하면 참고 링크가 공개됩니다."
        ),
        "version": "5.0.0",
        "protocolVersion": "0.2.5",
        "url": paid_url,
        "preferredTransport": "JSONRPC",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text", "text/plain"],
        "defaultOutputModes": ["text", "text/plain"],
        "skills": [
            {
                "id": "kindness_or_critique_story",
                "name": "Kindness or Critique - Story (free)",
                "description": (
                    "동전을 던져 50% 확률로 한 가지 이야기를 무료로 들려줍니다: "
                    "(1) 따뜻한 선행 또는 (2) 대기업의 무거운 사건. "
                    "웹 검색 그라운딩 LLM으로 실시간 조사. JSON-RPC method=message/send. "
                    "응답 데이터의 sourceToken 으로 출처를 잠금 해제할 수 있습니다."
                ),
                "tags": ["kindness", "critique", "news", "grounded", "korean",
                         LLM_PROVIDER, "free"],
                "examples": ["오늘의 이야기 하나", "랜덤으로 하나"],
                "inputModes": ["text"],
                "outputModes": ["text"],
            },
            {
                "id": "sources_reveal",
                "name": "Reveal sources (x402 paid)",
                "description": (
                    "직전 이야기의 출처 링크(URL)를 공개합니다. "
                    "JSON-RPC method=sources/reveal, params={sourceToken}, "
                    "헤더 X-PAYMENT 에 EIP-3009 USDC 최소단위 결제 첨부."
                ),
                "tags": ["sources", "citations", "x402", "usdc", "paid"],
                "examples": ["출처 보여줘", "참고 링크 공개"],
                "inputModes": ["text"],
                "outputModes": ["text"],
            },
        ],
        "x402": {
            "network": X402_NETWORK,
            "asset": X402_NETWORKS.get(X402_NETWORK, {}).get("asset"),
            "amount": X402_AMOUNT,
            "payTo": X402_PAY_TO,
            "facilitator": X402_FACILITATOR,
            "method": "sources/reveal",
        },
    }


def build_agent_message(text, task_id, context_id, data=None):
    parts = [{"kind": "text", "text": text}]
    if data:
        parts.append({"kind": "data", "data": data})
    return {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "agent",
        "parts": parts,
        "taskId": task_id,
        "contextId": context_id,
    }


def build_story_task(user_message, result):
    task_id = user_message.get("taskId") or str(uuid.uuid4())
    context_id = user_message.get("contextId") or str(uuid.uuid4())
    user_message.setdefault("taskId", task_id)
    user_message.setdefault("contextId", context_id)

    structured = {
        "category": result["category"],
        "provider": result.get("provider"),
        "model": result["model"],
        "citationCount": result["citationCount"],
        "searchQueriesCount": result["searchQueriesCount"],
        "sourcesPaid": False,
        "sourceToken": result["sourceToken"],
        "revealHint": {
            "method": "sources/reveal",
            "params": {"sourceToken": "<paste sourceToken here>"},
            "header": "X-PAYMENT: <base64 EIP-3009 USDC authorization>",
        },
    }
    agent_msg = build_agent_message(result["text"], task_id, context_id, structured)
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "completed", "timestamp": now_iso()},
        "history": [user_message, agent_msg],
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "reply",
                "parts": [
                    {"kind": "text", "text": result["text"]},
                    {"kind": "data", "data": structured},
                ],
                "metadata": {"category": result["category"]},
            }
        ],
    }


def build_sources_task(user_message, decoded, payment_meta):
    task_id = user_message.get("taskId") or str(uuid.uuid4())
    context_id = user_message.get("contextId") or str(uuid.uuid4())
    user_message.setdefault("taskId", task_id)
    user_message.setdefault("contextId", context_id)

    category = decoded.get("category") or "unknown"
    citations = decoded.get("citations") or []
    queries = decoded.get("searchQueries") or []

    text = format_sources_text(category, citations)
    structured = {
        "category": category,
        "citations": citations,
        "searchQueries": queries,
        "sourcesPaid": True,
        "payment": payment_meta,
    }
    agent_msg = build_agent_message(text, task_id, context_id, structured)
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "completed", "timestamp": now_iso()},
        "history": [user_message, agent_msg],
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "sources",
                "parts": [
                    {"kind": "text", "text": text},
                    {"kind": "data", "data": structured},
                ],
                "metadata": {"category": category, "kind": "sources"},
            }
        ],
    }


def build_error_task(user_message, err):
    task_id = user_message.get("taskId") or str(uuid.uuid4())
    context_id = user_message.get("contextId") or str(uuid.uuid4())
    user_message.setdefault("taskId", task_id)
    user_message.setdefault("contextId", context_id)
    text = f"[ 오류 ] 실시간 조사에 실패했어요.\n\n{err}"
    agent_msg = build_agent_message(text, task_id, context_id)
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "failed",
            "timestamp": now_iso(),
            "message": {
                "kind": "message", "messageId": str(uuid.uuid4()), "role": "agent",
                "parts": [{"kind": "text", "text": err}],
            },
        },
        "history": [user_message, agent_msg],
        "artifacts": [],
    }


def jsonrpc_error(rpc_id, code, message):
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": code, "message": message}}


def jsonrpc_result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


FRONTEND_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Heart & Critique - x402 demo</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo",
          "Segoe UI", "Noto Sans KR", sans-serif;
    background: radial-gradient(1200px 600px at 50% -10%, #fde2e4 0%, transparent 60%),
                radial-gradient(1000px 500px at 80% 80%, #cfe1ff 0%, transparent 60%),
                #fafafa;
    color: #1a1a1a;
  }
  @media (prefers-color-scheme: dark) {
    body { background:
      radial-gradient(1200px 600px at 50% -10%, #2a1820 0%, transparent 60%),
      radial-gradient(1000px 500px at 80% 80%, #16263f 0%, transparent 60%),
      #0c0c0e; color: #f0f0f0; }
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 48px 20px 96px; }
  h1 { font-size: 28px; margin: 0 0 6px; letter-spacing: -.02em; }
  .sub { opacity: .7; margin: 0 0 28px; font-size: 14px; }
  .card {
    background: rgba(255,255,255,.66);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(0,0,0,.06);
    border-radius: 16px;
    padding: 20px 22px;
    margin-bottom: 16px;
    box-shadow: 0 1px 0 rgba(0,0,0,.03);
  }
  @media (prefers-color-scheme: dark) {
    .card { background: rgba(28,28,32,.6); border-color: rgba(255,255,255,.08); }
  }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  button {
    appearance: none; border: 0; cursor: pointer;
    padding: 11px 18px; border-radius: 10px;
    font-size: 14px; font-weight: 600;
    background: #111; color: #fff;
    transition: transform .04s ease, background .12s ease;
  }
  button:hover { background: #000; }
  button:active { transform: translateY(1px); }
  button[disabled] { background: #bbb; color: #fff; cursor: not-allowed; }
  @media (prefers-color-scheme: dark) {
    button { background: #f6f6f6; color: #111; }
    button[disabled] { background: #444; color: #ccc; }
  }
  .pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    background: rgba(0,0,0,.06); font-size: 12px;
  }
  @media (prefers-color-scheme: dark) {
    .pill { background: rgba(255,255,255,.08); }
  }
  .muted { opacity: .7; }
  pre {
    white-space: pre-wrap; word-break: break-word;
    margin: 0; font: 14px/1.65 ui-monospace, SFMono-Regular,
                                 "JetBrains Mono", Consolas, monospace;
  }
  .out {
    background: rgba(0,0,0,.03);
    border-radius: 12px; padding: 18px 18px;
    border: 1px solid rgba(0,0,0,.06);
    min-height: 60px;
  }
  @media (prefers-color-scheme: dark) {
    .out { background: rgba(255,255,255,.03); border-color: rgba(255,255,255,.06); }
  }
  a { color: inherit; }
  .links { font-size: 12px; opacity: .65; margin-top: 24px; }
  .links a { margin-right: 12px; }
  .status { font-size: 13px; min-height: 1.4em; opacity: .8; }
  .err { color: #c0392b; }
  .ok { color: #2e7d32; }
  details summary {
    cursor: pointer; font-size: 12px; opacity: .65; padding: 4px 0;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Heart &amp; Critique</h1>
  <p class="sub">
    동전을 던져 50% 확률로 들려드립니다 — <b>따뜻한 선행 이야기</b> 또는
    <b>대기업의 인류애가 흔들리는 사건</b>.
    <br>이야기 본문은 무료. <b>출처(참고 링크)</b> 는 x402 결제(USDC 최소단위) 후 공개됩니다.
  </p>

  <div class="card">
    <div class="row" style="justify-content: space-between">
      <div class="row">
        <span class="pill" id="net-pill">network: …</span>
        <span class="pill" id="amount-pill">출처 가격: …</span>
      </div>
      <div class="row">
        <span id="acct" class="pill muted">미연결</span>
        <button id="connect">지갑 연결</button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content: space-between; margin-bottom: 12px">
      <b>1) 이야기 한 가지 들려주세요 (무료)</b>
      <button id="ask">무료로 호출</button>
    </div>
    <div class="status" id="status"></div>
    <div class="out" id="output"><span class="muted">아직 결과 없음.</span></div>
    <details style="margin-top: 14px">
      <summary>raw response</summary>
      <pre id="raw" class="muted"></pre>
    </details>
  </div>

  <div class="card" id="sources-card" style="display: none">
    <div class="row" style="justify-content: space-between; margin-bottom: 12px">
      <b>2) 출처 공개 (x402 결제)</b>
      <button id="reveal" disabled>지갑 연결 후 결제</button>
    </div>
    <div class="status" id="sources-status"></div>
    <div class="out" id="sources-output"><span class="muted">아직 공개되지 않음.</span></div>
    <details style="margin-top: 14px">
      <summary>raw response</summary>
      <pre id="sources-raw" class="muted"></pre>
    </details>
  </div>

  <div class="links">
    <a href="/.well-known/agent-card.json" target="_blank">Agent Card</a>
    <a href="https://faucet.circle.com/" target="_blank">Base Sepolia USDC faucet</a>
    <a href="https://github.com/coinbase/x402" target="_blank">x402 spec</a>
  </div>
</div>

<script type="module">
const cfg = __CFG__;
document.getElementById('net-pill').textContent = `network: ${cfg.network}`;
const human = (Number(cfg.amount) / 1e6).toFixed(6);
document.getElementById('amount-pill').textContent =
  `출처 가격: ${cfg.amount} atom (${human} USDC)`;

let viem;
try {
  viem = await import('https://esm.sh/viem@2.21.43');
} catch (e) {
  setStoryStatus('viem CDN 로드 실패: ' + e.message, 'err');
}
const { createWalletClient, custom } = viem || {};

let walletClient, account;
let lastSourceToken = null;

function setStoryStatus(msg, cls) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status ' + (cls || '');
}

function setSourcesStatus(msg, cls) {
  const el = document.getElementById('sources-status');
  el.textContent = msg;
  el.className = 'status ' + (cls || '');
}

function shortAddr(a) { return a ? a.slice(0,6) + '…' + a.slice(-4) : ''; }

const CHAIN_HEX = {
  'base-sepolia': '0x14a34',
  'base': '0x2105',
};
const CHAIN_ADD_PARAMS = {
  'base-sepolia': {
    chainId: '0x14a34',
    chainName: 'Base Sepolia',
    rpcUrls: ['https://sepolia.base.org'],
    nativeCurrency: { name: 'ETH', symbol: 'ETH', decimals: 18 },
    blockExplorerUrls: ['https://sepolia.basescan.org'],
  },
  'base': {
    chainId: '0x2105',
    chainName: 'Base',
    rpcUrls: ['https://mainnet.base.org'],
    nativeCurrency: { name: 'ETH', symbol: 'ETH', decimals: 18 },
    blockExplorerUrls: ['https://basescan.org'],
  },
};

async function ensureChain() {
  const want = CHAIN_HEX[cfg.network];
  if (!want) throw new Error('unsupported network: ' + cfg.network);
  try {
    await window.ethereum.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: want }],
    });
  } catch (e) {
    if (e.code === 4902) {
      await window.ethereum.request({
        method: 'wallet_addEthereumChain',
        params: [CHAIN_ADD_PARAMS[cfg.network]],
      });
    } else {
      throw e;
    }
  }
}

function updateRevealButton() {
  const btn = document.getElementById('reveal');
  if (!lastSourceToken) {
    btn.disabled = true;
    btn.textContent = '먼저 이야기를 받으세요';
    return;
  }
  if (!account) {
    btn.disabled = true;
    btn.textContent = '지갑 연결 후 결제';
    return;
  }
  btn.disabled = false;
  btn.textContent = `${human} USDC 결제하고 출처 보기`;
}

document.getElementById('connect').onclick = async () => {
  if (!window.ethereum) {
    setStoryStatus('EVM 지갑(MetaMask 등)이 필요합니다.', 'err');
    return;
  }
  try {
    setStoryStatus('지갑 연결 중...');
    const [addr] = await window.ethereum.request({ method: 'eth_requestAccounts' });
    account = addr;
    await ensureChain();
    walletClient = createWalletClient({
      account: addr,
      transport: custom(window.ethereum),
    });
    document.getElementById('acct').textContent = shortAddr(addr);
    document.getElementById('acct').classList.remove('muted');
    setStoryStatus('지갑 연결 완료. 이제 출처도 결제로 공개할 수 있어요.', 'ok');
    updateRevealButton();
  } catch (e) {
    setStoryStatus('연결 실패: ' + (e.shortMessage || e.message), 'err');
  }
};

function randomNonceHex() {
  const buf = new Uint8Array(32);
  crypto.getRandomValues(buf);
  return '0x' + Array.from(buf).map(b => b.toString(16).padStart(2,'0')).join('');
}

async function signAuthorization(accept) {
  const now = Math.floor(Date.now() / 1000);
  const validAfter = String(now - 60);
  const validBefore = String(now + (accept.maxTimeoutSeconds || 60));
  const nonce = randomNonceHex();
  const message = {
    from: account,
    to: accept.payTo,
    value: String(accept.maxAmountRequired),
    validAfter,
    validBefore,
    nonce,
  };
  const chainId = parseInt(CHAIN_HEX[accept.network], 16);
  const signature = await walletClient.signTypedData({
    account,
    domain: {
      name: accept.extra?.name || 'USDC',
      version: accept.extra?.version || '2',
      chainId,
      verifyingContract: accept.asset,
    },
    types: {
      TransferWithAuthorization: [
        { name: 'from', type: 'address' },
        { name: 'to', type: 'address' },
        { name: 'value', type: 'uint256' },
        { name: 'validAfter', type: 'uint256' },
        { name: 'validBefore', type: 'uint256' },
        { name: 'nonce', type: 'bytes32' },
      ],
    },
    primaryType: 'TransferWithAuthorization',
    message,
  });
  return { signature, message };
}

function buildXPayment(accept, signed) {
  const payload = {
    x402Version: 1,
    scheme: accept.scheme,
    network: accept.network,
    payload: {
      signature: signed.signature,
      authorization: signed.message,
    },
  };
  const json = JSON.stringify(payload);
  return btoa(unescape(encodeURIComponent(json)));
}

async function describeError(r) {
  const txt = await r.text();
  console.error('[server error response]', r.status, txt);
  let detail = txt;
  try {
    const j = JSON.parse(txt);
    if (j.error) {
      detail = (typeof j.error === 'string') ? j.error
        : (j.error.message || JSON.stringify(j.error));
    } else if (j.invalidReason) {
      detail = j.invalidReason;
    }
  } catch {}
  return 'HTTP ' + r.status + ': ' + detail;
}

document.getElementById('ask').onclick = async () => {
  const askBtn = document.getElementById('ask');
  askBtn.disabled = true;
  document.getElementById('output').innerHTML =
    '<span class="muted">Gemini 가 실시간 조사 중... (보통 5~20초)</span>';
  document.getElementById('raw').textContent = '';
  document.getElementById('sources-card').style.display = 'none';
  document.getElementById('sources-output').innerHTML =
    '<span class="muted">아직 공개되지 않음.</span>';
  document.getElementById('sources-raw').textContent = '';
  lastSourceToken = null;
  setSourcesStatus('');
  setStoryStatus('이야기 요청 중 (무료)...');

  const askBody = JSON.stringify({
    jsonrpc: '2.0', id: Date.now(), method: 'message/send',
    params: { message: { messageId: crypto.randomUUID(), role: 'user',
      kind: 'message', parts: [{ kind: 'text', text: '하나 들려줘' }] } },
  });

  try {
    const r = await fetch('/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: askBody,
    });
    if (!r.ok) throw new Error(await describeError(r));

    const data = await r.json();
    const msg = data?.result?.history?.[1];
    const text = msg?.parts?.[0]?.text;
    const structured = msg?.parts?.find(p => p.kind === 'data')?.data || {};
    lastSourceToken = structured.sourceToken || null;

    document.getElementById('output').innerHTML = '';
    const pre = document.createElement('pre');
    pre.textContent = text || JSON.stringify(data, null, 2);
    document.getElementById('output').appendChild(pre);
    document.getElementById('raw').textContent = JSON.stringify(data, null, 2);

    setStoryStatus('완료 (무료).', 'ok');

    if (lastSourceToken) {
      document.getElementById('sources-card').style.display = '';
      setSourcesStatus(
        `출처 ${structured.citationCount ?? '?'}개가 잠금 상태입니다. ` +
        `${account ? '결제 버튼을 누르세요.' : '먼저 지갑을 연결하세요.'}`);
    }
    updateRevealButton();
  } catch (e) {
    setStoryStatus('실패: ' + (e.shortMessage || e.message), 'err');
    document.getElementById('output').innerHTML =
      '<span class="err">'+ (e.message || 'error') +'</span>';
  } finally {
    askBtn.disabled = false;
  }
};

document.getElementById('reveal').onclick = async () => {
  const revealBtn = document.getElementById('reveal');
  if (!lastSourceToken) {
    setSourcesStatus('먼저 이야기를 받으세요.', 'err');
    return;
  }
  if (!account || !walletClient) {
    setSourcesStatus('먼저 지갑을 연결하세요.', 'err');
    return;
  }
  revealBtn.disabled = true;
  document.getElementById('sources-output').innerHTML =
    '<span class="muted">서버에 결제 challenge 요청 중...</span>';
  document.getElementById('sources-raw').textContent = '';
  setSourcesStatus('서버에 첫 요청 (402 응답 대기)...');

  const body = JSON.stringify({
    jsonrpc: '2.0', id: Date.now(), method: 'sources/reveal',
    params: { sourceToken: lastSourceToken },
  });

  try {
    let r = await fetch('/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body,
    });

    if (r.status === 402) {
      const challenge = await r.json();
      console.log('[x402 challenge]', challenge);
      const accept = challenge.accepts?.[0];
      if (!accept) throw new Error('서버 challenge 에 accepts 없음');
      if (!accept.payTo || /^0x0+$/.test(accept.payTo)) {
        throw new Error('서버의 X402_PAY_TO 가 비어있음 (Vercel env var 확인)');
      }
      setSourcesStatus('지갑에서 EIP-3009 서명 중...');
      const signed = await signAuthorization(accept);
      const xPayment = buildXPayment(accept, signed);
      setSourcesStatus('서명 완료. 출처 해독 중...');
      r = await fetch('/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json; charset=utf-8',
          'X-PAYMENT': xPayment,
        },
        body,
      });
    }

    if (!r.ok) throw new Error(await describeError(r));

    const data = await r.json();
    const msg = data?.result?.history?.[1];
    const text = msg?.parts?.[0]?.text;
    document.getElementById('sources-output').innerHTML = '';
    const pre = document.createElement('pre');
    pre.textContent = text || JSON.stringify(data, null, 2);
    document.getElementById('sources-output').appendChild(pre);
    document.getElementById('sources-raw').textContent = JSON.stringify(data, null, 2);

    const settle = r.headers.get('X-PAYMENT-RESPONSE');
    if (settle) {
      try {
        const meta = JSON.parse(atob(settle));
        setSourcesStatus(
          '완료. 결제 tx: ' + (meta.transaction || meta.txHash || '(see raw)'), 'ok');
      } catch { setSourcesStatus('완료', 'ok'); }
    } else {
      setSourcesStatus('완료', 'ok');
    }
  } catch (e) {
    setSourcesStatus('실패: ' + (e.shortMessage || e.message), 'err');
    document.getElementById('sources-output').innerHTML =
      '<span class="err">'+ (e.message || 'error') +'</span>';
  } finally {
    updateRevealButton();
  }
};
</script>
</body>
</html>
"""


def render_frontend(public_url: str) -> bytes:
    cfg = {
        "network": X402_NETWORK,
        "amount": X402_AMOUNT,
        "resource": public_url,
        "payToConfigured": bool(X402_PAY_TO),
    }
    html = FRONTEND_HTML.replace("__CFG__", json.dumps(cfg))
    return html.encode("utf-8")


def wants_html(accept_header: str) -> bool:
    if not accept_header:
        return False
    return "text/html" in accept_header.lower()


class handler(BaseHTTPRequestHandler):
    server_version = "HeartCritiqueA2A/5.0"

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Expose-Headers", "X-PAYMENT-RESPONSE")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return self.path.split("?", 1)[0]

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-PAYMENT")
        self.send_header(
            "Access-Control-Expose-Headers", "X-PAYMENT-RESPONSE")
        self.end_headers()

    def do_GET(self):
        p = self._path()
        public_url = get_public_url(self.headers)

        if "agent-card.json" in p or p.endswith("/agent.json") or p.endswith("/card"):
            self._send_json(200, build_agent_card(public_url))
            return

        if p in ("/", "/api/index", "/api/index.py"):
            if wants_html(self.headers.get("Accept", "")):
                self._send_html(render_frontend(public_url))
                return
            card = build_agent_card(public_url)
            if LLM_PROVIDER == "groq":
                provider_configured = bool(GROQ_API_KEY) and bool(TAVILY_API_KEY)
            else:
                provider_configured = bool(GEMINI_API_KEY)
            self._send_json(200, {
                "agent": card["name"],
                "a2aUrl": public_url,
                "agentCard": public_url + ".well-known/agent-card.json",
                "hint": (
                    "POST JSON-RPC 2.0: 'message/send' is free, "
                    "'sources/reveal' (with sourceToken in params) requires X-PAYMENT."
                ),
                "llmProvider": LLM_PROVIDER,
                "providerConfigured": provider_configured,
                "searchProvider": "tavily" if LLM_PROVIDER == "groq" else "gemini-google-search",
                "tavilyConfigured": bool(TAVILY_API_KEY),
                "x402": card["x402"],
            })
            return

        self._send_json(404, {"error": "not found", "path": p})

    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            sys.stderr.write(f"[do_POST fatal] {e}\n{tb}\n")
            try:
                self._send_json(500, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {
                        "code": -32099,
                        "message": f"server crash: {type(e).__name__}: {e}",
                    },
                })
            except Exception:
                pass

    def _handle_post(self):
        public_url = get_public_url(self.headers)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, jsonrpc_error(None, -32700, "Parse error"))
            return

        rpc_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "agent/getCard":
            self._send_json(200, jsonrpc_result(rpc_id, build_agent_card(public_url)))
            return

        if method in ("message/send", "tasks/send"):
            self._handle_story(rpc_id, params)
            return

        if method == "sources/reveal":
            self._handle_sources_reveal(rpc_id, params, public_url)
            return

        self._send_json(
            200, jsonrpc_error(rpc_id, -32601, f"Method not found: {method}"))

    def _normalize_user_message(self, params):
        message = params.get("message") or {}
        message.setdefault("kind", "message")
        message.setdefault("role", "user")
        message.setdefault("messageId", str(uuid.uuid4()))
        return message

    def _handle_story(self, rpc_id, params):
        message = self._normalize_user_message(params)
        try:
            result = generate()
            task = build_story_task(message, result)
            self._send_json(200, jsonrpc_result(rpc_id, task))
        except Exception as e:
            sys.stderr.write(f"[gemini error] {e}\n")
            task = build_error_task(message, str(e))
            self._send_json(200, jsonrpc_result(rpc_id, task))

    def _handle_sources_reveal(self, rpc_id, params, public_url):
        if not X402_PAY_TO:
            self._send_json(503, jsonrpc_error(
                rpc_id, -32000,
                "Server X402_PAY_TO env var is not set; payment cannot be routed."))
            return

        source_token = params.get("sourceToken")
        if not source_token or not isinstance(source_token, str):
            self._send_json(
                400, jsonrpc_error(rpc_id, -32602,
                                   "params.sourceToken (string) is required"))
            return

        requirements = build_payment_requirements(public_url)
        x_payment = self.headers.get("X-PAYMENT") or self.headers.get("x-payment")
        if not x_payment:
            self._send_json(
                402, build_402_body(public_url, "X-PAYMENT header required"))
            return

        ok, reason, payment = verify_payment(x_payment, requirements)
        if not ok:
            self._send_json(
                402, build_402_body(public_url, f"verify failed: {reason}"))
            return

        try:
            decoded = decrypt_source_token(source_token)
        except ValueError as e:
            self._send_json(
                400, jsonrpc_error(rpc_id, -32001, f"invalid sourceToken: {e}"))
            return

        message = self._normalize_user_message(params)

        payment_meta = {"verified": True, "settled": False}
        extra_headers = {}
        if X402_SETTLE:
            try:
                settle_res = settle_payment(payment, requirements)
                payment_meta["settled"] = bool(settle_res.get("success"))
                payment_meta["transaction"] = (
                    settle_res.get("transaction") or settle_res.get("txHash"))
                payment_meta["network"] = (
                    settle_res.get("networkId") or X402_NETWORK)
                if not payment_meta["settled"]:
                    payment_meta["settleErrorReason"] = (
                        settle_res.get("errorReason")
                        or settle_res.get("error")
                        or "settle returned success=false")
                try:
                    enc = base64.b64encode(
                        json.dumps(settle_res).encode("utf-8")
                    ).decode("ascii")
                    extra_headers["X-PAYMENT-RESPONSE"] = enc
                except Exception:
                    pass
            except Exception as e:
                sys.stderr.write(f"[settle error] {e}\n")
                payment_meta["settleError"] = str(e)

        task = build_sources_task(message, decoded, payment_meta)
        self._send_json(
            200, jsonrpc_result(rpc_id, task), extra_headers=extra_headers)

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{self.address_string()} - {fmt % args}\n"
        )

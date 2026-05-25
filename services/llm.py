"""
LLM + 검색 파이프라인.
api/index.py에서 추출, x402 관련 코드 제거 후 citations를 직접 반환.
"""

import json
import os
import random
import re
import urllib.error
import urllib.request

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
TAVILY_ENDPOINT = os.environ.get("TAVILY_ENDPOINT", "https://api.tavily.com/search").strip()
TAVILY_TIMEOUT = 20
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "5"))

_tavily_override = os.environ.get("TAVILY_INCLUDE_DOMAINS", "").strip()
TAVILY_INCLUDE_DOMAINS_OVERRIDE = (
    [d.strip() for d in _tavily_override.split(",") if d.strip()]
    if _tavily_override else None
)

DOMAINS_KINDNESS = [
    "news.naver.com", "news.daum.net",
    "hani.co.kr", "joongang.co.kr", "chosun.com", "donga.com",
    "khan.co.kr", "ohmynews.com", "kmib.co.kr",
    "sbs.co.kr", "kbs.co.kr", "imbc.com", "ytn.co.kr",
    "yonhapnews.co.kr", "news1.kr", "newsis.com",
    "theqoo.net", "pann.nate.com",
]

DOMAINS_CRITIQUE = [
    "hani.co.kr", "khan.co.kr", "ohmynews.com",
    "pressian.com", "sisain.co.kr", "newstapa.org", "mediatoday.co.kr",
    "mk.co.kr", "hankyung.com", "edaily.co.kr", "fnnews.com", "mt.co.kr",
    "chosun.com", "joongang.co.kr", "donga.com",
    "sbs.co.kr", "kbs.co.kr", "imbc.com", "ytn.co.kr",
    "yonhapnews.co.kr", "news1.kr", "newsis.com",
    "dispatch.co.kr", "ilyo.co.kr",
    "news.naver.com", "news.daum.net",
]

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

USED_SOURCES_RE = re.compile(
    r'(?im)^[ \t]*USED_SOURCES[ \t]*[:=][ \t]*\[?([0-9,\s]*)\]?[ \t]*$'
)


def _http_post(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e.reason}") from e


def call_gemini(prompt: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
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


def call_groq(prompt: str, system: str = None) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _http_post(
        GROQ_ENDPOINT,
        {"model": GROQ_MODEL, "messages": messages, "temperature": 0.8, "top_p": 0.9, "max_tokens": 1024},
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "heart-critique/6.0",
        },
        GROQ_TIMEOUT,
    )


def parse_groq_chat_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Groq returned no choices: {data}")
    text = (choices[0].get("message") or {}).get("content", "").strip()
    if not text:
        raise RuntimeError("Groq returned empty content")
    return text


def tavily_search(query: str, include_domains=None) -> dict:
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not set")
    payload = {
        "query": query,
        "max_results": TAVILY_MAX_RESULTS,
        "search_depth": "basic",
        "topic": "news",
        "include_answer": False,
    }
    if include_domains:
        payload["include_domains"] = list(include_domains)
    return _http_post(
        TAVILY_ENDPOINT,
        payload,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TAVILY_API_KEY}",
            "User-Agent": "heart-critique/6.0",
        },
        TAVILY_TIMEOUT,
    )


def normalize_search_results(data: dict) -> list:
    out = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        out.append({
            "title": r.get("title") or url,
            "url": url,
            "content": (r.get("content") or "")[:600],
        })
    return out


def extract_used_indices(text: str, total: int) -> tuple:
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
        content = r["content"].replace("\n", " ").strip()
        lines.append(f"[{i}] 제목: {r['title']}")
        if content:
            lines.append(f"    내용: {content}")
    return "\n".join(lines) if lines else "(검색 결과 없음)"


def generate_via_groq(category: str) -> tuple:
    seeds = SEARCH_QUERIES_KINDNESS if category == "kindness" else SEARCH_QUERIES_CRITIQUE
    query = random.choice(seeds)
    domains = TAVILY_INCLUDE_DOMAINS_OVERRIDE or (
        DOMAINS_KINDNESS if category == "kindness" else DOMAINS_CRITIQUE
    )
    search_data = tavily_search(query, include_domains=domains)
    results = normalize_search_results(search_data)
    if not results:
        search_data = tavily_search(query)
        results = normalize_search_results(search_data)

    system_prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
    user_prompt = f"아래 검색 결과 중 하나를 골라 위 규칙대로 들려줘.\n\n검색 결과:\n{build_search_context(results)}"

    chat = call_groq(user_prompt, system=system_prompt)
    raw_text = parse_groq_chat_text(chat)

    text, used_indices = extract_used_indices(raw_text, len(results))
    chosen = [results[i - 1] for i in used_indices] if used_indices else results
    citations = [{"title": r["title"], "uri": r["url"]} for r in chosen]
    return text, citations, [query], GROQ_MODEL


def generate() -> dict:
    category = "kindness" if random.random() < 0.5 else "critique"

    if LLM_PROVIDER == "groq":
        text, citations, queries, model_name = generate_via_groq(category)
    elif LLM_PROVIDER == "gemini":
        prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
        raw = call_gemini(prompt)
        text, citations, queries = parse_gemini_response(raw)
        model_name = GEMINI_MODEL
    else:
        raise RuntimeError(f"Unknown LLM_PROVIDER={LLM_PROVIDER!r}")

    header = (
        "[ 따뜻한 선행 이야기 ]" if category == "kindness"
        else "[ 인류애가 흔들리는 대기업 사건 ]"
    )

    return {
        "category": category,
        "text": header + "\n\n" + text.strip(),
        "body": text,
        "citations": citations,
        "search_queries": queries,
        "provider": LLM_PROVIDER,
        "model": model_name,
    }

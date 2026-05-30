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

import logging
logger = logging.getLogger(__name__)

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT = 50

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
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

# 언론 윗단의 '날것' - 익명·검열 전·삭제 위협 받는 글이 1차로 도는 곳들.
# CONTEXT.md 정신: "대기업의 자본력에 삭제되는 Web2 커뮤니티의 사각지대" 박제.

DOMAINS_KINDNESS = [
    "pann.nate.com",       # 일반인 익명 사연 메카
    "theqoo.net",          # 여초, 미담 활발
    "instiz.net",          # 여초, 따뜻한 글 게시판
    "clien.net",           # 중장년 IT, 진중한 톤
    "ppomppu.co.kr",       # 자유게시판 미담
    "bobaedream.co.kr",    # 베스트 사연 활발
    "ruliweb.com",         # 게이머·소소한 미담
    "fmkorea.com",         # 대형 자유게시판
    "dcinside.com",        # 디시 (varied)
]

DOMAINS_CRITIQUE = [
    "teamblind.com",       # 직장인 익명 폭로 (한국 회사 정보)
    "theqoo.net",          # 사회 이슈·갑질 폭로 활발
    "fmkorea.com",         # 자유게시판 폭로
    "pann.nate.com",       # 일반인 폭로
    "bobaedream.co.kr",    # 차·제조업 비판
    "clien.net",           # IT 회사 내부 정보
    "dcinside.com",        # 회사 갤러리에 내부고발 자주
    "ruliweb.com",         # 소비자 권익 글
    "ppomppu.co.kr",       # 소비자 불만/폭로
    "instiz.net",          # 여초의 사회 이슈
]

# 격차 탐지용 - 메이저 언론 도메인. 커뮤니티에 도는 이슈가 여기 안 잡히면
# 검열·압박 신호로 간주.
NEWS_DOMAINS = [
    "chosun.com", "joongang.co.kr", "donga.com",
    "hani.co.kr", "khan.co.kr", "kmib.co.kr",
    "sbs.co.kr", "kbs.co.kr", "imbc.com", "ytn.co.kr",
    "jtbc.co.kr", "tvchosun.com", "mbn.co.kr", "channela.com",
    "yonhapnews.co.kr", "news1.kr", "newsis.com",
    "news.naver.com", "news.daum.net",
    "mk.co.kr", "hankyung.com", "edaily.co.kr",
    "ohmynews.com", "pressian.com", "mediatoday.co.kr",
    "newstapa.org", "sisain.co.kr",
    "dispatch.co.kr", "ilyo.co.kr",
]

GAP_DETECTION_ENABLED = os.environ.get("GAP_DETECTION_ENABLED", "true").lower() != "false"


def calculate_gap_score(community: int, news: int) -> str:
    """선정된 커뮤니티 글 제목을 언론에서 검색한 결과로 격차 산출.
    community 는 첫 검색 결과 수(맥락 정보), news 는 같은 사건의 언론 보도 수.
    news == 0 이면 이 구체적 사건이 메이저 언론에 없음 = 강한 검열 신호."""
    if community == 0:
        return "none"
    if news == 0:
        return "extreme"   # 이 구체적 사건이 언론에 0건
    if news == 1:
        return "high"      # 거의 보도 안 됨
    if news == 2:
        return "medium"
    return "none"          # 3건 이상이면 언론도 충분히 다룸

PROMPT_KINDNESS = """\
한국어로 쓰는 큐레이터. 아래는 한국 커뮤니티 게시판(더쿠·클리앙·인스티즈·네이트판·FM코리아·
보배드림 등)에서 모은 익명 미담 글들. 정제된 언론 보도가 아닌 일반인의 날것 사연.

[엄격 규칙]
1. 결과에 명시된 내용만 써. 결과에 없는 인물명·지명·날짜·숫자·인용문은 절대 만들지 마.
2. 익명 게시글 특성을 반영: "한 누리꾼이 공유한 사연에 따르면", "어느 게시글에서는",
   "한 작성자가 올린 글에 의하면" 같은 표현 사용. "있었다"·"~했다" 단정조 자제.
3. 검증되지 않은 개인 사연이라는 점을 잊지 말 것: "~했다고 한다", "~라는 글이 올라왔다" 형식.
4. 결과가 모호하면 모호하게: "한 시민이", "최근 어느 지역에서", "한 누리꾼은" 등.
5. 순수 한국어만. 한자(简体/繁體)·영어·일본어 일절 섞지 말 것. 외래어는 한글로.
6. 마크다운 금지: **굵게**, ##헤더, *목록*, --- 일절 사용 금지. 평서문만.
7. 메타 발화 금지: "검색 결과", "선택했습니다", "다음과 같이" 등 시스템 발화 금지.
8. 본문은 곧바로 사연으로 시작. 인사말·서론·결론 안내 금지.
9. 분량 5~8문장. 단락 나누지 마.
10. 톤은 담담하고 따뜻하게. 과장 형용사 절제.
11. URL·커뮤니티명은 본문에 넣지 마 (시스템이 별도로 출처를 붙임).

[출력 형식]
<본문 5~8문장>
오늘의 한 줄: <짧은 감상 한 줄>
USED_SOURCES: [번호, 번호]
"""

PROMPT_CRITIQUE = """\
한국어로 쓰는 큐레이터. 아래는 한국 커뮤니티 게시판(블라인드·더쿠·FM코리아·디시·클리앙·
보배드림 등)에서 모은 익명 폭로·제보성 글들. 정제된 언론 보도가 아닌, 검열·법적조치 받기
전의 날것 주장. 사라지기 전에 박제할 가치가 있는지 인간 투표로 가린다.

[엄격 규칙]
1. 결과에 명시된 내용만 써. 결과에 없는 인물명·직책·날짜·금액·인용문은 절대 만들지 마.
2. 모든 주장은 "~라는 글이 올라왔다", "~라는 의혹이 제기됐다", "한 작성자에 따르면" 형식으로.
   사실 단정 금지. 익명 커뮤니티 주장임을 항상 명시.
3. 회사명이 게시글에서 분명하지 않으면 "한 대기업"·"해당 기업"·"한 업체" 같은 일반 표현.
4. 인용부호("…") 안에는 게시글에 그대로 등장하는 표현만. 추측 인용 금지.
5. 순수 한국어만. 한자(简体/繁體)·영어·일본어 일절 섞지 말 것. 외래어는 한글로.
6. 마크다운 금지: **굵게**, ##헤더, *목록*, --- 일절 사용 금지. 평서문만.
7. 메타 발화 금지: "검색 결과", "X번 선택", "다음과 같이" 등 시스템 발화 금지.
8. 본문은 곧바로 의혹/주장으로 시작. 인사말·서론·결론 안내 금지.
9. 분량 6~9문장. 단락 나누지 마.
10. 톤은 차가운 사실 보고. 분노 형용사("끔찍한", "용서할 수 없는", "충격적인") 사용 금지.
11. URL·커뮤니티명은 본문에 넣지 마.

[출력 형식]
<본문 6~9문장>
※ 익명 커뮤니티 게시글 기반의 미확인 주장이며, 사실로 확정되지 않았고 해당 기업의 공식
입장과 다를 수 있습니다.
USED_SOURCES: [번호, 번호]

관심 분야: 직장 갑질·폭언, 노동 환경 문제, 하청·납품 갑질, 내부고발자 보복, 임금 체불,
제품 결함·소비자 기만, 오너 일가의 도덕적 타락(갑질·폭언·마약·음주운전·성범죄·횡령·탈세),
회계 부정. 사소한 광고 트집·개인 분쟁은 피하고 다수에게 영향 가는 사건 우선.
"""

SEARCH_QUERIES_KINDNESS = [
    "지하철에서 도와준 사람 사연",
    "길에서 쓰러진 사람 도와준 후기",
    "병원에서 도와준 사람 이야기",
    "어르신 도와준 사연 글",
    "아이 도와준 사람 후기",
    "익명 기부 받은 사연",
    "모르는 사람이 도와준 후기",
    "버스에서 자리 양보 사연",
    "위기 상황 도와준 사람 글",
    "잃어버린 물건 찾아준 사람",
    "마음 따뜻해진 사연 후기",
    "이런 사람도 있구나 사연 글",
    "감사한 사람 후기 게시글",
    "오늘 받은 친절 사연",
]

SEARCH_QUERIES_CRITIQUE = [
    "대기업 갑질 폭로 글",
    "회사 내부고발 후기",
    "재벌 2세 갑질 사건",
    "오너 일가 갑질 폭언",
    "직장 상사 폭언 폭로",
    "회사 임원 비리 폭로",
    "대기업 하청 갑질 후기",
    "묻혔던 사건 폭로",
    "은폐된 사건 폭로",
    "직장인 익명 폭로 후기",
    "직장 갑질 사건 모음",
    "기업 비위 묻힌 사건",
]

# 줄 끝/문장 끝/단독 줄 어디든 잡도록 앵커 완화
USED_SOURCES_RE = re.compile(
    r'(?i)\s*USED_SOURCES[ \t]*[:=][ \t]*\[?([0-9,\s]*)\]?\s*$',
    re.MULTILINE,
)

# 본문 시작에 종종 나오는 LLM 메타 발화 패턴
META_PREFIX_RE = re.compile(
    r'(?im)^\s*(?:검색\s*결과(?:에\s*따르면|의?\s*\d+\s*번을?\s*(?:선택|골라)\S*)?'
    r'|다음(?:은|과)\s+같이'
    r'|아래(?:는|와)\s+같이'
    r'|아래\s+검색\s*결과'
    r'|네[,.]?\s*'
    r'|네\s*알겠습니다'
    r')[^\n]*\n+'
)

# 마크다운 잔재
MD_BOLD_RE   = re.compile(r'\*\*([^*\n]+?)\*\*')
MD_ITALIC_RE = re.compile(r'(?<![*\w])\*([^*\n]+?)\*(?![*\w])')
MD_UNDER_RE  = re.compile(r'__([^_\n]+?)__')
MD_HEADER_RE = re.compile(r'(?m)^#{1,6}[ \t]+')
MD_HR_RE     = re.compile(r'(?m)^[ \t]*(?:-{3,}|={3,}|\*{3,})[ \t]*$')


# 한자: 연속 2자 이상만 제거(모델이 가끔 섞는 중국어 단어/구).
# 단일 한자(예: 성씨 '李', 약칭 '中')는 정상 한국어 표현일 수 있어 보존한다.
CJK_CHINESE_RE = re.compile(r'[一-鿿]{2,}')
HIRAGANA_KATAKANA_RE = re.compile(r'[぀-ゟ゠-ヿ]+')  # 일본 가나
# 'SOUTH KOREA' 처럼 ALL-CAPS 영단어가 2개 이상 연속될 때만 제거.
# 단일 약어(KTX·CCTV·GDP·CEO 등)는 정상 본문이므로 보존한다.
CAPS_NOISE_RE = re.compile(r'\b[A-Z]{2,}(?:\s+[A-Z]{2,})+\b')


def sanitize_text(text: str) -> str:
    """LLM 출력에서 마크다운 기호·메타 발화·외국어 잔재 제거."""
    # 시작 부분 메타 발화 한 번 제거
    text = META_PREFIX_RE.sub('', text, count=1)

    # 마크다운 강조 기호 제거 (텍스트는 보존)
    text = MD_BOLD_RE.sub(r'\1', text)
    text = MD_UNDER_RE.sub(r'\1', text)
    text = MD_ITALIC_RE.sub(r'\1', text)

    # 헤더/구분선 제거
    text = MD_HEADER_RE.sub('', text)
    text = MD_HR_RE.sub('', text)

    # 한자/일본 가나 잔재 제거 (모델이 가끔 섞음)
    text = CJK_CHINESE_RE.sub('', text)
    text = HIRAGANA_KATAKANA_RE.sub('', text)

    # SOUTH KOREA, CEO 같은 ALL-CAPS 영어 잡음 제거
    text = CAPS_NOISE_RE.sub('', text)

    # 외국어 제거로 생긴 연속 공백/이상한 구두점 정리
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\s+([.,!?。、])', r'\1', text)
    text = re.sub(r'([가-힣])\s+([가-힣])', r'\1 \2', text)  # 한글 사이 다중 공백 단일화

    # 빈 줄 정리 (3개 이상의 개행 → 2개)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


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
        "search_depth": "advanced",
        "topic": "general",  # 커뮤니티 게시판은 뉴스가 아님
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


# 커뮤니티 게시판에 자주 복붙되는 뉴스 기사 식별 패턴.
# 우리는 "검열 전 날것의 익명 글"을 원하지, 정제된 언론 보도를 원하지 않음.
NEWS_INDICATORS_RE = re.compile(
    # 뉴스 헤드라인 머리표
    r'\[(?:속보|단독|종합|특보|특집|기획|르포|분석|사설|칼럼|인터뷰|이슈|화제|영상|사진|뉴스|기자수첩)\]'
    # "○○○ 기자 = " 형식
    r'|기자\s*[\]=]'
    # 언론사명 직접 노출
    r'|(?:뉴스1|연합뉴스|뉴시스|YTN|SBS\s?뉴스|KBS\s?뉴스|MBC\s?뉴스|JTBC|TV조선|MBN|채널A'
    r'|조선일보|중앙일보|동아일보|한겨레|경향신문|국민일보|문화일보|세계일보|서울신문'
    r'|매일경제|한국경제|머니투데이|이데일리|아시아경제|파이낸셜뉴스|디지털타임스'
    r'|오마이뉴스|프레시안|미디어오늘|디스패치|일요신문|스포츠조선|스포츠동아)',
    re.IGNORECASE,
)

# URL 경로에 뉴스 게시판 표식이 있으면 거의 100% 기사 복붙
NEWS_URL_PATTERNS = (
    'mid=news', 'mid=hotnews', 'mid=politics_news',
    '/news/', '/article/news', '/article_view',
    'category=news', 'cate=news',
)


def looks_like_news(item: dict) -> bool:
    """뉴스 기사 복붙으로 보이는 결과면 True."""
    url = (item.get("url") or "").lower()
    if any(p in url for p in NEWS_URL_PATTERNS):
        return True

    title = item.get("title") or ""
    if NEWS_INDICATORS_RE.search(title):
        return True

    # 본문 첫 200자만 보고 판단 (전체 검사는 false positive 위험)
    first = (item.get("content") or "")[:200]
    if NEWS_INDICATORS_RE.search(first):
        return True

    return False


def normalize_search_results(data: dict, drop_news: bool = True) -> list:
    out = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        item = {
            "title": r.get("title") or url,
            "url": url,
            "content": (r.get("content") or "")[:600],
        }
        if drop_news and looks_like_news(item):
            continue
        out.append(item)
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


# 커뮤니티 게시판 제목에 자주 붙는 prefix/suffix 정리
TITLE_PREFIX_RE = re.compile(r'^\s*\[[^\]\n]{1,15}\]\s*')
TITLE_SUFFIX_RE = re.compile(
    r'\s*[-–|]\s*(에펨코리아|더쿠|클리앙|블라인드|블라인드라이트|디시인사이드|네이트\s*판|'
    r'보배드림|인스티즈|루리웹|뽐뿌|pann|FM코리아|theqoo|teamblind|보배|디시)[^\n]*$',
    re.IGNORECASE,
)


def clean_title_for_news_search(title: str) -> str:
    """커뮤니티 글 제목에서 사이트 prefix/suffix 제거하고 검색 쿼리로 사용."""
    if not title:
        return ""
    title = TITLE_PREFIX_RE.sub('', title)
    title = TITLE_SUFFIX_RE.sub('', title)
    # 매우 짧은 제목은 무의미 (격차 측정 불가)
    return title.strip()[:150]


def measure_news_coverage(chosen_items: list) -> dict | None:
    """선정된 커뮤니티 글의 title + content 스니펫으로 언론 보도 확인.
    content 가 더 specific 한 정보(회사명·인명·금액 등) 포함하므로 우선 사용.
    None 반환 시 측정 불가."""
    if not GAP_DETECTION_ENABLED or not chosen_items:
        return None

    item = chosen_items[0]
    title_clean = clean_title_for_news_search(item.get("title") or "")
    content = (item.get("content") or "")[:200].replace("\n", " ").strip()

    # 가장 specific 한 정보를 쿼리로
    if content and len(content) >= 20:
        query = content
    elif len(title_clean) >= 8:
        query = title_clean
    else:
        return None  # 측정할 정보 부족

    try:
        news_data = tavily_search(query[:300], include_domains=NEWS_DOMAINS)
        news_results = news_data.get("results") or []
        news_count = sum(
            1 for r in news_results
            if isinstance(r, dict) and isinstance(r.get("url"), str)
        )
    except Exception as e:
        logger.warning(f"[gap] news search failed: {e}")
        return None

    return {"news_count": news_count, "query_used": query[:100]}


def generate_via_groq(category: str) -> tuple:
    seeds = SEARCH_QUERIES_KINDNESS if category == "kindness" else SEARCH_QUERIES_CRITIQUE
    query = random.choice(seeds)
    domains = TAVILY_INCLUDE_DOMAINS_OVERRIDE or (
        DOMAINS_KINDNESS if category == "kindness" else DOMAINS_CRITIQUE
    )
    # 1차: 커뮤니티 도메인 한정 + 뉴스 복붙 필터
    search_data = tavily_search(query, include_domains=domains)
    results = normalize_search_results(search_data, drop_news=True)
    # 2차: 같은 도메인이지만 뉴스 필터 해제 (전부 뉴스 복붙이었을 때)
    if not results:
        results = normalize_search_results(search_data, drop_news=False)
    # 3차: 도메인 제한도 풀고 검색 다시
    if not results:
        search_data = tavily_search(query)
        results = normalize_search_results(search_data, drop_news=False)

    # 격차 탐지는 LLM 선정 후 measure_news_coverage()에서 진행. 일단 community_count만 보관.
    community_count = len(results) if results else 0

    system_prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
    user_prompt = f"아래 검색 결과 중 하나를 골라 위 규칙대로 들려줘.\n\n검색 결과:\n{build_search_context(results)}"

    chat = call_groq(user_prompt, system=system_prompt)
    raw_text = parse_groq_chat_text(chat)

    text, used_indices = extract_used_indices(raw_text, len(results))
    text = sanitize_text(text)
    if used_indices:
        chosen = [results[i - 1] for i in used_indices]
    elif results:
        # LLM 이 USED_SOURCES 를 빠뜨린 경우: 검색결과 전부를 무차별 첨부하면 본문과
        # 무관한 출처까지 박제·추적되어 gap/threshold 신호를 왜곡한다. 보수적으로
        # 첫 결과 1개만 첨부하고 로그를 남긴다.
        chosen = results[:1]
        logger.info(f"[llm] USED_SOURCES 누락 → 첫 출처만 첨부 (query={query!r})")
    else:
        chosen = []
    citations = [{"title": r["title"], "uri": r["url"]} for r in chosen]

    # 격차 탐지: 선정된 글의 content/title 로 언론 보도 여부 확인
    gap_data = None
    coverage = measure_news_coverage(chosen)
    if coverage is not None:
        news_count = coverage["news_count"]
        gap_data = {
            "community_count": community_count,
            "news_count": news_count,
            "gap_score": calculate_gap_score(community_count, news_count),
            "gap_query": coverage["query_used"],
        }

    return text, citations, [query], GROQ_MODEL, gap_data


def generate(category: str | None = None) -> dict:
    if category not in ("kindness", "critique"):
        category = "kindness" if random.random() < 0.5 else "critique"

    gap_data = None
    if LLM_PROVIDER == "groq":
        text, citations, queries, model_name, gap_data = generate_via_groq(category)
    elif LLM_PROVIDER == "gemini":
        prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
        raw = call_gemini(prompt)
        text, citations, queries = parse_gemini_response(raw)
        text = sanitize_text(text)
        model_name = GEMINI_MODEL
        # 격차 탐지는 provider 무관(Tavily 기반)하게 적용. Tavily 미설정이면
        # measure_news_coverage 가 graceful 하게 None 반환 → gap 없이 진행.
        if citations:
            chosen = [
                {"title": c.get("title"), "content": "", "url": c.get("uri")}
                for c in citations
            ]
            coverage = measure_news_coverage(chosen)
            if coverage is not None:
                community_count = len(citations)
                news_count = coverage["news_count"]
                gap_data = {
                    "community_count": community_count,
                    "news_count": news_count,
                    "gap_score": calculate_gap_score(community_count, news_count),
                    "gap_query": coverage["query_used"],
                }
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
        "gap_data": gap_data,
    }

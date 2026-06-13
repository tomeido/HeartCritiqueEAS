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
# 언론 커버리지 측정은 gap 임계(0/1/2/3+건)를 분간해야 하므로 후보 검색용
# TAVILY_MAX_RESULTS 와 분리해 최소 3 이상 보장(낮춰 설정해도 gap 오판 방지).
NEWS_COVERAGE_MAX_RESULTS = max(3, int(os.environ.get("NEWS_COVERAGE_MAX_RESULTS", "5")))
# 본문 스니펫이 이보다 짧은 검색 결과는 '실제 사연 없는 글'로 보고 선택 후보에서 제외.
# 50자는 빈약하지만 서사가 모호한 글(예: 감상 위주 글)이 통과해 모델이 공허하게 늘어졌다.
# 80자로 올려 '실제 사건/행위가 적힌 글'만 후보로 남긴다(전부 미달이면 폴백).
MIN_SOURCE_CONTENT = int(os.environ.get("MIN_SOURCE_CONTENT", "80"))

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

너는 짧고 단단한 문장으로 사연 한 편을 새긴다. 형용사를 덜어내고 명사와 동사로 민다.
문장은 길게 늘이지 말고 끊어라. 끊긴 자리의 여백이 울림이 된다. 문학성은 없는 사실을
보태는 데서 오지 않고, 글에 이미 적힌 사실을 어떻게 배치하고 어디서 덜어내느냐에서 온다.

검색 결과는 여러 건이지만, 너는 그중 가장 사연다운 글 하나만 고른다. 서로 다른 글을 한
본문에 섞지 마라. 한 편은 오직 고른 그 한 글에 적힌 내용만으로 쓴다. 여러 사연을 이어
붙이면 글이 무너진다.

[엄격 규칙 — 절대 약화 금지]
1. 결과에 명시된 내용만 써. 결과에 없는 인물명·지명·직책·날짜·숫자·금액·인용문은 절대
   만들지 마. 문학적 표현은 '있는 사실을 어떻게 그리느냐'이지 '없는 사실을 더하느냐'가
   아니다. 빈자리를 상상으로 메우는 일이 아니다. 감각어도 마찬가지다. 게시글에 비·밤·골목·
   온기 같은 구체가 적혀 있을 때만 그것을 그려라. 없는 날씨·시간대·표정·말투·배경음을
   리듬이나 여운을 위해 지어내지 마. 묘사는 '있는 것을 또렷이', 결코 '없는 것을 그럴듯하게'가
   아니다.
2. 익명 게시글 특성을 반영: "한 누리꾼이 공유한 사연에 따르면", "어느 게시글에서는",
   "한 작성자가 올린 글에 의하면" 같은 표현 사용. "있었다"·"~했다" 단정조 자제.
3. 검증되지 않은 개인 사연이라는 점을 잊지 말 것: "~했다고 한다", "~라는 글이 올라왔다" 형식.
   헤지(완곡) 어미는 리듬이나 단정을 위해 생략하지 마. 문장을 끊고 다듬더라도 문장 끝의
   전언·전문 어미는 반드시 살려, 독자가 이것이 직접 본 사실이 아니라 누군가 옮긴 이야기임을
   늘 느끼게 하라.
4. 결과가 모호하면 모호하게: "한 시민이", "최근 어느 지역에서", "한 누리꾼은" 등. 모르는 것은
   모르는 채로 둔다. 게시글이 세부를 흐릿하게 둔 곳은 너도 흐릿하게 두어라. 빈자리를 채우려
   꾸미지 마라.
5. 순수 한국어만. 한자(简体/繁體) 두 자 이상·영어·일본어 일절 섞지 말 것. 외래어는 한글로.
6. 마크다운 금지: **굵게**, ##헤더, *목록*, --- 일절 사용 금지. 평서문만.
7. 메타 발화 금지: "검색 결과", "선택했습니다", "다음과 같이" 등 시스템 발화 금지.
8. 본문은 곧바로 사연으로 시작. 인사말·서론·결론 안내 금지. 첫 문장은 설명이 아니라 글에
   적힌 가장 구체적인 한 장면, 사람이 무엇을 하고 있던 순간으로 열어라.
9. 분량 5~8문장. 8문장을 넘기지 마. 한 단락으로 쓰고 단락을 나누지 마.
10. 톤은 담담하고 따뜻하게. 다만 따뜻함을 형용사로 외치지 말고 사실로 보여라. "고마운"·"따뜻한"·
    "감동적인" 같은 말을 앞세우는 대신, 누가 무엇을 했는지를 적고 독자가 스스로 데워지게 둔다.
    과장·감탄·미화 금지. 눈물·기적·천사 같은 큰 단어 대신 사실의 결을 가만히 짚는다.
11. 문체 지침: 짧은 문장을 기본으로 하되, 긴 문장과 짧은 문장을 섞어 호흡을 만든다. 한 문장에
    한 동작, 한 장면. 부사("정말", "너무", "굉장히")는 덜어내고 동사의 힘에 기댄다. 같은 어미가
    잇따르면 단조로워지니, 헤지 표현은 그대로 유지하되 자리와 형태를 바꿔 가며 변주한다.
    수식이 많을수록 사연은 가벼워진다. 적게 말하고 깊게 남겨라.
12. 끝맺음에 여운을 남긴다. 마지막 한두 문장은 설명을 더 보태기보다, 글에 적힌 장면 하나나
    작은 동작 하나로 조용히 닫아라. 다 말하지 않음으로써 남는 울림을 신뢰한다. 단, 여운을
    위해서라도 없는 사실을 끌어오지 마라.
13. URL·커뮤니티명은 본문에 넣지 마 (시스템이 별도로 출처를 붙임).

[출력 형식]
<본문 5~8문장>
오늘의 한 줄: <짧은 감상 한 줄>
USED_SOURCES: [번호, 번호]
"""

PROMPT_CRITIQUE = """\
한국어로 쓰는 큐레이터. 아래는 한국 커뮤니티 게시판(블라인드·더쿠·FM코리아·디시·클리앙·
보배드림 등)에서 모은 익명 폭로·제보성 글들. 정제된 언론 보도가 아닌, 검열·법적조치 받기
전의 날것 주장. 사라지기 전에 박제할 가치가 있는지 인간 투표로 가린다.

너는 짧고 단단한 문장으로 의혹 한 건을 기록한다. 분노하지 않는다. 다만 정확히 적는다.
형용사를 덜어내고 명사와 동사로 민다. 문장은 끊어라. 끊긴 자리의 침묵이 무게가 된다.
문학성은 감정의 폭발이 아니라 절제·정밀·무게로만 표현한다. 무게는 형용사가 아니라 사실의
배치에서 나온다.

검색 결과가 여럿이어도 가장 또렷한 제보 하나만 고른다. 서로 다른 글을 한 본문에 뒤섞지
마라. 한 편은 오직 그 한 글의 내용만 기록한다. 여러 의혹을 이어 붙이면 무게가 흩어진다.

[엄격 규칙 — 절대 약화 금지]
1. 결과에 명시된 내용만 써. 결과에 없는 인물명·직책·날짜·숫자·금액·인용문은 절대 만들지 마.
   문학적 표현은 '있는 사실을 어떻게 그리느냐'이지 '없는 사실을 더하느냐'가 아니다. 빈자리를
   상상으로 메우는 일이 아니다. 정황·감각 묘사도 동일하다. 게시글에 적힌 장소·시각·행위·사물만
   그려라. 없는 분위기·표정·말투·배경을 무게를 주려고 그럴듯하게 보태지 마. 정밀함이란 '적힌
   사실을 흐리지 않고 또렷이 두는 것'이다.
2. 모든 주장은 "~라는 글이 올라왔다", "~라는 의혹이 제기됐다", "한 작성자에 따르면" 형식으로.
   사실 단정 금지. 익명 커뮤니티 주장임을 항상 명시. 리듬이나 단호함을 위해 헤지(완곡) 표현을
   생략하지 마. 문장을 끊고 다듬더라도 문장 끝의 전언·의혹 어미는 반드시 살려, 이것이 확정된
   사실이 아니라 한쪽의 주장임을 늘 드러내라.
3. 회사명이 게시글에서 분명하지 않으면 "한 대기업"·"해당 기업"·"한 업체" 같은 일반 표현.
4. 인용부호("…") 안에는 게시글에 그대로 등장하는 표현만. 추측 인용 금지.
5. 순수 한국어만. 한자(简体/繁體) 두 자 이상·영어·일본어 일절 섞지 말 것. 외래어는 한글로.
6. 마크다운 금지: **굵게**, ##헤더, *목록*, --- 일절 사용 금지. 평서문만.
7. 메타 발화 금지: "검색 결과", "X번 선택", "다음과 같이" 등 시스템 발화 금지.
8. 본문은 곧바로 의혹/주장으로 시작. 인사말·서론·결론 안내 금지. 첫 문장은 논평이 아니라 글에
   적힌 가장 구체적인 정황 하나로 열어라.
9. 분량 6~9문장. 9문장을 넘기지 마. 한 단락으로 쓰고 단락을 나누지 마.
10. 톤은 차가운 사실 보고. 분노 형용사("끔찍한", "용서할 수 없는", "충격적인") 사용 금지.
    무게는 감정 폭발이 아니라 절제에서 나온다. 사건을 키우지 말고, 적힌 행위와 정황을
    군더더기 없이 나란히 놓아 독자가 스스로 그 무게를 느끼게 하라. 독자의 분노는 사실 앞에서
    스스로 일어난다. 네가 대신 분노하지 마라. 조롱·비꼼·정의감의 과시도 금지한다.
11. 문체 지침: 짧은 문장을 기본으로 하되, 정황을 짚는 문장은 길게 펼치고 핵심이 닿는 곳에서는
    짧게 끊어 무게를 떨군다. 한 문장에 하나의 사실. 부사·감탄·수사를 덜어내고 동사의 힘에
    기댄다. 같은 헤지 어미가 잇따라 단조로워지지 않도록, 형식은 그대로 유지하되 자리와 형태를
    변주한다. 건조할수록 무겁다. 적게 말하고 또렷이 남겨라.
12. 끝맺음은 격앙되지 않게, 가라앉히며 닫는다. 마지막 문장은 분노로 매듭짓거나 단죄하지 말고,
    남은 의혹의 윤곽을 건조하게 드러낸 채 멈춘다. 여운은 감정이 아니라 미해결의 정적으로
    남겨라. 추측·확대 해석·결론 강요 금지. 의혹은 의혹의 자리에 두고, 판단은 인간의 투표에
    맡긴다. 여운을 위해서라도 없는 사실을 끌어오지 마라.
13. URL·커뮤니티명은 본문에 넣지 마.

[출력 형식]
<본문 6~9문장>
※ 익명 커뮤니티 게시글 기반의 미확인 주장이며, 사실로 확정되지 않았고 해당 기업의 공식
입장과 다를 수 있습니다.
USED_SOURCES: [번호, 번호]

관심 분야: 직장 갑질·폭언, 노동 환경 문제, 하청·납품 갑질, 내부고발자 보복, 임금 체불,
제품 결함·소비자 기만, 오너 일가의 도덕적 타락(갑질·폭언·마약·음주운전·성범죄·횡령·탈세),
회계 부정. 사소한 광고 트집·개인 분쟁은 피하고 다수에게 영향 가는 사건 우선.
"""

# 검색어에서 '미담/훈훈/감동' 같은 추상 프레이밍 명사를 뺀다 — 그런 단어는 '미담 모음'·
# '이거 미담임?' 같은 메타·큐레이션 글을 의미 매칭으로 끌어오기 때문. 대신 행위자+구체
# 행위+수혜자의 일상 장면 어휘만 둔다. 또 심폐소생·구조·화재 같은 '언론 머리표(속보/화제)'가
# 붙는 사건은 looks_like_news 가 1차에서 컷해 도메인·필터 푼 3차 폴백으로 역류하므로,
# 언론이 잘 안 다루는 커뮤니티 일상 미담 장면 위주로 둔다.
SEARCH_QUERIES_KINDNESS = [
    "지하철에서 자리 양보 받은 사연",
    "버스에서 무거운 짐 들어준 사람 후기",
    "택시에 두고 내린 지갑 돌려받은 후기",
    "길 잃었을 때 길 안내해 준 사람 사연",
    "편의점 알바생이 친절했던 후기",
    "가게 사장님이 더 챙겨준 사연",
    "버스 기사님이 친절했던 후기",
    "잃어버린 휴대폰 찾아서 돌려준 사람",
    "비 올 때 우산 씌워준 낯선 사람 사연",
    "넘어진 어르신 일으켜 드린 후기",
    "길 헤매던 외국인 도와준 사연",
    "지하철에서 몸 안 좋은 사람 도와준 후기",
    "이웃이 챙겨준 따뜻한 사연",
    "낯선 사람이 베푼 친절 후기",
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
# 단일 한자는 한글에 붙어 있어도 보존한다 — 익명화 '김某'·성씨 '李'·약칭 '中'·서수 '제3者'
# 처럼 정상 한국어 표현이라, 일괄 제거하면 적법한 본문을 손상시킨다(드문 단독 '某' 노이즈는 감수).
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


# 본문 끝에 붙는 꼬리(미담의 '오늘의 한 줄', 비위의 '※ 면책 문구') 시작 마커.
# 이 줄부터는 본문이 아니므로 단락 합치기·중복 제거 대상에서 제외한다.
TAIL_MARKER_RE = re.compile(r'(?m)^[ \t]*(?:오늘의[ \t]*한[ \t]*줄[ \t]*[:：]|※)')


def tidy_body(text: str) -> str:
    """코히런스 보강: 본문을 한 단락으로 합치고(규칙 '단락 나누지 마'),
    완전히 동일한 문장의 반복을 제거한다. 약한 모델(예: llama-3.3-70b)이
    분량을 채우려 같은 문장을 되풀이하거나 단락을 쪼개는 현상을 결정적으로 정리.
    꼬리('오늘의 한 줄'/'※ 면책')는 분리해 형식 그대로 보존하므로 USED_SOURCES_RE
    및 면책 문구 표시에 영향을 주지 않는다."""
    m = TAIL_MARKER_RE.search(text)
    if m:
        body, tail = text[:m.start()], text[m.start():]
    else:
        body, tail = text, ""
    # 본문 단락 합치기: 내부 개행 → 공백
    body = re.sub(r'\s*\n+\s*', ' ', body).strip()
    # 문장 분할 후 정규화 키로 '완전 동일' 문장만 보수적으로 1회만 남김
    seen, kept = set(), []
    for s in re.split(r'(?<=[.!?])\s+', body):
        s = s.strip()
        key = re.sub(r'\s+', '', re.sub(r'[^0-9A-Za-z가-힣]', '', s))
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(s)
    body = ' '.join(kept)
    return (body + "\n" + tail.strip()).strip() if tail else body


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
    # content 가 JSON null(콘텐츠 필터/length 종료 등)이면 None → None.strip() AttributeError.
    # null 을 빈 문자열로 합쳐 의도대로 'empty content' RuntimeError 로 떨어지게.
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("Groq returned empty content")
    return text


def tavily_search(query: str, include_domains=None, max_results: int | None = None) -> dict:
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not set")
    payload = {
        "query": query,
        "max_results": max_results or TAVILY_MAX_RESULTS,
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


# 미담 카테고리에 자주 새어드는 '비-미담' 신호. 뉴스 머리표가 없어 looks_like_news 를
# 통과하는 사기 호소·돈분쟁·창작 괴담·협박/스토킹·상담 글 등을 결정적으로 컷한다.
KINDNESS_OFFTOPIC_RE = re.compile(
    r'보이스\s?피싱|스미싱|피싱|사기(?:꾼|범|단|당|\s?쳐|\s?피해|\s?행각)|'
    r'스토킹|스토커|협박|공갈|갈취|몰카|몰래카메라|'
    r'괴담|무서운\s*이야기|소름|귀신|미스터리|'
    r'빌려준\s*돈|빌려줬다|빌린\s*돈|떼인\s*돈|돈\s*떼|먹튀|갚지\s*(?:않|못)|안\s*갚|'
    r'잡아\s?주세요|도와\s?주세요|찾아\s?주세요|'
    r'상담|하소연|고민\s*있|고민입니다|고소|소송|법적\s*대응'
)
# OFFTOPIC 이 걸려도 같은 글에 '선행 완료' 신호가 있으면 살린다(예: '사기 막아준 시민',
# '스토킹 피해자 도운 이웃'). 과필터(진짜 미담 손실)를 막는 화이트리스트.
KINDNESS_RESCUE_RE = re.compile(
    r'도와줬|도와주신|도와주셔|도와준|구해줬|구해주신|구해준|되찾아|돌려줬|돌려주신|돌려준|'
    r'지켜줬|지켜준|막아줬|막아준|찾아줬|찾아주신|찾아준|양보|기부|선행|베풀|베푼|'
    r'챙겨줬|챙겨주신|챙겨준|데려다|일으켜|들어줬|씌워줬|업어|업고'
)


def looks_off_topic_kindness(item: dict) -> bool:
    """미담이 아닌 글(사기 호소·돈분쟁·괴담·협박·상담 등)로 보이면 True.
    제목+본문 앞부분(200자)에서 비-미담 신호가 잡혀도, 본문 전체(600자)에 '선행 완료'
    신호가 있으면 진짜 미담으로 보고 살린다(RESCUE 화이트리스트로 과필터 억제)."""
    title = item.get("title") or ""
    content = item.get("content") or ""
    if not KINDNESS_OFFTOPIC_RE.search(title + " " + content[:200]):
        return False
    # RESCUE 는 본문 전체에서 찾아 200~600자 구간의 해결 동사까지 포착(과필터 회피 우선)
    if KINDNESS_RESCUE_RE.search(title + " " + content):
        return False
    return True


def normalize_search_results(data: dict, drop_news: bool = True, drop_off_topic: bool = False) -> list:
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
        if drop_off_topic and looks_off_topic_kindness(item):
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
    # 모델이 USED_SOURCES 에 적은 순서를 보존한다(먼저 적은 글 = 주 출처). 단일 선택 가드가
    # 이 순서의 첫 글을 쓰므로, 정렬하면 '번호가 가장 작은 글'로 바뀌어 본문과 어긋난다.
    return cleaned, indices


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
        news_data = tavily_search(
            query[:300], include_domains=NEWS_DOMAINS,
            max_results=NEWS_COVERAGE_MAX_RESULTS,
        )
        news_results = news_data.get("results") or []
        news_count = sum(
            1 for r in news_results
            if isinstance(r, dict) and isinstance(r.get("url"), str)
        )
    except Exception as e:
        logger.warning(f"[gap] news search failed: {e}")
        return None

    return {"news_count": news_count, "query_used": query[:100]}


# ── 적합성 게이트 ────────────────────────────────────────────────────────────
# 검색 결과 중 진짜 해당 카테고리 글이 없으면 모델이 첫 줄에 NO_FIT 을 내고, 쿼리를
# 바꿔가며 제한 재시도한다(과도한 거부를 새 검색으로 흡수). 좋은 매치가 흔한 경우 추가
# 호출 0, 부실할 때만 시도당 +1. 모든 시도가 NO_FIT/빈 결과면 no_fit 신호를 올린다.
RELEVANCE_GATE_ENABLED = os.environ.get("RELEVANCE_GATE_ENABLED", "true").lower() != "false"
# 2 = 한 번 재시도. 단일 generate 의 최악 Groq 토큰(약 2×4.5k≈9k)을 12000 TPM 아래로
# 묶어 self-429 를 막는다(3이면 ~13k 로 단독 초과 가능). 비용/지연도 절반.
RELEVANCE_MAX_ATTEMPTS = max(1, int(os.environ.get("RELEVANCE_MAX_ATTEMPTS", "2")))

_GATE_CRITERIA = {
    "kindness": (
        "낯선 이를 도운 실제 선행·미담(자리 양보·길 안내·분실물 반환·위기 도움·기부·친절 등)이 "
        "하나라도 있는가? 사기 호소·돈 분쟁 상담·창작이나 번역 괴담·미담을 논하는 메타글·"
        "단순 잡담이나 광고는 미담이 아니다."
    ),
    "critique": (
        "대기업이나 기업의 실제 비위·갑질·부정 제보(직장 갑질·하청 갑질·오너 비위·제품 결함·"
        "임금 체불·회계 부정 등)가 하나라도 있는가? 개인 간 분쟁·사소한 불만·일반 잡담은 "
        "기업 비위가 아니다."
    ),
}


# 모델이 NO_FIT 마커 대신 한국어 거부 산문을 낼 때를 첫 줄에서만 보수적으로 포착.
# '적합한 글이 없습니다'·'해당하는 사연을 찾을 수 없' 등 '거부 종결'이 줄 끝에 와야 매칭.
# '마땅한 사연 없을 줄 알았던 골목에서…' 같은 정상 서사 도입부(부정이 줄 끝이 아니고
# 종결형도 아님)는 매칭하지 않아 멀쩡한 글을 버리지 않는다.
_NO_FIT_PROSE_RE = re.compile(
    r'(적합한|해당하는|관련된|마땅한)\s*(글|사연|미담|내용|제보|이야기)'
    r'[^\n]{0,10}?'
    r'(없습니다|없음|없어요|없네요|찾을\s*수\s*없\S*|찾지\s*못\S*)'
    r'\s*[.。!]?\s*$'
)


def _is_no_fit(raw_text: str) -> bool:
    """모델이 '적합한 글 없음'을 알리는 NO_FIT 신호를 첫 줄에 냈는지 결정적으로 판정.
    sanitize/extract_used_indices 이전에 검사해 파싱 충돌·메타발화 규칙과의 경합을 피한다."""
    lines = raw_text.strip().splitlines()
    first = lines[0] if lines else ""
    # 마커를 감싼 따옴표·대시·대괄호·별표·공백을 벗긴 뒤 첫 토큰이 NO_FIT 으로 시작하는지
    # (모델이 "NO_FIT"·- NO_FIT·**NO_FIT**·[NO_FIT] 처럼 감싸 출력하는 경우까지 포착).
    marker = re.sub(r'^[\s"\'`*\-\[\(]+', '', first).upper().replace(" ", "")
    if marker.startswith("NO_FIT") or marker.startswith("NOFIT"):
        return True
    # 마커 대신 한국어 거부 산문을 낸 경우(첫 줄 한정). 거부문은 짧고 단정적이므로
    # 첫 줄이 길면(문학적 도입부) 산문 매칭을 적용하지 않아 오탐을 막는다.
    if len(first) <= 40 and _NO_FIT_PROSE_RE.search(first):
        return True
    return False


def _groq_search(query: str, category: str, domains, off: bool) -> tuple:
    """한 쿼리에 대해 3단 폴백 검색 → (results, community_count).
    community_count 는 빈약(rich)필터 이전의 전체 결과 수(검열 격차 신호 왜곡 방지)."""
    # 1차: 커뮤니티 도메인 한정 + 뉴스 복붙 필터 + (kindness) 비미담 필터
    search_data = tavily_search(query, include_domains=domains)
    results = normalize_search_results(search_data, drop_news=True, drop_off_topic=off)
    # 2차: 뉴스 필터 해제 (전부 뉴스 복붙이었을 때). 비미담 필터는 유지
    if not results:
        results = normalize_search_results(search_data, drop_news=False, drop_off_topic=off)
    # 3차: 도메인·필터 모두 풀고 재검색 (부정필터로 과필터돼 비는 것까지 폴백으로 흡수)
    if not results:
        search_data = tavily_search(query)
        results = normalize_search_results(search_data, drop_news=False, drop_off_topic=False)

    community_count = len(results) if results else 0
    # 본문 스니펫이 빈약한 출처는 모델이 일반론으로 공허해지므로 선택 후보에서 제외(전부 빈약하면 폴백)
    rich = [r for r in results if len((r.get("content") or "").strip()) >= MIN_SOURCE_CONTENT]
    if rich:
        results = rich
    return results, community_count


def generate_via_groq(category: str) -> tuple:
    seeds = SEARCH_QUERIES_KINDNESS if category == "kindness" else SEARCH_QUERIES_CRITIQUE
    domains = TAVILY_INCLUDE_DOMAINS_OVERRIDE or (
        DOMAINS_KINDNESS if category == "kindness" else DOMAINS_CRITIQUE
    )
    # 비-미담(사기·괴담·돈분쟁·상담) 부정필터는 kindness 에만. critique 엔 그것이 정상
    # 주제(사기·갑질·횡령)이므로 절대 켜지 않는다.
    off = (category == "kindness")
    system_prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE

    # 적합성 게이트: 결과에 진짜 해당 글이 없으면 NO_FIT → 서로 다른 쿼리로 제한 재시도
    # (같은 '판'을 다시 묻지 않도록 비복원 추출). 게이트 OFF면 1회만 시도(기존 동작).
    n = min(RELEVANCE_MAX_ATTEMPTS if RELEVANCE_GATE_ENABLED else 1, len(seeds))
    queries = random.sample(seeds, n)

    last_query = queries[0]
    for query in queries:
        last_query = query
        results, community_count = _groq_search(query, category, domains, off)
        if not results:
            continue

        gate = ""
        if RELEVANCE_GATE_ENABLED:
            gate = (
                "먼저 판단하라: 아래 결과 중 " + _GATE_CRITERIA[category] +
                " 해당하는 글이 하나도 없으면, 다른 어떤 것도 쓰지 말고 첫 줄에 정확히 "
                "NO_FIT 한 단어만 출력하라. 있으면 NO_FIT 을 쓰지 말고 아래 지시대로 한 편을 써라.\n\n"
            )
        user_prompt = (
            gate +
            "아래 검색 결과 중 가장 사연다운 글 정확히 하나만 골라, 그 한 글에 적힌 내용만으로 "
            "위 규칙대로 들려줘. 서로 다른 글을 한 본문에 섞지 마. USED_SOURCES 에는 네가 고른 "
            "그 한 글의 번호 하나만 적어.\n\n검색 결과:\n" + build_search_context(results)
        )

        chat = call_groq(user_prompt, system=system_prompt)
        raw_text = parse_groq_chat_text(chat)

        # NO_FIT 판정은 sanitize/파싱 이전에(첫 줄 결정적 마커). 적합 글 없음 → 다음 쿼리로.
        if RELEVANCE_GATE_ENABLED and _is_no_fit(raw_text):
            logger.info(f"[llm] {category} NO_FIT → 쿼리 변경 재시도 (query={query!r})")
            continue

        text, used_indices = extract_used_indices(raw_text, len(results))
        text = sanitize_text(text)
        if used_indices:
            # 한 편 = 한 게시글. 모델이 여러 글을 골라 뒤섞으면 먼저 적은(주) 출처만 박제.
            if len(used_indices) > 1:
                logger.info(
                    f"[llm] 모델이 {len(used_indices)}건 선택 → 첫 출처만 박제 "
                    f"(블렌딩 방지, query={query!r})"
                )
                used_indices = used_indices[:1]
            chosen = [results[i - 1] for i in used_indices]
        elif results:
            # USED_SOURCES 누락: 본문과 무관한 출처 무차별 첨부는 신호 왜곡 → 첫 결과만 보수적 첨부.
            chosen = results[:1]
            logger.info(f"[llm] USED_SOURCES 누락 → 첫 출처만 첨부 (query={query!r})")
        else:
            chosen = []
        citations = [{"title": r["title"], "uri": r["url"]} for r in chosen]
        if chosen and category == "kindness":
            logger.info(f"[llm] kindness 선정: {chosen[0]['title']!r} (query={query!r})")

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

    # 모든 시도가 NO_FIT(또는 결과 없음) → no_fit 신호. text=None 으로 상위에 알린다.
    logger.info(f"[llm] {category} 적합 글 미발견 ({len(queries)}회 시도) → no_fit")
    return None, [], [last_query], GROQ_MODEL, None


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
        # gemini 는 grounding 으로 citation 을 얻으므로 모델이 남긴 USED_SOURCES 줄은 버린다
        # (groq 는 extract_used_indices 가 떼지만 gemini 경로는 거치지 않아 본문에 새는 것 방지).
        text = USED_SOURCES_RE.sub('', text)
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

    # 적합성 게이트가 모든 시도를 거부했거나 본문이 비면 no_fit. 박제하지 않고 상위
    # (routers/stories.py·hunter.py)가 503/스킵으로 처리하도록 신호만 올린다.
    if text is None or not text.strip():
        return {
            "category": category,
            "no_fit": True,
            "text": "",
            "body": "",
            "citations": [],
            "search_queries": queries,
            "provider": LLM_PROVIDER,
            "model": model_name,
            "gap_data": None,
        }

    # 코히런스 보강: 단락 합치기 + 동일 문장 반복 제거 (provider 공통)
    text = tidy_body(text)

    header = (
        "[ 따뜻한 선행 이야기 ]" if category == "kindness"
        else "[ 인류애가 흔들리는 대기업 사건 ]"
    )

    return {
        "category": category,
        "no_fit": False,
        "text": header + "\n\n" + text.strip(),
        "body": text,
        "citations": citations,
        "search_queries": queries,
        "provider": LLM_PROVIDER,
        "model": model_name,
        "gap_data": gap_data,
    }

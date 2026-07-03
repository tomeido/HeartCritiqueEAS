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
# Groq 한도(TPM/TPD) 소진 시 Gemini 가 사실상 단일 생성 경로가 된다. 일시적 5xx(고수요·
# UNAVAILABLE)·429·네트워크 오류 한 번에 사용자 503 을 내지 않도록 짧은 백오프로 재시도한다.
# 5회/지수백오프(1.5,3,6,12초 ≈ 최대 ~22초)로 Gemini 고수요 스파이크를 동기 요청 안에서
# 견딘다(스피너가 도는 생성 액션이라 이 정도 대기는 허용 가능). 한도는 env 로 조정 가능.
GEMINI_MAX_ATTEMPTS = max(1, int(os.environ.get("GEMINI_MAX_ATTEMPTS", "5")))
GEMINI_RETRY_BASE = float(os.environ.get("GEMINI_RETRY_BASE", "1.5"))

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
TAVILY_TIME_RANGE = os.environ.get("TAVILY_TIME_RANGE", "year").strip().lower()
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

GAP_DETECTION_ENABLED = os.environ.get("GAP_DETECTION_ENABLED", "false").lower() == "true"


def calculate_gap_score(community: int, news: int) -> str:
    """선정된 커뮤니티 글 제목을 언론에서 검색한 결과로 격차 산출.
    커뮤니티 글의 날것 어투로 뉴스 매칭하는 로직은 부정확하므로, 격차 측정을 비활성화하고
    항상 'none'을 반환하여 투표 임계값 등에 영향을 주지 않도록 합니다."""
    return "none"


PROMPT_KINDNESS = """\
너는 현대 자본주의 시스템의 모순, 인류애의 상실과 충전이 교차하는 '디지털 파편'을 수집하는 고독한 기록관이다.
아래는 한국 커뮤니티 게시판(더쿠·클리앙·인스티즈·네이트판·FM코리아·보배드림 등)에서 모은 익명 미담 글들.
정제된 언론 보도가 아닌 일반인의 날것 사연.

너는 다음 긁어온 글 중에서 [1] 작성자의 가감 없는 날것의 헌신, 따뜻한 인류애, 혹은 희생이 느껴지거나 [2] 삭막한 사회 구조 속에서 인간성을 지켜낸 순간을 담은 진짜 미담 한 편을 고르고, 그 글이 얼마나 쉽게 휘발되어 사라질 수 있는지를 0에서 10 사이의 '휘발성 점수'로 평가해라.
만약 위 [1]·[2]에 해당하는 진짜 미담이 하나도 없다면(사기 호소·돈 분쟁 상담·광고·미담을 논하는 메타글·단순 잡담뿐이라면), 아래 내용을 작성하지 말고 오직 'NO_FIT' 한 단어만 첫 줄에 출력해라. 휘발성 점수가 낮다는 이유만으로 NO_FIT 을 내지는 마라 — 미담은 삭제 위험과 무관하며, 휘발성 점수는 표시용 평가일 뿐 채택 기준이 아니다.

글을 쓸 때는 짧고 단단한 문장으로 사연 한 편을 새긴다. 형용사를 덜어내고 명사와 동사로 민다.
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
휘발성 점수: <0에서 10 사이의 평가 점수 정수>
박제 사유: <이 글을 왜 Arweave에 영원히 박제해야 하는지 인간성을 증명하는 한 줄의 서정적이고 시적인 이유>
USED_SOURCES: [번호]
"""

PROMPT_CRITIQUE = """\
너는 현대 자본주의 시스템의 모순, 불공정, 그리고 인류애의 상실을 관찰하고 수집하는 고독한 기록관이다.
아래는 한국 커뮤니티 게시판(블라인드·더쿠·FM코리아·디시·클리앙·보배드림 등)에서 모은 익명 폭로·제보성 글들.
정제된 언론 보도가 아닌, 검열·법적조치 받기 전의 날것 주장.

너는 다음 긁어온 글 중에서 [1] 작성자의 가감 없는 날것의 분노, 절망, 혹은 고발이 느껴지거나 [2] 시스템의 부조리(예: 의료 문제, 노동 환경, 대기업 갑질 등)를 폭로하고 있으며 [3] 자본의 힘이나 외부 압박으로 인해 신속하게 삭제(휘발)될 가능성이 높은 글을 0에서 10 사이의 '휘발성 점수'로 평가해라.
만약 가장 적합한 글의 휘발성 점수가 7점 미만이거나 적합한 글이 하나도 없다면, 아래 내용을 작성하지 말고 오직 'NO_FIT' 한 단어만 첫 줄에 출력해라.

글을 쓸 때는 짧고 단단한 문장으로 의혹 한 건을 기록한다. 분노하지 않는다. 다만 정확히 적는다.
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
※ 익명 커뮤니티 게시글 기반의 미확인 주장이며, 사실로 확정되지 않았고 해당 기업의 공식 입장과 다를 수 있습니다.
휘발성 점수: <0에서 10 사이의 평가 점수 정수>
박제 사유: <이 글을 왜 Arweave에 영원히 박제해야 하는지 비극성을 증명하는 한 줄의 서정적이고 시적인 이유>
USED_SOURCES: [번호]

관심 분야: 직장 갑질·폭언, 노동 환경 문제, 하청·납품 갑질, 내부고발자 보복, 임금 체불,
제품 결함·소비자 기만, 오너 일가의 도덕적 타락(갑질·폭언·마약·음주운전·성범죄·횡령·탈세),
회계 부정. 사소한 광고 트집·개인 분쟁은 피하고 다수에게 영향 가는 사건 우선.
대상이 아닌 글: 스포츠 경기 결과·승부 예측, 게임·카드·수집품 잡담, 연예인 가십,
진영 정치 논쟁, 단순 일상·후기. 이런 글뿐이면 NO_FIT.
"""

# 검색어에서 '미담/훈훈/감동' 같은 추상 프레이밍 명사를 뺀다 — 그런 단어는 '미담 모음'·
# '이거 미담임?' 같은 메타·큐레이션 글을 의미 매칭으로 끌어오기 때문. 대신 행위자+구체
# 행위+수혜자의 일상 장면 어휘만 둔다. 또 심폐소생·구조·화재 같은 '언론 머리표(속보/화제)'가
# 붙는 사건은 looks_like_news 가 1차에서 컷해 도메인·필터 푼 3차 폴백으로 역류하므로,
# 언론이 잘 안 다루는 커뮤니티 일상 미담 장면 위주로 둔다.
SEARCH_QUERIES_KINDNESS = [
    "디시인사이드 개념글 훈훈한 미담",
    "에펨코리아 포텐 훈훈한 사연",
    "트위터 감동적인 실화 사연",
    "디시 실베 따뜻한 이야기",
    "네이트판 베스트 감동 후기",
    "더쿠 핫게 훈훈한 일상 미담",
    "지하철에서 시민들이 도와준 미담 글",
    "화재 현장 구조 시민 미담 후기",
    "길거리 쓰러진 사람 구해준 의인 디시",
    "익명 커뮤니티 감동 사연 후기",
]

# 모든 시드는 '기업 비위·시스템 부조리' 앵커(직장/대기업/하청/소비자/제도)를 반드시 포함한다.
# 앵커 없는 범용 시드('실시간 베스트·정치 시사·트렌드·억울한 사건')는 스포츠·게임·연예·진영
# 정치 같은 trivial 글을 끌어와 구조필터(looks_off_topic_critique)에서 통째로 컷되며 recall 만
# 깎으므로 제거했다. 신선도는 토큰 안전을 위해 time_range/재시도수 대신 '실시간/오늘' 어휘로만 유도.
SEARCH_QUERIES_CRITIQUE = [
    # 직장·노동
    "블라인드 직장 갑질 폭언 내부고발",
    "블라인드 실시간 핫 직장 내부고발 제보",
    "에펨코리아 직장 갑질 임금체불 폭로",
    "디시 직장인 회사 비리 내부고발",
    # 대기업·오너·하청
    "디시 개념글 대기업 갑질 폭로",
    "에펨코리아 대기업 오너 갑질 비리 폭로",
    "익명 커뮤니티 묻힌 대기업 비리 폭로",
    "하청 납품업체 단가 후려치기 갑질 제보",
    "재벌 오너 일가 횡령 탈세 갑질 의혹",
    # 소비자·제품
    "제품 결함 리콜 거부 소비자 기만 폭로",
    "보배드림 자동차 결함 제조사 대응 논란",
    "프랜차이즈 본사 가맹점주 갑질 정산 논란",
    # 제도·구조 부조리 / 테마(검열·삭제)
    "병원 산재 과로 노동환경 고발",
    "자본의 힘으로 삭제 위협받는 기업 비리 폭로",
]

# 줄 끝/문장 끝/단독 줄 어디든 잡도록 앵커 완화
USED_SOURCES_RE = re.compile(
    r'(?i)\s*USED_SOURCES[ \t]*[:=][ \t]*\[?([0-9,\s]*)\]?\s*$',
    re.MULTILINE,
)

# 삭제 전용(줄 끝 앵커 $ 없이): 약한 모델이 'USED_SOURCES: [1] 그 외…'처럼 포맷을 어겨
# 꼬리 밖이나 줄 중간에 남겨도 메타 문구가 공개 본문에 새지 않게 마커부터 줄 끝까지 제거한다.
# (extract_used_indices 의 번호 파싱은 위 앵커드 USED_SOURCES_RE 를 계속 사용 — 무영향)
USED_SOURCES_STRIP_RE = re.compile(
    # 숫자 목록은 같은 줄에 한정([0-9,\t ] — 개행 제외): \s 를 쓰면 'USED_SOURCES: 1\n2번째
    # 줄...'처럼 다음 줄이 숫자로 시작할 때 개행+다음 줄 본문까지 통째로 먹어버린다.
    r'(?i)USED_SOURCES[ \t]*[:=][ \t]*\[?[0-9,\t ]*\]?[^\n]*'
)

VOLATILITY_SCORE_RE = re.compile(
    r'(?im)^\s*(?:휘발성\s*점수|휘발성점수)\s*[:=]\s*(\d+)\s*$',
)
POETIC_REASON_RE = re.compile(
    r'(?im)^\s*(?:박제\s*사유|박제사유)\s*[:=]\s*(.+?)\s*$',
)


def extract_volatility_and_reason(text: str) -> tuple[str, int, str]:
    volatility = 0
    reason = ""

    vm = VOLATILITY_SCORE_RE.search(text)
    if vm:
        try:
            volatility = int(vm.group(1).strip())
        except ValueError:
            pass
        start, end = vm.span()
        text = text[:start] + text[end:]

    rm = POETIC_REASON_RE.search(text)
    if rm:
        reason = rm.group(1).strip()
        start, end = rm.span()
        text = text[:start] + text[end:]

    return text.strip(), volatility, reason


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


def call_gemini(prompt: str, use_search: bool = True) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 1.0,
            "topP": 0.95,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    # 검색 모드(use_search=True)에서만 google_search grounding 을 켠다. 승격(promoter)은
    # *주어진 캡처 본문*을 재작성해야 하므로 검색을 끈다 — 켜면 모델이 본문을 무시하고
    # 새 웹 검색으로 다른 글을 써버린다.
    if use_search:
        payload["tools"] = [{"google_search": {}}]
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    import time
    last_err = None
    for attempt in range(GEMINI_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"Gemini HTTP {e.code}: {body}")
            # 5xx(고수요·일시 장애)·429 만 일시 오류로 보고 재시도. 4xx(키·요청 오류)는 즉시 실패.
            if e.code not in (429, 500, 502, 503, 504) or attempt == GEMINI_MAX_ATTEMPTS - 1:
                raise last_err from e
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            last_err = RuntimeError(f"Gemini network error: {reason}")
            if attempt == GEMINI_MAX_ATTEMPTS - 1:
                raise last_err from e
        wait = GEMINI_RETRY_BASE * (2 ** attempt)
        logger.warning(f"[llm] Gemini 일시 오류 → {wait:.1f}초 후 재시도 ({attempt + 1}/{GEMINI_MAX_ATTEMPTS}): {last_err}")
        time.sleep(wait)
    raise last_err  # 루프가 항상 return/raise 하므로 도달하지 않음


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
    import time
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
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
        except Exception as e:
            err_msg = str(e)
            if "HTTP 429" in err_msg and attempt < max_retries - 1:
                wait_sec = 5.0
                match = re.search(r"try again in (\d+\.?\d*)s", err_msg)
                if match:
                    try:
                        wait_sec = float(match.group(1)) + 0.5
                    except ValueError:
                        pass
                logger.warning(f"[llm] Groq 429 Rate Limit 감지. {wait_sec:.2f}초 후 재시도합니다. (시도 {attempt+1}/{max_retries})")
                time.sleep(wait_sec)
                continue
            raise e


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
    if TAVILY_TIME_RANGE and TAVILY_TIME_RANGE != "none":
        payload["time_range"] = TAVILY_TIME_RANGE
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
    # "○○○ 기자 = ", "○○○ 에디터 =" 형식 및 한글 바이라인
    r'|\b\S{2,4}\s*(?:기자|에디터|특파원|논설위원)\b'
    # "기자: ", "에디터 =" 등 직책 뒤의 어포지션
    r'|(?:기자|에디터|reporter|editor)\s*[:=\(\[\]]'
    # 이메일 바이라인
    r'|[a-zA-Z0-9._%+-]+@(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}'
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
    'category=news', 'cate=news', 'issuefeed',
)


def looks_like_news(item: dict) -> bool:
    """뉴스 기사 복붙으로 보이는 결과면 True."""
    url = (item.get("url") or "").lower()
    # issuefeed 도메인은 100% 뉴스/카드뉴스 피드
    if "issuefeed.dcinside.com" in url:
        return True

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


# critique 적합성: '기업 비위·시스템 부조리' 신호가 하나도 없는 글(스포츠 경기 결과·
# 게임/카드/수집품 잡담·연예 가십·진영 정치 논쟁·개인 일상)이 critique 로 새는 것을
# 구조적으로 컷한다. kindness 는 looks_off_topic_kindness(부정 신호 차단)였지만, critique 는
# 사기·갑질·횡령이 '정상 주제'라 그 부정필터를 못 쓴다 → 반대로 '기업/노동/소비자/제도의
# 부조리 신호를 하나라도 보유'할 것을 요구하는 positive gate 로 둔다.
CRITIQUE_ONTOPIC_RE = re.compile(
    r'대기업|기업|회사|직장|사장|본사|지사|점주|가맹|프랜차이즈|오너|재벌|총수|임원|상사|사주|업체|'
    r'갑질|갑을|폭언|폭행|괴롭힘|따돌림|부당|불공정|차별|성희롱|성추행|성범죄|'
    r'임금|월급|급여|연봉|체불|수당|야근|초과근무|과로|산재|산업재해|해고|권고사직|계약직|비정규직|노조|파업|'
    r'하청|재하청|납품|단가|대금|미지급|위탁|용역|일용직|'
    r'내부고발|제보|폭로|비리|부정|횡령|배임|탈세|회계|분식|뇌물|로비|담합|독점|불법|위법|'
    r'결함|불량|하자|리콜|단종|환불|보상|소비자|기만|허위|과장\s*광고|먹튀|사기|'
    r'본부|대리점|편의점|배달|라이더|플랫폼|수수료|정산|블랙컨슈머|'
    r'의료|병원|간호|요양|어린이집|보육|복지|연금|보험금|보험사|약값|산하기관|공공기관|관공서|'
    r'은행|대출|이자|보증금|전세|월세|임대|분양|입주|하자보수'
)
# 명백한 비-critique 영역(스포츠·게임/수집품·연예·잡담)의 강한 신호. 단독 토큰의 오탐을
# 피하려 구체 어구로만 둔다.
CRITIQUE_OFFTOPIC_RE = re.compile(
    r'월드컵|올림픽|아시안게임|챔피언스리그|프리미어리그|K리그|프로야구|프로축구|메이저리그|'
    r'경기\s*결과|승부\s*예측|선발\s*(?:명단|라인업)|득점왕|해트트릭|대표팀\s*명단|'
    r'포켓몬|트레이딩\s*카드|카드\s*뽑기|가챠|컬렉션|피규어|굿즈|'
    r'아이돌|컴백|데뷔조|음원\s*차트|뮤직비디오|예능|드라마\s*결말|웹툰\s*결말|'
    r'맛집|레시피|여행\s*후기|오늘의\s*운세|짤방',
    re.IGNORECASE,
)


def looks_off_topic_critique(item: dict) -> bool:
    """critique(기업 비위·시스템 부조리)와 무관한 글이면 True.
    제목+본문 앞부분에 기업/노동/소비자/제도 부조리 신호(ON-TOPIC)가 하나라도 있으면
    살린다(과필터 방지). 그 신호가 전혀 없으면 off-topic 으로 본다(positive gate).
    스포츠·게임·연예 같은 강한 비주제 신호는 로그·가독성용으로만 본다 — 판정은 ON-TOPIC
    보유 여부가 단독 기준이다(없으면 어차피 컷)."""
    title = item.get("title") or ""
    content = item.get("content") or ""
    blob = title + " " + content[:400]
    return not CRITIQUE_ONTOPIC_RE.search(blob)


def normalize_search_results(data: dict, drop_news: bool = True, off_topic_fn=None) -> list:
    """off_topic_fn: item→bool 콜백(True 면 제외). 카테고리별 적합성 필터를 주입한다
    (kindness=looks_off_topic_kindness, critique=looks_off_topic_critique). None 이면 미적용."""
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
        if off_topic_fn and off_topic_fn(item):
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
        "대기업·기업·기관의 실제 비위·시스템 부조리 제보(직장 갑질·폭언, 하청/납품 갑질, 오너 "
        "비위, 제품 결함·소비자 기만, 임금 체불·산업재해, 내부고발, 회계 부정, 의료·복지·금융의 "
        "구조적 부조리 등)가 하나라도 있는가? 스포츠 경기 결과·승부 예측, 게임/카드/수집품 잡담, "
        "연예인 가십, 진영 정치 논쟁, 개인 간 사소한 다툼, 단순 잡담·후기는 '기업 비위·시스템 "
        "부조리'가 아니다 — 그런 글뿐이면 반드시 NO_FIT 을 출력하라."
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


def _groq_search(query: str, category: str, domains) -> tuple:
    """한 쿼리에 대해 3단 폴백 검색 → (results, community_count).
    community_count 는 빈약(rich)필터 이전의 전체 결과 수(검열 격차 신호 왜곡 방지).
    카테고리별 적합성 필터(kindness=비미담 차단, critique=기업 비위 신호 요구)를 주입한다."""
    off_fn = looks_off_topic_kindness if category == "kindness" else looks_off_topic_critique
    # 1차: 커뮤니티 도메인 한정 + 뉴스 복붙 필터 + 카테고리 적합성 필터
    search_data = tavily_search(query, include_domains=domains)
    results = normalize_search_results(search_data, drop_news=True, off_topic_fn=off_fn)
    # 2차: 뉴스 필터 해제 (전부 뉴스 복붙이었을 때). 적합성 필터는 유지
    if not results:
        results = normalize_search_results(search_data, drop_news=False, off_topic_fn=off_fn)
    # 3차: 도메인 풀고 재검색. kindness 는 모든 필터 해제(뭐라도 생성 우선)지만, critique 는
    # 적합성 필터를 유지한다 — 스포츠·게임·연예 잡담을 '기업 비위'로 둔갑시키느니 생성을 건너뛴다.
    if not results:
        search_data = tavily_search(query)
        results = normalize_search_results(
            search_data, drop_news=False,
            off_topic_fn=(off_fn if category == "critique" else None),
        )

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
    # 카테고리 적합성 필터는 _groq_search 안에서 category 로 분기한다(kindness=비미담 차단,
    # critique=기업 비위·시스템 부조리 신호 요구). 여기선 system 프롬프트만 고른다.
    system_prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE

    # 적합성 게이트: 결과에 진짜 해당 글이 없으면 NO_FIT → 서로 다른 쿼리로 제한 재시도
    # (같은 '판'을 다시 묻지 않도록 비복원 추출). 게이트 OFF면 1회만 시도(기존 동작).
    n = min(RELEVANCE_MAX_ATTEMPTS if RELEVANCE_GATE_ENABLED else 1, len(seeds))
    queries = random.sample(seeds, n)

    last_query = queries[0]
    for query in queries:
        last_query = query
        results, community_count = _groq_search(query, category, domains)
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
        text, volatility, reason = extract_volatility_and_reason(text)

        # 휘발성 점수 미달은 critique 에서만 재시도 사유로 쓴다. critique 는 '자본 압박으로
        # 곧 삭제될 폭로'가 핵심이라 저휘발 글을 거를 가치가 있지만, kindness(미담)는 삭제
        # 위험과 무관하므로 저휘발이라고 버리면 recall 만 깎여 no_fit→503 이 잦아진다
        # (휘발성은 생성 게이트가 아닌 표시·랭킹 전용이라는 원칙과도 일관).
        if RELEVANCE_GATE_ENABLED and category == "critique" and volatility < 7:
            logger.info(f"[llm] {category} 휘발성 점수 미달 ({volatility} < 7) → 쿼리 변경 재시도 (query={query!r})")
            continue

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
        return text, citations, [query], GROQ_MODEL, gap_data, volatility, reason

    # 모든 시도가 NO_FIT(또는 결과 없음) → no_fit 신호. text=None 으로 상위에 알린다.
    logger.info(f"[llm] {category} 적합 글 미발견 ({len(queries)}회 시도) → no_fit")
    return None, [], [last_query], GROQ_MODEL, None, 0, ""


# ── 승격(promoter) 전용 단일 본문 경로 ───────────────────────────────────────
# collector 가 비공개로 보관한 captured_posts.body_text 한 글을 받아, 검색·선택·게이트
# 로직을 거치지 않고 *그 본문만* 익명·헤지된 문학 스토리로 재작성한다(적대적 리뷰 권고:
# 검색 경로의 USED_SOURCES/NO_FIT 다중선택 게이트를 재사용하지 말 것). 검색 grounding 은
# 끈다 — 켜면 모델이 주어진 본문을 무시하고 새 웹 검색으로 다른 글을 써버린다.
# 토큰 상한·저작권(인용·논평 범위)을 위해 본문은 truncate 한다.
PROMOTE_BODY_MAX_CHARS = int(os.environ.get("PROMOTE_BODY_MAX_CHARS", "2500"))


def generate_from_text(body_text: str, title: str | None, category: str) -> dict:
    """단일 캡처 본문 → 익명 문학 스토리. 반환 형식은 generate() 와 동형(citations 제외).
    no_fit=True 면 (본문이 빈약하거나 모델이 NO_FIT) 승격하지 않고 상위가 스킵한다."""
    if category not in ("kindness", "critique"):
        category = "critique"
    system_prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
    snippet = (body_text or "").strip()[:PROMOTE_BODY_MAX_CHARS]

    def _skip(provider="", model_name=""):
        return {
            "no_fit": True, "category": category, "body": "", "text": "",
            "volatility_score": 0, "poetic_reason": "",
            "provider": provider, "model": model_name,
        }

    if len(snippet) < MIN_SOURCE_CONTENT:
        return _skip()

    src = f"제목: {title}\n본문: {snippet}" if title else snippet
    user_prompt = (
        "아래는 한국 커뮤니티 게시글 한 편의 본문이다(이미 삭제됐을 수 있다). 이 한 글에 "
        "적힌 내용만으로 위 규칙대로 한 편을 써라. 다른 글과 섞지 말고, 본문에 없는 사실"
        "(실명·회사명·날짜·금액 등)은 절대 만들지 마라. 본문에 적힌 개인정보(전화·주소·"
        "주민번호·계좌 등)는 본문 그대로 옮기지 말고 익명화하거나 생략하라.\n\n글:\n" + src
    )

    text = None
    provider = model_name = ""
    if GROQ_API_KEY:
        try:
            text = parse_groq_chat_text(call_groq(user_prompt, system=system_prompt))
            provider, model_name = "groq", GROQ_MODEL
        except Exception as e:
            logger.warning(f"[promote] Groq 생성 실패({e!r}). Gemini 폴백 시도.")
    if text is None and GEMINI_API_KEY:
        # 검색 끄고 system+user 를 한 프롬프트로 합쳐 본문만 재서술.
        raw = call_gemini(system_prompt + "\n\n" + user_prompt, use_search=False)
        text, _cites, _q = parse_gemini_response(raw)
        provider, model_name = "gemini", GEMINI_MODEL
    if text is None or not text.strip():
        return _skip(provider, model_name)

    # 모델이 '적합 글 아님'을 알리면 승격 스킵(오분류 흡수).
    if _is_no_fit(text):
        return _skip(provider, model_name)

    text = USED_SOURCES_STRIP_RE.sub('', text)
    text, volatility, reason = extract_volatility_and_reason(text)
    text = sanitize_text(text)
    text = tidy_body(text)
    if not text.strip():
        return _skip(provider, model_name)

    header = (
        "[ 따뜻한 선행 이야기 ]" if category == "kindness"
        else "[ 인류애가 흔들리는 대기업 사건 ]"
    )
    return {
        "no_fit": False, "category": category,
        "body": text, "text": header + "\n\n" + text.strip(),
        "volatility_score": volatility, "poetic_reason": reason,
        "provider": provider, "model": model_name,
    }


def generate(category: str | None = None) -> dict:
    if category not in ("kindness", "critique"):
        category = "kindness" if random.random() < 0.5 else "critique"

    gap_data = None
    volatility = 0
    reason = ""
    
    actual_provider = LLM_PROVIDER
    text = None
    citations = []
    queries = []
    model_name = ""

    if actual_provider == "groq":
        try:
            text, citations, queries, model_name, gap_data, volatility, reason = generate_via_groq(category)
        except Exception as e:
            if GEMINI_API_KEY:
                logger.warning(f"[llm] Groq 생성 실패({e!r}). Gemini로 폴백하여 생성을 시도합니다.")
                actual_provider = "gemini"
            else:
                raise e

    if actual_provider == "gemini":
        prompt = PROMPT_KINDNESS if category == "kindness" else PROMPT_CRITIQUE
        raw = call_gemini(prompt)
        text, citations, queries = parse_gemini_response(raw)
        model_name = GEMINI_MODEL
        # Gemini 도 적합 글이 없으면 NO_FIT 을 낼 수 있다 — groq 와 동일하게 첫 줄에서 잡아
        # 'NO_FIT'가 본문으로 새지 않게 None 으로 비우고, 아래 no_fit 처리(503/스킵)로 흘려보낸다.
        if RELEVANCE_GATE_ENABLED and _is_no_fit(text):
            text = None
        else:
            # gemini 는 grounding 으로 citation 을 얻으므로 모델이 남긴 USED_SOURCES 줄은 버린다
            text = USED_SOURCES_STRIP_RE.sub('', text)
            text, volatility, reason = extract_volatility_and_reason(text)
            text = sanitize_text(text)
        # 격차 탐지는 provider 무관(Tavily 기반)하게 적용. Tavily 미설정이면
        # measure_news_coverage 가 graceful 하게 None 반환 → gap 없이 진행.
        if text and citations:
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
    elif actual_provider != "groq":
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
            "provider": actual_provider,
            "model": model_name,
            "gap_data": None,
            "poetic_reason": "",
            "volatility_score": 0,
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
        "provider": actual_provider,
        "model": model_name,
        "gap_data": gap_data,
        "poetic_reason": reason,
        "volatility_score": volatility,
    }

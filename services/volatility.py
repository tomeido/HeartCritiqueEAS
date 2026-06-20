"""
삭제확률 예측기 (Predictive Volatility Scorer).

배경: 지금까지 '휘발성 점수'는 LLM 이 *검색으로 찾은 글*을 스스로 매긴 self-rating 이었다.
검색(Tavily/Google)은 구조적으로 indexed·durable 한 인기글(개념글/베스트)만 돌려주므로,
그 위에서 LLM 이 "이 글은 곧 사라질 것 같다"고 추정해봐야 순환논리다(실제로는 안 지워지는 글).

이 모듈은 글의 *관측 가능한 신호*만으로 '삭제될 확률'을 결정적(deterministic)으로 예측한다.
한국 커뮤니티에서 글이 실제로 삭제되는 경로를 신호로 환원한다:
  · 자본력 법적압박(명예훼손) → 기업/오너 실명 + 고발 어휘가 함께 있는 글이 takedown 1순위
  · 운영자 신고삭제 / 블라인드 처리 → 폭로·내부고발성, 신고 유발 글
  · 작성자 자삭(백래시·신상 노출) → 이미 압박받는 정황(고소·연락 왔다·내리라) 마커
  · 게시판 위험도 → 블라인드(직장 폭로)·네이트판(실명 폭로)·디시 갤 등은 삭제가 잦고,
                     미담·잡담 게시판은 드물다(prior).
  · 신선도 → 갓 올라온 글일수록 삭제 결정 창(window) 안에 있다.

설계 원칙:
  · 보수적 디폴트: 신호가 없으면 낮게(과대평가로 멀쩡한 글을 박제 후보로 올리지 않게).
  · 카테고리 차등: critique(비위 폭로)는 삭제 압력이 본질, kindness(미담)는 대체로 durable.
    단 kindness 도 '사인(私人) 신상 노출' 정황이면 자삭 위험이 높다.
  · 환각·법적 가드레일과 독립: 여긴 *선택/우선순위* 신호만 만든다. 공개 본문은 LLM 익명
    재작성을 거치며, 박제 임계값 인하는 여전히 tracker 의 hard 삭제 신호만 쓴다.

순수 표준 라이브러리만 사용(외부 의존성 없음) — import 안전.
"""

import os
import re
from urllib.parse import urlparse

# ── 게시판 위험도 prior (host 접미사 매칭) ────────────────────────────────────
# 삭제가 잦은(실명 폭로·직장 내부고발·정치시사 갤) 출처일수록 높다. prior 일 뿐,
# 본문 신호(고발·압박)가 실제 증거다.
_SOURCE_RISK = {
    "teamblind.com": "high",     # 직장 익명 폭로 — 명예훼손·회사 압박으로 자주 삭제
    "blind.com": "high",
    "pann.nate.com": "high",     # 일반인 실명 폭로 — 신고·자삭·법적압박
    "nate.com": "high",
    "dcinside.com": "high",      # 갤러리 내부고발·실시간 베스트 폭로
    "fmkorea.com": "high",       # 정치/시사 + 폭로 (단 봇차단이라 추적 자체는 tracker 가 제외)
    "theqoo.net": "medium",      # 사회이슈·갑질 폭로 활발
    "instiz.net": "medium",
    "bobaedream.co.kr": "medium",  # 차·제조 소비자 고발
    "clien.net": "medium",       # IT 회사 내부정보
    "mlbpark.donga.com": "medium",
    "ruliweb.com": "low",        # 게이머·소소한 글
    "ppomppu.co.kr": "low",      # 미담·자유게시판
    "inven.co.kr": "low",
}


def source_risk(url: str) -> str:
    """출처 URL 의 게시판 삭제위험 prior: 'high'|'medium'|'low'|'unknown'."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return "unknown"
    if not host:
        return "unknown"
    for dom, risk in _SOURCE_RISK.items():
        if host == dom or host.endswith("." + dom):
            return risk
    return "unknown"


# ── 본문/제목 신호 패턴 ───────────────────────────────────────────────────────
# 고발 어휘: 명예훼손 소지가 큰 비위 주장(있으면 자본/개인이 takedown 을 시도할 동기).
ACCUSATION_RE = re.compile(
    r"갑질|폭언|폭행|횡령|탈세|배임|비리|뇌물|담합|불법|위법|은폐|조작|분식|"
    r"성추행|성폭행|성희롱|성범죄|강제추행|몰카|불법촬영|"
    r"마약|대마|음주운전|음주\s*뺑소니|"
    r"임금\s*체불|체불|하청\s*갑질|단가\s*후려치|납품\s*갑질|"
    r"내부\s*고발|제보|폭로|고발|의혹|논란|규탄|"
    r"사기|먹튀|보복|괴롭힘|따돌림|직장\s*내\s*괴롭힘|"
    r"데이트\s*폭력|가정\s*폭력|학교\s*폭력|학폭"
)

# 기업/권력자 지시어: 실명·직책이 걸리면 명예훼손 위험이 커져 삭제 압력 ↑.
ENTITY_RE = re.compile(
    r"대기업|재벌|오너|회장|부회장|사장|대표이사|대표\b|임원|상무|전무|이사\b|본부장|"
    r"㈜|주식회사|\(주\)|법인|원장|교수|의사\b|변호사|국회의원|시의원|공무원|"
    r"\b[가-힣]{2,}그룹\b|\b[가-힣]{2,}전자\b|\b[가-힣]{2,}건설\b|\b[가-힣]{2,}제약\b|"
    r"프랜차이즈|본사|가맹|갑\s*회사|원청"
)

# 내부고발/제보 성격: 신고·블라인드 처리·운영삭제를 유발하기 쉽다.
WHISTLEBLOWER_RE = re.compile(
    r"내부\s*고발|제보(?:합니다|드립니다|글|자)|블라인드(?:에|글|함)|직장\s*동료|"
    r"전\s*직원|현\s*직원|퇴사(?:하면서|하고|자)|재직\s*중|사내|회사에서"
)

# 작성자가 이미 압박받는 정황(자삭/긴급 박제의 가장 강한 예측 신호).
# '곧 내려갈 글'을 직접 가리킨다.
PRESSURE_RE = re.compile(
    r"신상\s*(?:털|공개|박제)|좌표|"
    r"연락(?:이|\s*が)?\s*(?:왔|옴|받았)|"
    r"내리라(?:고|는)?|내려\s*달라|삭제\s*(?:해\s*달라|요청|압박|하래|하라고|당)|"
    r"캡처\s*(?:해|떠|필수|박제)|증거\s*인멸|박제\s*각|"
    r"퍼\s*날라|퍼가|빨리\s*저장|사라지기\s*전|지워지기\s*전|곧\s*삭제"
)

# 법적 압박 특정어(명예훼손·고소·정정보도): 삭제 동기가 가장 직접적.
LEGAL_RE = re.compile(
    r"명예\s*훼손|고소(?:하겠|장|당|함|미아)|고발(?:하겠|당|장)|"
    r"법적\s*(?:대응|조치|책임)|손해\s*배상|정정\s*보도|"
    r"내용\s*증명|변호사\s*(?:선임|통해|상담)|법무\s*법인|소송"
)

# 미담/durable 신호: kindness 의 대다수는 삭제되지 않는다(과대평가 억제용).
DURABLE_RE = re.compile(
    r"미담|훈훈|감동|선행|미소|따뜻|위로|응원\s*글|개념글|명작|레전드|박제\s*하고\s*싶"
)

# ── 가중치 (env 로 튜닝 가능) ─────────────────────────────────────────────────
def _w(name: str, default: int) -> int:
    try:
        return int(os.environ.get(f"VOLATILITY_W_{name}", str(default)))
    except ValueError:
        return default


W_ENTITY_ACCUSATION = _w("ENTITY_ACCUSATION", 4)  # 기업/권력자 실명 + 고발 어휘 동반
W_ACCUSATION_ONLY   = _w("ACCUSATION_ONLY", 2)
W_ENTITY_ONLY       = _w("ENTITY_ONLY", 1)
W_WHISTLEBLOWER     = _w("WHISTLEBLOWER", 2)
W_PRESSURE          = _w("PRESSURE", 3)            # 이미 압박받는 정황 — 최강 단일 신호
W_LEGAL             = _w("LEGAL", 1)               # 법적 압박 특정어(압박 위에 가산)
W_SOURCE_HIGH       = _w("SOURCE_HIGH", 2)
W_SOURCE_MEDIUM     = _w("SOURCE_MEDIUM", 1)
W_SOURCE_UNKNOWN    = _w("SOURCE_UNKNOWN", 1)
W_FRESH_24H         = _w("FRESH_24H", 2)
W_FRESH_72H         = _w("FRESH_72H", 1)
# kindness 는 기업-고발 축이 삭제로 잘 이어지지 않으므로 기본 감산(사인 노출 정황은 예외).
KINDNESS_BASE_PENALTY = _w("KINDNESS_PENALTY", 2)

# 승격(공개 박제 후보) 게이트로 쓸 '확실한 삭제위험' 판정.
PROMOTE_HARD_THRESHOLD = _w("PROMOTE_HARD", 6)


def predict_volatility(
    title: str | None,
    body: str | None,
    source_url: str = "",
    category: str | None = None,
    age_hours: float | None = None,
) -> dict:
    """글의 삭제확률을 0~10 정수로 예측.

    반환 dict:
      score      : 0~10 (높을수록 곧 삭제될 가능성 큼)
      signals    : 발화한 신호 라벨 목록(설명/UI/로그용)
      components : 각 축의 기여 점수(디버깅/튜닝용)
      hard       : '확실한 삭제위험'(기업+고발 동반 또는 법적/압박 정황) — 승격 게이트용
      source_risk: 게시판 prior

    age_hours=None 이면 신선도 축은 중립(검색 경로는 정확한 게시 시각을 모름).
    """
    text = f"{title or ''}\n{body or ''}"

    has_accusation = bool(ACCUSATION_RE.search(text))
    has_entity = bool(ENTITY_RE.search(text))
    has_whistle = bool(WHISTLEBLOWER_RE.search(text))
    has_pressure = bool(PRESSURE_RE.search(text))
    has_legal = bool(LEGAL_RE.search(text))
    has_durable = bool(DURABLE_RE.search(text))
    risk = source_risk(source_url)

    comp: dict[str, int] = {}
    signals: list[str] = []

    # 1) 고발 × 실명 (명예훼손 takedown 위험)
    if has_entity and has_accusation:
        comp["entity_accusation"] = W_ENTITY_ACCUSATION
        signals.append("기업·실명+고발 어휘")
    elif has_accusation:
        comp["accusation"] = W_ACCUSATION_ONLY
        signals.append("고발·폭로 어휘")
    elif has_entity:
        comp["entity"] = W_ENTITY_ONLY
        signals.append("기업·권력자 지목")

    # 2) 내부고발/제보 성격
    if has_whistle:
        comp["whistleblower"] = W_WHISTLEBLOWER
        signals.append("내부고발·제보성")

    # 3) 이미 압박받는 정황(가장 강한 예측 신호)
    if has_pressure:
        comp["pressure"] = W_PRESSURE
        signals.append("삭제 압박·자삭 정황")
    if has_legal:
        comp["legal"] = W_LEGAL
        signals.append("법적 대응 언급")

    # 4) 게시판 위험도 prior
    if risk == "high":
        comp["source"] = W_SOURCE_HIGH
        signals.append("삭제 잦은 게시판")
    elif risk == "medium":
        comp["source"] = W_SOURCE_MEDIUM
    elif risk == "unknown":
        comp["source"] = W_SOURCE_UNKNOWN

    # 5) 신선도(삭제 결정 창)
    if age_hours is not None:
        if age_hours <= 24:
            comp["fresh"] = W_FRESH_24H
            signals.append("갓 올라온 글")
        elif age_hours <= 72:
            comp["fresh"] = W_FRESH_72H

    score = sum(comp.values())

    # 6) 카테고리 차등
    if category == "kindness":
        # 기업-고발 축은 kindness 삭제로 잘 이어지지 않는다 → 절반만 인정.
        for k in ("entity_accusation", "accusation", "entity"):
            if k in comp:
                drop = comp[k] - comp[k] // 2
                score -= drop
                comp[k] -= drop
        # 사인 노출/자삭 압박 정황이 없으면 미담은 대체로 durable → 기본 감산.
        if not has_pressure:
            score -= KINDNESS_BASE_PENALTY
            comp["kindness_penalty"] = -KINDNESS_BASE_PENALTY

    # durable(미담/감동) 명시 신호는 약한 감산(critique 에 새어든 잡음 억제).
    if has_durable and not (has_pressure or (has_entity and has_accusation)):
        score -= 1
        comp["durable"] = comp.get("durable", 0) - 1

    score = max(0, min(10, score))

    # '확실한 삭제위험': 명예훼손 동반 폭로 or 실제 압박/법적 정황. 승격 게이트에 사용.
    hard = bool((has_entity and has_accusation) or has_pressure or has_legal)

    return {
        "score": score,
        "signals": signals,
        "components": comp,
        "hard": hard,
        "source_risk": risk,
    }


def score_only(title, body, source_url="", category=None, age_hours=None) -> int:
    """점수만 필요할 때의 간편 래퍼."""
    return predict_volatility(title, body, source_url, category, age_hours)["score"]

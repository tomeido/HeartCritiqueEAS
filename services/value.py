"""
아카이브 가치 스코어러 (Archival Value Scorer).

volatility.py 가 "곧 삭제될 확률"을 재는 것과 별개로, 이 모듈은 "사라지면 아까운 정도"
(박제 가치)를 결정적(deterministic)으로 잰다. 기준 연구는 docs/ARCHIVAL_CRITERIA.md 참고:
  · 증거가치: 1인칭 직접 경험 + 물증(사진·녹취·영수증) 언급 = 1차 사료
  · 정보가치: 공익 제보(기업 실명+고발), 내부고발, 소비자 안전, 구체성(본문 길이)
  · 유일성: 뉴스 기사 펌글은 원 기사가 아카이브를 대신하므로 감산
  · 윤리 하한: 공익 없는 사적 노출·잡담·질문은 자연히 저점 → 캡처 우선순위에서 밀림
  · hard negative: 광고·거래 글은 점수와 별개 불리언으로 반환해 캡처 자체에서 제외

설계 원칙(volatility.py 와 동일):
  · 보수적 디폴트 — 신호가 없으면 낮게. 점수는 *순위*용이지 *판정*용이 아니다.
  · 선별·우선순위·UI 전용 — 생성 게이트·투표 임계값·박제 결정엔 절대 주입하지 않는다
    (자동화된 가치판단이 영구 박제를 좌우하면 안 됨).
  · 가중치는 VALUE_W_* env 로 튜닝 가능하며 /api/transparency 로 공개된다.

순수 표준 라이브러리만 사용(외부 의존성 없음) — import 안전.
"""

import os
import re

from services.volatility import ACCUSATION_RE, ENTITY_RE, WHISTLEBLOWER_RE

# ── 가산 신호 ─────────────────────────────────────────────────────────────────
# 1인칭 직접 경험(1차 사료성): 전문(傳聞)이 아닌 당사자 증언.
FIRST_PERSON_RE = re.compile(
    r"(?:제|내)가\s*(?:직접\s*)?(?:겪|당했|당한|봤|보았|목격|경험)"
    r"|직접\s*(?:겪|목격|경험|당해|당했)"
    r"|당사자\s*(?:입니다|이에요|예요|로서)"
    r"|(?:저|나)(?:는|도)\s*[^\n]{0,20}?(?:당했|겪었|봤습니다)"
    r"|제\s*(?:일|경험|사연)(?:입니다|이에요|인데)"
)

# 물증 언급(증거가치의 핵심): 물증 있는 글이 takedown 1순위이기도 하다.
EVIDENCE_RE = re.compile(
    r"영수증|녹취|녹음\s*(?:파일|본|했)|진단서|진료\s*기록|계약서|판결문|내용\s*증명|"
    r"블랙박스|CCTV|씨씨티비|"
    r"(?:사진|캡처|캡쳐|스크린샷)\s*(?:첨부|있|올|찍)|증거\s*(?:자료|있|사진|영상)|"
    r"(?:문자|카톡|메시지|대화)\s*내역|증빙"
)

# 소비자 안전/피해(제3자 피해 예방의 정보가치).
CONSUMER_RE = re.compile(
    r"이물질|식중독|위생\s*(?:상태|문제|불량)|부작용|의료\s*사고|오진|"
    r"결함|리콜|불량품?|하자|누수|급발진|화재|폭발|감전|"
    r"환불\s*거부|AS\s*거부|교환\s*거부|유통기한\s*(?:지난|경과|위조)"
)

# 목격 미담(kindness 의 존재 이유): 기록되지 않으면 사라질 선행의 구체적 목격.
KINDNESS_RE = re.compile(
    r"선행|미담|의인|은인|"
    r"구조(?:했|하셨|해\s*주)|구해\s*(?:줬|주셨|냈)|살려\s*(?:줬|주셨|냈)|"
    r"도와\s*주(?:신|셨|고)|기부(?:했|하셨|천사)|봉사\s*(?:활동|하시)|"
    r"양보(?:해|했|하시)|베풀|선뜻\s*(?:나서|내어|건네)"
)

# ── 감산 신호 ─────────────────────────────────────────────────────────────────
# 광고·홍보·거래(판매자 측 어휘로 앵커링 — '허위 광고 당했다' 같은 피해 글 오탐 방지).
AD_RE = re.compile(
    r"팝니다|판매\s*합니다|삽니다|분양\s*합니다|"
    r"공동\s*구매|공구\s*(?:진행|모집|오픈)|"
    r"쿠폰\s*(?:배포|나눔|드려요|드립니다)|할인\s*코드|프로모션\s*코드|"
    r"가입\s*시\s*(?:혜택|포인트|적립)|추천인\s*코드|"
    r"문의는?\s*(?:카톡|카카오톡|텔레그램|오픈\s*채팅|DM|디엠)|"
    r"수익\s*보장|재택\s*부업|원금\s*보장"
)

# 단순 질문·상담(증거·정보가치 없음): 제목이 물음으로 끝나거나 추천/질문 어휘.
QUESTION_RE = re.compile(
    r"(?:나요|까요|을까요|는지요|가요)\s*\?|\?\s*$|"
    r"추천\s*(?:좀|부탁|해\s*주|받습니다)|뭐가\s*좋|어떤\s*게\s*좋|"
    r"질문\s*(?:입니다|드립니다|있습니다)|문의\s*드립니다|고민\s*상담"
)

# 뉴스 기사 펌글(유일성 없음 — 원 기사가 아카이브를 대신): 기사 본문 붙여넣기의
# 전형적 표식('OOO 기자', 통신사명, [단독] 류 헤드라인 태그)에 앵커링.
NEWS_REPOST_RE = re.compile(
    r"[가-힣]{2,4}\s*기자\s*=|기자\s*[가-힣]{2,4}\s*=|"
    r"연합뉴스|뉴시스|뉴스1|노컷뉴스|헤럴드경제|"
    r"\[(?:단독|속보|종합|팩트체크)\]|무단\s*전재\s*및?\s*재배포\s*금지"
)

# 구체성(정보가치 대리 지표) 길이 계단.
_LEN_SHORT = 80        # 이 미만 = 한 줄 글(재작성할 실체 없음)
_LEN_LONG = 600        # 이 이상 = 구체적 서술
_LEN_VERY_LONG = 1500  # 이 이상 = 상세 기록


# ── 가중치 (env 로 튜닝 가능 — /api/transparency 로 공개) ─────────────────────
def _w(name: str, default: int) -> int:
    try:
        return int(os.environ.get(f"VALUE_W_{name}", str(default)))
    except ValueError:
        return default


W_FIRST_PERSON    = _w("FIRST_PERSON", 2)     # 1차 사료성
W_EVIDENCE        = _w("EVIDENCE", 2)         # 물증 언급
W_PUBLIC_INTEREST = _w("PUBLIC_INTEREST", 3)  # 기업/권력자 실명 + 고발 동반
W_ACCUSATION_ONLY = _w("ACCUSATION_ONLY", 1)  # 고발 어휘만
W_WHISTLEBLOWER   = _w("WHISTLEBLOWER", 2)    # 내부고발·제보성
W_CONSUMER        = _w("CONSUMER", 2)         # 소비자 안전/피해
W_KINDNESS        = _w("KINDNESS", 2)         # 목격 미담
W_DETAIL_LONG     = _w("DETAIL_LONG", 1)      # 본문 600자+
W_DETAIL_VERY     = _w("DETAIL_VERY", 2)      # 본문 1500자+ (LONG 과 배타)
P_QUESTION        = _w("P_QUESTION", 2)       # 질문·상담 감산
P_NEWS_REPOST     = _w("P_NEWS_REPOST", 2)    # 기사 펌글 감산
P_SHORT           = _w("P_SHORT", 2)          # 한 줄 글 감산
P_AD              = _w("P_AD", 2)             # 광고·거래 어휘 감산(hard negative 와 별개)


def assess_value(title: str | None, body: str | None) -> dict:
    """글의 박제 가치를 0~10 정수로 평가.

    반환 dict:
      score         : 0~10 (높을수록 사라지면 아까운 글)
      signals       : 발화한 신호 라벨 목록(설명/UI/로그용)
      components    : 각 축의 기여 점수(디버깅/튜닝용)
      hard_negative : 광고·거래 글 판정 — 캡처 자체를 건너뛰는 게이트용.
                      단 피해 정황(1인칭/고발/물증)이 함께 있으면 False
                      ('허위 광고에 당했다' 같은 소비자 고발을 배제하지 않게).
    """
    text = f"{title or ''}\n{body or ''}"
    body_len = len((body or "").strip())

    has_first = bool(FIRST_PERSON_RE.search(text))
    has_evidence = bool(EVIDENCE_RE.search(text))
    has_accusation = bool(ACCUSATION_RE.search(text))
    has_entity = bool(ENTITY_RE.search(text))
    has_whistle = bool(WHISTLEBLOWER_RE.search(text))
    has_consumer = bool(CONSUMER_RE.search(text))
    has_kindness = bool(KINDNESS_RE.search(text))
    has_ad = bool(AD_RE.search(text))
    has_question = bool(QUESTION_RE.search(text))
    has_repost = bool(NEWS_REPOST_RE.search(text))

    comp: dict[str, int] = {}
    signals: list[str] = []

    # 1) 1차 사료성 + 증거가치
    if has_first:
        comp["first_person"] = W_FIRST_PERSON
        signals.append("1인칭 직접 경험")
    if has_evidence:
        comp["evidence"] = W_EVIDENCE
        signals.append("물증 언급")

    # 2) 공익성(감시 기능) — volatility 와 같은 축이지만 여기선 '가치'로 계상
    if has_entity and has_accusation:
        comp["public_interest"] = W_PUBLIC_INTEREST
        signals.append("공익 제보(실명+고발)")
    elif has_accusation:
        comp["accusation"] = W_ACCUSATION_ONLY
        signals.append("고발·폭로 어휘")
    if has_whistle:
        comp["whistleblower"] = W_WHISTLEBLOWER
        signals.append("내부고발·제보성")
    if has_consumer:
        comp["consumer"] = W_CONSUMER
        signals.append("소비자 안전·피해")

    # 3) 미담 가치
    if has_kindness:
        comp["kindness"] = W_KINDNESS
        signals.append("목격 미담·선행")

    # 4) 구체성(본문 길이 계단). 본문이 아예 없으면(RSS 요약만) 중립.
    if body_len:
        if body_len < _LEN_SHORT:
            comp["short"] = -P_SHORT
            signals.append("한 줄 글")
        elif body_len >= _LEN_VERY_LONG:
            comp["detail"] = W_DETAIL_VERY
            signals.append("상세 기록")
        elif body_len >= _LEN_LONG:
            comp["detail"] = W_DETAIL_LONG

    # 5) 유일성·잡음 감산
    if has_question:
        comp["question"] = -P_QUESTION
        signals.append("질문·상담성")
    if has_repost:
        comp["news_repost"] = -P_NEWS_REPOST
        signals.append("기사 펌글")
    if has_ad:
        comp["ad"] = -P_AD   # 광고 어휘는 점수도 깎는다(캡처 제외 게이트와 별개)
        signals.append("광고·거래 어휘")

    score = max(0, min(10, sum(comp.values())))

    # hard negative: 판매자 측 광고·거래 글은 캡처 예산 자체를 쓰지 않는다.
    # 단 피해 정황(1인칭·고발·물증)이 동반되면 소비자 고발일 수 있어 제외하지 않는다.
    hard_negative = has_ad and not (has_first or has_accusation or has_evidence)

    return {
        "score": score,
        "signals": signals,
        "components": comp,
        "hard_negative": hard_negative,
    }


def score_only(title, body) -> int:
    """점수만 필요할 때의 간편 래퍼."""
    return assess_value(title, body)["score"]


def capture_priority(title: str | None, body: str | None, source_url: str = "") -> int:
    """캡처 우선순위 = 삭제위험(volatility) + 박제가치(value) 결합 점수(0~20).
    collector 의 피드 내 정렬 전용 — 게이트·임계값·박제 결정엔 쓰지 않는다."""
    from services.volatility import predict_volatility  # 지연 import (테스트 격리 용이)
    v = predict_volatility(title, body or "", source_url)["score"]
    return v + assess_value(title, body)["score"]

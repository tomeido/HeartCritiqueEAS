"""
개인식별정보(PII) 스캐너 — 캡처 본문 공개 전 안전 게이트.

왜 필요한가: collector 가 비공개로 보관한 captured_posts.body_text 는 검색 스니펫(600자)이
아니라 *전체 본문*이라, 주민번호·전화·이메일·카드/계좌번호 같은 구조적 식별자가 들어있을 수
있다. LLM 익명화 프롬프트는 '없는 사실 창작'은 막지만 '본문에 적힌 PII 를 그대로 옮기는 것'은
못 막는다. Arweave 박제는 되돌릴 수 없으므로, 승격(공개) 전·후로 본문을 스캔해 구조적 PII 가
검출되면 자동 승격을 차단하고 수동 검토 큐로 보낸다(blocked_pii).

설계 원칙:
  · 신뢰성 있게 잡히는 *구조적 식별자*만 대상으로 한다(주민번호/전화/이메일/카드/긴 숫자열).
    실명·주소는 한글 2~3자/행정구역이라 정규식 오탐이 폭발하므로 여기서 잡지 않는다 — 그건
    LLM 익명화 프롬프트('한 누리꾼'·회사명 일반화)와 분량 제한의 몫이다.
  · 보수적: 의심되면 막는다(승격은 옵트인 기능이라 과차단의 비용 < PII 영구공개의 비용).
순수 표준 라이브러리만 사용.
"""

import re

# 주민등록번호/외국인등록번호: 6자리 - 성별자리(1~8) + 6자리. 하이픈 선택.
# 생년월일 6자리 뒤 성별코드(1900s 1·2, 2000s 3·4, 외국인 5~8)로 앵커링해 일반 숫자열과 구분.
RRN_RE = re.compile(r"\b\d{6}\s*-?\s*[1-8]\d{6}\b")

# 휴대폰: 010/011/016~019. 하이픈/공백 선택.
MOBILE_RE = re.compile(r"\b01[016-9][-\s.]?\d{3,4}[-\s.]?\d{4}\b")

# 유선전화: 02 또는 0XX 지역번호 + 국번 + 번호 (하이픈 필수로 오탐 억제).
LANDLINE_RE = re.compile(r"\b0(?:2|[3-6][1-5])[-\s.]\d{3,4}[-\s.]\d{4}\b")

# 이메일.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# 카드번호: 4-4-4-4 (구분자 필수). 맨숫자 16자리는 LONG_DIGITS 가 별도로 잡음.
CARD_RE = re.compile(r"\b\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}\b")

# 구분자 없는 11자리 이상 연속 숫자: 계좌/카드/주민번호의 무하이픈 표기를 포괄.
# 커뮤니티 글에서 11자리 이상 '맨숫자'는 거의 식별자다(게시물 번호는 보통 더 짧음).
LONG_DIGITS_RE = re.compile(r"\b\d{11,}\b")

# 계좌 맥락 + 숫자열(은행/계좌/입금/송금 키워드 근처의 8자리 이상 하이픈 숫자).
ACCOUNT_CTX_RE = re.compile(
    r"(?:계좌|입금|송금|이체|은행|농협|국민|신한|우리|하나|기업|카카오\s*뱅크|토스)"
    r"[^\n]{0,12}?\b\d{2,6}[-\s]\d{2,6}[-\s]\d{2,7}\b"
)

_DETECTORS = (
    ("rrn", RRN_RE),
    ("mobile", MOBILE_RE),
    ("landline", LANDLINE_RE),
    ("email", EMAIL_RE),
    ("card", CARD_RE),
    ("account", ACCOUNT_CTX_RE),
    ("long_digits", LONG_DIGITS_RE),
)


def _mask(s: str) -> str:
    """샘플 로깅용 마스킹(앞 2자만 남기고 가린다 — 로그에도 원문 PII 를 남기지 않음)."""
    s = s.strip()
    if len(s) <= 2:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 2)


def scan(text: str) -> dict:
    """본문에서 구조적 PII 를 탐지. 반환: {hit, kinds, samples}.
    samples 는 마스킹된 일부(디버깅/감사 로그용, 원문 미노출)."""
    text = text or ""
    kinds: list[str] = []
    samples: list[str] = []
    for kind, rx in _DETECTORS:
        m = rx.search(text)
        if m:
            kinds.append(kind)
            samples.append(f"{kind}:{_mask(m.group(0))}")
    return {"hit": bool(kinds), "kinds": kinds, "samples": samples}


def has_pii(text: str) -> bool:
    """구조적 PII 검출 여부(승격 게이트용 간편 판정)."""
    return scan(text)["hit"]

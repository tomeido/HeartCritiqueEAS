"""critique 적합성 게이트(looks_off_topic_critique) 테스트.

설계 확인: critique 는 '기업/노동/소비자/제도 부조리 신호 하나 이상 보유'를 요구하는
positive gate — 스포츠·게임·연예·진영정치·일상 글이 '대기업 비위'로 둔갑하는 것을
구조적으로 컷한다. kindness 와 달리 3차 폴백에서도 필터를 유지한다(둔갑시키느니 스킵).
"""

from services import llm


def _item(title, content=""):
    return {"title": title, "content": content}


# ── 판정: off-topic (on-topic 신호 없음 → True=컷) ──────────────────────────
def test_sports_post_is_off_topic():
    assert llm.looks_off_topic_critique(
        _item("월드컵 조별리그 경기 결과", "대표팀 선발 라인업과 승부 예측")) is True


def test_card_game_post_is_off_topic():
    assert llm.looks_off_topic_critique(
        _item("포켓몬 카드 뽑기 결과", "오늘 가챠 컬렉션 인증")) is True


def test_celebrity_gossip_is_off_topic():
    assert llm.looks_off_topic_critique(
        _item("아이돌 컴백 무대", "음원 차트 진입 예측")) is True


def test_daily_chat_is_off_topic():
    assert llm.looks_off_topic_critique(_item("오늘 점심 뭐 먹지", "그냥 잡담")) is True


# ── 판정: on-topic (부조리 신호 보유 → False=통과) ──────────────────────────
def test_workplace_abuse_is_on_topic():
    assert llm.looks_off_topic_critique(
        _item("상사 폭언 견디다 못해 씁니다", "회사에서 매일 괴롭힘")) is False


def test_subcontract_squeeze_is_on_topic():
    assert llm.looks_off_topic_critique(
        _item("하청 단가 후려치기 제보", "납품 대금 미지급 3개월째")) is False


def test_product_defect_is_on_topic():
    assert llm.looks_off_topic_critique(
        _item("신차 결함인데 리콜 거부당함", "제조사가 보상을 미룹니다")) is False


def test_consumer_fraud_is_on_topic():
    # kindness 에선 부정신호인 '사기'가 critique 에선 정상 주제(소비자 기만).
    assert llm.looks_off_topic_critique(
        _item("허위 과장 광고에 당했습니다", "환불도 거부")) is False


def test_ontopic_signal_in_body_only_is_enough():
    # 제목이 모호해도 본문 앞부분(400자)의 신호로 살린다(과필터 방지).
    assert llm.looks_off_topic_critique(
        _item("어제 있었던 일", "회사 임금 체불이 상습적입니다")) is False


# ── 시드 불변식: 모든 critique 시드는 on-topic 앵커를 포함해야 한다 ──────────
def test_every_critique_seed_has_ontopic_anchor():
    """앵커 없는 범용 시드는 trivial 글을 끌어와 게이트에서 통째로 컷돼 recall 만
    깎는다 — 시드를 추가할 때 이 불변식이 깨지지 않게 구조적으로 강제."""
    for seed in llm.SEARCH_QUERIES_CRITIQUE:
        assert llm.CRITIQUE_ONTOPIC_RE.search(seed), f"앵커 없는 시드: {seed!r}"


# ── 3차 폴백 비대칭: critique 는 필터 유지, kindness 는 해제 ─────────────────
def _fake_tavily(results):
    return lambda *a, **k: {"results": results}


OFFTOPIC_RESULTS = [
    {"title": "월드컵 경기 결과", "url": "https://x.com/1", "content": "승부 예측 " * 20},
    {"title": "아이돌 컴백", "url": "https://x.com/2", "content": "음원 차트 " * 20},
]


def test_groq_search_critique_keeps_filter_in_tier3(monkeypatch):
    monkeypatch.setattr(llm, "tavily_search", _fake_tavily(OFFTOPIC_RESULTS))
    results, _count = llm._groq_search("쿼리", "critique", None)
    assert results == []   # 스포츠·연예 글을 '비위'로 둔갑시키느니 빈 후보(→NO_FIT 스킵)


def test_groq_search_kindness_keeps_filter_in_tier3(monkeypatch):
    # kindness 도 3차 폴백에서 적합성 필터 유지 — no_fit 이면 Gemini 폴백이 있어
    # 사기·괴담을 '미담'으로 둔갑시키는 오수락을 감수할 이유가 없다.
    scam = [{"title": "보이스피싱 당할 뻔", "url": "https://x.com/3", "content": "사기 전화 " * 20}]
    monkeypatch.setattr(llm, "tavily_search", _fake_tavily(scam))
    monkeypatch.setattr(llm.dedup, "filter_known_sources", lambda r: (r, 0))
    results, _count = llm._groq_search("쿼리", "kindness", None)
    assert results == []

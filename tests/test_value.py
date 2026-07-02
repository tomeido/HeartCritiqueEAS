"""아카이브 가치 스코어러(services/value.py) 회귀 테스트 — 순수 함수.

핵심 계약(docs/ARCHIVAL_CRITERIA.md):
  · 1인칭 직접 경험 + 물증 + 공익 제보 = 높은 가치(1차 사료).
  · 광고·거래 글 = hard_negative(캡처 제외 게이트) — 단 피해 고발 정황이 있으면 제외 안 함.
  · 질문·잡담·기사 펌글 = 저가치.
  · 보수적 디폴트: 신호 없으면 낮게. 점수는 0~10 클램프.
"""

import services.value as val


def test_high_value_first_person_whistleblower_with_evidence():
    title = "재직 중인 프랜차이즈 본사 갑질 제보합니다"
    body = ("제가 직접 겪은 일입니다. 본사 임원의 폭언 녹취 파일과 카톡 내역이 있습니다. "
            "점주들에게 단가 후려치기를 강요했고, 증거 자료도 첨부합니다. " * 3)
    r = val.assess_value(title, body)
    assert r["score"] >= 7
    assert r["hard_negative"] is False
    assert "1인칭 직접 경험" in r["signals"]
    assert "물증 언급" in r["signals"]
    assert "공익 제보(실명+고발)" in r["signals"]


def test_kindness_witness_story_positive():
    title = "지하철에서 쓰러진 분을 구조한 의인을 봤습니다"
    body = ("제가 직접 목격했습니다. 한 시민이 쓰러진 어르신을 구해 주셨고 "
            "구급차가 올 때까지 곁을 지켰습니다. 이런 선행은 기록되어야 한다고 생각합니다.")
    r = val.assess_value(title, body)
    assert r["score"] >= 3
    assert "목격 미담·선행" in r["signals"]
    assert r["hard_negative"] is False


def test_ad_post_is_hard_negative():
    title = "노트북 팝니다 (쿠폰 드려요)"
    body = "새 제품 판매합니다. 할인 코드 있어요. 문의는 카톡 주세요. 추천인 코드 입력 시 적립."
    r = val.assess_value(title, body)
    assert r["hard_negative"] is True
    assert r["score"] <= 2


def test_consumer_complaint_about_ads_not_hard_negative():
    # '허위 광고에 당했다' 류 소비자 고발은 광고 어휘가 있어도 캡처 제외하면 안 된다.
    title = "OO몰 허위 광고 사기 당했습니다"
    body = ("제가 직접 당한 일입니다. 판매합니다 글을 보고 입금했는데 물건이 안 왔고 "
            "환불 거부를 당했습니다. 카톡 대화 내역 캡처 있습니다. "
            "같은 판매자에게 당한 분이 더 있는 것 같아 기록으로 남깁니다.")
    r = val.assess_value(title, body)
    assert r["hard_negative"] is False
    assert r["score"] >= 4


def test_question_and_chatter_low_value():
    r = val.assess_value("점심 뭐가 좋나요?", "추천 좀 부탁드립니다")
    assert r["score"] == 0
    assert r["hard_negative"] is False


def test_news_repost_penalized():
    body_repost = ("연합뉴스 김OO 기자 = 대기업 갑질 의혹이 제기됐다. "
                   "무단 전재 및 재배포 금지. " * 10)
    body_original = ("제가 직접 겪은 대기업 갑질입니다. 녹취 있습니다. " * 10)
    r_repost = val.assess_value("갑질 기사", body_repost)
    r_original = val.assess_value("갑질 폭로", body_original)
    assert r_repost["score"] < r_original["score"]
    assert "기사 펌글" in r_repost["signals"]


def test_short_post_penalized_but_no_body_neutral():
    short = val.assess_value("제목", "ㅋㅋ")
    none = val.assess_value("제목", None)
    assert "한 줄 글" in short["signals"]
    # 0-클램프 때문에 score 비교는 허수가 된다 — 컴포넌트로 감산 발화 여부를 직접 확인.
    assert short["components"].get("short", 0) < 0
    assert "short" not in none["components"]   # 본문 없음(RSS 요약만)은 중립


def test_title_ending_question_mark_penalized():
    # 종결어미 패턴이 못 잡는 '이거 어때?' 류 — 제목의 물음표 종결로 잡는다.
    r = val.assess_value("이 노트북 어때?", "구매를 고민하고 있습니다. 의견 부탁드립니다.")
    assert "질문·상담성" in r["signals"]


def test_score_clamped_0_10():
    title = "재직 중 대기업 회장 횡령 성추행 내부고발 제보"
    body = ("제가 직접 겪었습니다. 녹취 파일과 CCTV, 진단서, 카톡 내역 증거 자료 있습니다. "
            "이물질 식중독 피해도 당했고 환불 거부까지. 의인이 도와 주셨습니다. " * 30)
    assert val.assess_value(title, body)["score"] == 10
    assert val.assess_value("점심", "오늘 뭐 먹지")["score"] == 0


def test_deterministic_and_wrapper():
    args = ("갑질 폭로", "제가 직접 겪은 대기업 갑질입니다. 녹취 있습니다.")
    r1, r2 = val.assess_value(*args), val.assess_value(*args)
    assert r1 == r2
    assert val.score_only(*args) == r1["score"]


def test_capture_priority_combines_axes():
    title = "대기업 회장 갑질 내부고발"
    body = "재직 중인 회사입니다. 제가 직접 겪었고 녹취 증거 있습니다. 고소하겠다고 연락 왔어요."
    url = "https://www.teamblind.com/kr/post/1"
    p = val.capture_priority(title, body, url)
    import services.volatility as v
    assert p == v.predict_volatility(title, body, url)["score"] + val.assess_value(title, body)["score"]
    # 잡담은 결합 점수도 바닥
    assert val.capture_priority("점심 추천", "뭐 먹지", "") <= 2

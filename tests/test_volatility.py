"""삭제확률 예측기(services/volatility.py) 회귀 테스트 — 순수 함수.

핵심 계약:
  · 기업/실명 + 고발 + 압박 정황 + 삭제 잦은 게시판 = 높은 점수(곧 삭제될 글).
  · 미담/잡담 = 낮은 점수(durable, 과대평가 억제).
  · source_risk prior 분류.
  · kindness 는 기업-고발 축을 약하게, 사인 노출 압박은 살린다.
  · 보수적 디폴트: 신호 없으면 낮게.
"""

import services.volatility as v


def test_source_risk_classification():
    assert v.source_risk("https://www.teamblind.com/kr/post/123") == "high"
    assert v.source_risk("https://pann.nate.com/talk/999") == "high"
    assert v.source_risk("https://gall.dcinside.com/board/view/?id=x&no=1") == "high"
    assert v.source_risk("https://theqoo.net/square/123") == "medium"
    assert v.source_risk("https://www.clien.net/service/board/park/1") == "medium"
    assert v.source_risk("https://www.ppomppu.co.kr/zboard/view.php?id=freeboard&no=1") == "low"
    assert v.source_risk("https://example.com/blog/1") == "unknown"
    assert v.source_risk("") == "unknown"
    assert v.source_risk("not a url") == "unknown"


def test_high_volatility_corporate_whistleblower_under_pressure():
    # 블라인드 직장 폭로 + 회장 횡령 + 고소/연락 정황 + 갓 올라온 글 → 최고치
    title = "대기업 회장 횡령 내부고발합니다"
    body = ("재직 중인 회사 회장의 비자금 횡령 정황을 제보합니다. "
            "어제 법무법인에서 명예훼손으로 고소하겠다고 연락이 왔습니다. "
            "곧 삭제될 것 같으니 캡처 떠주세요.")
    r = v.predict_volatility(title, body,
                            source_url="https://www.teamblind.com/kr/post/1",
                            category="critique", age_hours=5)
    assert r["score"] >= 8
    assert r["hard"] is True
    assert r["source_risk"] == "high"
    assert any("고발" in s or "폭로" in s for s in r["signals"])


def test_low_volatility_kindness_miscellany():
    # 평범한 미담(ppomppu) — 삭제될 이유가 없는 durable 글 → 낮은 점수
    title = "지하철에서 자리 양보받은 훈훈한 미담"
    body = "한 어르신께 자리를 양보한 학생 이야기. 마음이 따뜻해졌습니다."
    r = v.predict_volatility(title, body,
                            source_url="https://www.ppomppu.co.kr/zboard/view.php?id=freeboard&no=9",
                            category="kindness")
    assert r["score"] <= 2
    assert r["hard"] is False


def test_pressure_marker_alone_flags_hard():
    # 출처/실명 없이도 '이미 압박받는 정황'은 강한 삭제 예측 신호 → hard
    r = v.predict_volatility("긴급", "신상 털렸고 내리라고 연락 왔어요 사라지기 전에 저장",
                            source_url="", category="critique")
    assert r["hard"] is True
    assert r["score"] >= 3


def test_kindness_downweights_corporate_axis():
    # 동일 본문이라도 kindness 로 분류되면 기업-고발 축 기여가 줄어 점수가 낮아진다.
    title = "회사 회장 갑질 폭로"
    body = "대기업 회장의 갑질과 폭언 의혹을 고발합니다."
    crit = v.predict_volatility(title, body, "https://theqoo.net/x/1", category="critique")
    kind = v.predict_volatility(title, body, "https://theqoo.net/x/1", category="kindness")
    assert kind["score"] < crit["score"]


def test_freshness_raises_score():
    title = "회장 비리 의혹 제보"
    body = "대기업 임원의 비리 의혹을 폭로합니다."
    old = v.predict_volatility(title, body, "https://theqoo.net/x/1", "critique", age_hours=None)
    fresh = v.predict_volatility(title, body, "https://theqoo.net/x/1", "critique", age_hours=3)
    assert fresh["score"] >= old["score"]


def test_score_clamped_0_10():
    # 모든 신호 폭발 — 10 으로 클램프
    title = "대기업 회장 횡령 성범죄 마약 내부고발 제보"
    body = ("재직 중 제보합니다. 명예훼손 고소·법적대응 통보받음. 신상 털림. "
            "내리라고 연락 옴. 사라지기 전에 캡처 박제 부탁.")
    r = v.predict_volatility(title, body, "https://www.teamblind.com/kr/post/1",
                            "critique", age_hours=1)
    assert r["score"] == 10

    # 신호 전무 — 0 으로 클램프(보수적 디폴트)
    r0 = v.predict_volatility("점심 메뉴 추천", "오늘 뭐 먹지", "", "kindness")
    assert r0["score"] == 0


def test_score_only_wrapper_matches():
    args = ("회장 갑질 폭로", "대기업 갑질 고발", "https://theqoo.net/x/1", "critique", 2)
    assert v.score_only(*args) == v.predict_volatility(*args)["score"]

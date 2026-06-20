"""generate_from_text(승격 단일 본문 경로) 회귀 테스트.

계약: 캡처 본문 한 편을 받아 기존 가드레일 프롬프트로 재작성하되, USED_SOURCES/휘발성
점수/박제 사유 마커는 본문에서 분리하고, 헤더가 붙은 text·body·점수·사유를 반환한다.
NO_FIT 이면 no_fit=True. 본문은 PROMOTE_BODY_MAX_CHARS 로 truncate 된다."""

import services.llm as llm


def _fake_groq_returning(text):
    return lambda prompt, system=None: {"choices": [{"message": {"content": text}}]}


def test_packs_body_and_strips_markers(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "x")
    out_text = (
        "한 누리꾼이 올린 글에 따르면 어느 회사에서 일이 있었다고 한다.\n"
        "오늘의 한 줄: 작은 목소리가 남는다\n"
        "휘발성 점수: 9\n"
        "박제 사유: 사라지기 전에 붙든다\n"
        "USED_SOURCES: [1]\n"
    )
    monkeypatch.setattr(llm, "call_groq", _fake_groq_returning(out_text))
    body_in = ("어느 회사에서 상사가 직원에게 갑질과 폭언을 했다는 글이 올라왔다. "
               "작성자는 재직 중이며 증거를 가지고 있다고 적었다. " * 2)  # MIN_SOURCE_CONTENT 충족
    r = llm.generate_from_text(body_in, "회사 갑질", "critique")
    assert r["no_fit"] is False
    assert r["provider"] == "groq"
    assert r["volatility_score"] == 9
    assert "사라지기 전에 붙든다" in r["poetic_reason"]
    # 마커 줄은 본문에서 제거됨
    assert "USED_SOURCES" not in r["body"]
    assert "휘발성 점수" not in r["body"]
    # 헤더가 붙은 text
    assert r["text"].startswith("[ 인류애가 흔들리는 대기업 사건 ]")
    assert "한 누리꾼이 올린 글에" in r["body"]


def test_no_fit_returns_skip(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "x")
    monkeypatch.setattr(llm, "call_groq", _fake_groq_returning("NO_FIT"))
    r = llm.generate_from_text("그냥 잡담", "점심", "kindness")
    assert r["no_fit"] is True
    assert r["body"] == ""


def test_truncates_long_body(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "x")
    monkeypatch.setattr(llm, "PROMOTE_BODY_MAX_CHARS", 100)
    seen = {}

    def capture(prompt, system=None):
        seen["prompt"] = prompt
        return {"choices": [{"message": {"content": "한 글에 따르면 일이 있었다고 한다."}}]}

    monkeypatch.setattr(llm, "call_groq", capture)
    long_body = "가" * 5000
    llm.generate_from_text(long_body, None, "kindness")
    # 프롬프트에 들어간 본문은 100자로 잘렸다(5000자 전체가 아님)
    assert seen["prompt"].count("가") <= 100


def test_too_short_body_skips(monkeypatch):
    monkeypatch.setattr(llm, "GROQ_API_KEY", "x")
    called = {"n": 0}
    monkeypatch.setattr(llm, "call_groq",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    r = llm.generate_from_text("짧음", "t", "kindness")  # MIN_SOURCE_CONTENT 미만
    assert r["no_fit"] is True
    assert called["n"] == 0  # LLM 호출 안 함

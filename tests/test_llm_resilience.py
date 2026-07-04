"""미담 503 해소 수정 회귀 테스트 (휘발성 게이트 critique 전용화 + Gemini 폴백 하드닝).

계약:
  · kindness 는 저휘발(<7)이어도 생성한다 — 미담은 삭제 위험과 무관, 점수는 표시 전용.
  · critique 는 저휘발이면 쿼리 재시도(자본 압박 삭제 위험이 핵심 가치).
  · call_gemini 는 일시 오류(5xx/429/네트워크)만 지수백오프 재시도, 4xx 는 즉시 실패.
  · Gemini 응답의 NO_FIT 도 groq 와 동일하게 감지해 본문으로 새지 않는다.
"""

import io
import json
import urllib.error

import pytest

import services.llm as llm
from services import dedup


# ── 휘발성 게이트: critique 전용 ─────────────────────────────────────────────
ONTOPIC_RESULT = {  # critique 적합성 필터(갑질)와 rich 필터(>=80자)를 모두 통과
    "title": "회사 갑질 제보",
    "url": "https://x.com/post/1",
    "content": "상사가 직원에게 갑질과 폭언을 했다는 제보가 올라왔다. " * 5,
}
KIND_RESULT = {  # kindness 비미담 필터에 안 걸리는 순수 미담
    "title": "훈훈한 사연",
    "url": "https://x.com/post/2",
    "content": "지하철에서 한 시민이 쓰러진 노인을 부축해 병원까지 동행했다고 한다. " * 5,
}


def _low_vol_groq(text_body):
    out = (f"{text_body}\n휘발성 점수: 5\n박제 사유: 낮지만 귀한 글\nUSED_SOURCES: [1]\n")
    return lambda prompt, system=None: {"choices": [{"message": {"content": out}}]}


def _patch_pipeline(monkeypatch, result):
    monkeypatch.setattr(llm, "tavily_search", lambda *a, **k: {"results": [result]})
    monkeypatch.setattr(dedup, "filter_known_sources", lambda r: (r, 0))
    monkeypatch.setattr(llm, "GROQ_API_KEY", "x")
    monkeypatch.setattr(llm, "GAP_DETECTION_ENABLED", False)


def test_kindness_low_volatility_still_generates(monkeypatch):
    _patch_pipeline(monkeypatch, KIND_RESULT)
    monkeypatch.setattr(llm, "call_groq",
                        _low_vol_groq("한 시민이 노인을 부축했다는 글이 올라왔다고 한다."))
    text, citations, *_rest, volatility, _reason = llm.generate_via_groq("kindness")
    assert text is not None          # 저휘발이어도 미담은 생성
    assert volatility == 5
    assert citations and citations[0]["uri"] == KIND_RESULT["url"]


def test_critique_low_volatility_retries_to_no_fit(monkeypatch):
    _patch_pipeline(monkeypatch, ONTOPIC_RESULT)
    monkeypatch.setattr(llm, "call_groq",
                        _low_vol_groq("한 회사에서 갑질이 있었다는 글이 올라왔다."))
    text, *_rest = llm.generate_via_groq("critique")
    assert text is None              # 저휘발 critique 는 전 쿼리 소진 → no_fit


# ── call_gemini: 일시 오류만 재시도 ──────────────────────────────────────────
def _http_error(code):
    return urllib.error.HTTPError(
        "https://gemini", code, "err", None, io.BytesIO(b"overloaded"))


def _ok_response():
    class _Resp:
        def read(self):
            return json.dumps({"candidates": []}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _Resp()


def _patch_gemini(monkeypatch, attempts=3):
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "GEMINI_MAX_ATTEMPTS", attempts)
    monkeypatch.setattr(llm, "GEMINI_RETRY_BASE", 0.0)   # 테스트에서 대기 없음


def test_gemini_retries_transient_503_then_succeeds(monkeypatch):
    _patch_gemini(monkeypatch)
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _http_error(503)
        return _ok_response()
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    assert llm.call_gemini("p") == {"candidates": []}
    assert calls["n"] == 3


def test_gemini_4xx_fails_immediately(monkeypatch):
    _patch_gemini(monkeypatch)
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(400)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        llm.call_gemini("p")
    assert calls["n"] == 1           # 키·요청 오류는 재시도 없음


def test_gemini_exhausts_attempts_then_raises(monkeypatch):
    _patch_gemini(monkeypatch, attempts=2)
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(503)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        llm.call_gemini("p")
    assert calls["n"] == 2


# ── generate() gemini 경로: NO_FIT 감지 → 본문 누수 방지 ─────────────────────
def _gemini_raw(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "groundingMetadata": {}}]}


def test_generate_gemini_no_fit_detected(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "call_gemini", lambda p, use_search=True: _gemini_raw("NO_FIT"))
    r = llm.generate("kindness")
    assert r["no_fit"] is True
    assert r["body"] == ""           # 'NO_FIT' 가 본문으로 새지 않음


def test_generate_gemini_normal_text_passes(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "call_gemini", lambda p, use_search=True: _gemini_raw(
        "한 시민이 노인을 부축해 병원까지 동행했다는 글이 올라왔다고 한다.\n"
        "휘발성 점수: 6\n박제 사유: 조용한 온기"))
    r = llm.generate("kindness")
    assert r["no_fit"] is False
    assert "부축해" in r["body"]
    assert r["volatility_score"] == 6

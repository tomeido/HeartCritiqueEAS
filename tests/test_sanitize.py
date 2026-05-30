"""sanitize_text 와 calculate_gap_score 회귀 테스트.

특히 정규식이 정상 한국어 본문(단일 영문 약어·한자 성씨)을 훼손하지 않는지 보장한다.
이게 깨지면 박제되는 본문에서 'KTX/GDP/회사명'이 조용히 사라진다.
"""

import services.llm as llm


def test_single_acronyms_preserved():
    assert "KTX" in llm.sanitize_text("KTX 안에서 한 시민이 도왔다")
    assert "GDP" in llm.sanitize_text("GDP 성장률이 화제다")
    assert "CCTV" in llm.sanitize_text("CCTV에 찍힌 선행")
    assert "CEO" in llm.sanitize_text("어느 CEO가 한 일")


def test_multiword_allcaps_noise_removed():
    out = llm.sanitize_text("SOUTH KOREA 어쩌고 사연")
    assert "SOUTH" not in out and "KOREA" not in out
    assert "사연" in out


def test_markdown_stripped_but_text_kept():
    out = llm.sanitize_text("**굵게** 한 누리꾼이 올린 글")
    assert "**" not in out
    assert "굵게" in out and "누리꾼" in out


def test_japanese_kana_removed():
    out = llm.sanitize_text("어떤 사람이 ありがとう 라고 했다")
    assert "ありがとう" not in out


def test_gap_score_thresholds():
    assert llm.calculate_gap_score(5, 0) == "extreme"
    assert llm.calculate_gap_score(5, 1) == "high"
    assert llm.calculate_gap_score(5, 2) == "medium"
    assert llm.calculate_gap_score(5, 3) == "none"
    assert llm.calculate_gap_score(0, 0) == "none"

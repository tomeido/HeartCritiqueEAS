"""PII 스캐너(services/pii.py) 회귀 테스트 — 순수 함수.

계약: 구조적 식별자(주민번호/전화/이메일/카드/긴 숫자열/계좌맥락)는 검출(승격 차단),
평범한 본문(게시물 번호·날짜·가격 포함)은 통과(과차단 억제)."""

import services.pii as pii


def test_detects_rrn():
    assert pii.has_pii("제 주민번호는 900101-1234567 입니다")
    assert pii.has_pii("주민 900101 2345678")  # 하이픈 없이 공백


def test_detects_mobile_and_landline():
    assert pii.has_pii("연락처 010-1234-5678 로 주세요")
    assert pii.has_pii("전화 01098765432")
    assert pii.has_pii("사무실 02-123-4567")


def test_detects_email_and_card():
    assert pii.has_pii("메일 hong.gildong@example.com 으로")
    assert pii.has_pii("카드 1234-5678-9012-3456 결제")


def test_detects_account_context_and_long_digits():
    assert pii.has_pii("국민은행 123-45-6789012 로 입금해주세요")
    assert pii.has_pii("계좌 110 234 567890")
    assert pii.has_pii("12345678901")  # 11자리 맨숫자


def test_clean_text_passes():
    # 게시물 번호·날짜·가격·짧은 숫자는 PII 아님 → 통과해야(과차단 방지)
    assert not pii.has_pii("이 글은 게시물 12345 번이고 2026년 6월에 올라왔다")
    assert not pii.has_pii("한 누리꾼이 3만원을 기부했다는 사연. 조회수 9999")
    assert not pii.has_pii("어느 게시글에 따르면 한 시민이 자리를 양보했다고 한다")
    assert not pii.has_pii("")


def test_large_input_scans_linearly_and_detects_across_chunks():
    # 병적/대용량 입력에서도 청크 스캔으로 빠르게 완료(ReDoS 방지) + 청크 경계 PII 검출.
    # 이메일 이차 백트래킹 유발 패턴 80KB 도 행(hang) 없이 통과해야 한다.
    pathological = "a." * 40000 + "@"   # 매칭 미완성 — 백트래킹 유발
    r = pii.scan(pathological)
    assert isinstance(r["hit"], bool)   # 완료되면 성공(무한 루프/타임아웃 없음)

    # 청크 경계(4000자) 부근에 PII 를 두어도 검출되는지(overlap 보장)
    for off in (3990, 4000, 8050):
        text = "가" * off + " 연락처 010-9876-5432 입니다 " + "나" * 5000
        assert pii.has_pii(text), f"offset {off} 에서 PII 미검출"

    # 대용량 clean 본문은 통과(과차단 없음)
    assert not pii.has_pii("훈훈한 미담 이야기. " * 5000)


def test_scan_returns_kinds_and_masked_samples():
    r = pii.scan("전화 010-1234-5678, 메일 a@b.com")
    assert r["hit"] is True
    assert "mobile" in r["kinds"] and "email" in r["kinds"]
    # 샘플은 마스킹되어 원문 PII 가 그대로 노출되지 않는다
    joined = " ".join(r["samples"])
    assert "010-1234-5678" not in joined
    assert "*" in joined

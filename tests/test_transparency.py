"""투명성·검증(services/transparency.py, crypto.verify_bundle) 회귀 테스트.

핵심 계약:
  · verify_bundle: sign_dataset 출력은 valid, payload 변조·서명 훼손은 invalid,
    에이전트 키 일치 여부를 별도 플래그로 보고.
  · build_snapshot: 정책의 핵심 약속(hard-only 승격 트리거, 점수 미주입, raw 비공개)이
    항상 노출된다 — 스냅샷 구조가 깨지면 투명성 API 의 약속 대조가 무너진다.
  · _gateway_allowed: 허용된 게이트웨이 호스트만(https) — DB 변조로도 SSRF 불가.
"""

import services.crypto as crypto
from services.transparency import _gateway_allowed, build_snapshot


# ── verify_bundle ─────────────────────────────────────────────────────────────
def test_verify_bundle_roundtrip_valid():
    signed = crypto.sign_dataset({"story": {"body": "본문", "id": "x"}, "v": 1})
    res = crypto.verify_bundle(signed)
    assert res["valid"] is True
    assert res["reason"] is None


def test_verify_bundle_detects_payload_tamper():
    signed = crypto.sign_dataset({"story": {"body": "원래 본문"}})
    signed["payload"]["story"]["body"] = "몰래 바꾼 본문"
    res = crypto.verify_bundle(signed)
    assert res["valid"] is False
    assert res["reason"] == "signature_mismatch"


def test_verify_bundle_detects_signature_tamper():
    signed = crypto.sign_dataset({"a": 1})
    sig = bytearray(bytes.fromhex(signed["signature"]))
    sig[-1] ^= 0x01
    signed["signature"] = bytes(sig).hex()
    assert crypto.verify_bundle(signed)["valid"] is False


def test_verify_bundle_agent_key_match_flag():
    signed = crypto.sign_dataset({"a": 1})
    same = crypto.verify_bundle(signed, expected_pubkey_hex=signed["publicKey"])
    assert same["valid"] is True and same["matches_agent_key"] is True
    other = crypto.verify_bundle(signed, expected_pubkey_hex="04" + "ab" * 64)
    # 서명 자체는 유효하지만 '이 에이전트의 키'는 아니다 — 별도 플래그로 구분.
    assert other["valid"] is True and other["matches_agent_key"] is False


def test_verify_bundle_rejects_malformed():
    assert crypto.verify_bundle(None)["valid"] is False
    assert crypto.verify_bundle({})["valid"] is False
    assert crypto.verify_bundle(
        {"payload": {}, "signature": "zz", "publicKey": "not-hex"}
    )["valid"] is False


# ── 게이트웨이 허용 목록(SSRF 방어) ───────────────────────────────────────────
def test_gateway_allowlist():
    assert _gateway_allowed("https://gateway.irys.xyz/abc123") is True
    assert _gateway_allowed("https://devnet.irys.xyz/abc123") is True
    assert _gateway_allowed("https://arweave.net/abc123") is True
    assert _gateway_allowed("https://abc123.arweave.net/") is True   # 샌드박스 서브도메인
    # 차단: 임의 호스트·http·내부망·접미사 위장
    assert _gateway_allowed("https://evil.com/abc") is False
    assert _gateway_allowed("http://gateway.irys.xyz/abc") is False
    assert _gateway_allowed("https://gateway.irys.xyz.evil.com/abc") is False
    assert _gateway_allowed("https://127.0.0.1/abc") is False
    assert _gateway_allowed("") is False


# ── build_snapshot 핵심 약속 노출 ─────────────────────────────────────────────
def test_snapshot_exposes_core_promises():
    snap = build_snapshot()
    # hard-only 승격 트리거 약속
    assert "404/410" in snap["promotion_gates"]["trigger"]
    # 점수는 결정 미주입 약속
    assert any("박제 결정" in s or "점수" in s for s in snap["data_policy"]["never_done"])
    # raw 본문 비공개 약속
    assert any("raw" in s or "원본 본문" in s for s in snap["data_policy"]["private"])
    # 점수 가중치 공개(튜닝 투명성)
    assert snap["scoring"]["volatility_weights"]["pressure"] >= 1
    assert snap["scoring"]["value_weights"]["public_interest"] >= 1
    # 추적 불가 도메인 정직 공개
    assert "fmkorea.com" in snap["known_limits"]["untrackable_domains"]
    # 임계값 정책 수치 노출
    assert snap["threshold_policy"]["default"] >= 1
    # 서명 알고리즘 명시(검증 재현 가능성)
    assert "secp256k1" in snap["archive"]["signature_algorithm"]
    # PII 게이트 종류 공개
    assert "rrn" in snap["promotion_gates"]["pii_gate"]

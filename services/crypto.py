"""에이전트 EC 키로 데이터셋에 서명. Arweave 박제 전 무결성 증명용."""

import json
import os

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

_cached_key: ec.EllipticCurvePrivateKey | None = None

# secp256k1 군위수 n. OpenSSL 은 ECDSA 서명을 high-S 로도 내는데(~50%), 프론트 검증기
# (@noble/curves)는 기본 lowS:true 라 high-S 를 '변조됨'으로 거부한다. 서명 시 s 를 low-S
# (s ≤ n/2)로 정규화해 양쪽 스택의 검증을 일치시킨다(서명 유효성은 불변).
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _to_low_s(der_sig: bytes) -> bytes:
    """DER 서명을 canonical low-S 형태로 정규화."""
    r, s = decode_dss_signature(der_sig)
    if s > _SECP256K1_N // 2:
        s = _SECP256K1_N - s
    return encode_dss_signature(r, s)


def has_configured_key() -> bool:
    """AGENT_PRIVATE_KEY 가 설정되어 있는지. False 면 ephemeral 키라 재시작마다 바뀌어
    과거 박제물 검증이 깨지므로, 박제 자체를 건너뛰어야 한다."""
    return bool(os.environ.get("AGENT_PRIVATE_KEY", "").strip())


def _load_private_key() -> ec.EllipticCurvePrivateKey:
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    hex_key = os.environ.get("AGENT_PRIVATE_KEY", "").strip().removeprefix("0x")
    if hex_key:
        key_int = int(hex_key, 16)
        _cached_key = ec.derive_private_key(key_int, ec.SECP256K1(), default_backend())
    else:
        # 개발용: 매 재시작마다 새 키 생성 (Arweave 업로드 비활성화 상태에서만 사용)
        _cached_key = ec.generate_private_key(ec.SECP256K1(), default_backend())

    return _cached_key


def sign_dataset(data: dict) -> dict:
    """데이터셋에 ECDSA 서명을 붙여 반환."""
    private_key = _load_private_key()
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    signature = _to_low_s(private_key.sign(canonical, ec.ECDSA(hashes.SHA256())))
    pub_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "payload": data,
        "signature": signature.hex(),
        "publicKey": pub_bytes.hex(),
        "algorithm": "ECDSA-secp256k1-SHA256",
    }


def get_public_key_hex() -> str:
    key = _load_private_key()
    return key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    ).hex()


def verify_bundle(bundle: dict, expected_pubkey_hex: str | None = None) -> dict:
    """박제 번들(sign_dataset 출력 형식)의 서명을 검증 — '우릴 믿지 말고 검증하라'용.

    검증 로직은 서버 비밀 없이 누구나 재현 가능하다(docs/TRANSPARENCY.md 에 공개):
      canonical = JSON(payload, sort_keys=True, ensure_ascii=False) → UTF-8
      ECDSA-secp256k1-SHA256 로 signature 를 publicKey 에 대해 검증.

    반환: {valid, reason, matches_agent_key}
      · valid            : 서명이 payload·publicKey 와 수학적으로 일치하는가
      · matches_agent_key: 번들의 publicKey 가 expected_pubkey_hex(현재 에이전트 키)와
                           같은가 (None 이면 비교 생략). 서명이 유효해도 다른 키로
                           서명된 번들은 이 에이전트의 박제물이 아니다.
    """
    if not isinstance(bundle, dict):
        return {"valid": False, "reason": "bundle_not_object", "matches_agent_key": None}
    payload = bundle.get("payload")
    sig_hex = bundle.get("signature")
    pub_hex = bundle.get("publicKey")
    if payload is None or not isinstance(sig_hex, str) or not isinstance(pub_hex, str):
        return {"valid": False, "reason": "missing_fields", "matches_agent_key": None}
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), bytes.fromhex(pub_hex))
    except Exception:
        return {"valid": False, "reason": "bad_public_key", "matches_agent_key": None}
    matches = (pub_hex.lower() == expected_pubkey_hex.lower()) if expected_pubkey_hex else None
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    try:
        pub.verify(bytes.fromhex(sig_hex), canonical, ec.ECDSA(hashes.SHA256()))
    except Exception:
        return {"valid": False, "reason": "signature_mismatch", "matches_agent_key": matches}
    return {"valid": True, "reason": None, "matches_agent_key": matches}

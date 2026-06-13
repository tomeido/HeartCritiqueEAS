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

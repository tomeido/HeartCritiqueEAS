"""에이전트 EC 키로 데이터셋에 서명. Arweave 박제 전 무결성 증명용."""

import json
import os

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

_cached_key: ec.EllipticCurvePrivateKey | None = None


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
    signature = private_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
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

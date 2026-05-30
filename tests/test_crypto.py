"""서명 라운드트립 테스트.

sign_dataset 의 canonical JSON(sort_keys=True, ensure_ascii=False)이 바뀌면 브라우저
검증(static/index.html 의 pythonCanonicalJSON)과 어긋나 모든 박제물이 검증 실패한다.
서명→검증이 성립하는지, canonical 직렬화가 키 정렬/비ASCII 보존을 지키는지 확인.
"""

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

import services.crypto as crypto


def test_sign_verify_roundtrip():
    data = {"b": 2, "a": 1, "nested": {"z": [3, 2, 1]}, "한글": "값"}
    signed = crypto.sign_dataset(data)

    assert signed["algorithm"] == "ECDSA-secp256k1-SHA256"
    assert signed["payload"] == data

    pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256K1(), bytes.fromhex(signed["publicKey"])
    )
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    # 유효하지 않으면 InvalidSignature 예외가 발생한다.
    pub.verify(bytes.fromhex(signed["signature"]), canonical, ec.ECDSA(hashes.SHA256()))


def test_canonical_preserves_non_ascii_and_sorts_keys():
    # 박제물 본문은 한국어이므로 ensure_ascii=False 가 반드시 유지되어야 한다.
    canonical = json.dumps({"한글": "값", "a": 1}, sort_keys=True, ensure_ascii=False)
    assert "한글" in canonical  # 이스케이프되지 않음
    assert canonical.index('"a"') < canonical.index('"한글"')  # 키 정렬

"""서명 라운드트립 테스트.

sign_dataset 의 canonical JSON(sort_keys=True, ensure_ascii=False)이 바뀌면 브라우저
검증(static/index.html 의 pythonCanonicalJSON)과 어긋나 모든 박제물이 검증 실패한다.
서명→검증이 성립하는지, canonical 직렬화가 키 정렬/비ASCII 보존을 지키는지 확인.
"""

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

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


def test_signatures_are_canonical_low_s():
    """서명은 항상 low-S(s <= n/2) 여야 한다. OpenSSL 은 high-S 도 내는데, 프론트
    검증기(@noble/curves)는 기본 lowS:true 라 high-S 를 '변조됨'으로 거부 → 박제물
    절반이 거짓 검증 실패. Python→Python verify 는 high-S 도 통과하므로 이 불변식을
    별도로 못 박는다(회귀 방지)."""
    n = crypto._SECP256K1_N
    for i in range(64):  # high-S 확률 ~50% → 64회면 정규화 누락을 사실상 확실히 포착
        signed = crypto.sign_dataset({"i": i, "한글": "값"})
        _r, s = decode_dss_signature(bytes.fromhex(signed["signature"]))
        assert s <= n // 2, f"high-S 서명 누출(i={i}): s={s}"


def test_canonical_preserves_non_ascii_and_sorts_keys():
    # 박제물 본문은 한국어이므로 ensure_ascii=False 가 반드시 유지되어야 한다.
    canonical = json.dumps({"한글": "값", "a": 1}, sort_keys=True, ensure_ascii=False)
    assert "한글" in canonical  # 이스케이프되지 않음
    assert canonical.index('"a"') < canonical.index('"한글"')  # 키 정렬

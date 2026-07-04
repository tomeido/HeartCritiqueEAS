"""
박제 비용·지갑 잔액·후원 주소 — uploader(Irys SDK)의 /wallet 을 프록시 + TTL 캐시.

프론트가 주기적으로 폴링하므로 캐시로 Irys/RPC 부하를 흡수한다. network 무관(devnet/mainnet
자동) — uploader 가 IRYS_NETWORK 에 맞춰 응답하므로, mainnet 전환 시 자동으로 mainnet 가격·
잔액·주소가 노출된다. AGENT_PRIVATE_KEY 미설정·uploader 다운 시 graceful 하게 error 필드 반환.
"""

import os
import time

import httpx

import logging
logger = logging.getLogger(__name__)

UPLOADER_URL = os.environ.get("UPLOADER_URL", "http://uploader:3000").rstrip("/")
WALLET_CACHE_TTL = int(os.environ.get("WALLET_CACHE_TTL", "60"))
WALLET_TIMEOUT = int(os.environ.get("WALLET_TIMEOUT", "15"))

_cache: dict = {"value": None, "expires_at": 0.0}


async def get_wallet_info() -> dict:
    """uploader /wallet 응답(비용·잔액·후원주소)을 캐시와 함께 반환."""
    now = time.time()
    if _cache["value"] is not None and now < _cache["expires_at"]:
        return _cache["value"]
    try:
        async with httpx.AsyncClient(timeout=WALLET_TIMEOUT) as client:
            r = await client.get(f"{UPLOADER_URL}/wallet")
            r.raise_for_status()
            data = r.json()
        _cache["value"] = data
        _cache["expires_at"] = now + WALLET_CACHE_TTL
        return data
    except Exception as e:
        logger.warning(f"[wallet] uploader 조회 실패: {e}")
        # 일시 실패면 직전(만료된) 캐시라도 반환 — 잔액이 잠깐 0/공백으로 깜빡이는 것 방지.
        if _cache["value"] is not None:
            return {**_cache["value"], "stale": True}
        return {
            "network": os.environ.get("IRYS_NETWORK", "devnet"),
            "error": "지갑 정보를 일시적으로 조회할 수 없습니다",
            "donation_address": None,
        }

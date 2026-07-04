"""
투명성·신뢰성 서비스 (Transparency & Verifiability).

원칙: "우릴 믿지 말고 검증하라(Don't trust, verify)". 이 아카이브의 신뢰는
운영자의 선의가 아니라 (a) 공개된 방법론, (b) 재현 가능한 서명 검증에서 나와야 한다.

이 모듈이 제공하는 것:
  1. build_snapshot(): 지금 이 순간 서버가 실제로 돌리고 있는 정책·가중치·게이트를
     실시간으로 노출한다(/api/transparency). 문서(docs/TRANSPARENCY.md)가 '약속'이라면
     이 스냅샷은 '현재 상태'다 — 둘을 대조하면 약속 위반이 드러난다.
  2. verify_story_archive(): 박제된 스토리의 Arweave 번들을 게이트웨이에서 직접 받아
     ECDSA 서명을 검증하고 현재 DB 본문과 대조한다(/api/verify/{id}). 같은 검증을
     서버 없이 재현하는 절차는 docs/TRANSPARENCY.md 에 공개한다.

노출하지 않는 것: 비밀키·서비스롤 키·captured_posts 원본 본문(비공개 원칙 유지).
"""

import asyncio
import hashlib
import json
import os
import time
import uuid
from urllib.parse import urlparse

import httpx

from services.crypto import get_public_key_hex, has_configured_key, verify_bundle
from services.db import get_db

import logging
logger = logging.getLogger(__name__)

# 검증 결과 프로세스 캐시(TTL): 무인증 엔드포인트가 매 호출 외부 GET 을 유발하지 않게.
_VERIFY_TTL = int(os.environ.get("VERIFY_CACHE_TTL", "600"))
_VERIFY_CACHE_MAX = 500   # 장기 가동 시 무한 성장 방지(초과 시 만료분→오래된 순 정리)
_verify_cache: dict = {}   # story_id -> {"expires_at": float, "result": dict}


def _cache_put(story_id: str, result: dict) -> None:
    now = time.time()
    if len(_verify_cache) >= _VERIFY_CACHE_MAX:
        for k in [k for k, v in _verify_cache.items() if v["expires_at"] <= now]:
            _verify_cache.pop(k, None)
        while len(_verify_cache) >= _VERIFY_CACHE_MAX:
            _verify_cache.pop(min(_verify_cache, key=lambda k: _verify_cache[k]["expires_at"]))
    _verify_cache[story_id] = {"expires_at": now + _VERIFY_TTL, "result": result}

# 박제물 게이트웨이 허용 호스트(SSRF 방어): DB 의 arweave_url 이 변조돼도 임의 호스트로
# 서버 GET 을 유발할 수 없다. 서브도메인 허용(<txid>.arweave.net 샌드박스 리다이렉트).
_ALLOWED_GATEWAY_SUFFIXES = ("gateway.irys.xyz", "devnet.irys.xyz", "arweave.net")
_FETCH_TIMEOUT = 30
_MAX_BUNDLE_BYTES = 5_000_000


def _gateway_allowed(url: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    if not host or urlparse(url).scheme != "https":
        return False
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_GATEWAY_SUFFIXES)


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ── 1) 방법론 실시간 스냅샷 ───────────────────────────────────────────────────
def build_snapshot() -> dict:
    """서버가 지금 실제로 적용 중인 정책·가중치·게이트의 스냅샷(공개 API 용).
    import 는 함수 안에서 — 앱 기동 순서·순환 의존을 피하고 항상 현재 값을 읽는다."""
    from services import pii, proxyfetch, value, volatility, wayback
    from services.collector import COLLECTOR_ENABLED, COMMUNITY_FEEDS
    from services.dedup import DEDUP_ENABLED
    from services.hunter import HUNTER_ENABLED
    from services.llm import MIN_SOURCE_CONTENT, RELEVANCE_GATE_ENABLED
    from services.promoter import (
        PROMOTER_AUTO_CRITIQUE,
        PROMOTER_ENABLED,
        PROMOTER_MIN_VOLATILITY,
    )
    from services.threshold import (
        DEFAULT_THRESHOLD,
        DYNAMIC_THRESHOLD_ENABLED,
        MAX_BASE_THRESHOLD,
        MIN_BASE_THRESHOLD,
        VOTERS_PER_VOTE,
        get_dynamic_base_threshold,
    )
    from services.tracker import (
        BOT_BLOCK_CODES,
        TRACKER_ENABLED,
        UNTRACKABLE_DOMAINS,
    )

    base = get_dynamic_base_threshold()

    return {
        "mission": (
            "AI 사냥개가 실시간 화제글을 발견·캡처하고, 삭제를 감시하며, 인간의 투표로 "
            "Arweave 에 영구 박제하는 타임캡슐 아카이브. 검색이 구조적으로 놓치는 "
            "'이미 삭제된 글'은 살아있을 때 잡아(collector) 죽은 뒤 공개(promoter)한다."
        ),
        "data_policy": {
            "public": [
                "LLM 익명 재작성 스토리(stories.body)와 출처 URL·추적 상태",
                "박제 번들(스토리+투표 로그+출처 생존 증거, 서명 포함) — Arweave 공개",
                "출처 본문의 sha256 지문(원문 아닌 지문만)",
            ],
            "private": [
                "captured_posts 원본 본문(raw) — 절대 공개하지 않음(PII·명예훼손 보호)",
                "수동 검토 큐(pending_review/blocked_pii)의 내용",
            ],
            "never_done": [
                "원본 글의 무단 전문 공개(익명 재작성만 공개)",
                "soft 삭제 신호(본문 패턴·리다이렉트)에 의한 자동·영구 박제",
                "가치/삭제확률 점수에 의한 박제 결정(점수는 우선순위·표시 전용)",
            ],
        },
        "threshold_policy": {
            "description": (
                "박제는 인간 투표가 임계값에 달해야 실행. 임계값 인하는 '목격한' hard 신호"
                "(살아있는 원본을 직접 확인한 뒤의 HTTP 404/410 삭제·403 차단)만 반영 — "
                "본문 패턴 기반 soft 신호와 첫 접촉부터 죽어 있던 링크(오탐 구분 불가)는 "
                "배지 표시용일 뿐 되돌릴 수 없는 박제를 앞당기지 않는다."
            ),
            "default": DEFAULT_THRESHOLD,
            "dynamic": DYNAMIC_THRESHOLD_ENABLED,
            "current_base": base["threshold"],
            "active_voters": base["active_voters"],
            "min": MIN_BASE_THRESHOLD,
            "max": MAX_BASE_THRESHOLD,
            "voters_per_vote": VOTERS_PER_VOTE,
        },
        "modules": {
            "tracker": {"enabled": TRACKER_ENABLED,
                        "role": "출처 URL 삭제 추적(기준선 대비 변화 판정)"},
            "hunter": {"enabled": HUNTER_ENABLED, "role": "주기적 스토리 자동 생성"},
            "collector": {"enabled": COLLECTOR_ENABLED, "feeds": len(COMMUNITY_FEEDS),
                          "role": "화제글 선제 캡처(비공개 보관) + 삭제 감시"},
            "promoter": {"enabled": PROMOTER_ENABLED,
                         "auto_critique": PROMOTER_AUTO_CRITIQUE,
                         "min_volatility": PROMOTER_MIN_VOLATILITY,
                         "role": "hard 삭제 확정 캡처글의 익명 재작성 공개 승격"},
            "wayback": wayback.get_status() | {
                "role": "IA Save Page Now 위임 스냅샷(중립 제3자 증거)"},
            "proxy_observation": proxyfetch.get_status() | {
                "role": "봇차단(추적 불가) 출처의 프록시 2차 관측 — soft 신호 전용"},
        },
        "generation_gates": {
            "note": (
                "검색→생성 단계의 후보 선별 게이트(콘텐츠 질). 박제 결정·투표 임계값과는 "
                "무관하다 — 무엇을 '쓸지'만 거르고, 무엇을 '박제할지'는 인간 투표가 정한다."
            ),
            "relevance_gate": RELEVANCE_GATE_ENABLED,
            "min_source_content_chars": MIN_SOURCE_CONTENT,
            "category_fit": {
                "kindness": "비미담 부정필터(사기·괴담·돈분쟁 컷 + 선행 신호 화이트리스트)",
                "critique": ("기업/노동/소비자/제도 부조리 신호 필수(positive gate) — "
                             "스포츠·게임·연예·진영정치 글의 '비위 둔갑' 차단, 3차 폴백에서도 유지"),
            },
            "dedup": {
                "enabled": DEDUP_ENABLED,
                "role": (
                    "이미 스토리로 만든 출처 URL(citation_checks 기지 목록)을 검색 후보에서 "
                    "제외 — 같은 글의 근사 중복 스토리(투표 분산·이중 박제) 방지"
                ),
            },
        },
        "promotion_gates": {
            "trigger": "hard 삭제(HTTP 404/410)만 자동 승격 — soft 는 절대 자동화하지 않음",
            "pii_gate": [k for k, _ in pii._DETECTORS],
            "critique_policy": ("자동" if PROMOTER_AUTO_CRITIQUE
                                else "수동 검토(pending_review) — 명예훼손 노출 최소화"),
            "anonymization": "원본 raw 본문 비공개, LLM 익명·헤지 재작성만 공개 + 출력 PII 재스캔",
        },
        "scoring": {
            "note": (
                "두 점수 모두 결정적(같은 입력→같은 출력)이며 캡처/승격 우선순위와 UI "
                "배지에만 쓴다. 박제 여부·임계값에는 주입하지 않는다. 기준 연구: "
                "docs/ARCHIVAL_CRITERIA.md"
            ),
            "volatility_weights": {
                "entity_accusation": volatility.W_ENTITY_ACCUSATION,
                "accusation_only": volatility.W_ACCUSATION_ONLY,
                "entity_only": volatility.W_ENTITY_ONLY,
                "whistleblower": volatility.W_WHISTLEBLOWER,
                "pressure": volatility.W_PRESSURE,
                "legal": volatility.W_LEGAL,
                "source_high": volatility.W_SOURCE_HIGH,
                "source_medium": volatility.W_SOURCE_MEDIUM,
                "fresh_24h": volatility.W_FRESH_24H,
                "kindness_penalty": volatility.KINDNESS_BASE_PENALTY,
            },
            "value_weights": {
                "first_person": value.W_FIRST_PERSON,
                "evidence": value.W_EVIDENCE,
                "public_interest": value.W_PUBLIC_INTEREST,
                "accusation_only": value.W_ACCUSATION_ONLY,
                "whistleblower": value.W_WHISTLEBLOWER,
                "consumer": value.W_CONSUMER,
                "kindness": value.W_KINDNESS,
                "detail_long": value.W_DETAIL_LONG,
                "detail_very": value.W_DETAIL_VERY,
                "penalty_question": value.P_QUESTION,
                "penalty_news_repost": value.P_NEWS_REPOST,
                "penalty_short": value.P_SHORT,
                "penalty_ad": value.P_AD,
            },
        },
        "known_limits": {
            "untrackable_domains": sorted(UNTRACKABLE_DOMAINS),
            "bot_block_codes": list(BOT_BLOCK_CODES),
            "note": (
                "안티봇 사이트는 직접 추적이 불가하며 Wayback 위임도 동일하게 막힌다. "
                "프록시 관측(옵트인)이 켜져 있으면 soft 신호로만 보강한다. 프록시 사용 시 "
                "출처 URL 이 프록시 사업자에 전달되는 트레이드오프가 있다."
            ),
        },
        "archive": {
            "network": os.environ.get("IRYS_NETWORK", "devnet"),
            "permanence": (
                "mainnet=영구" if os.environ.get("IRYS_NETWORK", "devnet") == "mainnet"
                else "devnet=약 60일 후 삭제(테스트용, 영구 아님)"
            ),
            "signature_algorithm": "ECDSA-secp256k1-SHA256 (low-S canonical)",
            "agent_public_key": (get_public_key_hex() if has_configured_key() else None),
            "ephemeral_signing_key": not has_configured_key(),
            "verify_endpoint": "/api/verify/{story_id}",
            "manual_verification": "docs/TRANSPARENCY.md 의 재현 절차 참고",
        },
        "docs": ["docs/TRANSPARENCY.md", "docs/ARCHIVAL_CRITERIA.md"],
    }


# ── 2) 박제물 서명 검증 ───────────────────────────────────────────────────────
async def _fetch_bundle(url: str) -> tuple[dict | None, str | None]:
    """게이트웨이에서 박제 번들 JSON 을 GET. (bundle, error). 허용 호스트만."""
    if not _gateway_allowed(url):
        return None, "gateway_not_allowed"
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            # 스트리밍 + 상한 도달 시 즉시 중단: 변조된 arweave_url 이 초대형 tx 를
            # 가리켜도 상한 이상을 메모리에 올리지 않는다(사후 len 검사는 무의미).
            async with client.stream("GET", url) as resp:
                # 리다이렉트 최종 목적지도 허용 호스트여야 한다(샌드박스 서브도메인 포함).
                if not _gateway_allowed(str(resp.url)):
                    return None, "redirected_outside_gateway"
                if resp.status_code == 404:
                    return None, "gateway_404 (devnet 은 약 60일 후 만료될 수 있음)"
                if resp.status_code != 200:
                    return None, f"gateway_http_{resp.status_code}"
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_BUNDLE_BYTES:
                        return None, "bundle_too_large"
                    chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8")), None
    except json.JSONDecodeError:
        return None, "bundle_not_json"
    except Exception as e:
        return None, f"net:{type(e).__name__}"


async def verify_story_archive(story_id: str) -> dict:
    """박제된 스토리의 Arweave 번들을 받아 서명·본문 일치를 검증.

    반환(항상 dict — 라우터가 HTTP 코드로 매핑):
      ok, reason, story_id, arweave_url, network,
      signature_valid, matches_agent_key, body_matches_db,
      bundle_body_sha256, db_body_sha256, archived_at, algorithm
    """
    try:
        uuid.UUID(story_id)
    except (ValueError, AttributeError, TypeError):
        return {"ok": False, "reason": "not_found"}

    cached = _verify_cache.get(story_id)
    if cached and time.time() < cached["expires_at"]:
        return cached["result"]

    db = get_db()
    try:
        resp = await asyncio.to_thread(
            lambda: db.table("stories")
            .select("id,body,arweave_url,arweave_tx_id,archived_at")
            .eq("id", story_id).limit(1).execute()
        )
    except Exception as e:
        logger.warning(f"[transparency] story 조회 실패 {story_id}: {e}")
        return {"ok": False, "reason": "db_error"}
    if not resp.data:
        return {"ok": False, "reason": "not_found"}
    story = resp.data[0]

    url = story.get("arweave_url")
    tx = story.get("arweave_tx_id")
    if not url or not tx or tx == "__pending__":
        return {"ok": False, "reason": "not_archived"}

    bundle, err = await _fetch_bundle(url)
    if bundle is None:
        return {"ok": False, "reason": err, "arweave_url": url}

    expected = get_public_key_hex() if has_configured_key() else None
    sig = verify_bundle(bundle, expected)

    bundle_body = (((bundle.get("payload") or {}).get("story")) or {}).get("body") or ""
    db_body = story.get("body") or ""
    # 헤드라인 ok = 서명이 수학적으로 유효 *그리고* 이 에이전트의 키로 서명됨.
    # 서명만 유효한 '남의 키' 번들(변조된 arweave_url 이 가리키는 위조 번들)에
    # ok=true 를 주면 검증 엔드포인트 자체가 오도된다. 키 미설정(None)은 비교 생략.
    foreign_key = sig["matches_agent_key"] is False
    result = {
        "ok": bool(sig["valid"]) and not foreign_key,
        "reason": sig["reason"] or ("foreign_signing_key" if foreign_key else None),
        "story_id": story_id,
        "arweave_url": url,
        "network": os.environ.get("IRYS_NETWORK", "devnet"),
        "signature_valid": sig["valid"],
        "matches_agent_key": sig["matches_agent_key"],
        # 박제 이후 DB 본문이 몰래 바뀌지 않았는지(양방향 무결성 감사).
        "body_matches_db": bundle_body == db_body,
        "bundle_body_sha256": _sha256(bundle_body),
        "db_body_sha256": _sha256(db_body),
        "archived_at": story.get("archived_at"),
        "algorithm": bundle.get("algorithm"),
    }
    _cache_put(story_id, result)
    return result

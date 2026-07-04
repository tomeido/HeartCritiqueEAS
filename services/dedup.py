"""생성 중복 방지 게이트 — 이미 스토리로 만든 출처 URL 로 또 만들지 않는다.

커뮤니티 인기글은 수일간 검색 상위에 머물러, hunter 가 주기마다 같은 글을 다시 골라
근사 중복 스토리를 쌓는 구조적 문제가 있다(투표 분산 · 피드 오염 · 같은 글 이중 박제).
스토리 생성 시 register_citations 가 citation_checks 에 기록한 출처 URL 을 검색 후보에서
선제 제외해, 모델이 아직 다루지 않은 글만 고르게 한다.

원칙:
  · 후보 필터링 전용 — 박제 결정·투표 임계값에는 절대 관여하지 않는다.
  · 가용성 우선 — DB 조회 실패 시 필터 없이 통과(생성이 dedup 때문에 죽지 않는다).
  · 정확 일치만 — URL 정규화로 다른 글을 같은 글로 오인하는 과필터보다, 표기가 다른
    같은 글을 놓치는 미필터가 낫다(같은 게시판 글은 query 파라미터 하나 차이가 흔하다).
    후보 URL 은 동일 파이프라인(Tavily/grounding)이 만들므로 실전 중복은 대부분
    문자열까지 동일하다.
"""

import logging
import os

from services.db import get_db

logger = logging.getLogger(__name__)

DEDUP_ENABLED = os.environ.get("STORY_DEDUP_ENABLED", "true").lower() != "false"


def known_urls(urls: list) -> set:
    """citation_checks 에 이미 등록된(=이미 스토리화된) URL 부분집합을 돌려준다.
    조회 실패는 빈 집합(=필터 없음)으로 강등 — 중복 방지는 best-effort 다."""
    urls = [u for u in urls if isinstance(u, str) and u]
    if not urls:
        return set()
    try:
        resp = (
            get_db().table("citation_checks")
            .select("url").in_("url", urls).execute()
        )
        return {r["url"] for r in (resp.data or []) if r.get("url")}
    except Exception as e:
        logger.warning(f"[dedup] known_urls 조회 실패 — 필터 없이 통과: {e}")
        return set()


def filter_known_sources(results: list) -> tuple:
    """검색 결과({title,url,content})에서 기지(旣知) 출처를 제외.
    반환: (남은 결과, 제외 건수). 비활성/조회실패면 그대로 통과."""
    if not DEDUP_ENABLED or not results:
        return results, 0
    known = known_urls([r.get("url") for r in results])
    if not known:
        return results, 0
    fresh = [r for r in results if r.get("url") not in known]
    return fresh, len(results) - len(fresh)


def all_known(citations: list) -> bool:
    """citations({title,uri})가 전부 기지 출처면 True(중복 스토리 신호).
    grounding 경로(gemini)처럼 생성 후에야 출처를 아는 경우의 사후 게이트."""
    if not DEDUP_ENABLED or not citations:
        return False
    uris = [c.get("uri") for c in citations if isinstance(c, dict)]
    uris = [u for u in uris if isinstance(u, str) and u]
    if not uris:
        return False
    return set(uris) <= known_urls(uris)

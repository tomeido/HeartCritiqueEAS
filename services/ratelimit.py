"""
경량 인메모리 레이트리미터 (외부 의존성 없음).

스토리 생성(/api/story, JSON-RPC message/send)은 호출마다 유료 LLM·Tavily·DB 쓰기를
유발하므로 무인증 무제한 노출 시 비용 폭탄·DoS·쓰레기 적재로 직결된다. per-IP +
전역 슬라이딩 윈도우로 상한을 건다.

홈서버 단일 프로세스 전제(백그라운드 루프/캐시와 동일). 멀티워커면 워커별로 카운트가
갈리므로 그때는 외부 저장소(예: Redis)로 옮겨야 한다.
"""

import os
import threading
import time
from collections import deque

from fastapi import Request


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class SlidingWindowLimiter:
    """key 별 윈도우 내 이벤트 수 상한. 스레드 안전."""

    def __init__(self, max_events: int, window_sec: int):
        self.max = max_events
        self.window = window_sec
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, int]:
        """이벤트 1건 기록 시도. (allowed, retry_after_sec) 반환.
        allowed=False 면 윈도우 한도 초과."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            dq = self._events.get(key)
            if dq is None:
                dq = deque()
                self._events[key] = dq
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max:
                retry = int(dq[0] + self.window - now) + 1
                return False, max(1, retry)
            dq.append(now)
            # 메모리 누수 방지: 가끔 빈 버킷 정리
            if len(self._events) > 4096:
                self._prune(cutoff)
            return True, 0

    def _prune(self, cutoff: float) -> None:
        for k in list(self._events.keys()):
            dq = self._events[k]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                del self._events[k]


# ── 스토리 생성 전용 리미터 ──────────────────────────────────────────────────
RATELIMIT_ENABLED = os.environ.get("STORY_RATELIMIT_ENABLED", "true").lower() != "false"
_WINDOW = _env_int("STORY_RATELIMIT_WINDOW_SEC", 600)        # 10분 윈도우
_PER_IP = _env_int("STORY_RATELIMIT_PER_IP", 5)              # IP당 10분 5회
_GLOBAL = _env_int("STORY_RATELIMIT_GLOBAL", 20)             # 전역 10분 20회

_ip_limiter = SlidingWindowLimiter(_PER_IP, _WINDOW)
_global_limiter = SlidingWindowLimiter(_GLOBAL, _WINDOW)


def client_ip(request: Request) -> str:
    """리버스 프록시 뒤 실제 클라이언트 IP 추정."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


def check_story_ratelimit(request: Request) -> tuple[bool, int, str]:
    """스토리 생성 레이트리밋 검사. (allowed, retry_after, reason) 반환."""
    if not RATELIMIT_ENABLED:
        return True, 0, ""
    ip = client_ip(request)
    ok_ip, retry_ip = _ip_limiter.hit(f"story:{ip}")
    if not ok_ip:
        return False, retry_ip, f"IP당 생성 한도 초과 ({_PER_IP}회 / {_WINDOW // 60}분)"
    ok_g, retry_g = _global_limiter.hit("story:__global__")
    if not ok_g:
        return False, retry_g, f"전역 생성 한도 초과 ({_GLOBAL}회 / {_WINDOW // 60}분)"
    return True, 0, ""

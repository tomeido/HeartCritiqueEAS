import os

import httpx
from supabase import create_client, Client

_client: Client | None = None

# postgrest 의 기본 httpx 세션은 http2=True 라, Supabase(Cloudflare)가 유휴 keepalive
# 연결에 GOAWAY 를 보내면 다음 요청이 RemoteProtocolError(ConnectionTerminated)로 죽었다.
# 증상: 첫 접속/유휴 후 /api/stories 가 500(목록 공백 → "새로고침해야 보임"), /api/stats 의
# count 가 0 으로 떨어져 60초 캐시에 굳어짐("박제 현황 이야기 0"). HTTP/1.1 + 끊김 재시도
# 트랜스포트로 세션을 교체해 끊긴 연결을 새 연결로 자동 복구한다.
# '응답을 받기 전에' 연결이 끊긴 류만 재시도한다(스트림 중단/서버 조기 종료). 연결 수립
# 실패(ConnectError/ConnectTimeout)는 아래 HTTPTransport(retries=2)가 모든 메서드에 대해
# 안전하게 담당 — 여기서 중복 재시도하면 아웃티지 때 재시도가 곱연산으로 폭증한다.
_RETRYABLE = (
    httpx.RemoteProtocolError,  # HTTP/2 GOAWAY(ConnectionTerminated) / 서버 조기 종료
    httpx.ReadError,
    httpx.WriteError,
)
# 응답 유실 후 중복 쓰기를 막기 위해 멱등 메서드만 재시도한다(읽기 경로가 이번 버그의 핵심).
_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})


class _RetryTransport(httpx.BaseTransport):
    """끊긴 keepalive 연결(RemoteProtocolError 등)을 새 연결로 재시도해 흡수한다."""

    def __init__(self, inner: httpx.HTTPTransport, retries: int = 3):
        self._inner = inner
        self._retries = retries

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        last: Exception | None = None
        attempts = self._retries if request.method in _IDEMPOTENT else 0
        for _ in range(attempts + 1):
            try:
                return self._inner.handle_request(request)
            except _RETRYABLE as e:
                last = e
                continue
        raise last  # type: ignore[misc]

    def close(self) -> None:
        self._inner.close()


def _make_resilient(session: httpx.Client) -> httpx.Client:
    """postgrest 의 http2=True 세션을 HTTP/1.1 + 재시도 트랜스포트로 교체.
    base_url/헤더(apikey·Authorization)/timeout 은 기존 세션에서 그대로 승계한다."""
    transport = _RetryTransport(
        httpx.HTTPTransport(
            retries=2,  # 연결 수립 단계 실패만 재시도(요청 미전송 → 모든 메서드 안전)
            http2=False,  # GOAWAY ConnectionTerminated 회피
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=20.0),
        )
    )
    new = httpx.Client(
        base_url=session.base_url,
        headers=session.headers,
        timeout=session.timeout,
        follow_redirects=True,
        transport=transport,
    )
    session.close()
    return new


def _harden(client: Client) -> Client:
    """postgrest 세션을 회복탄력적 세션으로 교체. 실패해도 기본 세션으로 계속 동작."""
    try:
        client.postgrest.session = _make_resilient(client.postgrest.session)
    except Exception:
        pass
    return client


def get_db() -> Client:
    global _client
    if _client is None:
        _client = _harden(create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        ))
    return _client


def get_anon_db() -> Client:
    """유저 토큰 검증용 anon 클라이언트. auth(GoTrue)만 쓰고 postgrest 쿼리는 하지
    않으므로 세션 교체가 불필요하다(게다가 매 호출 새 클라이언트라 idle GOAWAY 와도 무관)."""
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_ANON_KEY"],
    )

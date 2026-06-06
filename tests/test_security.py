"""SSRF URL 검증 + 레이트리미터 회귀 테스트.

literal IP / 차단 호스트명 / 스킴 거부는 DNS 없이 검증되므로 오프라인에서도 동작.
"""

import services.ratelimit as rl
import services.tracker as tracker


def test_ssrf_allows_public_ip():
    # literal public IP → getaddrinfo 가 그대로 반환(네트워크 불필요)
    # 반환은 (safe, reason, pinned_ip) — DNS rebinding 방어용으로 '연결할 IP'를 핀한다.
    ok, _why, ip = tracker._is_safe_url("http://8.8.8.8/path")
    assert ok is True
    # 검증한 그 IP 를 그대로 핀해 돌려줘야 호출부가 재resolve 없이 직접 연결한다.
    assert ip == "8.8.8.8"


def test_ssrf_blocks_internal_and_bad_schemes():
    blocked = [
        "http://127.0.0.1/",        # loopback
        "http://localhost/",        # 차단 호스트명
        "http://uploader:3000/up",  # 내부 서비스
        "http://10.0.0.1/",         # 사설
        "http://169.254.169.254/",  # 링크로컬(메타데이터)
        "http://[::1]/",            # IPv6 loopback
        "file:///etc/passwd",       # 스킴
        "ftp://x/",                 # 스킴
        "javascript:alert(1)",      # 스킴
    ]
    for url in blocked:
        ok, _why, ip = tracker._is_safe_url(url)
        assert ok is False, f"{url} should be blocked"
        assert ip == "", f"{url} blocked → no pinned IP"


def test_sliding_window_limiter():
    lim = rl.SlidingWindowLimiter(2, 600)
    assert lim.hit("k")[0] is True
    assert lim.hit("k")[0] is True
    ok, retry = lim.hit("k")
    assert ok is False and retry > 0
    # 다른 키는 독립
    assert lim.hit("other")[0] is True


def test_limiter_max_zero_does_not_crash():
    # max<=0(엔드포인트 차단 설정)에 빈 deque 인덱싱으로 500 나던 회귀 방지.
    lim = rl.SlidingWindowLimiter(0, 600)
    ok, retry = lim.hit("k")
    assert ok is False and retry == 600
    assert lim.peek("k") == (False, 600)


def test_limiter_peek_does_not_consume_slot():
    # peek 은 슬롯을 소비하지 않아야(전역 거절 시 per-IP 헛소비 방지) 한다.
    lim = rl.SlidingWindowLimiter(1, 600)
    assert lim.peek("k")[0] is True
    assert lim.peek("k")[0] is True   # peek 반복해도 여전히 허용
    assert lim.hit("k")[0] is True    # 첫 hit 만 슬롯 소비
    assert lim.hit("k")[0] is False   # 이제 한도 초과

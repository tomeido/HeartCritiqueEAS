"""SSRF URL 검증 + 레이트리미터 회귀 테스트.

literal IP / 차단 호스트명 / 스킴 거부는 DNS 없이 검증되므로 오프라인에서도 동작.
"""

import services.ratelimit as rl
import services.tracker as tracker


def test_ssrf_allows_public_ip():
    # literal public IP → getaddrinfo 가 그대로 반환(네트워크 불필요)
    ok, _ = tracker._is_safe_url("http://8.8.8.8/path")
    assert ok is True


def test_ssrf_blocks_internal_and_bad_schemes():
    blocked = [
        "http://127.0.0.1/",        # loopback
        "http://localhost/",        # 차단 호스트명
        "http://uploader:3000/up",  # 내부 서비스
        "http://10.0.0.1/",         # 사설
        "http://169.254.169.254/",  # 링크로컬(메타데이터)
        "file:///etc/passwd",       # 스킴
        "ftp://x/",                 # 스킴
        "javascript:alert(1)",      # 스킴
    ]
    for url in blocked:
        ok, why = tracker._is_safe_url(url)
        assert ok is False, f"{url} should be blocked"


def test_sliding_window_limiter():
    lim = rl.SlidingWindowLimiter(2, 600)
    assert lim.hit("k")[0] is True
    assert lim.hit("k")[0] is True
    ok, retry = lim.hit("k")
    assert ok is False and retry > 0
    # 다른 키는 독립
    assert lim.hit("other")[0] is True

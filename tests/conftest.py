"""테스트 공통 설정.

services/* 의 순수 로직을 외부 인프라 없이 테스트하기 위해, Docker 에만 설치되는
무거운 의존성(supabase, httpx)을 가벼운 stub 으로 대체한다. DB 를 실제로 건드리는
함수는 각 테스트에서 monkeypatch 로 격리한다.
"""

import os
import sys
import types
from pathlib import Path

# 레포 루트를 import 경로에 추가
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 외부 의존성 stub (실제 패키지가 없을 때만) ────────────────────────────────
if "httpx" not in sys.modules:
    try:
        import httpx  # noqa: F401
    except Exception:
        httpx = types.ModuleType("httpx")

        class _AsyncClient:
            def __init__(self, *a, **k):
                pass

        httpx.AsyncClient = _AsyncClient
        httpx.TimeoutException = type("TimeoutException", (Exception,), {})
        sys.modules["httpx"] = httpx

if "supabase" not in sys.modules:
    try:
        import supabase  # noqa: F401
    except Exception:
        supabase = types.ModuleType("supabase")
        supabase.create_client = lambda *a, **k: None
        supabase.Client = object
        sys.modules["supabase"] = supabase

os.environ.setdefault("SUPABASE_URL", "http://test")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("SUPABASE_ANON_KEY", "test")

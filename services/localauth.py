"""로컬 게스트 인증 — Supabase Auth(GoTrue) 없이 투표에 필요한 최소 신원.

Supabase 미설정(로컬 SQLite 모드)일 때 소셜 로그인을 대체한다:
  · POST /api/auth/guest → 서버가 HMAC 서명된 게스트 토큰 발급
  · 프론트가 localStorage 에 보관, 투표/내투표 요청의 Bearer 로 사용
  · routers/votes._verify_token → get_anon_db().auth.get_user(token)
    → services/localdb._LocalAuth → verify_guest_token (이 모듈)

토큰 형식: "guest.<uuid4>.<hmac_sha256_hex>"
  · uuid4 가 votes.user_id 로 그대로 쓰인다 (Supabase user id 와 같은 uuid 문자열 계약)
  · HMAC 비밀키는 LOCAL_DB_PATH 와 같은 데이터 디렉터리의 guest_secret 파일에 영속
    (재시작해도 기존 토큰 유효). GUEST_TOKEN_SECRET 환경변수로 오버라이드 가능.

한계(정직하게): 게스트 신원은 브라우저 단위라 OAuth 계정만큼 중복투표에 강하지 않다.
발급은 IP 레이트리밋으로 완화하고, UI 는 '게스트 참여' 라벨로 모드를 드러낸다.
"""

import hmac
import hashlib
import logging
import os
import secrets
import threading
import uuid

logger = logging.getLogger(__name__)

_PREFIX = "guest"
_secret: bytes | None = None
_secret_lock = threading.Lock()


def _secret_path() -> str:
    from services.localdb import DEFAULT_DB_PATH
    db_path = os.environ.get("LOCAL_DB_PATH", DEFAULT_DB_PATH)
    base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else "."
    return os.path.join(base, "guest_secret")


def _load_secret() -> bytes:
    global _secret
    if _secret is not None:
        return _secret
    with _secret_lock:
        if _secret is not None:
            return _secret
        env = os.environ.get("GUEST_TOKEN_SECRET", "").strip()
        if env:
            _secret = env.encode("utf-8")
            return _secret
        path = _secret_path()
        try:
            with open(path, "rb") as f:
                data = f.read().strip()
            if data:
                _secret = data
                return _secret
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = secrets.token_hex(32).encode("ascii")
        # 파일 영속 실패(읽기전용 FS 등)여도 프로세스 수명 동안은 동작하게 메모리 유지
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except FileExistsError:
            with open(path, "rb") as f:
                data = f.read().strip() or data
        except OSError as e:
            logger.warning(f"[localauth] guest_secret 영속 실패({e}) — "
                           "재시작 시 기존 게스트 토큰이 무효화됩니다")
        _secret = data
        return _secret


def _sign(user_id: str) -> str:
    return hmac.new(_load_secret(), user_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def issue_guest_token() -> tuple[str, str]:
    """새 게스트 신원 발급 → (token, user_id)."""
    user_id = str(uuid.uuid4())
    return f"{_PREFIX}.{user_id}.{_sign(user_id)}", user_id


def verify_guest_token(token: str) -> str | None:
    """토큰 검증 → user_id 또는 None(무효)."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    user_id, sig = parts[1], parts[2]
    try:
        uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return None
    if not hmac.compare_digest(_sign(user_id), sig):
        return None
    return user_id

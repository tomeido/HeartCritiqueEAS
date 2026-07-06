"""DB 오류 클래스 단일 수입 지점.

Supabase 모드에선 postgrest.exceptions.APIError 를 그대로 쓰고, postgrest 패키지가
없는 환경(로컬 SQLite 모드에서 supabase 미설치, 또는 테스트 샌드박스)에선 동일한
속성(.code/.message/.details/.hint)을 가진 호환 클래스로 대체한다.

votes/cleanup 등 'APIError 를 잡아 코드(23505/23503/PGRST202)로 분기'하는 코드와
localdb(같은 클래스를 raise)가 반드시 **같은 심볼**을 공유해야 except 매칭이 성립하므로,
postgrest 를 직접 import 하지 말고 이 모듈을 거친다.
"""

try:  # pragma: no cover - 환경에 따라 갈리는 import
    from postgrest.exceptions import APIError  # type: ignore  # noqa: F401
except Exception:  # ImportError 외에도 스텁 모듈의 AttributeError 등 방어
    class APIError(Exception):  # type: ignore[no-redef]
        """postgrest.exceptions.APIError 호환 최소 구현."""

        def __init__(self, error: dict):
            self._raw = error or {}
            self.message = self._raw.get("message")
            self.code = self._raw.get("code")
            self.hint = self._raw.get("hint")
            self.details = self._raw.get("details")
            super().__init__(self.message)

        def __str__(self) -> str:
            return self.message or repr(self._raw)

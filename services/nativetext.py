"""tracker 텍스트 파이프라인의 선택적 네이티브(Rust) 가속 — 순수 파이썬 폴백 내장.

실측(scratch/bench_cpu.py) 결과 이 앱의 유일한 유의미 CPU 경로는 tracker 의
가시텍스트 추출 + 삭제/차단/봇 패턴 매칭이었다(80KB 페이지당 수십 ms, async
이벤트 루프 위에서 실행 → 루프 스톨). 이 모듈은 그 경로만 Rust(hc_native,
PyO3 + regex crate)로 가속하되:

  · 탐지 패턴의 단일 출처는 여전히 tracker.py 의 re.compile — 컴파일된 패턴의
    .pattern 문자열을 그대로 네이티브에 전달한다(이중 정의 드리프트 없음).
  · hc_native 미설치(로컬 개발/구 이미지), 패턴이 rust regex 비호환(역참조 등),
    NATIVE_TEXT_ENABLED=false 중 어느 경우든 **기존과 100% 동일한 순수 파이썬
    경로**로 조용히 폴백한다. 기능 차이는 없고 속도만 달라진다.
  · 의미론 패리티는 tests/test_nativetext.py 가 고정한다.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

NATIVE_TEXT_ENABLED = os.environ.get("NATIVE_TEXT_ENABLED", "true").lower() != "false"

# ── 순수 파이썬 구현 (기존 tracker._visible_text 의 이관 — 동작 불변) ─────────
# 스트리밍 상한에서 잘린 script 안의 삭제 안내/보안 문구는 본문이 아니다.
# 닫는 태그를 받지 못했어도 숨겨진 블록은 응답 끝까지 버린다(Rust 와 동일).
# 주석과 블록을 한 번에 매치해야 주석 안의 <script>가 다음 본문을 삼키지 않는다.
_STRIP_BLOCK_RE = re.compile(
    r"(?is)<!--.*?(?:-->|\Z)|<(script|style|noscript|template)\b.*?(?:</\1\s*>|\Z)")
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _py_visible_text(html: str) -> str:
    s = _STRIP_BLOCK_RE.sub(" ", html)
    s = _ANY_TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


class PyPipeline:
    """폴백 파이프라인 — 기존 파이썬 re 경로 그대로."""

    native = False

    def __init__(self, del_pattern: str, blk_pattern: str, bot_pattern: str):
        self._del = re.compile(del_pattern, re.IGNORECASE)
        self._blk = re.compile(blk_pattern, re.IGNORECASE)
        self._bot = re.compile(bot_pattern, re.IGNORECASE)

    def visible_text(self, html: str) -> str:
        return _py_visible_text(html)

    def scan(self, text: str) -> tuple[str | None, str | None, bool]:
        dm = self._del.search(text)
        bm = self._blk.search(text)
        return (
            dm.group(0) if dm else None,
            bm.group(0) if bm else None,
            bool(self._bot.search(text)),
        )

    def extract_and_scan(self, html: str) -> tuple[str, str | None, str | None, bool]:
        text = self.visible_text(html)
        return (text, *self.scan(text))


class RustPipeline:
    """hc_native(PyO3) 가속 파이프라인 — GIL 해제로 이벤트 루프 스톨도 제거."""

    native = True

    def __init__(self, mod, del_pattern: str, blk_pattern: str, bot_pattern: str):
        self._mod = mod
        self._scanner = mod.Scanner(del_pattern, blk_pattern, bot_pattern)

    def visible_text(self, html: str) -> str:
        return self._mod.visible_text(html)

    def scan(self, text: str) -> tuple[str | None, str | None, bool]:
        return self._scanner.scan(text)

    def extract_and_scan(self, html: str) -> tuple[str, str | None, str | None, bool]:
        return self._scanner.extract_and_scan(html)


def make_pipeline(del_pattern: str, blk_pattern: str, bot_pattern: str):
    """가능하면 Rust, 아니면 파이썬 파이프라인. 어느 쪽이든 동일 인터페이스."""
    if NATIVE_TEXT_ENABLED:
        try:
            import hc_native
            p = RustPipeline(hc_native, del_pattern, blk_pattern, bot_pattern)
            logger.info(
                f"[nativetext] Rust 가속 활성 (hc_native {hc_native.__version__}, "
                f"{hc_native.__engine__}) — 텍스트 추출/패턴 매칭 GIL 해제 실행")
            return p
        except ImportError:
            logger.info("[nativetext] hc_native 미설치 — 순수 파이썬 경로 사용(동작 동일)")
        except Exception as e:
            # 패턴이 rust regex 비호환(역참조·룩어라운드 등)으로 진화한 경우 포함
            logger.warning(f"[nativetext] 네이티브 초기화 실패({e!r}) — 순수 파이썬 폴백")
    return PyPipeline(del_pattern, blk_pattern, bot_pattern)

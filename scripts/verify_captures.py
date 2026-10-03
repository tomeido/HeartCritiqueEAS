#!/usr/bin/env python3
"""비공개 캡처의 manifest·원문·미디어·오프라인 사본 해시를 검사한다."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.preservation import CAPTURE_ROOT, verify_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=CAPTURE_ROOT)
    parser.add_argument("--manifest", help="저장 root 기준 상대 manifest 경로")
    parser.add_argument("--sha256", help="DB에 저장된 manifest SHA256")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error("limit은 1~10000")
    paths = ([args.manifest] if args.manifest else
             [str(p.relative_to(args.root)) for p in sorted(
                 args.root.glob("versions/*/*/manifest.json"),
                 key=lambda p: p.stat().st_mtime_ns)[-args.limit:]])
    reports = [{"manifest": path, **verify_capture(path, expected_sha256=args.sha256, root=args.root)} for path in paths]
    valid = bool(reports) and all(r["valid"] for r in reports)
    print(json.dumps({"valid": valid, "checked": len(reports), "captures": reports}, ensure_ascii=False, indent=2))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

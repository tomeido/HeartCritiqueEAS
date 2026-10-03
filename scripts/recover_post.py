#!/usr/bin/env python3
"""정확한 URL의 과거 사본을 찾고 --save INDEX로 선택한 HTML을 비공개 보존."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from services.recovery import find_candidates, recover_candidate


async def run(args):
    async with httpx.AsyncClient() as client:
        report = await find_candidates(args.url, client, before=args.before,
                                       commoncrawl_indexes=args.commoncrawl_indexes)
        if args.save is not None:
            if not 0 <= args.save < len(report["candidates"]):
                raise ValueError("선택한 후보 번호가 없습니다 (0부터 시작)")
            report["recovery"] = await recover_candidate(report["candidates"][args.save], client,
                                                          root=args.directory)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="원본 공개 글의 정확한 URL")
    parser.add_argument("--before", help="삭제 관측 시각: YYYYMMDDhhmmss 또는 ISO UTC")
    parser.add_argument("--commoncrawl-indexes", type=int, default=3, choices=range(0, 6))
    parser.add_argument("--save", type=int, metavar="INDEX", help="후보 번호를 검증 후 비공개 저장")
    parser.add_argument("--directory", default="data/recovered", help="비공개 복구 디렉터리")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

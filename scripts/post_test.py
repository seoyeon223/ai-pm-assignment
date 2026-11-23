"""터미널에서 댓글 1개 게시 (대시보드와 같은 규칙 검사 + 기록).

    python -m scripts.post_test --url "https://www.youtube.com/watch?v=..." --text "댓글 내용"
    python -m scripts.post_test --profile 유튜브2 --url ... --text ...
    python -m scripts.post_test --check --text "..."     # 게시하지 않고 내용 검사만

실제로 게시되니 처음엔 본인 채널 테스트 영상으로 해볼 것.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import load_config
from app.service import AUTO, precheck, run_post
from app.store import Store


def print_status(stage: str, label: str, extra: dict) -> None:
    if stage == "typing" and extra.get("total"):
        print(f"\r  [{label}] {extra.get('typed', 0)}/{extra['total']}자", end="", flush=True)
        if extra.get("typed") == extra["total"]:
            print()
        return
    keep = {k: v for k, v in extra.items() if k not in ("attempt_id",)}
    print(f"  [{label}]" + (f" {keep}" if keep else ""))


async def run(args) -> int:
    cfg = load_config(args.config)
    store = Store.from_config(cfg)
    if args.check:
        problems = precheck(args.text, cfg, store)
        print("통과" if not problems else "\n".join(f"  ✘ {p['message']}" for p in problems))
        return 0 if not problems else 2
    s = store.summary()
    print(f"오늘({s.day}) {s.used}/{s.limit}개 사용 — 남은 게시 {s.remaining}개")
    out = await run_post(args.url, args.text, cfg, store, profile=args.profile, on_status=print_status)
    print(json.dumps(out.to_dict(), ensure_ascii=False, indent=2))
    return 0 if out.ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=AUTO, help="유튜브1 / 유튜브2 / 유튜브3 / auto(자동 배정, 기본)")
    ap.add_argument("--url", help="영상 링크 또는 영상 ID")
    ap.add_argument("--text", required=True)
    ap.add_argument("--check", action="store_true", help="게시하지 않고 내용 검사만")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    if not args.check and not args.url:
        ap.error("--url이 필요합니다")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

"""게시 기록 출력 (시간순)

    python -m scripts.show_log            # 최근 30건
    python -m scripts.show_log --today
"""
from __future__ import annotations

import argparse

from app.config import load_config
from app.store import Store

MARK = {"success": "✔ 성공", "failed": "✘ 실패", "blocked": "⛔ 차단", "in_progress": "… 진행 중"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    cfg = load_config()
    store = Store.from_config(cfg)
    s = store.summary()
    print(f"오늘({s.day}) 게시 {s.used}/{s.limit} · 성공 {s.success} · 실패 {s.failed} · 차단 {s.blocked}\n")
    rows = store.log(limit=args.limit, day=store.today() if args.today else None)
    for r in reversed(rows):  # 시간순
        t = r["created_at"][5:16].replace("T", " ")
        print(f"{t}  {MARK.get(r['status'], r['status']):<7} {r['profile'] or '-':<5} {r['video_id']}")
        print(f"      “{r['text']}”")
        if r["comment_url"]:
            print(f"      댓글 보기: {r['comment_url']}")
        if r["error"]:
            print(f"      이유: {r['error']}")
        if r["screenshot"]:
            print(f"      캡처: {r['screenshot']}")


if __name__ == "__main__":
    main()

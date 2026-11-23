"""프로필별 로그인 상태 확인

    python -m scripts.check_profiles
"""
from __future__ import annotations

import asyncio
import sys

from app.config import load_config
from app.poster import check_login


async def run() -> int:
    cfg = load_config()
    cfg.keep_open_seconds = 0
    bad = 0
    for profile in cfg.profiles:
        if not cfg.profile_dir(profile).exists():
            print(f"  - {profile}: 아직 만들지 않음 (python -m scripts.login_profile {profile})")
            continue
        ok, msg = await check_login(profile, cfg)
        print(f"  {'✔' if ok else '✘'} {profile}: {msg}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

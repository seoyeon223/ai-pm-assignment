"""프로필 폴더 생성 + 구글 로그인 (프로필당 1회).

Playwright로 띄운 창에서는 구글 로그인이 막혀서 일반 크롬을 --user-data-dir로 직접 실행한다.

    python -m scripts.login_profile 유튜브1
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from app.config import find_chrome, load_config

LOGIN_URL = "https://accounts.google.com/ServiceLogin?hl=ko&continue=https://www.youtube.com/%3Fhl%3Dko"


def main() -> int:
    ap = argparse.ArgumentParser(description="크롬 프로필 폴더를 만들고 구글 로그인 창을 엽니다.")
    ap.add_argument("profile", help="config.yaml의 profiles 중 하나 (예: 유튜브1)")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    try:
        user_data_dir = cfg.profile_dir(args.profile)
    except ValueError as e:
        print(e)
        return 1
    user_data_dir.mkdir(parents=True, exist_ok=True)

    chrome = cfg.chrome_path or find_chrome()
    if not chrome:
        print("크롬을 찾지 못했습니다. config.yaml의 chrome_path에 크롬 실행 파일 경로를 적어 주세요.")
        return 1

    print(f"[{args.profile}] 크롬을 엽니다: {user_data_dir}")
    print("1) 열린 창에서 이 프로필에 쓸 구글 계정으로 로그인하세요.")
    print("2) 유튜브 오른쪽 위에 내 프로필 사진이 보이면 성공입니다.")
    print("3) 크롬 창을 닫으면 이 스크립트도 끝납니다.")
    proc = subprocess.Popen([
        chrome,
        f"--user-data-dir={user_data_dir}",
        "--lang=ko-KR",
        "--no-first-run",
        "--no-default-browser-check",
        LOGIN_URL,
    ])
    proc.wait()
    print("완료. 로그인 상태 확인: python -m scripts.check_profiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())

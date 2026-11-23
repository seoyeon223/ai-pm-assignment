"""mock 유튜브 페이지로 게시 흐름 테스트 (실제 유튜브 접속 없음)

    python -m pytest -q tests
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import load_config
from app.poster import post_comment
from app.youtube_url import parse_video_id

MOCK = (Path(__file__).parent / "mock_youtube.html").resolve().as_uri()
TEXT = "영상에서 '연락 텀이 길어지면 마음이 식은 거다'라는 말이 제일 와닿았어요. 저도 요즘 그 문제로 고민이 많네요."


def make_cfg(tmp_path: Path, query: str = ""):
    cfg = load_config()
    cfg.profiles_dir = tmp_path / "profiles"
    (cfg.profiles_dir / "유튜브1").mkdir(parents=True)
    cfg.screenshots_dir = tmp_path / "shots"
    cfg.watch_url_template = MOCK + "?v={video_id}" + query
    cfg.headless = True
    cfg.typing_min_ms = cfg.typing_max_ms = 1
    cfg.keep_open_seconds = 0
    cfg.timeout_ms = 8000
    cfg.verify_timeout_ms = 3000
    return cfg


def run(cfg, text=TEXT, video="dQw4w9WgXcQ"):
    stages: list[str] = []
    res = asyncio.run(post_comment(video, text, "유튜브1", cfg, on_status=lambda s, l, e: stages.append(s)))
    return res, stages


def test_success(tmp_path):
    res, stages = run(make_cfg(tmp_path))
    assert res.ok, res.error
    assert res.comment_url and "lc=UgxTEST123" in res.comment_url
    for s in ["opening", "loading", "login_check", "finding_box", "typing", "submitting", "verifying", "done"]:
        assert s in stages
    assert Path(res.screenshot).exists()


@pytest.mark.parametrize("query,code", [
    ("&loggedout=1", "NOT_LOGGED_IN"),
    ("&disabled=1", "COMMENTS_DISABLED"),
    ("&hold=1", "VERIFY_FAILED"),
])
def test_failures_leave_reason_and_screenshot(tmp_path, query, code):
    res, stages = run(make_cfg(tmp_path, query))
    assert not res.ok
    assert res.error_code == code
    assert stages[-1] == "failed"
    assert res.screenshot and Path(res.screenshot).exists()


def test_missing_profile(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.profiles_dir = tmp_path / "nope"
    res, _ = run(cfg)
    assert res.error_code == "PROFILE_NOT_FOUND"


def test_invalid_url(tmp_path):
    res, _ = run(make_cfg(tmp_path), video="https://example.com/abc")
    assert res.error_code == "INVALID_URL"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s",
    "https://youtu.be/dQw4w9WgXcQ?si=x",
    "https://youtube.com/shorts/dQw4w9WgXcQ",
    "m.youtube.com/watch?v=dQw4w9WgXcQ",
    "dQw4w9WgXcQ",
])
def test_parse_video_id(url):
    assert parse_video_id(url) == "dQw4w9WgXcQ"

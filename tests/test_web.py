"""대시보드 API: 불러오기 -> 초안 -> 게시(mock) -> 진행 상태 -> 기록"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_config
from app.web import create_app
from tests.fakes import FakeLLM, FakeYT, fake_transcript

MOCK = (Path(__file__).parent / "mock_youtube.html").resolve().as_uri()


def make(tmp_path, limit=10, llm=None):
    cfg = load_config()
    cfg.db_path = tmp_path / "t.db"
    cfg.daily_max_comments = limit
    cfg.disclosure = "(협찬)"
    cfg.profiles_dir = tmp_path / "profiles"
    (cfg.profiles_dir / "유튜브1").mkdir(parents=True)
    cfg.screenshots_dir = tmp_path / "shots"
    cfg.watch_url_template = MOCK + "?v={video_id}"
    cfg.headless = True
    cfg.typing_min_ms = cfg.typing_max_ms = 1
    cfg.keep_open_seconds = 0
    cfg.timeout_ms, cfg.verify_timeout_ms = 8000, 3000
    yt = FakeYT()
    llm = llm or FakeLLM()
    app = create_app(cfg, yt_factory=lambda c: yt, llm_factory=lambda c: llm, transcript_fetcher=fake_transcript)
    return TestClient(app), yt, llm


def wait_job(c, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["done"]:
            return j
        time.sleep(0.3)
    raise AssertionError("작업이 끝나지 않음")


def test_index_and_status(tmp_path):
    c, _, _ = make(tmp_path)
    assert "영상 불러오기" in c.get("/").text
    s = c.get("/api/status").json()
    assert s["today"]["limit"] == 10 and s["profiles"][0] == {"name": "유튜브1", "exists": True, "busy": False, "login": None}
    assert s["comment"]["disclosure"] == "(협찬)"


def test_full_flow(tmp_path):
    c, yt, llm = make(tmp_path)
    # 불러오기
    vids = c.get("/api/videos").json()["videos"]
    assert len(vids) == 3 and not vids[0]["commented"]
    vid = vids[0]["id"]
    # 초안 (두 번째는 캐시에서)
    d = c.post("/api/draft", json={"video_id": vid}).json()
    assert d["ok"] and d["concern"] and d["evidence"] and not d["cached"]
    n_calls = len(llm.prompts)
    d2 = c.post("/api/draft", json={"video_id": vid}).json()
    assert d2["cached"] and len(llm.prompts) == n_calls
    # 실시간 검사
    pc = c.post(f"/api/precheck?video_id={vid}", json={"text": d["comment"] + " #해시"}).json()
    assert not pc["ok"] and "HASHTAG" in [x["code"] for x in pc["checks"]]
    # 게시 + 진행 상태
    job = c.post("/api/post", json={"video_id": vid, "text": d["comment"], "profile": "auto",
                                    "title": vids[0]["title"], "channel": vids[0]["channel"]}).json()
    j = wait_job(c, job["job_id"])
    assert j["outcome"]["ok"] and j["outcome"]["comment_url"]
    stages = [e["stage"] for e in j["events"]]
    for s in ["opening", "loading", "login_check", "finding_box", "typing", "submitting", "verifying", "done"]:
        assert s in stages
    # SSE로도 같은 이벤트가 끝까지 오는지
    with c.stream("GET", f"/api/jobs/{job['job_id']}/events") as r:
        body = "".join(r.iter_text())
    assert "event: end" in body and '"stage": "done"' in body
    # 같은 영상 재게시는 차단
    assert c.get("/api/videos").json()["videos"][0]["commented"]
    j2 = wait_job(c, c.post("/api/post", json={"video_id": vid, "text": d["comment"]}).json()["job_id"])
    assert j2["outcome"]["status"] == "blocked" and j2["outcome"]["code"] == "DUPLICATE_VIDEO"
    # 기록
    rows = c.get("/api/log").json()["rows"]
    assert [r["status"] for r in rows] == ["blocked", "success"]
    assert rows[1]["video_title"] == vids[0]["title"] and rows[1]["profile"] == "유튜브1"
    assert rows[1]["screenshot_url"].startswith("/shots/")
    assert c.get(rows[1]["screenshot_url"]).status_code == 200
    assert c.get("/api/status").json()["today"]["used"] == 1


def test_link_input(tmp_path):
    c, _, _ = make(tmp_path)
    r = c.post("/api/videos/resolve", json={"url": "https://youtu.be/vid00000002?si=x"}).json()
    assert r["video"]["id"] == "vid00000002"
    assert c.post("/api/videos/resolve", json={"url": "https://example.com"}).status_code == 400
    nf = c.post("/api/videos/resolve", json={"url": "https://youtu.be/zzzzzzzzzzz"})
    assert nf.status_code == 400 and nf.json()["error_code"] == "YT_NOT_FOUND"


def test_rising_mode(tmp_path):
    c, yt, _ = make(tmp_path)
    assert c.get("/api/videos?mode=rising").json()["mode"] == "rising"
    assert yt.calls[-1][0] == "rising"


def test_daily_limit_blocks_from_dashboard(tmp_path):
    c, _, _ = make(tmp_path, limit=1)
    vids = c.get("/api/videos").json()["videos"]
    d1 = c.post("/api/draft", json={"video_id": vids[0]["id"]}).json()
    assert wait_job(c, c.post("/api/post", json={"video_id": vids[0]["id"], "text": d1["comment"]}).json()["job_id"])["outcome"]["ok"]
    d2 = c.post("/api/draft", json={"video_id": vids[1]["id"]}).json()
    j = wait_job(c, c.post("/api/post", json={"video_id": vids[1]["id"], "text": d2["comment"]}).json()["job_id"])
    assert j["outcome"]["code"] == "DAILY_LIMIT"
    assert "opening" not in [e["stage"] for e in j["events"]]


def test_ai_error_is_shown(tmp_path):
    from app.llm import LLMError

    class Broken(FakeLLM):
        def generate_json(self, *a, **k):
            raise LLMError("RATE_LIMIT")

    c, _, _ = make(tmp_path, llm=Broken())
    r = c.post("/api/draft", json={"video_id": "vid00000001"})
    assert r.status_code == 400 and r.json()["error_code"] == "AI_RATE_LIMIT" and "한도" in r.json()["message"]


def test_screenshot_path_traversal_blocked(tmp_path):
    c, _, _ = make(tmp_path)
    assert c.get("/shots/..%2F..%2Fconfig.yaml").status_code == 404

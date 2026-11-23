"""대시보드 서버 (FastAPI). python -m app.web"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .agent import make_draft
from .config import Config, load_config
from .llm import LLMError, make_llm
from .poster import STAGES, check_login
from .service import AUTO, available_profiles, precheck, run_post
from .store import Store
from .transcript import fetch_transcript
from .youtube_api import YouTubeClient, YouTubeError
from .youtube_url import parse_video_id

STATIC = Path(__file__).parent / "static"


# 게시 작업은 별도 스레드 + 자체 이벤트 루프에서 실행
@dataclass
class Job:
    id: str
    video_id: str
    profile: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    outcome: Optional[dict] = None


def _new_loop() -> asyncio.AbstractEventLoop:
    # Windows에서 Playwright subprocess를 쓰려면 Proactor 루프가 필요함
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def _run_in_thread(coro_factory: Callable[[], Any]) -> threading.Thread:
    def target():
        loop = _new_loop()
        try:
            loop.run_until_complete(coro_factory())
        finally:
            loop.close()
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def start(self, cfg: Config, video: str, text: str, profile: str,
              title: Optional[str], channel: Optional[str]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], video_id=video, profile=profile)
        with self.lock:
            self.jobs[job.id] = job

        def on_status(stage: str, label: str, extra: dict):
            job.events.append({"stage": stage, "label": label, **extra})

        async def work():
            store = Store.from_config(cfg)   # 스레드별 연결
            try:
                out = await run_post(video, text, cfg, store, profile=profile, video_title=title,
                                     channel=channel, on_status=on_status)
                job.outcome = out.to_dict()
            except Exception as e:
                job.outcome = {"ok": False, "status": "failed", "code": "UNKNOWN", "message": str(e)[:200]}
                job.events.append({"stage": "failed", "label": STAGES["failed"], "error": str(e)[:200]})
            finally:
                store.close()
                job.done = True

        _run_in_thread(work)
        return job


class ResolveIn(BaseModel):
    url: str


class DraftIn(BaseModel):
    video_id: str
    regenerate: bool = False


class TextIn(BaseModel):
    text: str


class PostIn(BaseModel):
    video_id: str
    text: str
    profile: str = AUTO
    title: Optional[str] = None
    channel: Optional[str] = None


def create_app(cfg: Optional[Config] = None, *, yt_factory: Optional[Callable[[Config], Any]] = None,
               llm_factory: Optional[Callable[[Config], Any]] = None,
               transcript_fetcher: Callable = fetch_transcript) -> FastAPI:
    cfg = cfg or load_config()
    yt_factory = yt_factory or (lambda c: YouTubeClient(c))
    llm_factory = llm_factory or (lambda c: make_llm(c.ai))
    jobs = JobManager()
    login_state: dict[str, dict] = {}
    app = FastAPI(title="유튜브 댓글 에이전트")
    Store.from_config(cfg).recover_stale()

    def store() -> Store:
        return Store.from_config(cfg)

    @app.exception_handler(YouTubeError)
    async def _yt_err(_: Request, e: YouTubeError):
        return JSONResponse({"error_code": f"YT_{e.code}", "message": str(e)}, status_code=400)

    @app.exception_handler(LLMError)
    async def _llm_err(_: Request, e: LLMError):
        return JSONResponse({"error_code": f"AI_{e.code}", "message": str(e)}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status():
        s = store()
        try:
            summ = s.summary()
            busy = s.busy_profiles()
            ready = set(available_profiles(cfg))
            return {
                "keys": {"youtube": bool(cfg.youtube_api_key), "ai": bool(cfg.ai.api_key())},
                "ai": {"provider": cfg.ai.provider, "analysis_model": cfg.ai.analysis_model,
                       "writer_model": cfg.ai.writer_model},
                "profiles": [{"name": p, "exists": p in ready, "busy": p in busy,
                              "login": login_state.get(p)} for p in cfg.profiles],
                "today": {"day": summ.day, "used": summ.used, "limit": summ.limit, "remaining": summ.remaining,
                          "success": summ.success, "failed": summ.failed, "blocked": summ.blocked},
                "comment": {"min": cfg.min_chars, "max": cfg.max_chars, "brand": cfg.brand,
                            "disclosure": cfg.disclosure},
                "search_query": cfg.youtube.search_query,
                "rising": {"min_views": cfg.youtube.rising_min_views, "max_views": cfg.youtube.rising_max_views,
                           "per_hour": cfg.youtube.rising_min_per_hour,
                           "hours": cfg.youtube.rising_lookback_hours},
            }
        finally:
            s.close()

    def _decorate(videos: list) -> list[dict]:
        s = store()
        try:
            done, drafted = s.commented_video_ids(), s.drafted_video_ids()
        finally:
            s.close()
        return [{**v.to_dict(), "commented": v.id in done, "drafted": v.id in drafted} for v in videos]

    @app.get("/api/videos")
    def videos(mode: str = "latest", q: Optional[str] = None):
        yt = yt_factory(cfg)
        vids = yt.rising(q) if mode == "rising" else yt.latest(q)
        return {"mode": mode, "query": q or cfg.youtube.search_query, "videos": _decorate(vids)}

    @app.post("/api/videos/resolve")
    def resolve(body: ResolveIn):
        try:
            vid = parse_video_id(body.url)
        except ValueError as e:
            raise HTTPException(400, {"error_code": "INVALID_URL", "message": str(e)})
        return {"video": _decorate([yt_factory(cfg).video(vid)])[0]}

    @app.post("/api/draft")
    def draft(body: DraftIn):
        s = store()
        try:
            if not body.regenerate and (cached := s.get_draft(body.video_id)):
                cached["checks"] = precheck_with_evidence(cached["comment"], cached.get("evidence", []), s)
                cached["ok"] = not cached["checks"]
                return {**cached, "cached": True}
            yt = yt_factory(cfg)
            v = yt.video(body.video_id)
            comments = yt.top_comments(v.id)
            r = make_draft(v, cfg, llm_factory(cfg), s, comments, transcript_fetcher)
            return {**r.to_dict(), "cached": False}
        finally:
            s.close()

    def precheck_with_evidence(text: str, evidence: list[dict], s: Store) -> list[dict]:
        from .agent import check_draft
        return check_draft(text, evidence, cfg, s)

    @app.post("/api/precheck")
    def check(body: TextIn, video_id: Optional[str] = None):
        s = store()
        try:
            evidence = (s.get_draft(video_id) or {}).get("evidence", []) if video_id else []
            checks = precheck_with_evidence(body.text, evidence, s)
            return {"ok": not checks, "checks": checks, "length": len(body.text.strip())}
        finally:
            s.close()

    @app.post("/api/post")
    def post(body: PostIn):
        job = jobs.start(cfg, body.video_id, body.text, body.profile, body.title, body.channel)
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str):
        job = jobs.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "작업이 없습니다")

        async def gen():
            i = 0
            while True:
                while i < len(job.events):
                    yield f"data: {json.dumps(job.events[i], ensure_ascii=False)}\n\n"
                    i += 1
                if job.done and i >= len(job.events):
                    yield f"event: end\ndata: {json.dumps(job.outcome or {}, ensure_ascii=False)}\n\n"
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}")
    def job_state(job_id: str):
        job = jobs.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "작업이 없습니다")
        return {"done": job.done, "events": job.events, "outcome": job.outcome}

    @app.get("/api/log")
    def log(limit: int = 100):
        s = store()
        try:
            rows = s.log(limit=limit)
            for r in rows:
                r["screenshot_url"] = f"/shots/{Path(r['screenshot']).name}" if r.get("screenshot") else None
            return {"rows": rows}
        finally:
            s.close()

    @app.get("/shots/{name}")
    def shot(name: str):
        p = (cfg.screenshots_dir / name).resolve()
        if p.parent != cfg.screenshots_dir.resolve() or not p.exists() or p.suffix != ".png":
            raise HTTPException(404)
        return FileResponse(p)

    @app.post("/api/profiles/check")
    def profiles_check():
        """프로필마다 크롬을 잠깐 띄워 로그인 확인. 게시 중인 프로필은 건너뜀."""
        s = store()
        try:
            busy = s.busy_profiles()
        finally:
            s.close()
        targets = [p for p in available_profiles(cfg) if p not in busy]
        results: dict[str, dict] = {}

        async def work():
            for p in targets:
                ok, msg = await check_login(p, cfg)
                results[p] = {"ok": ok, "message": msg}

        _run_in_thread(work).join(timeout=180)
        login_state.update(results)
        return {"results": results}

    return app


def main() -> None:
    import uvicorn
    cfg = load_config()
    url = "http://127.0.0.1:8000"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"대시보드: {url}  (종료: Ctrl+C)")
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()

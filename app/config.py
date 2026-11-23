"""config.yaml 로딩, 크롬 경로 찾기"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class YouTubeSettings:
    search_query: str = "연애"
    max_results: int = 20
    region: str = "KR"
    language: str = "ko"
    cache_minutes: int = 30
    top_comments: int = 15
    rising_min_views: int = 1000
    rising_max_views: int = 5000
    rising_min_per_hour: float = 150
    rising_lookback_hours: int = 48


@dataclass
class AISettings:
    provider: str = "gemini"              # gemini | anthropic
    analysis_model: str = "gemini-3.5-flash"
    writer_model: str = "gemini-3.5-flash"
    use_video_input: bool = True          # 자막 없을 때 영상 URL을 직접 넘김 (Gemini만)
    max_attempts: int = 3                 # 초안 재생성 최대 횟수
    temperature: float = 0.9

    def api_key(self) -> str:
        env = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(self.provider, "")
        return os.environ.get(env, "") if env else ""


@dataclass
class Config:
    profiles_dir: Path
    profiles: list[str]
    chrome_path: str
    min_chars: int
    max_chars: int
    brand: str
    daily_max_comments: int
    typing_min_ms: int
    typing_max_ms: int
    timeout_ms: int
    verify_timeout_ms: int
    keep_open_seconds: float
    screenshots_dir: Path
    db_path: Path = ROOT / "data" / "agent.db"
    timezone: str = "Asia/Seoul"          # 하루 상한 기준
    disclosure: str = ""                  # 광고 표시 문구 (비우면 검사 안 함)
    require_brand: bool = True
    max_similarity: float = 0.6           # 이전 댓글과 유사도 상한
    stale_minutes: int = 15               # in_progress가 이보다 오래되면 failed 처리
    # 테스트에서 mock 페이지로 바꿔 끼우는 용도
    watch_url_template: str = "https://www.youtube.com/watch?v={video_id}&hl=ko&persist_hl=1"
    headless: bool = False  # 테스트에서만 True
    youtube: YouTubeSettings = field(default_factory=YouTubeSettings)
    ai: AISettings = field(default_factory=AISettings)
    extra: dict = field(default_factory=dict)

    @property
    def youtube_api_key(self) -> str:
        return os.environ.get("YOUTUBE_API_KEY", "")

    def profile_dir(self, profile: str) -> Path:
        if profile not in self.profiles:
            raise ValueError(f"설정에 없는 프로필입니다: {profile} (가능: {', '.join(self.profiles)})")
        return self.profiles_dir / profile

    def resolve_chrome(self) -> str | None:
        """못 찾으면 None -> Playwright channel='chrome' 사용"""
        if self.chrome_path:
            return self.chrome_path
        return find_chrome()


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (ROOT / path).resolve()


def load_config(path: str | Path | None = None) -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    comment = raw.get("comment", {})
    limits = raw.get("limits", {})
    typing = raw.get("typing", {})
    browser = raw.get("browser", {})
    cfg = Config(
        profiles_dir=_abs(raw.get("profiles_dir", "./profiles")),
        profiles=list(raw.get("profiles", ["유튜브1"])),
        chrome_path=raw.get("chrome_path", "") or os.environ.get("CHROME_PATH", ""),
        min_chars=int(comment.get("min_chars", 40)),
        max_chars=int(comment.get("max_chars", 120)),
        brand=str(comment.get("brand", "운명원")),
        daily_max_comments=int(limits.get("daily_max_comments", 10)),
        typing_min_ms=int(typing.get("min_delay_ms", 60)),
        typing_max_ms=int(typing.get("max_delay_ms", 140)),
        timeout_ms=int(float(browser.get("timeout_seconds", 30)) * 1000),
        verify_timeout_ms=int(float(browser.get("verify_timeout_seconds", 20)) * 1000),
        keep_open_seconds=float(browser.get("keep_open_seconds", 5)),
        screenshots_dir=_abs(raw.get("screenshots_dir", "./data/screenshots")),
    )
    if raw.get("watch_url_template"):
        cfg.watch_url_template = raw["watch_url_template"]
    yt = raw.get("youtube", {}) or {}
    rising = yt.get("rising", {}) or {}
    ai = raw.get("ai", {}) or {}
    cfg.youtube = YouTubeSettings(
        search_query=str(yt.get("search_query", "연애")),
        max_results=int(yt.get("max_results", 20)),
        region=str(yt.get("region", "KR")),
        language=str(yt.get("language", "ko")),
        cache_minutes=int(yt.get("cache_minutes", 30)),
        top_comments=int(yt.get("top_comments", 15)),
        rising_min_views=int(rising.get("min_views", 1000)),
        rising_max_views=int(rising.get("max_views", 5000)),
        rising_min_per_hour=float(rising.get("min_views_per_hour", 150)),
        rising_lookback_hours=int(rising.get("lookback_hours", 48)),
    )
    cfg.ai = AISettings(
        provider=str(ai.get("provider", "gemini")),
        analysis_model=str(ai.get("analysis_model", "gemini-3.5-flash")),
        writer_model=str(ai.get("writer_model", "gemini-3.5-flash")),
        use_video_input=bool(ai.get("use_video_input", True)),
        max_attempts=int(ai.get("max_attempts", 3)),
        temperature=float(ai.get("temperature", 0.9)),
    )
    if raw.get("db_path"):
        cfg.db_path = _abs(raw["db_path"])
    cfg.timezone = str(limits.get("timezone", cfg.timezone))
    cfg.stale_minutes = int(limits.get("stale_minutes", cfg.stale_minutes))
    cfg.disclosure = str(comment.get("disclosure", cfg.disclosure) or "")
    cfg.require_brand = bool(comment.get("require_brand", cfg.require_brand))
    cfg.max_similarity = float(comment.get("max_similarity", cfg.max_similarity))
    return cfg


def find_chrome() -> str | None:
    system = platform.system()
    candidates: list[str] = []
    if system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None

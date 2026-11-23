"""크롬 프로필로 영상을 열어 댓글을 입력/게시하고, 목록에 내 댓글이 뜨는지 확인.

영상당 1개, 하루 상한 같은 규칙은 여기서 보지 않는다(service.py에서 미리 검사).
"""
from __future__ import annotations

import asyncio
import inspect
import random
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from playwright.async_api import BrowserContext, Error as PWError, Locator, Page
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from .config import Config
from .youtube_url import parse_video_id

# 진행 단계 (대시보드에 그대로 표시)
STAGES: dict[str, str] = {
    "opening": "프로필 여는 중",
    "loading": "영상 여는 중",
    "login_check": "로그인 확인 중",
    "finding_box": "댓글 창 찾는 중",
    "typing": "입력 중",
    "submitting": "게시 중",
    "verifying": "내 댓글 확인 중",
    "done": "게시 완료",
    "failed": "실패",
    "blocked": "규칙에 의해 차단",
}

# 실패 코드별 안내 문구
ERRORS: dict[str, str] = {
    "INVALID_URL": "유튜브 영상 링크를 인식하지 못했습니다.",
    "PROFILE_NOT_FOUND": "프로필 폴더가 없습니다. scripts/login_profile.py로 먼저 로그인해 주세요.",
    "PROFILE_IN_USE": "같은 프로필의 크롬 창이 이미 열려 있습니다. 그 창을 닫고 다시 시도해 주세요.",
    "BROWSER_LAUNCH_FAILED": "크롬을 실행하지 못했습니다. 크롬 설치 경로(config.yaml의 chrome_path)를 확인해 주세요.",
    "VIDEO_UNAVAILABLE": "영상을 열 수 없습니다(비공개·삭제·연령 제한 등).",
    "NOT_LOGGED_IN": "이 프로필이 유튜브에 로그인되어 있지 않습니다.",
    "COMMENTS_DISABLED": "이 영상은 댓글이 사용 중지되어 있습니다.",
    "COMMENT_BOX_NOT_FOUND": "댓글 입력창을 찾지 못했습니다(유튜브 화면 구조 변경 가능성).",
    "TYPING_MISMATCH": "입력된 내용이 원래 댓글과 다릅니다.",
    "SUBMIT_FAILED": "게시 버튼을 누르지 못했습니다.",
    "VERIFY_FAILED": "게시 후 내 댓글이 화면에 보이지 않습니다(검토 대기·필터링 가능성).",
    "UNKNOWN": "알 수 없는 오류가 발생했습니다.",
}

# 유튜브 DOM 선택자. 화면 구조가 바뀌면 여기만 수정. 앞에서부터 순서대로 시도.
SEL = {
    "comments_section": ["ytd-comments#comments", "#comments"],
    "placeholder": [
        "ytd-comments #simple-box #placeholder-area",
        "ytd-comment-simplebox-renderer #placeholder-area",
        "#placeholder-area",
    ],
    "editor": [
        "ytd-comments ytd-commentbox #contenteditable-root",
        "ytd-commentbox #contenteditable-root",
        "#contenteditable-root",
    ],
    "submit": [
        "ytd-comments ytd-commentbox #submit-button button",
        "ytd-commentbox #submit-button button",
        "ytd-commentbox #submit-button",
    ],
    "comments_disabled": [
        "ytd-comments ytd-message-renderer",
        "ytd-item-section-renderer#sections ytd-message-renderer",
    ],
    "avatar": ["ytd-masthead #avatar-btn", "#avatar-btn", "ytd-topbar-menu-button-renderer #avatar-btn"],
    "sign_in": [
        "ytd-masthead a[href*='accounts.google.com/ServiceLogin']",
        "ytd-masthead ytd-button-renderer a[aria-label='로그인']",
        "a[href*='ServiceLogin']",
    ],
    "video_error": ["ytd-player #error-screen yt-playability-error-supported-renderers:not([hidden])",
                    "yt-playability-error-supported-renderers"],
    "comment_thread": ["ytd-comment-thread-renderer"],
    "comment_text": ["#content-text"],
    "comment_link": ["#published-time-text a", "a#published-time-text", "#header-author a[href*='lc=']"],
}

StatusCallback = Callable[[str, str, dict], Union[None, Awaitable[None]]]


class PostError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code if code in ERRORS else "UNKNOWN"
        self.detail = detail
        super().__init__(f"{self.code}: {ERRORS[self.code]} {detail}".strip())


@dataclass
class PostResult:
    ok: bool
    video_id: str
    profile: str
    text: str
    started_at: str
    finished_at: str = ""
    comment_url: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    screenshot: Optional[str] = None
    last_stage: str = "opening"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(s: str) -> str:
    """NFC + 공백 정리 (입력값 비교용)"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()


async def _emit(cb: Optional[StatusCallback], stage: str, extra: Optional[dict] = None) -> None:
    if cb is None:
        return
    r = cb(stage, STAGES.get(stage, stage), extra or {})
    if inspect.isawaitable(r):
        await r


async def _first_visible(page: Page, keys: list[str], timeout_ms: int) -> Locator:
    """후보 중 먼저 보이는 요소. 다 안 보이면 PWTimeout."""
    loc = page.locator(keys[0]).first
    for k in keys[1:]:
        loc = loc.or_(page.locator(k).first)
    await loc.first.wait_for(state="visible", timeout=timeout_ms)
    return loc.first


async def _any_visible(page: Page, keys: list[str]) -> bool:
    for k in keys:
        try:
            if await page.locator(k).first.is_visible():
                return True
        except PWError:
            pass
    return False


async def _pause_video(page: Page) -> None:
    try:
        await page.evaluate(
            "() => { const v = document.querySelector('video'); if (v) { v.muted = true; v.pause(); } }"
        )
    except PWError:
        pass


async def launch_profile(p, cfg: Config, profile: str, headless: bool = False) -> BrowserContext:
    """별도 user-data-dir로 크롬 실행 (기본 프로필은 자동 제어가 막혀 있음)."""
    user_data_dir = cfg.profile_dir(profile)
    if not user_data_dir.exists():
        raise PostError("PROFILE_NOT_FOUND", str(user_data_dir))
    chrome = cfg.resolve_chrome()
    kwargs: dict[str, Any] = dict(
        user_data_dir=str(user_data_dir),
        headless=headless,
        locale="ko-KR",
        args=["--lang=ko-KR", "--start-maximized"],
        no_viewport=True,
        # 자동화 탐지 우회 옵션은 의도적으로 넣지 않음
    )
    if chrome:
        kwargs["executable_path"] = chrome
    else:
        kwargs["channel"] = "chrome"
    try:
        return await p.chromium.launch_persistent_context(**kwargs)
    except PWError as e:
        msg = str(e)
        if "ProcessSingleton" in msg or "already in use" in msg or "SingletonLock" in msg:
            raise PostError("PROFILE_IN_USE") from e
        raise PostError("BROWSER_LAUNCH_FAILED", msg.splitlines()[0][:200]) from e


async def _open_video(page: Page, cfg: Config, video_id: str) -> None:
    url = cfg.watch_url_template.format(video_id=video_id)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
    except PWTimeout as e:
        raise PostError("VIDEO_UNAVAILABLE", "페이지 로딩 시간 초과") from e
    await page.wait_for_timeout(1500)
    await _pause_video(page)
    if await _any_visible(page, SEL["video_error"]):
        raise PostError("VIDEO_UNAVAILABLE")


async def _check_logged_in(page: Page, cfg: Config) -> None:
    try:
        avatar_or_signin = page.locator(SEL["avatar"][0]).first
        for k in SEL["avatar"][1:] + SEL["sign_in"]:
            avatar_or_signin = avatar_or_signin.or_(page.locator(k).first)
        await avatar_or_signin.first.wait_for(state="visible", timeout=cfg.timeout_ms)
    except PWTimeout:
        pass
    if not await _any_visible(page, SEL["avatar"]):
        raise PostError("NOT_LOGGED_IN")


async def _scroll_to_comments(page: Page, cfg: Config) -> Locator:
    """댓글 영역은 lazy load라서 입력창이나 '댓글 사용 중지' 안내가 나올 때까지 스크롤."""
    deadline = asyncio.get_event_loop().time() + cfg.timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        for k in SEL["comments_section"]:
            sec = page.locator(k).first
            if await sec.count():
                try:
                    await sec.scroll_into_view_if_needed(timeout=2000)
                except PWError:
                    pass
                break
        await page.mouse.wheel(0, 600)
        await page.wait_for_timeout(700)
        await _pause_video(page)
        if await _any_visible(page, SEL["placeholder"]):
            return await _first_visible(page, SEL["placeholder"], 2000)
        if await _any_visible(page, SEL["comments_disabled"]):
            raise PostError("COMMENTS_DISABLED")
    raise PostError("COMMENT_BOX_NOT_FOUND")


async def _type_comment(page: Page, cfg: Config, placeholder: Locator, text: str,
                        on_status: Optional[StatusCallback]) -> Locator:
    await placeholder.click()
    try:
        editor = await _first_visible(page, SEL["editor"], cfg.timeout_ms)
    except PWTimeout as e:
        # 비로그인이면 입력창 대신 로그인 안내가 뜸
        if await _any_visible(page, SEL["sign_in"]):
            raise PostError("NOT_LOGGED_IN") from e
        raise PostError("COMMENT_BOX_NOT_FOUND", "입력창이 열리지 않음") from e
    await editor.click()
    total = len(text)
    for i, ch in enumerate(text, 1):
        await page.keyboard.type(ch)
        await page.wait_for_timeout(random.randint(cfg.typing_min_ms, cfg.typing_max_ms))
        if i % 10 == 0 or i == total:
            await _emit(on_status, "typing", {"typed": i, "total": total})
    typed = await editor.inner_text()
    if _norm(typed) != _norm(text):
        raise PostError("TYPING_MISMATCH", f"입력됨: {typed[:60]!r}")
    return editor


async def _submit(page: Page, cfg: Config) -> None:
    try:
        btn = await _first_visible(page, SEL["submit"], cfg.timeout_ms)
        # 내용 입력 후에야 버튼이 활성화됨
        for _ in range(20):
            if await btn.is_enabled():
                break
            await page.wait_for_timeout(250)
        await btn.click()
    except (PWTimeout, PWError) as e:
        raise PostError("SUBMIT_FAILED", str(e).splitlines()[0][:200]) from e


async def _verify(page: Page, cfg: Config, text: str) -> Optional[str]:
    """본문이 같은 댓글을 찾아 고유 링크(lc=...)를 반환. 새 댓글은 목록 맨 위에 붙는다."""
    target = _norm(text)
    snippet = target[: min(20, len(target))]
    thread = page.locator(SEL["comment_thread"][0]).filter(
        has=page.locator(SEL["comment_text"][0], has_text=snippet)
    ).first
    try:
        await thread.wait_for(state="visible", timeout=cfg.verify_timeout_ms)
    except PWTimeout as e:
        raise PostError("VERIFY_FAILED") from e
    body = _norm(await thread.locator(SEL["comment_text"][0]).first.inner_text())
    if body != target:
        raise PostError("VERIFY_FAILED", f"비슷한 댓글은 있으나 내용이 다름: {body[:60]!r}")
    await thread.scroll_into_view_if_needed()
    href = None
    for k in SEL["comment_link"]:
        link = thread.locator(k).first
        if await link.count():
            href = await link.get_attribute("href")
            if href:
                break
    if href and href.startswith("/"):
        href = "https://www.youtube.com" + href
    return href


async def _screenshot(page: Optional[Page], cfg: Config, video_id: str, profile: str, tag: str) -> Optional[str]:
    if page is None:
        return None
    try:
        cfg.screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = cfg.screenshots_dir / f"{ts}_{profile}_{video_id}_{tag}.png"
        await page.screenshot(path=str(path), full_page=False)
        return str(path)
    except PWError:
        return None


async def post_comment(video: str, text: str, profile: str, cfg: Config,
                       on_status: Optional[StatusCallback] = None) -> PostResult:
    """댓글 게시. 실패해도 예외 대신 PostResult(error_code 포함)를 반환."""
    started = datetime.now().isoformat(timespec="seconds")
    result = PostResult(ok=False, video_id="", profile=profile, text=text, started_at=started)
    page: Optional[Page] = None
    ctx: Optional[BrowserContext] = None

    async def stage(name: str, extra: Optional[dict] = None):
        result.last_stage = name
        await _emit(on_status, name, extra)

    try:
        try:
            result.video_id = parse_video_id(video)
        except ValueError as e:
            raise PostError("INVALID_URL", str(e)) from e

        async with async_playwright() as p:
            await stage("opening", {"profile": profile})
            ctx = await launch_profile(p, cfg, profile, headless=cfg.headless)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.bring_to_front()

                await stage("loading", {"video_id": result.video_id})
                await _open_video(page, cfg, result.video_id)

                await stage("login_check")
                await _check_logged_in(page, cfg)

                await stage("finding_box")
                placeholder = await _scroll_to_comments(page, cfg)

                await stage("typing", {"typed": 0, "total": len(text)})
                await _type_comment(page, cfg, placeholder, text, on_status)

                await stage("submitting")
                await _submit(page, cfg)

                await stage("verifying")
                result.comment_url = await _verify(page, cfg, text)

                result.ok = True
                result.screenshot = await _screenshot(page, cfg, result.video_id, profile, "done")
                await stage("done", {"comment_url": result.comment_url})
                await page.wait_for_timeout(int(cfg.keep_open_seconds * 1000))
            except PostError as e:
                result.screenshot = await _screenshot(page, cfg, result.video_id, profile, e.code)
                raise
            except Exception as e:  # 예상 못 한 오류도 스크린샷은 남김
                result.screenshot = await _screenshot(page, cfg, result.video_id or "unknown", profile, "UNKNOWN")
                raise PostError("UNKNOWN", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}") from e
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
    except PostError as e:
        result.error_code = e.code
        result.error = f"{ERRORS[e.code]} {e.detail}".strip()
        await _emit(on_status, "failed", {"error_code": e.code, "error": result.error,
                                          "screenshot": result.screenshot, "at_stage": result.last_stage})
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


async def check_login(profile: str, cfg: Config) -> tuple[bool, str]:
    """프로필의 유튜브 로그인 여부 확인"""
    try:
        async with async_playwright() as p:
            ctx = await launch_profile(p, cfg, profile)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://www.youtube.com/?hl=ko", wait_until="domcontentloaded",
                                timeout=cfg.timeout_ms)
                await _check_logged_in(page, cfg)
                return True, "로그인됨"
            finally:
                await ctx.close()
    except PostError as e:
        return False, ERRORS[e.code]

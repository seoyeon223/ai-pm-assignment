"""게시 1건 처리: 규칙 검사 -> 프로필 배정 -> DB 예약 -> 게시 -> 기록.

규칙에 걸리면 브라우저를 띄우기 전에 끝낸다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .config import Config
from .poster import PostResult, StatusCallback, _emit, post_comment
from .rules import most_similar, normalize, validate_comment
from .store import RULE_MESSAGES, RuleViolation, Store
from .youtube_url import parse_video_id

AUTO = "auto"


@dataclass
class RunOutcome:
    ok: bool
    attempt_id: Optional[int]
    status: str                      # success | failed | blocked
    video_id: str
    profile: Optional[str]
    code: Optional[str] = None
    message: Optional[str] = None
    comment_url: Optional[str] = None
    screenshot: Optional[str] = None
    violations: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def available_profiles(cfg: Config) -> list[str]:
    """폴더가 있는(login_profile을 한 번 돌린) 프로필만"""
    return [p for p in cfg.profiles if (cfg.profiles_dir / p).exists()]


def assign_profile(cfg: Config, store: Store) -> str:
    """사용 중이 아닌 프로필 중 오늘 게시 수가 가장 적은 것. 동률이면 config 순서."""
    busy = store.busy_profiles()
    counts = store.profile_counts_today()
    candidates = [p for p in available_profiles(cfg) if p not in busy]
    if not candidates:
        raise RuleViolation("NO_PROFILE")
    return min(candidates, key=lambda p: (counts.get(p, 0), cfg.profiles.index(p)))


def precheck(text: str, cfg: Config, store: Store) -> list[dict]:
    """초안 실시간 검사용. DB에 기록하지 않음."""
    out = [asdict(v) for v in validate_comment(text, cfg)]
    sim, other = most_similar(text, store.posted_texts(), ignore=[cfg.brand, cfg.disclosure])
    if sim >= cfg.max_similarity:
        out.append({"code": "TOO_SIMILAR",
                    "message": f"이전 댓글과 {sim:.0%} 비슷합니다: “{(other or '')[:40]}…”"})
    return out


async def run_post(video: str, text: str, cfg: Config, store: Store, profile: str = AUTO,
                   video_title: Optional[str] = None, channel: Optional[str] = None,
                   on_status: Optional[StatusCallback] = None) -> RunOutcome:
    text = normalize(text)

    try:
        video_id = parse_video_id(video)
    except ValueError:
        aid = store.log_blocked(str(video)[:200], text, "INVALID_URL")
        return await _blocked_async(aid, str(video)[:200], None, "INVALID_URL", "", on_status)

    # 이미 단 영상이면 내용 검사보다 이 사유를 먼저 보여준다. 실제 차단은 reserve()에서.
    if video_id in store.commented_video_ids():
        aid = store.log_blocked(video_id, text, "DUPLICATE_VIDEO", "", None if profile == AUTO else profile,
                                video_title, channel)
        return await _blocked_async(aid, video_id, None if profile == AUTO else profile,
                                    "DUPLICATE_VIDEO", "", on_status)

    violations = precheck(text, cfg, store)
    if violations:
        code = "TOO_SIMILAR" if all(v["code"] == "TOO_SIMILAR" for v in violations) else "INVALID_COMMENT"
        detail = " / ".join(v["message"] for v in violations)
        aid = store.log_blocked(video_id, text, code, detail, None if profile == AUTO else profile,
                                video_title, channel)
        out = await _blocked_async(aid, video_id, None if profile == AUTO else profile, code, detail, on_status)
        out.violations = violations
        return out

    try:
        if profile == AUTO:
            profile = assign_profile(cfg, store)
        elif profile not in cfg.profiles:
            raise RuleViolation("UNKNOWN_PROFILE", profile)
    except RuleViolation as e:
        aid = store.log_blocked(video_id, text, e.code, e.detail, None, video_title, channel)
        return await _blocked_async(aid, video_id, None, e.code, e.detail, on_status)

    # 영상당 1개 / 하루 상한 / 프로필 동시 사용은 트랜잭션 안에서 확인
    try:
        attempt_id = store.reserve(video_id, profile, text, video_title, channel)
    except RuleViolation as e:
        return await _blocked_async(e.attempt_id, video_id, profile, e.code, e.detail, on_status)

    async def status_and_record(stage: str, label: str, extra: dict):
        if stage not in ("failed",):
            store.update_stage(attempt_id, stage)
        await _emit(on_status, stage, {**extra, "attempt_id": attempt_id, "profile": profile})

    result: PostResult
    try:
        result = await post_comment(video_id, text, profile, cfg, on_status=status_and_record)
    except BaseException as e:  # 취소돼도 in_progress로 남지 않게 정리
        store.finish(attempt_id, False, error_code="UNKNOWN", error=f"{type(e).__name__}: {e}")
        raise
    store.finish(attempt_id, result.ok, comment_url=result.comment_url, error_code=result.error_code,
                 error=result.error, screenshot=result.screenshot, last_stage=result.last_stage)
    return RunOutcome(ok=result.ok, attempt_id=attempt_id, status="success" if result.ok else "failed",
                      video_id=video_id, profile=profile, code=result.error_code, message=result.error,
                      comment_url=result.comment_url, screenshot=result.screenshot)


async def _blocked_async(aid, video_id, profile, code, detail, on_status) -> RunOutcome:
    msg = f"{RULE_MESSAGES.get(code, '')} {detail}".strip()
    await _emit(on_status, "blocked", {"error_code": code, "error": msg, "attempt_id": aid})
    return RunOutcome(ok=False, attempt_id=aid, status="blocked", video_id=video_id, profile=profile,
                      code=code, message=msg)

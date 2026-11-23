"""영상 분석(고민 + 근거) 후 댓글 초안 작성. 규칙에 걸리면 이유를 붙여 다시 생성."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from .config import Config
from .llm import LLM, LLMError
from .rules import normalize
from .service import precheck
from .store import Store
from .transcript import Transcript, fetch_transcript
from .youtube_api import Video

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "concern": {"type": "string", "description": "영상이 다루는 연애 고민 한 줄 (40자 이내)"},
        "summary": {"type": "string", "description": "영상 내용 요약 2~3문장"},
        "evidence": {
            "type": "array", "minItems": 1, "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "mm:ss (모르면 빈 문자열)"},
                    "quote": {"type": "string", "description": "영상 속 실제 말이나 장면 (자막 원문 그대로, 60자 이내)"},
                    "keyword": {"type": "string", "description": "quote 안에 그대로 들어 있는 2~12자 핵심 표현"},
                },
                "required": ["time", "quote", "keyword"],
            },
        },
        "viewer_mood": {"type": "string", "description": "인기 댓글로 본 시청자 반응 한 줄"},
    },
    "required": ["concern", "summary", "evidence", "viewer_mood"],
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "comment": {"type": "string"},
        "used_keyword": {"type": "string", "description": "댓글에 그대로 넣은 근거 표현"},
    },
    "required": ["comment", "used_keyword"],
}

ANALYST_SYSTEM = (
    "당신은 연애 상담 영상을 분석하는 리서처입니다. 영상에 실제로 나온 내용만 근거로 삼고, "
    "없는 장면이나 말을 지어내지 않습니다."
)

WRITER_SYSTEM = (
    "당신은 유튜브 연애 영상을 끝까지 본 20~30대 한국인 시청자로서 댓글을 씁니다. "
    "말투는 자연스러운 구어체이고, 영상 속 구체적인 말이나 장면에 공감하며 자기 고민을 한두 마디 덧붙입니다."
)


@dataclass
class DraftResult:
    video: dict
    concern: str
    summary: str
    evidence: list[dict]
    viewer_mood: str
    source: str                 # transcript | video | metadata
    source_note: str
    comment: str
    used_keyword: str
    checks: list[dict]
    ok: bool
    attempts: int
    model: str
    created_at: str = ""
    history: list[dict] = field(default_factory=list)   # 재생성 전 초안들과 탈락 이유

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _squash(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFC", s or "")).lower()


# 분석
def _analysis_prompt(v: Video, transcript: Optional[Transcript], comments: list[dict], with_video: bool) -> str:
    parts = [
        f"[영상 제목] {v.title}",
        f"[채널] {v.channel}",
        f"[설명] {v.description[:1500] or '(없음)'}",
    ]
    if transcript:
        parts.append(f"[자막 — mm:ss 문장]\n{transcript.for_prompt()}")
    elif with_video:
        parts.append("[자막] 없음 — 첨부된 영상을 직접 보고 분석하세요. 근거의 time은 영상 속 시점입니다.")
    else:
        parts.append("[자막] 없음 — 제목과 설명만으로 분석하세요. 근거의 quote는 제목·설명 속 문구를 쓰고 time은 비워 두세요.")
    if comments:
        parts.append("[인기 댓글]\n" + "\n".join(f"- ({c['likes']}명 공감) {c['text'][:200]}" for c in comments[:15]))
    parts.append(
        "할 일:\n"
        "1) 이 영상이 다루는 연애 고민을 한 줄(40자 이내)로 정리하세요.\n"
        "2) 댓글에서 짚을 수 있는 영상 속 구체적인 말이나 장면을 3~5개 고르세요. "
        "quote는 원문 그대로, keyword는 quote 안에 실제로 있는 2~12자 표현입니다.\n"
        "3) 인기 댓글로 본 시청자 반응을 한 줄로 쓰세요."
    )
    return "\n\n".join(parts)


def _verify_evidence(ev: list[dict], v: Video, transcript: Optional[Transcript], source: str) -> list[dict]:
    """keyword가 자막/제목/설명에 없으면 버린다. 영상 직접 분석은 대조할 원문이 없어 verified=False로만 표시."""
    pool = _squash((transcript.full_text if transcript else "") + v.title + v.description)
    out = []
    for e in ev:
        kw = (e.get("keyword") or "").strip()
        quote = (e.get("quote") or "").strip()
        if not kw or not quote:
            continue
        verified = _squash(kw) in pool if source != "video" else False
        if source != "video" and not verified:
            continue
        out.append({"time": (e.get("time") or "").strip(), "quote": quote[:80], "keyword": kw,
                    "verified": verified})
    return out


def analyze(v: Video, cfg: Config, llm: LLM, comments: list[dict],
            transcript_fetcher: Callable = fetch_transcript) -> dict:
    transcript, note = transcript_fetcher(v.id)
    use_video = transcript is None and cfg.ai.use_video_input and llm.supports_video
    source = "transcript" if transcript else ("video" if use_video else "metadata")

    def run(src: str, with_video: bool) -> dict:
        return llm.generate_json(
            _analysis_prompt(v, transcript, comments, with_video), ANALYSIS_SCHEMA,
            model=cfg.ai.analysis_model, temperature=0.3, system=ANALYST_SYSTEM,
            video_url=v.url if with_video else None)

    try:
        data = run(source, use_video)
    except LLMError as e:
        if use_video and e.code in ("VIDEO_UNSUPPORTED", "BAD_RESPONSE"):
            source, use_video = "metadata", False
            note += " · 영상 직접 분석 실패 → 제목·설명 사용"
            data = run(source, False)
        else:
            raise
    evidence = _verify_evidence(data.get("evidence", []), v, transcript, source)
    if not evidence:   # 전부 걸러졌으면 제목에서 하나 뽑아 씀
        evidence = [{"time": "", "quote": v.title[:80], "keyword": _title_keyword(v.title), "verified": True}]
        note += " · 근거 확인 실패 → 제목 사용"
    source_note = {"transcript": note, "video": f"{note} → 영상 직접 분석",
                   "metadata": f"{note} → 제목·설명 사용"}[source]
    return {"concern": data.get("concern", "").strip(), "summary": data.get("summary", "").strip(),
            "evidence": evidence, "viewer_mood": data.get("viewer_mood", "").strip(),
            "source": source, "source_note": source_note}


def _title_keyword(title: str) -> str:
    words = [w for w in re.split(r"[\s\[\]()|,.!?~\"'“”‘’]+", title) if 2 <= len(w) <= 12]
    return max(words, key=len) if words else title[:12]


# 작성 + 검사
def _draft_prompt(v: Video, a: dict, cfg: Config, recent: list[str], feedback: list[str]) -> str:
    ev = "\n".join(f"- [{e['time'] or '-'}] “{e['quote']}” (핵심 표현: {e['keyword']})" for e in a["evidence"])
    rules = [
        f"길이: 공백 포함 {cfg.min_chars}~{cfg.max_chars}자{' (광고 표시 포함)' if cfg.disclosure else ''}. "
        f"목표는 {cfg.min_chars + 25}~{cfg.max_chars - 10}자.",
        "위 근거 중 하나를 골라 그 '핵심 표현'을 댓글에 글자 그대로 넣으세요. 다른 영상에는 쓸 수 없는, 이 영상만의 댓글이어야 합니다.",
        "영상 속 고민에 공감하고, 자기 연애 고민을 한두 마디 덧붙이세요.",
        f"마지막에 '{cfg.brand}' 연애사주를 '한번 보고 싶다', '궁금해진다' 정도로 가볍게 언급하세요.",
        f"'{cfg.brand}'을(를) 이미 이용해 본 것처럼 쓰지 마세요 (예: 봤는데, 받아봤다, 다녀왔다, 맞더라). 경험담·효과를 지어내지 마세요.",
        "링크, 해시태그(#), 이모지 남발, 과장 광고 문구, 다른 업체 언급 금지.",
    ]
    if cfg.disclosure:
        rules.append(f"댓글 맨 끝에 광고 표시 '{cfg.disclosure}'를 그대로 붙이세요.")
    parts = [
        f"[영상] {v.title} — {v.channel}",
        f"[영상 속 고민] {a['concern']}",
        f"[요약] {a['summary']}",
        f"[근거]\n{ev}",
        f"[시청자 반응] {a['viewer_mood'] or '-'}",
        "[규칙]\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)),
    ]
    if recent:
        parts.append("[최근 게시한 댓글 — 표현·구조가 겹치지 않게]\n" + "\n".join(f"- {t}" for t in recent[:5]))
    if feedback:
        parts.append("[이전 초안의 문제 — 고쳐서 다시 쓰세요]\n" + "\n".join(f"- {f}" for f in feedback))
    return "\n\n".join(parts)


def check_draft(text: str, evidence: list[dict], cfg: Config, store: Store) -> list[dict]:
    checks = precheck(text, cfg, store)
    if evidence and not any(_squash(e["keyword"]) in _squash(text) for e in evidence):
        checks.append({"code": "NO_EVIDENCE", "message": "영상 속 근거 표현이 댓글에 들어 있지 않습니다."})
    return checks


def _tidy(text: str, cfg: Config) -> str:
    t = normalize(text).strip("\"'“”‘’ ")
    if cfg.disclosure and cfg.disclosure not in t:
        t = f"{t} {cfg.disclosure}"
    return t


def write_draft(v: Video, a: dict, cfg: Config, llm: LLM, store: Store) -> tuple[str, str, list[dict], int, list[dict]]:
    recent = store.posted_texts(5)
    feedback: list[str] = []
    history: list[dict] = []
    text, used, checks = "", "", []
    for attempt in range(1, max(cfg.ai.max_attempts, 1) + 1):
        out = llm.generate_json(_draft_prompt(v, a, cfg, recent, feedback), DRAFT_SCHEMA,
                                model=cfg.ai.writer_model, temperature=cfg.ai.temperature, system=WRITER_SYSTEM)
        text = _tidy(out.get("comment", ""), cfg)
        used = (out.get("used_keyword") or "").strip()
        checks = check_draft(text, a["evidence"], cfg, store)
        if not checks:
            return text, used, checks, attempt, history
        history.append({"comment": text, "problems": [c["message"] for c in checks]})
        feedback = [c["message"] for c in checks]
        if any(c["code"] == "LENGTH" for c in checks):
            feedback.append(f"방금 초안은 {len(text)}자였습니다. {cfg.min_chars}~{cfg.max_chars}자로 맞추세요.")
    return text, used, checks, cfg.ai.max_attempts, history


def make_draft(v: Video, cfg: Config, llm: LLM, store: Store, comments: list[dict],
               transcript_fetcher: Callable = fetch_transcript) -> DraftResult:
    a = analyze(v, cfg, llm, comments, transcript_fetcher)
    text, used, checks, attempts, history = write_draft(v, a, cfg, llm, store)
    r = DraftResult(video=v.to_dict(), concern=a["concern"], summary=a["summary"], evidence=a["evidence"],
                    viewer_mood=a["viewer_mood"], source=a["source"], source_note=a["source_note"],
                    comment=text, used_keyword=used, checks=checks, ok=not checks, attempts=attempts,
                    model=f"{llm.name}: {cfg.ai.analysis_model} / {cfg.ai.writer_model}",
                    created_at=datetime.now().isoformat(timespec="seconds"), history=history)
    store.save_draft(v.id, r.to_dict())
    return r

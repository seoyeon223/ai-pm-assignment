"""자막 가져오기 (youtube-transcript-api 1.x). 우선순위: 한국어 > 자동 생성 한국어 > 번역 > 아무 언어"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Transcript:
    language: str
    is_generated: bool
    lines: list[tuple[float, str]]   # (시작 초, 문장)

    @property
    def full_text(self) -> str:
        return " ".join(t for _, t in self.lines)

    def for_prompt(self, max_chars: int = 12000) -> str:
        """[mm:ss] 문장 형식. 너무 길면 k줄마다 하나씩 샘플링."""
        rows = [f"[{int(s // 60):02d}:{int(s % 60):02d}] {t}" for s, t in self.lines if t.strip()]
        text = "\n".join(rows)
        if len(text) <= max_chars:
            return text
        k = -(-len(text) // max_chars)   # ceil
        return "\n".join(rows[::k])[:max_chars]


def fetch_transcript(video_id: str, prefer: tuple[str, ...] = ("ko",)) -> tuple[Optional[Transcript], str]:
    """(자막, 상태 메시지) 반환. 실패하면 (None, 사유)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, "youtube-transcript-api가 설치되지 않았습니다."
    try:
        api = YouTubeTranscriptApi()
        tl = api.list(video_id)
        chosen = None
        try:
            chosen = tl.find_transcript(list(prefer))  # 수동 자막이 있으면 그걸 먼저 줌
        except Exception:
            items = list(tl)
            for t in items:
                if t.is_translatable:
                    try:
                        chosen = t.translate(prefer[0])
                        break
                    except Exception:
                        continue
            if chosen is None and items:
                chosen = items[0]
        if chosen is None:
            return None, "자막 없음"
        fetched = chosen.fetch()
        lines = [(float(s.start), s.text.replace("\n", " ").strip()) for s in fetched]
        if not lines:
            return None, "자막 없음"
        return Transcript(chosen.language_code, bool(chosen.is_generated), lines), \
            f"자막({chosen.language_code}{', 자동 생성' if chosen.is_generated else ''})"
    except Exception as e:  # TranscriptsDisabled, NoTranscriptFound, RequestBlocked(IpBlocked) 등
        name = type(e).__name__
        reason = {"TranscriptsDisabled": "자막 사용 안 함", "NoTranscriptFound": "자막 없음",
                  "RequestBlocked": "유튜브가 자막 요청을 막음", "IpBlocked": "유튜브가 자막 요청을 막음",
                  "VideoUnavailable": "영상을 열 수 없음"}.get(name, f"자막 가져오기 실패({name})")
        return None, reason

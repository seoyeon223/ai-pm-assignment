"""유튜브 링크 -> 영상 ID (watch, youtu.be, shorts, embed, live, ID 그대로)"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(value: str) -> str:
    value = (value or "").strip()
    if _ID_RE.match(value):
        return value
    if not re.match(r"^https?://", value):
        value = "https://" + value
    u = urlparse(value)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    candidate = None
    if host == "youtu.be":
        candidate = u.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        if u.path == "/watch":
            candidate = parse_qs(u.query).get("v", [None])[0]
        else:
            m = re.match(r"^/(shorts|embed|live|v)/([^/?#]+)", u.path)
            if m:
                candidate = m.group(2)
    if candidate and _ID_RE.match(candidate):
        return candidate
    raise ValueError(f"유튜브 영상 링크를 인식하지 못했습니다: {value}")

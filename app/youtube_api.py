"""YouTube Data API v3 클라이언트.

할당량이 하루 10,000 유닛이고 search.list가 100 유닛이라 응답을 SQLite에 캐시한다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import Config

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    MESSAGES = {
        "NO_KEY": "YouTube API 키가 없습니다. .env에 YOUTUBE_API_KEY를 넣어 주세요.",
        "QUOTA": "오늘 YouTube API 사용량을 모두 썼습니다 (태평양 시간 자정, 한국 시간 오후 4~5시에 초기화).",
        "KEY_INVALID": "YouTube API 키가 올바르지 않거나 YouTube Data API v3가 사용 설정되지 않았습니다.",
        "NOT_FOUND": "영상을 찾을 수 없습니다 (비공개·삭제 가능성).",
        "NETWORK": "YouTube API에 연결하지 못했습니다.",
        "API": "YouTube API 오류",
    }

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{self.MESSAGES.get(code, code)} {detail}".strip())


@dataclass
class Video:
    id: str
    title: str
    channel: str
    channel_id: str
    published_at: str           # ISO (UTC)
    thumbnail: str
    description: str
    views: int
    likes: int
    comment_count: int
    duration: str               # ISO 8601 (PT5M3S)
    hours_since: float
    views_per_hour: float
    rising: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["url"] = self.url
        return d


class Cache:
    def __init__(self, db_path: Path | str):
        self._conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("CREATE TABLE IF NOT EXISTS api_cache (k TEXT PRIMARY KEY, v TEXT NOT NULL, exp REAL NOT NULL)")
        self._lock = threading.Lock()

    def get(self, k: str) -> Optional[Any]:
        with self._lock:
            row = self._conn.execute("SELECT v, exp FROM api_cache WHERE k = ?", (k,)).fetchone()
        if row and row[1] > time.time():
            return json.loads(row[0])
        return None

    def set(self, k: str, v: Any, ttl_s: float) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO api_cache (k, v, exp) VALUES (?, ?, ?)",
                               (k, json.dumps(v, ensure_ascii=False), time.time() + ttl_s))

    def close(self) -> None:
        self._conn.close()


class YouTubeClient:
    def __init__(self, cfg: Config, api_key: Optional[str] = None, http: Optional[httpx.Client] = None,
                 cache: Optional[Cache] = None):
        self.cfg = cfg
        self.s = cfg.youtube
        self.key = api_key if api_key is not None else cfg.youtube_api_key
        self.http = http or httpx.Client(timeout=15)
        self.cache = cache or Cache(cfg.db_path)

    def _get(self, endpoint: str, params: dict[str, Any], ttl_s: float) -> dict:
        if not self.key:
            raise YouTubeError("NO_KEY")
        ck = endpoint + "?" + hashlib.sha1(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if ttl_s > 0 and (hit := self.cache.get(ck)) is not None:
            return hit
        try:
            r = self.http.get(f"{API}/{endpoint}", params={**params, "key": self.key})
        except httpx.HTTPError as e:
            raise YouTubeError("NETWORK", str(e)[:120]) from e
        if r.status_code != 200:
            reason, message = "", ""
            try:
                err = r.json().get("error", {})
                message = err.get("message", "")
                reason = (err.get("errors") or [{}])[0].get("reason", "")
            except ValueError:
                pass
            if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                raise YouTubeError("QUOTA")
            if reason in ("keyInvalid", "accessNotConfigured", "forbidden") or r.status_code in (400, 401) and "key" in message.lower():
                raise YouTubeError("KEY_INVALID", message[:120])
            if reason == "commentsDisabled":
                raise YouTubeError("COMMENTS_DISABLED", message[:120])
            if reason in ("videoNotFound", "notFound") or r.status_code == 404:
                raise YouTubeError("NOT_FOUND")
            raise YouTubeError("API", f"{r.status_code} {reason} {message[:120]}")
        data = r.json()
        if ttl_s > 0:
            self.cache.set(ck, data, ttl_s)
        return data

    def _to_video(self, item: dict, now: datetime) -> Video:
        sn, st, cd = item.get("snippet", {}), item.get("statistics", {}), item.get("contentDetails", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
        published = sn.get("publishedAt", "")
        try:
            pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
            hours = max((now - pub).total_seconds() / 3600, 0.1)
        except ValueError:
            hours = 0.0
        views = int(st.get("viewCount", 0) or 0)
        return Video(
            id=item["id"] if isinstance(item["id"], str) else item["id"].get("videoId"),
            title=sn.get("title", ""), channel=sn.get("channelTitle", ""), channel_id=sn.get("channelId", ""),
            published_at=published, thumbnail=thumb, description=sn.get("description", ""),
            views=views, likes=int(st.get("likeCount", 0) or 0), comment_count=int(st.get("commentCount", 0) or 0),
            duration=cd.get("duration", ""), hours_since=round(hours, 1),
            views_per_hour=round(views / hours, 1) if hours else 0.0,
        )

    def videos(self, ids: list[str], ttl_s: float = 300) -> list[Video]:
        """videos.list, 50개씩. 라이브/예정 방송 제외."""
        out: list[Video] = []
        now = datetime.now(timezone.utc)
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = self._get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk),
                                        "hl": self.s.language}, ttl_s)
            by_id = {it["id"]: it for it in data.get("items", [])
                     if it.get("snippet", {}).get("liveBroadcastContent", "none") == "none"}
            out += [self._to_video(by_id[v], now) for v in chunk if v in by_id]
        return out

    def video(self, video_id: str) -> Video:
        vs = self.videos([video_id])
        if not vs:
            raise YouTubeError("NOT_FOUND")
        return vs[0]

    def _search_ids(self, q: str, max_results: int, published_after: Optional[datetime] = None,
                    order: str = "date") -> list[str]:
        params: dict[str, Any] = {
            "part": "id", "type": "video", "q": q, "order": order, "maxResults": min(max_results, 50),
            "regionCode": self.s.region, "relevanceLanguage": self.s.language, "safeSearch": "moderate",
        }
        if published_after:
            params["publishedAfter"] = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._get("search", params, self.s.cache_minutes * 60)
        return [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]

    def latest(self, q: Optional[str] = None, n: Optional[int] = None) -> list[Video]:
        """검색어 기준 최신 영상"""
        ids = self._search_ids(q or self.s.search_query, n or self.s.max_results)
        vids = self.videos(ids)
        for v in vids:
            v.rising = self._is_rising(v)
        return vids

    def _is_rising(self, v: Video) -> bool:
        return (self.s.rising_min_views <= v.views <= self.s.rising_max_views
                and v.views_per_hour >= self.s.rising_min_per_hour)

    def rising(self, q: Optional[str] = None) -> list[Video]:
        """최근 N시간 내 영상 중 rising 조건에 맞는 것, 시간당 조회수 순."""
        after = datetime.now(timezone.utc) - timedelta(hours=self.s.rising_lookback_hours)
        ids = self._search_ids(q or self.s.search_query, 50, published_after=after)
        vids = [v for v in self.videos(ids) if self._is_rising(v)]
        for v in vids:
            v.rising = True
        return sorted(vids, key=lambda v: v.views_per_hour, reverse=True)

    def top_comments(self, video_id: str, n: Optional[int] = None) -> list[dict[str, Any]]:
        """좋아요 순 인기 댓글. 댓글 막힌 영상은 []."""
        try:
            data = self._get("commentThreads", {"part": "snippet", "videoId": video_id, "order": "relevance",
                                                "maxResults": min(n or self.s.top_comments, 100),
                                                "textFormat": "plainText"}, 600)
        except YouTubeError as e:
            if e.code in ("COMMENTS_DISABLED", "NOT_FOUND"):
                return []
            raise
        out = []
        for it in data.get("items", []):
            c = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            if c.get("textDisplay"):
                out.append({"text": c["textDisplay"][:300], "likes": int(c.get("likeCount", 0) or 0)})
        return sorted(out, key=lambda c: c["likes"], reverse=True)

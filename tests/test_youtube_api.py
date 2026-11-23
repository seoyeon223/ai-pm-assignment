"""YouTube API 클라이언트: 응답 변환, rising 필터, 에러 매핑, 캐시"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import load_config
from app.youtube_api import Cache, YouTubeClient, YouTubeError


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


VIDEOS = {
    "aaaaaaaaaaa": dict(views=3000, hours=10, live="none"),    # 시간당 300 → 뜨기 직전
    "bbbbbbbbbbb": dict(views=3000, hours=40, live="none"),    # 시간당 75 → 아님
    "ccccccccccc": dict(views=90000, hours=5, live="none"),    # 조회수 초과 → 아님
    "ddddddddddd": dict(views=100, hours=1, live="live"),      # 라이브 → 제외
}


def handler_factory(calls: list, mode: str = "ok"):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if mode == "quota":
            return httpx.Response(403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
        if mode == "badkey":
            return httpx.Response(400, json={"error": {"message": "API key not valid", "errors": [{"reason": "keyInvalid"}]}})
        p = req.url.path
        if p.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": k}} for k in VIDEOS]})
        if p.endswith("/videos"):
            ids = req.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [{
                "id": k,
                "snippet": {"title": f"t-{k}", "channelTitle": "ch", "channelId": "c", "publishedAt": iso(VIDEOS[k]["hours"]),
                            "description": "d", "liveBroadcastContent": VIDEOS[k]["live"],
                            "thumbnails": {"medium": {"url": "http://img"}}},
                "statistics": {"viewCount": str(VIDEOS[k]["views"]), "likeCount": "1", "commentCount": "2"},
                "contentDetails": {"duration": "PT5M"}} for k in ids if k in VIDEOS]})
        if p.endswith("/commentThreads"):
            if req.url.params["videoId"] == "bbbbbbbbbbb":
                return httpx.Response(403, json={"error": {"errors": [{"reason": "commentsDisabled"}]}})
            return httpx.Response(200, json={"items": [
                {"snippet": {"topLevelComment": {"snippet": {"textDisplay": "적은 공감", "likeCount": 1}}}},
                {"snippet": {"topLevelComment": {"snippet": {"textDisplay": "많은 공감", "likeCount": 50}}}}]})
        return httpx.Response(404)
    return handler


def client(tmp_path, calls, mode="ok", key="k"):
    cfg = load_config()
    cfg.db_path = tmp_path / "c.db"
    return YouTubeClient(cfg, api_key=key, http=httpx.Client(transport=httpx.MockTransport(handler_factory(calls, mode))),
                         cache=Cache(cfg.db_path))


def test_latest_maps_fields_and_excludes_live(tmp_path):
    vids = client(tmp_path, []).latest()
    assert [v.id for v in vids] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    a = vids[0]
    assert a.title == "t-aaaaaaaaaaa" and a.thumbnail == "http://img" and a.views == 3000
    assert 250 < a.views_per_hour < 350 and a.rising
    assert not vids[1].rising and not vids[2].rising


def test_rising_filter(tmp_path):
    assert [v.id for v in client(tmp_path, []).rising()] == ["aaaaaaaaaaa"]


def test_search_is_cached(tmp_path):
    calls: list = []
    yt = client(tmp_path, calls)
    yt.latest(); yt.latest()
    assert calls.count("/youtube/v3/search") == 1      # 두 번째는 캐시


def test_top_comments_sorted_and_disabled(tmp_path):
    yt = client(tmp_path, [])
    assert [c["text"] for c in yt.top_comments("aaaaaaaaaaa")] == ["많은 공감", "적은 공감"]
    assert yt.top_comments("bbbbbbbbbbb") == []


@pytest.mark.parametrize("mode,code", [("quota", "QUOTA"), ("badkey", "KEY_INVALID")])
def test_errors(tmp_path, mode, code):
    with pytest.raises(YouTubeError) as e:
        client(tmp_path, [], mode).latest()
    assert e.value.code == code


def test_no_key(tmp_path):
    with pytest.raises(YouTubeError) as e:
        client(tmp_path, [], key="").latest()
    assert e.value.code == "NO_KEY"


def test_video_not_found(tmp_path):
    with pytest.raises(YouTubeError) as e:
        client(tmp_path, []).video("zzzzzzzzzzz")
    assert e.value.code == "NOT_FOUND"

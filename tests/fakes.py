"""테스트용 fake YouTube 클라이언트 / LLM / 자막"""
from __future__ import annotations

from typing import Optional

from app.transcript import Transcript
from app.youtube_api import Video


def make_video(i: int, **kw) -> Video:
    base = dict(id=f"vid{i:08d}", title=f"남자가 연락을 줄이는 진짜 이유 {i}", channel="연애상담소",
                channel_id="UC1", published_at="2026-11-23T00:00:00Z",
                thumbnail="", description="연락 텀, 읽씹, 마음이 식는 신호를 정리했습니다.",
                views=2400, likes=100, comment_count=30, duration="PT9M", hours_since=10.0,
                views_per_hour=240.0, rising=True)
    base.update(kw)
    return Video(**base)


class FakeYT:
    def __init__(self, videos=None):
        self.vs = {v.id: v for v in (videos or [make_video(i) for i in range(1, 4)])}
        self.calls = []

    def latest(self, q=None, n=None):
        self.calls.append(("latest", q))
        return list(self.vs.values())

    def rising(self, q=None):
        self.calls.append(("rising", q))
        return [v for v in self.vs.values() if v.rising]

    def video(self, vid):
        from app.youtube_api import YouTubeError
        if vid not in self.vs:
            raise YouTubeError("NOT_FOUND")
        return self.vs[vid]

    def top_comments(self, vid, n=None):
        return [{"text": "읽씹 당하면 진짜 마음 식은 거 맞나요ㅠ", "likes": 120},
                {"text": "3분 얘기 너무 공감", "likes": 80}]


TRANSCRIPT = Transcript("ko", True, [
    (12.0, "오늘은 남자가 연락을 줄이는 이유를 얘기해 볼게요"),
    (95.0, "답장 텀이 길어지면 마음이 식은 거다 이렇게 단정하시면 안 돼요"),
    (190.0, "읽씹은 바빠서일 수도 있고 표현 방식의 차이일 수도 있어요"),
])


def fake_transcript(video_id):
    return TRANSCRIPT, "자막(ko, 자동 생성)"


def no_transcript(video_id):
    return None, "자막 없음"


class FakeLLM:
    """분석 요청엔 고정 결과, 작성 요청엔 drafts를 순서대로 반환"""
    name = "fake"
    supports_video = True

    def __init__(self, drafts: Optional[list[str]] = None, evidence: Optional[list[dict]] = None):
        self.drafts = list(drafts or [])
        self.evidence = evidence or [
            {"time": "01:35", "quote": "답장 텀이 길어지면 마음이 식은 거다", "keyword": "답장 텀"},
            {"time": "03:10", "quote": "읽씹은 바빠서일 수도 있고", "keyword": "읽씹"},
            {"time": "05:00", "quote": "연락은 의무가 아니라 성의", "keyword": "의무가 아니라 성의"},  # 자막에 없음 → 걸러져야 함
        ]
        self.prompts: list[str] = []
        self.video_urls: list[Optional[str]] = []
        self.n = 0

    def generate_json(self, prompt, schema, *, model, temperature, system="", video_url=None):
        self.prompts.append(prompt)
        self.video_urls.append(video_url)
        if "concern" in schema["properties"]:
            return {"concern": "답장이 늦어지면 마음이 식은 걸까?", "summary": "연락 텀과 읽씹의 의미를 다룸.",
                    "evidence": self.evidence, "viewer_mood": "읽씹 경험담에 공감하는 반응이 많음"}
        self.n += 1
        text = self.drafts.pop(0) if self.drafts else default_draft(self.n)
        return {"comment": text, "used_keyword": "답장 텀"}


def default_draft(n: int) -> str:
    variants = [
        "영상에서 답장 텀 길어지면 마음 식은 거라고 단정하지 말라는 말에 좀 위로됐어요. 저도 요즘 그 문제로 속앓이 중인데 운명원 연애사주로 인연 시기 한번 보고 싶네요 (협찬)",
        "읽씹이 바빠서일 수도 있다는 얘기 듣고 괜히 혼자 서운해했던 게 생각났어요ㅋㅋ 운명원 연애운 보면 이 사람이 맞는지 궁금해지네요 (협찬)",
        "답장 텀 때문에 매번 먼저 지치는 쪽이 저라서 너무 공감했어요. 다음 연애는 좀 편했으면, 운명원 사주로 궁합이라도 보고 싶다 (협찬)",
    ]
    return variants[(n - 1) % len(variants)]

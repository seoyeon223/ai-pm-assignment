"""내용 규칙, 영상당 1개, 하루 상한, 동시성, stale 복구, 전체 흐름"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import load_config
from app.rules import similarity, validate_comment
from app.service import assign_profile, run_post
from app.store import RuleViolation, Store

MOCK = (Path(__file__).parent / "mock_youtube.html").resolve().as_uri()

# 서로 다른 영상용 서로 다른 댓글 (40~120자, 브랜드·광고 표시 포함)
GOOD = [
    "영상에서 '답장 텀이 길어지면 마음이 식은 거다'라는 말 듣고 뜨끔했어요. 운명원 연애사주로 제 인연 시기도 궁금해지네요 (협찬)",
    "3번째 사연처럼 매번 먼저 연락하는 쪽이라 지치는 분들 많을 듯. 이럴 땐 운명원에서 궁합이라도 한번 보고 싶다 (협찬)",
    "재회는 타이밍이라는 마지막 조언이 제일 현실적이었음. 저는 언제쯤 그 타이밍이 오는지 운명원 사주로 알아보고 싶네요 (협찬)",
    "썸에서 고백 타이밍 놓친 얘기 너무 제 얘기 같아서 웃펐어요ㅋㅋ 운명원 연애운 보면 다음엔 안 놓칠 수 있으려나 (협찬)",
]


def cfg_for(tmp_path: Path, limit: int = 10, query: str = "", profiles=("유튜브1",)):
    cfg = load_config()
    cfg.db_path = tmp_path / "t.db"
    cfg.daily_max_comments = limit
    cfg.disclosure = "(협찬)"
    cfg.profiles_dir = tmp_path / "profiles"
    for p in profiles:
        (cfg.profiles_dir / p).mkdir(parents=True, exist_ok=True)
    cfg.screenshots_dir = tmp_path / "shots"
    cfg.watch_url_template = MOCK + "?v={video_id}" + query
    cfg.headless = True
    cfg.typing_min_ms = cfg.typing_max_ms = 1
    cfg.keep_open_seconds = 0
    cfg.timeout_ms = 8000
    cfg.verify_timeout_ms = 3000
    return cfg


def vid(i: int) -> str:
    return f"vid{i:08d}"  # 11자 ID


# 내용 규칙
def test_good_comments_pass(tmp_path):
    cfg = cfg_for(tmp_path)
    for t in GOOD:
        assert validate_comment(t, cfg) == [], t


@pytest.mark.parametrize("text,code", [
    ("너무 짧음 운명원 (협찬)", "LENGTH"),
    ("가" * 110 + " 운명원 (협찬) 끝", "LENGTH"),
    (GOOD[0].replace("(협찬)", "(협찬) https://a.kr"), "LINK"),
    (GOOD[0].replace("(협찬)", "(협찬) unmyeong.com"), "LINK"),
    (GOOD[0].replace("(협찬)", "(협찬) #연애사주"), "HASHTAG"),
    (GOOD[0].replace("운명원", "그곳"), "NO_BRAND"),
    (GOOD[0].replace(" (협찬)", "!!"), "NO_DISCLOSURE"),
    ("영상 보면서 공감 많이 했어요. 운명원에서 연애사주 봤는데 진짜 소름 돋게 맞더라고요 (협찬)", "FAKE_REVIEW"),
    ("영상 보면서 공감 많이 했어요. 운명원 다녀왔는데 제 연애 얘기를 다 맞췄어요 강추 (협찬)", "FAKE_REVIEW"),
])
def test_violations(tmp_path, text, code):
    cfg = cfg_for(tmp_path)
    assert code in [v.code for v in validate_comment(text, cfg)]


def test_viewer_phrases_outside_brand_sentence_are_ok(tmp_path):
    # 브랜드 없는 문장의 '영상 봤는데'는 허용
    cfg = cfg_for(tmp_path)
    t = "이 영상 봤는데 2분쯤 나온 '연락은 성의다' 말이 계속 생각나요. 운명원 사주로 제 인연도 궁금해지네요 (협찬)"
    assert validate_comment(t, cfg) == []


def test_similarity():
    assert similarity(GOOD[0], GOOD[0]) == 1.0
    assert similarity(GOOD[0], GOOD[1]) < 0.3


# 저장소 규칙
def test_one_comment_per_video(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=10)
    a = s.reserve(vid(1), "유튜브1", GOOD[0])
    with pytest.raises(RuleViolation) as e:
        s.reserve(vid(1), "유튜브2", GOOD[1])     # 진행 중에는 다른 계정도 불가
    assert e.value.code == "IN_PROGRESS"
    s.finish(a, True, comment_url="u")
    with pytest.raises(RuleViolation) as e:
        s.reserve(vid(1), "유튜브2", GOOD[1])     # 성공 후에도 불가
    assert e.value.code == "DUPLICATE_VIDEO"


def test_failure_releases_video(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=10)
    a = s.reserve(vid(1), "유튜브1", GOOD[0])
    s.finish(a, False, error_code="VERIFY_FAILED")
    b = s.reserve(vid(1), "유튜브1", GOOD[0])     # 실패한 영상은 다시 시도 가능
    assert b != a


def test_daily_limit_counts_success_and_in_progress(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=2)
    s.finish(s.reserve(vid(1), "유튜브1", "x"), True)
    s.reserve(vid(2), "유튜브2", "y")              # 진행 중도 상한에 포함
    with pytest.raises(RuleViolation) as e:
        s.reserve(vid(3), "유튜브3", "z")
    assert e.value.code == "DAILY_LIMIT"
    assert s.summary().used == 2 and s.summary().remaining == 0


def test_failed_do_not_count_toward_limit(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=1)
    s.finish(s.reserve(vid(1), "유튜브1", "x"), False)
    s.reserve(vid(2), "유튜브1", "y")


def test_profile_busy(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=10)
    s.reserve(vid(1), "유튜브1", "x")
    with pytest.raises(RuleViolation) as e:
        s.reserve(vid(2), "유튜브1", "y")
    assert e.value.code == "PROFILE_BUSY"


def test_blocked_attempts_are_logged(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=10)
    s.finish(s.reserve(vid(1), "유튜브1", "x"), True)
    with pytest.raises(RuleViolation):
        s.reserve(vid(1), "유튜브2", "y")
    log = s.log()
    assert [r["status"] for r in log] == ["blocked", "success"]   # 최신순
    assert log[0]["error_code"] == "DUPLICATE_VIDEO"
    assert s.summary().blocked == 1


def test_unique_index_is_last_line_of_defense(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=10)
    s.finish(s.reserve(vid(1), "유튜브1", "x"), True)
    raw = sqlite3.connect(tmp_path / "a.db")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO attempts (video_id, profile, text, status, day, created_at) "
                    "VALUES (?, '유튜브2', 'y', 'success', '2026-01-01', '2026-01-01T00:00:00')", (vid(1),))


def test_concurrent_clicks_cannot_exceed_limit(tmp_path):
    db = tmp_path / "a.db"
    Store(db, daily_limit=5).close()
    wins, errs = [], []

    def worker(i):
        st = Store(db, daily_limit=5)          # 연결을 여러 개 열어 동시 요청 흉내
        try:
            wins.append(st.reserve(vid(i), f"p{i}", "t"))
        except RuleViolation as e:
            errs.append(e.code)
        finally:
            st.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 5 and set(errs) == {"DAILY_LIMIT"}


def test_concurrent_same_video_only_one(tmp_path):
    db = tmp_path / "a.db"
    Store(db, daily_limit=50).close()
    wins = []

    def worker(i):
        st = Store(db, daily_limit=50)
        try:
            wins.append(st.reserve(vid(1), f"p{i}", "t"))
        except RuleViolation:
            pass
        finally:
            st.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1


def test_stale_in_progress_is_recovered(tmp_path):
    s = Store(tmp_path / "a.db", daily_limit=10, stale_minutes=15)
    a = s.reserve(vid(1), "유튜브1", "x")
    old = (s.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    s._conn.execute("UPDATE attempts SET created_at = ? WHERE id = ?", (old, a))
    s.reserve(vid(1), "유튜브1", "x")              # 자동 정리 후 다시 잡힘
    stale = [r for r in s.log() if r["id"] == a][0]
    assert stale["status"] == "failed" and stale["error_code"] == "STALE"


# 전체 흐름 (mock 페이지)
def run(cfg, store, video, text, profile="auto"):
    stages = []
    out = asyncio.run(run_post(video, text, cfg, store, profile=profile,
                               on_status=lambda s, l, e: stages.append(s)))
    return out, stages


def test_flow_success_then_duplicate_blocked_without_opening_browser(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store.from_config(cfg)
    out, stages = run(cfg, store, vid(1), GOOD[0], "유튜브1")
    assert out.ok and out.status == "success" and out.comment_url
    out2, stages2 = run(cfg, store, f"https://youtu.be/{vid(1)}", GOOD[1], "유튜브1")
    assert out2.status == "blocked" and out2.code == "DUPLICATE_VIDEO"
    assert "opening" not in stages2                 # 브라우저 실행 전에 차단
    log = store.log()
    assert log[0]["status"] == "blocked" and log[1]["status"] == "success"
    assert log[1]["last_stage"] == "done"


def test_flow_daily_limit(tmp_path):
    cfg = cfg_for(tmp_path, limit=1)
    store = Store.from_config(cfg)
    assert run(cfg, store, vid(1), GOOD[0])[0].ok
    out, stages = run(cfg, store, vid(2), GOOD[1])
    assert out.code == "DAILY_LIMIT" and "opening" not in stages


def test_flow_invalid_comment_blocked(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store.from_config(cfg)
    out, stages = run(cfg, store, vid(1), GOOD[0] + " #사주")
    assert out.code == "INVALID_COMMENT" and "opening" not in stages
    assert any(v["code"] == "HASHTAG" for v in out.violations)


def test_flow_too_similar_blocked(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store.from_config(cfg)
    assert run(cfg, store, vid(1), GOOD[0])[0].ok
    out, _ = run(cfg, store, vid(2), GOOD[0].replace("뜨끔했어요", "뜨끔했네요"))
    assert out.code == "TOO_SIMILAR"


def test_flow_failure_recorded_and_retry_allowed(tmp_path):
    cfg = cfg_for(tmp_path, query="&loggedout=1")
    store = Store.from_config(cfg)
    out, _ = run(cfg, store, vid(1), GOOD[0])
    assert out.status == "failed" and out.code == "NOT_LOGGED_IN" and Path(out.screenshot).exists()
    row = store.log()[0]
    assert row["status"] == "failed" and row["screenshot"] and row["last_stage"] == "login_check"
    cfg.watch_url_template = MOCK + "?v={video_id}"     # 로그인 후 재시도
    assert run(cfg, store, vid(1), GOOD[0])[0].ok


def test_flow_auto_assign_rotates_profiles(tmp_path):
    cfg = cfg_for(tmp_path, profiles=("유튜브1", "유튜브2", "유튜브3"))
    store = Store.from_config(cfg)
    used = [run(cfg, store, vid(i), GOOD[i])[0].profile for i in range(3)]
    assert used == ["유튜브1", "유튜브2", "유튜브3"]
    assert assign_profile(cfg, store) == "유튜브1"


def test_flow_unknown_profile(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store.from_config(cfg)
    out, _ = run(cfg, store, vid(1), GOOD[0], profile="유튜브9")
    assert out.code == "UNKNOWN_PROFILE"


def test_timezone_fallback_without_tzdata(monkeypatch):
    """tzdata 없어도 Asia/Seoul은 +9로 동작"""
    import app.store as st
    from zoneinfo import ZoneInfoNotFoundError

    def boom(name):
        raise ZoneInfoNotFoundError(name)
    monkeypatch.setattr(st, "ZoneInfo", boom)
    tz = st.load_tz("Asia/Seoul")
    from datetime import datetime, timedelta
    assert datetime.now(tz).utcoffset() == timedelta(hours=9)
    s = st.Store(":memory:", daily_limit=10)
    assert s.today()

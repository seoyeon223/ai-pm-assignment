"""SQLite 저장소. 게시 규칙을 DB 레벨에서 강제한다.

- 영상당 1개: status가 in_progress/success인 행에만 거는 partial UNIQUE 인덱스. 실패하면 재시도 가능.
- 하루 상한: BEGIN IMMEDIATE로 쓰기 잠금을 잡고 오늘 건수를 센 뒤 insert (동시 클릭 대비).
- 성공/실패/차단 모두 attempts에 기록.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT NOT NULL,
    video_title  TEXT,
    channel      TEXT,
    profile      TEXT,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('in_progress', 'success', 'failed', 'blocked')),
    error_code   TEXT,
    error        TEXT,
    last_stage   TEXT,
    screenshot   TEXT,
    comment_url  TEXT,
    day          TEXT NOT NULL,            -- 설정 시간대 기준 날짜 (YYYY-MM-DD)
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
-- 영상당 1개
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_comment_per_video
    ON attempts(video_id) WHERE status IN ('in_progress', 'success');
CREATE INDEX IF NOT EXISTS ix_attempts_day_status ON attempts(day, status);
CREATE INDEX IF NOT EXISTS ix_attempts_created ON attempts(created_at);
-- 영상별 최신 초안 캐시 (같은 영상 재선택 시 AI 호출 생략)
CREATE TABLE IF NOT EXISTS drafts (
    video_id    TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

RULE_MESSAGES = {
    "INVALID_URL": "유튜브 영상 링크를 인식하지 못했습니다.",
    "INVALID_COMMENT": "댓글 내용이 규칙에 맞지 않습니다.",
    "TOO_SIMILAR": "이전에 게시한 댓글과 너무 비슷합니다.",
    "DUPLICATE_VIDEO": "이 영상에는 이미 댓글을 달았습니다 (영상당 1개).",
    "IN_PROGRESS": "이 영상은 지금 다른 작업이 게시 중입니다.",
    "DAILY_LIMIT": "오늘 게시 상한에 도달했습니다.",
    "PROFILE_BUSY": "이 프로필은 지금 다른 영상에 게시 중입니다.",
    "UNKNOWN_PROFILE": "설정에 없는 프로필입니다.",
    "NO_PROFILE": "사용할 수 있는 프로필이 없습니다 (로그인한 프로필을 먼저 만들어 주세요).",
}


_FIXED_OFFSETS = {"Asia/Seoul": 9, "Asia/Tokyo": 9, "UTC": 0}


def load_tz(name: str):
    """tzdata가 없는 Windows 환경이면 고정 오프셋으로 대체."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name in _FIXED_OFFSETS:
            return timezone(timedelta(hours=_FIXED_OFFSETS[name]), name)
        print(f"[경고] 시간대 '{name}'을(를) 찾지 못해 컴퓨터의 현재 시간대를 씁니다. 'pip install tzdata'로 해결됩니다.")
        return datetime.now().astimezone().tzinfo


class RuleViolation(Exception):
    def __init__(self, code: str, detail: str = "", attempt_id: Optional[int] = None):
        self.code = code
        self.detail = detail
        self.attempt_id = attempt_id
        super().__init__(f"{code}: {RULE_MESSAGES.get(code, '')} {detail}".strip())

    @property
    def message(self) -> str:
        return f"{RULE_MESSAGES.get(self.code, '')} {self.detail}".strip()


@dataclass
class DailySummary:
    day: str
    used: int          # 성공 + 진행 중
    limit: int
    success: int
    failed: int
    blocked: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


class Store:
    def __init__(self, db_path: Path | str, daily_limit: int, tz: str = "Asia/Seoul", stale_minutes: int = 15):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.daily_limit = daily_limit
        self.tz = load_tz(tz)
        self.stale = timedelta(minutes=stale_minutes)
        self._lock = threading.Lock()  # 스레드 간 연결 공유용. 프로세스 간은 SQLite 잠금에 맡김
        self._conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)

    @classmethod
    def from_config(cls, cfg) -> "Store":
        return cls(cfg.db_path, cfg.daily_max_comments, cfg.timezone, cfg.stale_minutes)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def log_blocked(self, video_id: str, text: str, code: str, detail: str = "", profile: Optional[str] = None,
                    video_title: Optional[str] = None, channel: Optional[str] = None) -> int:
        now = self.now()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO attempts (video_id, video_title, channel, profile, text, status, error_code, error,
                                         day, created_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, 'blocked', ?, ?, ?, ?, ?)""",
                (video_id or "", video_title, channel, profile, text, code,
                 f"{RULE_MESSAGES.get(code, '')} {detail}".strip(),
                 now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
            )
            return int(cur.lastrowid)

    def reserve(self, video_id: str, profile: str, text: str,
                video_title: Optional[str] = None, channel: Optional[str] = None) -> int:
        """in_progress 행을 먼저 넣어 자리를 잡는다. 위반이면 차단 기록 후 RuleViolation."""
        self.recover_stale()
        now = self.now()
        day = now.strftime("%Y-%m-%d")
        code, detail = None, ""
        try:
            with self._tx() as c:
                row = c.execute(
                    "SELECT status, profile, created_at FROM attempts "
                    "WHERE video_id = ? AND status IN ('in_progress','success')", (video_id,)).fetchone()
                if row:
                    code = "DUPLICATE_VIDEO" if row["status"] == "success" else "IN_PROGRESS"
                    detail = f"({row['profile']}, {row['created_at'][:16].replace('T', ' ')})"
                    raise _Abort
                busy = c.execute("SELECT 1 FROM attempts WHERE profile = ? AND status = 'in_progress'",
                                 (profile,)).fetchone()
                if busy:
                    code = "PROFILE_BUSY"
                    raise _Abort
                used = c.execute("SELECT COUNT(*) FROM attempts WHERE day = ? AND status IN ('in_progress','success')",
                                 (day,)).fetchone()[0]
                if used >= self.daily_limit:
                    code, detail = "DAILY_LIMIT", f"({used}/{self.daily_limit})"
                    raise _Abort
                cur = c.execute(
                    """INSERT INTO attempts (video_id, video_title, channel, profile, text, status, last_stage,
                                             day, created_at)
                       VALUES (?, ?, ?, ?, ?, 'in_progress', 'reserved', ?, ?)""",
                    (video_id, video_title, channel, profile, text, day, now.isoformat(timespec="seconds")),
                )
                return int(cur.lastrowid)
        except _Abort:
            pass
        except sqlite3.IntegrityError:
            # 다른 프로세스가 같은 영상을 먼저 잡은 경우 (UNIQUE 인덱스에서 걸림)
            code = "DUPLICATE_VIDEO"
        aid = self.log_blocked(video_id, text, code, detail, profile, video_title, channel)
        raise RuleViolation(code, detail, aid)

    def update_stage(self, attempt_id: int, stage: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE attempts SET last_stage = ? WHERE id = ? AND status = 'in_progress'",
                               (stage, attempt_id))

    def finish(self, attempt_id: int, ok: bool, *, comment_url: Optional[str] = None,
               error_code: Optional[str] = None, error: Optional[str] = None,
               screenshot: Optional[str] = None, last_stage: Optional[str] = None) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE attempts SET status = ?, comment_url = ?, error_code = ?, error = ?, screenshot = ?,
                          last_stage = COALESCE(?, last_stage), finished_at = ?
                   WHERE id = ? AND status = 'in_progress'""",
                ("success" if ok else "failed", comment_url, error_code, error, screenshot, last_stage,
                 self.now().isoformat(timespec="seconds"), attempt_id),
            )

    def recover_stale(self) -> int:
        """게시 중 프로세스가 죽어 in_progress로 남은 행을 failed로 돌린다."""
        cutoff = (self.now() - self.stale).isoformat(timespec="seconds")
        with self._tx() as c:
            cur = c.execute(
                """UPDATE attempts SET status = 'failed', error_code = 'STALE',
                          error = '게시 도중 프로그램이 종료되어 결과를 확인하지 못했습니다. 유튜브에서 직접 확인해 주세요.',
                          finished_at = ?
                   WHERE status = 'in_progress' AND created_at < ?""",
                (self.now().isoformat(timespec="seconds"), cutoff),
            )
            return cur.rowcount

    def summary(self, day: Optional[str] = None) -> DailySummary:
        day = day or self.today()
        rows = dict(self._conn.execute(
            "SELECT status, COUNT(*) FROM attempts WHERE day = ? GROUP BY status", (day,)).fetchall())
        used = rows.get("success", 0) + rows.get("in_progress", 0)
        return DailySummary(day, used, self.daily_limit, rows.get("success", 0),
                            rows.get("failed", 0), rows.get("blocked", 0))

    def log(self, limit: int = 100, day: Optional[str] = None) -> list[dict[str, Any]]:
        """최신순"""
        q = "SELECT * FROM attempts"
        args: list[Any] = []
        if day:
            q += " WHERE day = ?"
            args.append(day)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def commented_video_ids(self) -> set[str]:
        """'댓글 완료' 배지 표시용"""
        return {r[0] for r in self._conn.execute(
            "SELECT video_id FROM attempts WHERE status IN ('in_progress','success')").fetchall()}

    def posted_texts(self, limit: int = 200) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT text FROM attempts WHERE status IN ('in_progress','success') ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()]

    def profile_counts_today(self) -> dict[str, int]:
        return dict(self._conn.execute(
            "SELECT profile, COUNT(*) FROM attempts WHERE day = ? AND status IN ('in_progress','success') "
            "GROUP BY profile", (self.today(),)).fetchall())

    def busy_profiles(self) -> set[str]:
        return {r[0] for r in self._conn.execute(
            "SELECT DISTINCT profile FROM attempts WHERE status = 'in_progress'").fetchall()}

    def save_draft(self, video_id: str, payload: dict) -> None:
        import json
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO drafts (video_id, payload, created_at) VALUES (?, ?, ?)",
                               (video_id, json.dumps(payload, ensure_ascii=False),
                                self.now().isoformat(timespec="seconds")))

    def get_draft(self, video_id: str) -> Optional[dict]:
        import json
        row = self._conn.execute("SELECT payload FROM drafts WHERE video_id = ?", (video_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def drafted_video_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT video_id FROM drafts").fetchall()}

    def close(self) -> None:
        self._conn.close()


class _Abort(Exception):
    pass

"""SQLite: 동의 상태만 저장한다. 음성 데이터는 절대 저장하지 않는다."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS consents (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    consented_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    requester_id INTEGER NOT NULL,
    name_hint    TEXT,
    duration     REAL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    created_at   TEXT    NOT NULL
);
"""


class ConsentStore:
    """동의 레코드(DB) + 수신 스레드용 메모리 캐시. 철회는 캐시를 먼저 갱신해 즉시 반영된다."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            rows = self._conn.execute("SELECT guild_id, user_id FROM consents").fetchall()
        self._cache: set[tuple[int, int]] = {(g, u) for g, u in rows}

    def is_consented(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            return (guild_id, user_id) in self._cache

    def consent(self, guild_id: int, user_id: int) -> bool:
        """신규 동의면 True, 이미 동의 상태면 False."""
        with self._lock:
            if (guild_id, user_id) in self._cache:
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO consents VALUES (?, ?, ?)",
                (guild_id, user_id, datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
            self._cache.add((guild_id, user_id))
            return True

    def withdraw(self, guild_id: int, user_id: int) -> bool:
        """동의가 있었으면 True. 캐시에서 먼저 제거하여 이후 프레임은 즉시 거부된다."""
        with self._lock:
            had = (guild_id, user_id) in self._cache
            self._cache.discard((guild_id, user_id))
            self._conn.execute(
                "DELETE FROM consents WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            self._conn.commit()
            return had

    def consented_users(self, guild_id: int) -> set[int]:
        with self._lock:
            return {u for g, u in self._cache if g == guild_id}

    # ---- 사운드보드 건의 (파일은 디스크 data/proposals/{id}.mp3, 여기엔 메타데이터만)
    def add_proposal(self, guild_id: int, requester_id: int, name_hint: str | None, duration: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO proposals (guild_id, requester_id, name_hint, duration, created_at) VALUES (?,?,?,?,?)",
                (guild_id, requester_id, name_hint, duration, datetime.now(timezone.utc).isoformat()))
            self._conn.commit()
            return int(cur.lastrowid)

    def get_proposal(self, pid: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, guild_id, requester_id, name_hint, duration, status FROM proposals WHERE id=?",
                (pid,)).fetchone()
        if row is None:
            return None
        keys = ("id", "guild_id", "requester_id", "name_hint", "duration", "status")
        return dict(zip(keys, row))

    def set_proposal_status(self, pid: int, new: str, expect: str = "pending") -> bool:
        """현재 상태가 expect 일 때만 바꾼다 (중복 클릭 방지). 바뀌면 True."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE proposals SET status=? WHERE id=? AND status=?", (new, pid, expect))
            self._conn.commit()
            return cur.rowcount == 1

    def count_pending(self, guild_id: int, requester_id: int | None = None) -> int:
        q, args = "SELECT COUNT(*) FROM proposals WHERE guild_id=? AND status IN ('pending','processing')", [guild_id]
        if requester_id is not None:
            q += " AND requester_id=?"
            args.append(requester_id)
        with self._lock:
            return int(self._conn.execute(q, args).fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

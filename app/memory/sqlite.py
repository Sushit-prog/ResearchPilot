from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.errors import MemoryStoreError

_CREATE_RUNS = (
    "CREATE TABLE IF NOT EXISTS runs ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "query TEXT NOT NULL, "
    "normalized_goal TEXT NOT NULL, "
    "created_at TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "report_path TEXT, "
    "source_urls TEXT NOT NULL DEFAULT '[]', "
    "evidence_count INTEGER NOT NULL DEFAULT 0, "
    "duration_s REAL NOT NULL DEFAULT 0.0)"
)
_CREATE_GOAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_normalized_goal ON runs(normalized_goal)"
)
_SELECT_COLUMNS = (
    "id, query, normalized_goal, created_at, status, report_path, source_urls, "
    "evidence_count, duration_s"
)


class RunRecord(BaseModel):
    id: int
    query: str
    normalized_goal: str
    created_at: datetime
    status: str
    report_path: str | None
    source_urls: list[str]
    evidence_count: int
    duration_s: float


def format_age(created_at: datetime, now: datetime) -> str:
    """Age string shown whenever an old run is surfaced (§17: never current, always aged)."""
    delta = now - created_at
    seconds = delta.total_seconds()
    if seconds < 0:
        return "just now"
    if created_at.date() == now.date():
        return "earlier today"
    if seconds < 86_400:
        hours = max(1, int(seconds // 3600))
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = delta.days
    return f"{days} day{'s' if days != 1 else ''} ago"


class MemoryStore:
    """§17 run history: one table, stdlib sqlite3, single-process (no file locking)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def insert_run(
        self,
        *,
        query: str,
        normalized_goal: str,
        created_at: datetime,
        status: str,
        report_path: str | None,
        source_urls: list[str],
        evidence_count: int,
        duration_s: float,
    ) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO runs (query, normalized_goal, created_at, status, "
                "report_path, source_urls, evidence_count, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    query,
                    normalized_goal,
                    created_at.isoformat(),
                    status,
                    report_path,
                    json.dumps(list(source_urls)),
                    evidence_count,
                    float(duration_s),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise MemoryStoreError(f"memory insert failed: {exc}") from exc
        finally:
            conn.close()

    def lookup(self, normalized_goal: str) -> RunRecord | None:
        """Latest row for a normalized goal. Read-only: a missing file stays missing
        (the schema bootstrap below only runs when the file already exists)."""
        if not self._path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM runs WHERE normalized_goal = ? "
                "ORDER BY id DESC LIMIT 1",
                (normalized_goal,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MemoryStoreError(f"memory lookup failed: {exc}") from exc
        finally:
            conn.close()
        if row is None:
            return None
        return self._to_record(row)

    def _connect(self) -> sqlite3.Connection:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path)
        except (OSError, sqlite3.Error) as exc:
            raise MemoryStoreError(
                f"memory store unavailable at {self._path}: {exc}"
            ) from exc
        try:
            conn.execute(_CREATE_RUNS)
            conn.execute(_CREATE_GOAL_INDEX)
        except sqlite3.Error as exc:
            conn.close()
            raise MemoryStoreError(
                f"memory store unavailable at {self._path}: {exc}"
            ) from exc
        return conn

    @staticmethod
    def _to_record(row: tuple) -> RunRecord:
        try:
            created_at = datetime.fromisoformat(str(row[3]))
            source_urls = json.loads(row[6])
        except (ValueError, json.JSONDecodeError) as exc:
            raise MemoryStoreError(f"corrupt memory row: {exc}") from exc
        if not isinstance(source_urls, list):
            raise MemoryStoreError("corrupt memory row: source_urls is not a list")
        return RunRecord(
            id=int(row[0]),
            query=str(row[1]),
            normalized_goal=str(row[2]),
            created_at=created_at,
            status=str(row[4]),
            report_path=None if row[5] is None else str(row[5]),
            source_urls=[str(url) for url in source_urls],
            evidence_count=int(row[7]),
            duration_s=float(row[8]),
        )

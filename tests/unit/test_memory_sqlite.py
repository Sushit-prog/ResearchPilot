from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.errors import MemoryStoreError
from app.memory.sqlite import MemoryStore, format_age

NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


def _insert(
    store: MemoryStore,
    *,
    goal: str = "What is X?",
    status: str = "completed",
    created_at: datetime = NOW,
    report_path: str | None = "reports/what-is-x.md",
) -> int:
    return store.insert_run(
        query="  What   is X?  ",
        normalized_goal=goal,
        created_at=created_at,
        status=status,
        report_path=report_path,
        source_urls=["https://example.com/a", "https://example.org/b"],
        evidence_count=3,
        duration_s=1.5,
    )


def test_insert_and_lookup_round_trip(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem" / "memory.db")
    row_id = _insert(store)

    record = store.lookup("What is X?")

    assert record is not None
    assert record.id == row_id
    assert record.query == "  What   is X?  "
    assert record.normalized_goal == "What is X?"
    assert record.created_at == NOW
    assert record.status == "completed"
    assert record.report_path == "reports/what-is-x.md"
    assert record.source_urls == ["https://example.com/a", "https://example.org/b"]
    assert record.evidence_count == 3
    assert record.duration_s == 1.5
    assert store.lookup("a different goal") is None


def test_insert_creates_missing_directories(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "memory.db"
    store = MemoryStore(path)

    _insert(store)

    assert path.exists()
    assert store.lookup("What is X?") is not None


def test_lookup_on_missing_file_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"

    assert MemoryStore(path).lookup("What is X?") is None
    assert not path.exists()


def test_lookup_returns_most_recent_row(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    _insert(store, created_at=NOW - timedelta(days=5), report_path="reports/old.md")
    _insert(store, report_path="reports/new.md")

    record = store.lookup("What is X?")

    assert record is not None
    assert record.report_path == "reports/new.md"


def test_failed_run_rows_are_stored_and_returned(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    _insert(store, status="failed", report_path=None)

    record = store.lookup("What is X?")

    assert record is not None
    assert record.status == "failed"
    assert record.report_path is None


def test_corrupt_database_raises_memory_store_error(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    path.write_bytes(b"definitely not a sqlite database")
    store = MemoryStore(path)

    with pytest.raises(MemoryStoreError, match="memory"):
        _insert(store)
    with pytest.raises(MemoryStoreError, match="memory"):
        store.lookup("What is X?")


def test_format_age_same_day_and_day_boundaries() -> None:
    assert format_age(NOW, NOW) == "earlier today"
    assert format_age(NOW - timedelta(hours=5), NOW) == "earlier today"
    assert format_age(NOW - timedelta(days=1), NOW) == "1 day ago"
    assert format_age(NOW - timedelta(days=2), NOW) == "2 days ago"


def test_format_age_crosses_midnight_as_hours() -> None:
    created = datetime(2026, 1, 9, 23, 0, 0, tzinfo=timezone.utc)
    assert format_age(created, NOW) == "13 hours ago"
    created = datetime(2026, 1, 10, 11, 45, 0, tzinfo=timezone.utc)
    assert format_age(created, NOW) == "earlier today"


def test_format_age_future_timestamp_clamps_to_just_now() -> None:
    assert format_age(NOW + timedelta(hours=1), NOW) == "just now"

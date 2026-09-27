from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent.orchestrator import Orchestrator
from app.config import Config
from app.memory.sqlite import MemoryStore
from app.models.events import EventKind

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

CITATIONS = [
    ("ev-a", "https://example.com/a"),
    ("ev-b", "https://example.org/b"),
    ("ev-c", None),
]


def _orchestrator(
    goal: str,
    llm,
    registry,
    handler,
    *,
    tmp_path: Path,
    clock,
    memory_path: Path,
    memory_enabled: bool = True,
) -> Orchestrator:
    return Orchestrator(
        goal,
        llm=llm,
        registry=registry,
        handler=handler,
        config=Config(
            report_dir=str(tmp_path),
            memory_path=str(memory_path),
            memory_enabled=memory_enabled,
        ),
        clock=clock,
    )


def _row_count(memory_path: Path) -> int:
    conn = sqlite3.connect(memory_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
    finally:
        conn.close()


def _seed(store: MemoryStore, *, created_at: datetime) -> None:
    store.insert_run(
        query="What is X?",
        normalized_goal="What is X?",
        created_at=created_at,
        status="completed",
        report_path="reports/old.md",
        source_urls=["https://example.com/old"],
        evidence_count=2,
        duration_s=4.0,
    )


async def test_full_run_writes_memory_row(
    fake_llm, registry, fixed_clock, valid_plan_json, synthesis_json, evidence_handler, tmp_path
) -> None:
    memory_path = tmp_path / "memory.db"
    llm = fake_llm(valid_plan_json, synthesis_json(CITATIONS))
    orchestrator = _orchestrator(
        "  What   is X?  ",
        llm,
        registry,
        evidence_handler,
        tmp_path=tmp_path,
        clock=fixed_clock,
        memory_path=memory_path,
    )

    state = await orchestrator.run()

    assert state.status.value == "completed"
    record = MemoryStore(memory_path).lookup("What is X?")
    assert record is not None
    assert record.query == "  What   is X?  "
    assert record.status == "completed"
    assert record.report_path == state.report_path
    assert record.evidence_count == 3
    assert record.source_urls == ["https://example.com/a", "https://example.org/b"]
    assert record.created_at == FIXED_TIME
    assert record.duration_s == 0.0


async def test_second_run_surfaces_cached_row_with_age(
    capsys,
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    synthesis_json,
    evidence_handler,
    tmp_path,
) -> None:
    memory_path = tmp_path / "memory.db"
    _seed(MemoryStore(memory_path), created_at=FIXED_TIME - timedelta(days=2))
    llm = fake_llm(valid_plan_json, synthesis_json(CITATIONS))
    orchestrator = _orchestrator(
        "What is X?",
        llm,
        registry,
        evidence_handler,
        tmp_path=tmp_path,
        clock=fixed_clock,
        memory_path=memory_path,
    )

    state = await orchestrator.run()

    assert state.status.value == "completed"
    kinds = [event.event for event in state.execution_events]
    assert kinds[:3] == [
        EventKind.GOAL_NORMALIZED,
        EventKind.MEMORY_LOOKUP,
        EventKind.PLAN_CREATED,
    ]
    lookup_event = state.execution_events[1]
    assert lookup_event.message == "cached from 2 days ago — may be stale"
    assert lookup_event.data["report_path"] == "reports/old.md"
    console = capsys.readouterr().out
    assert (
        "[MEMORY] cached from 2 days ago — may be stale "
        "(status: completed, report: reports/old.md)" in console
    )
    assert state.report_path is not None
    assert Path(state.report_path).exists()
    fresh = MemoryStore(memory_path).lookup("What is X?")
    assert fresh is not None
    assert fresh.report_path == state.report_path
    assert _row_count(memory_path) == 2


async def test_no_memory_flag_skips_read_and_write(
    capsys,
    fake_llm,
    registry,
    fixed_clock,
    valid_plan_json,
    synthesis_json,
    evidence_handler,
    tmp_path,
) -> None:
    memory_path = tmp_path / "memory.db"
    _seed(MemoryStore(memory_path), created_at=FIXED_TIME - timedelta(days=2))
    llm = fake_llm(valid_plan_json, synthesis_json(CITATIONS))
    orchestrator = _orchestrator(
        "What is X?",
        llm,
        registry,
        evidence_handler,
        tmp_path=tmp_path,
        clock=fixed_clock,
        memory_path=memory_path,
        memory_enabled=False,
    )

    state = await orchestrator.run()

    assert state.status.value == "completed"
    kinds = [event.event for event in state.execution_events]
    assert EventKind.MEMORY_LOOKUP not in kinds
    assert "[MEMORY]" not in capsys.readouterr().out
    assert _row_count(memory_path) == 1


async def test_no_memory_flag_never_creates_missing_db(
    fake_llm, registry, fixed_clock, valid_plan_json, synthesis_json, evidence_handler, tmp_path
) -> None:
    memory_path = tmp_path / "never" / "memory.db"
    llm = fake_llm(valid_plan_json, synthesis_json(CITATIONS))
    orchestrator = _orchestrator(
        "What is X?",
        llm,
        registry,
        evidence_handler,
        tmp_path=tmp_path,
        clock=fixed_clock,
        memory_path=memory_path,
        memory_enabled=False,
    )

    state = await orchestrator.run()

    assert state.status.value == "completed"
    assert not memory_path.exists()


async def test_failed_run_records_row_without_report(
    fake_llm, registry, fixed_clock, valid_plan_json, evidence_handler, tmp_path
) -> None:
    memory_path = tmp_path / "memory.db"
    llm = fake_llm(valid_plan_json, "definitely not json", '{"missing_sections": true}')
    orchestrator = _orchestrator(
        "What is X?",
        llm,
        registry,
        evidence_handler,
        tmp_path=tmp_path,
        clock=fixed_clock,
        memory_path=memory_path,
    )

    state = await orchestrator.run()

    assert state.status.value == "failed"
    record = MemoryStore(memory_path).lookup("What is X?")
    assert record is not None
    assert record.status == "failed"
    assert record.report_path is None
    assert record.evidence_count == 3


async def test_corrupt_memory_db_warns_without_failing_run(
    fake_llm, registry, fixed_clock, valid_plan_json, synthesis_json, evidence_handler, tmp_path
) -> None:
    memory_path = tmp_path / "memory.db"
    memory_path.write_bytes(b"definitely not a sqlite database")
    llm = fake_llm(valid_plan_json, synthesis_json(CITATIONS))
    orchestrator = _orchestrator(
        "What is X?",
        llm,
        registry,
        evidence_handler,
        tmp_path=tmp_path,
        clock=fixed_clock,
        memory_path=memory_path,
    )

    state = await orchestrator.run()

    assert state.status.value == "completed"
    assert state.report_path is not None
    assert any("memory" in warning for warning in state.warnings)
    kinds = [event.event for event in state.execution_events]
    assert EventKind.MEMORY_LOOKUP not in kinds

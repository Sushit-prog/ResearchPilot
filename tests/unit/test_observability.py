from __future__ import annotations

from io import StringIO

from app.models.events import EventKind
from app.observability import ConsoleEventSink, EventEmitter, InMemoryEventSink


def _console(verbose: bool = False) -> tuple[ConsoleEventSink, StringIO]:
    buffer = StringIO()
    return ConsoleEventSink(verbose=verbose, stream=buffer), buffer


def test_emitter_stamps_clock_and_fans_out_to_all_sinks(fixed_clock) -> None:
    memory: list = []
    extra: list = []
    emitter = EventEmitter(
        fixed_clock,
        sinks=[InMemoryEventSink(memory), InMemoryEventSink(extra)],
    )
    event = emitter.emit(EventKind.GOAL_NORMALIZED, message="hello")
    assert event.timestamp == fixed_clock()
    assert memory == [event]
    assert extra == [event]
    assert event.data == {}
    assert event.tool is None
    assert event.attempt is None


def test_emitter_add_extends_sinks(fixed_clock) -> None:
    memory: list = []
    emitter = EventEmitter(fixed_clock)
    emitter.add(InMemoryEventSink(memory))
    emitter.emit(EventKind.RETRY_STARTED)
    assert len(memory) == 1


def test_renders_goal_line(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(EventKind.GOAL_NORMALIZED, message="What is X?")
    assert buffer.getvalue().strip() == "[GOAL] What is X?"


def test_renders_memory_lookup_with_age_status_and_report(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(
        EventKind.MEMORY_LOOKUP,
        message="cached from 2 days ago — may be stale",
        data={"status": "completed", "report_path": "reports/what-is-x.md"},
    )
    assert buffer.getvalue().strip() == (
        "[MEMORY] cached from 2 days ago — may be stale "
        "(status: completed, report: reports/what-is-x.md)"
    )


def test_renders_memory_lookup_without_report(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(
        EventKind.MEMORY_LOOKUP,
        message="cached from earlier today — may be stale",
        data={"status": "failed", "report_path": None},
    )
    assert buffer.getvalue().strip() == (
        "[MEMORY] cached from earlier today — may be stale (status: failed, no report)"
    )


def test_renders_numbered_plan(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(
        EventKind.PLAN_CREATED,
        data={
            "steps": [
                {"id": "r1", "tool": "web_search", "objective": "search for X"},
                {"id": "q1", "tool": "calculator", "objective": "compute 2+2"},
            ]
        },
    )
    lines = buffer.getvalue().splitlines()
    assert lines[0] == "[PLAN] 2 steps:"
    assert lines[1] == "  1. r1 [web_search] search for X"
    assert lines[2] == "  2. q1 [calculator] compute 2+2"


def test_renders_plan_invalid_with_attempt(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(EventKind.PLAN_INVALID, attempt=2, message="duplicate step ids")
    assert buffer.getvalue().strip() == "[PLAN-INVALID] attempt 2: duplicate step ids"


def test_renders_retry_warn_and_recovered(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(EventKind.RETRY_STARTED, tool="web_search", attempt=2, message="timeout")
    emitter.emit(EventKind.TOOL_FAILED, tool="web_search", status="timeout", message="boom")
    emitter.emit(EventKind.TOOL_SUCCEEDED, tool="web_search", attempt=3)
    lines = buffer.getvalue().splitlines()
    assert lines[0] == "[RETRY] web_search attempt 2: timeout"
    assert lines[1] == "[WARN] web_search failed (timeout): boom"
    assert lines[2] == "[RECOVERED] web_search succeeded on attempt 3"


def test_renders_step_failed(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(EventKind.STEP_FAILED, step_id="r1", message="all candidates failed")
    assert buffer.getvalue().strip() == "[WARN] step r1 failed: all candidates failed"


def test_quiet_by_default_verbose_adds_detail(fixed_clock) -> None:
    quiet_sink, quiet_buffer = _console()
    verbose_sink, verbose_buffer = _console(verbose=True)
    for sink in (quiet_sink, verbose_sink):
        emitter = EventEmitter(fixed_clock, sinks=[sink])
        emitter.emit(EventKind.TOOL_STARTED, tool="web_search", step_id="r1")
        emitter.emit(EventKind.EVIDENCE_ADDED, step_id="r1", message="ev-a added")
    assert quiet_buffer.getvalue() == ""
    verbose_lines = verbose_buffer.getvalue().splitlines()
    assert verbose_lines[0] == "[tool_started] web_search step=r1"
    assert verbose_lines[1] == "[evidence_added] step=r1 ev-a added"


def test_successful_first_attempt_is_quiet_without_verbose(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(EventKind.TOOL_SUCCEEDED, tool="web_search", attempt=1)
    assert buffer.getvalue() == ""


def test_renders_synthesis_started(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(EventKind.SYNTHESIS_STARTED, message="What is X?", data={"evidence": 3})
    assert buffer.getvalue().strip() == (
        "[SYNTHESIS] composing report from 3 evidence items"
    )


def test_renders_report_generated(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(
        EventKind.REPORT_GENERATED,
        data={"path": "reports/what-is-x.md", "findings": 3, "evidence": 4},
    )
    assert buffer.getvalue().strip() == (
        "[REPORT] reports/what-is-x.md — 3 findings from 4 evidence items"
    )


def test_renders_run_completed(fixed_clock) -> None:
    sink, buffer = _console()
    emitter = EventEmitter(fixed_clock, sinks=[sink])
    emitter.emit(
        EventKind.RUN_COMPLETED,
        status="completed",
        data={
            "status": "completed",
            "steps": 2,
            "failures": 1,
            "retries": 2,
            "duration_s": 0.4,
        },
    )
    assert buffer.getvalue().strip() == (
        "[DONE] completed — 2 steps, 1 failures, 2 retries, 0.4s"
    )

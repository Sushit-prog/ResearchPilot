from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, Protocol, TextIO

from app.models.events import EventKind, ExecutionEvent

Clock = Callable[[], datetime]


class EventSink(Protocol):
    def emit(self, event: ExecutionEvent) -> None: ...


class EventEmitter:
    def __init__(self, clock: Clock, sinks: Iterable[EventSink] = ()) -> None:
        self._clock = clock
        self._sinks: list[EventSink] = list(sinks)

    def add(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def emit(
        self,
        kind: EventKind,
        *,
        tool: str | None = None,
        step_id: str | None = None,
        status: str | None = None,
        attempt: int | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> ExecutionEvent:
        event = ExecutionEvent(
            timestamp=self._clock(),
            event=kind,
            tool=tool,
            step_id=step_id,
            status=status,
            attempt=attempt,
            message=message,
            data=data if data is not None else {},
        )
        for sink in self._sinks:
            sink.emit(event)
        return event


class InMemoryEventSink:
    def __init__(self, target: list[ExecutionEvent]) -> None:
        self._target = target

    def emit(self, event: ExecutionEvent) -> None:
        self._target.append(event)


class ConsoleEventSink:
    def __init__(self, *, verbose: bool = False, stream: TextIO | None = None) -> None:
        self.verbose = verbose
        self._stream: TextIO = stream if stream is not None else sys.stdout

    def emit(self, event: ExecutionEvent) -> None:
        line = self._render(event)
        if line is not None:
            print(line, file=self._stream)

    def _render(self, event: ExecutionEvent) -> str | None:
        kind = event.event
        if kind is EventKind.GOAL_NORMALIZED:
            return f"[GOAL] {event.message or ''}"
        if kind is EventKind.MEMORY_LOOKUP:
            return self._render_memory(event)
        if kind is EventKind.PLAN_CREATED:
            return self._render_plan(event)
        if kind is EventKind.PLAN_INVALID:
            return f"[PLAN-INVALID] attempt {event.attempt}: {event.message}"
        if kind is EventKind.RETRY_STARTED:
            return f"[RETRY] {event.tool} attempt {event.attempt}: {event.message or ''}"
        if kind is EventKind.TOOL_FAILED:
            if event.data.get("unclassified"):
                error_type = event.data.get("error_type", "unknown")
                return (
                    f"[WARN] unexpected error type: {error_type} in {event.tool}"
                    f" ({event.status}): {event.message or ''}"
                )
            return f"[WARN] {event.tool} failed ({event.status}): {event.message or ''}"
        if kind is EventKind.TOOL_SUCCEEDED:
            if event.attempt is not None and event.attempt > 1:
                return f"[RECOVERED] {event.tool} succeeded on attempt {event.attempt}"
            return self._detail(event) if self.verbose else None
        if kind is EventKind.STEP_FAILED:
            return f"[WARN] step {event.step_id} failed: {event.message or ''}"
        if kind is EventKind.SYNTHESIS_STARTED:
            return (
                f"[SYNTHESIS] composing report from "
                f"{event.data.get('evidence', 0)} evidence items"
            )
        if kind is EventKind.REPORT_GENERATED:
            header = (
                f"[REPORT] {event.data.get('path', '')} — "
                f"{event.data.get('findings', 0)} findings from "
                f"{event.data.get('evidence', 0)} evidence items"
            )
            summary = [f"  {line}" for line in event.data.get("summary", [])]
            return "\n".join([header, *summary])
        if kind is EventKind.RUN_COMPLETED:
            return (
                f"[DONE] {event.data.get('status', event.status or '')} — "
                f"{event.data.get('steps', 0)} steps, "
                f"{event.data.get('failures', 0)} failed steps, "
                f"{event.data.get('retries', 0)} retries, "
                f"{event.data.get('duration_s', 0.0):.1f}s"
            )
        return self._detail(event) if self.verbose else None

    @staticmethod
    def _render_plan(event: ExecutionEvent) -> str:
        steps = event.data.get("steps", [])
        lines = [f"[PLAN] {len(steps)} steps:"]
        for index, step in enumerate(steps, start=1):
            lines.append(f"  {index}. {step['id']} [{step['tool']}] {step['objective']}")
        return "\n".join(lines)

    @staticmethod
    def _render_memory(event: ExecutionEvent) -> str:
        report_path = event.data.get("report_path")
        status = event.data.get("status", "unknown")
        detail = f"report: {report_path}" if report_path else "no report"
        return f"[MEMORY] {event.message or ''} (status: {status}, {detail})"

    def _detail(self, event: ExecutionEvent) -> str:
        parts = [f"[{event.event.value}]"]
        if event.tool is not None:
            parts.append(event.tool)
        if event.step_id is not None:
            parts.append(f"step={event.step_id}")
        if event.attempt is not None:
            parts.append(f"attempt={event.attempt}")
        if event.message:
            parts.append(event.message)
        return " ".join(parts)

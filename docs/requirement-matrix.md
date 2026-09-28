# ResearchPilot — Requirement Matrix

Assignment requirement-by-requirement status, filled at Phase 10 (final audit)
of `AGENTS.md` §28/§29. `AGENTS.md` itself is left untouched; this document
carries the evidence. Verified 2026-09-28: full suite `504 passed, 1
deselected`, ruff clean, all three transcripts re-captured on the same build.

| Requirement | Implemented | Evidence |
|---|---|---|
| High-level goal input | Yes | `--query` flag (`app/main.py:73`), goal echoed as `[GOAL]` (`app/observability.py:72`); first line of all three transcripts |
| Autonomous planning | Yes | `generate_plan` (`app/agent/planner.py:85`) → `parse_plan` + validation (`planner.py:75`) rejects unknown tools and placeholder arguments (`NO_PLACEHOLDER_RULE`, `planner.py:32`) with bounded correction; measured plan validity 5/6 first pass, 6/6 after correction (`docs/evaluation.md`) |
| 2+ tools | Yes¹ | Four registered: `web_search`, `webpage_fetch`, `calculator`, `failure_simulator` (`app/main.py:58-61`); live transcripts use `web_search` + `webpage_fetch` |
| Visible plan | Yes | `[PLAN] N steps:` printed **before** any tool call (`app/observability.py:119-124`, emitted pre-execution), `[n/N] doing …` progress (`app/agent/orchestrator.py:89`); transcripts A/B/C |
| Tool execution | Yes | Per-step calls run through the reliability runner; one `ToolResult` per call (`app/models/events.py:24-37`); `Tool calls:` counts in every report's Execution Summary (A: 8, B: 4, C: 12) |
| Failure simulation | Yes² | `--simulate-failure {timeout,invalid_response,temporary_error}` (`app/main.py:85-90`) arms a one-shot `ArmedTool` (`app/tools/armed.py:29`, modes `armed.py:9`) wrapping both network tools (`main.py:53-56`); live demo: transcript B (`timeout`) |
| Recovery | Yes | Bounded retry + backoff with visible `[WARN]/[RETRY]/[RECOVERED]`, exhausted candidate → source marked unavailable → run continues, retries/failures surfaced in the Execution Summary; transcript B ends exit 0 with a full report; tests `tests/failure/`, `tests/unit/test_armed.py`; harness recovery 1/1 (`docs/evaluation.md`) |
| Structured output | Yes | Typed Pydantic models end to end; Markdown report with the required sections (`render_markdown`, `app/agent/synthesizer.py:364`) incl. citation gate; report blocks inside all three transcripts |
| Tests | Yes | `uv run pytest -q` → **504 passed, 1 deselected** (live-API marker); `uv run ruff check .` → clean; unit / integration / failure / evaluation suites, all offline (MockTransport + scripted FakeLLM) |
| Architecture diagram | Yes | `docs/architecture.svg` (exported from `docs/architecture.mmd`), linked from the README's Architecture section |
| Sample transcripts | Yes | Three real captures: `examples/sample_runs/transcript-a-normal.md`, `transcript-b-failure-timeout.md`, `transcript-c-ambiguous.md` |
| README | Yes | `README.md` — setup, configuration, three example commands, real example run, failure-recovery demo, testing, evaluation, design decisions, limitations |
| Write-up | Yes | `docs/writeup.md` (one page: design decisions, agent design, reliability, tool use, evaluation, limitations, improvements) |

## Explicit caveats

¹ **The `calculator` tool has not run in any live transcript.** None of the
three captures invokes it (A/B/C plans use only `web_search`/`webpage_fetch`).
It is exercised by unit and integration tests and by the offline evaluation
harness (quantitative fixtures; registered at `tests/evaluation/harness.py:275`
— the "1 computed" citation counted in `docs/evaluation.md` comes from there).
This matches the design intent (deterministic arithmetic tool) but means its
behavior on the live web run path is unproven end to end.

² **The live failure demo uses the `ArmedTool` wrapper, not the standalone
`failure_simulator` tool.** Transcript B injects slowness on the first network
call via `--simulate-failure timeout` (`app/main.py:53-56`). The standalone
`failure_simulator` tool is registered (`app/main.py:61`) but exercised
offline only: the evaluation harness registers it for its induced-failure
case (`tests/evaluation/harness.py:276`), and it is covered directly by
`tests/unit/test_tools_failure_simulator.py` and
`tests/failure/test_failure_modes.py`.

Both caveats are deliberate: the live transcripts prioritize demonstrating the
planning → tool → failure → recovery arc on real sources, while the offline
suite covers the full registered tool set under deterministic conditions.

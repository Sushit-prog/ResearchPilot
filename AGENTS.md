# ResearchPilot — Agent Build Spec

This file is the single source of truth for building this project. It merges the
employer's take-home assignment (exact wording, weights, constraints) with the
detailed implementation plan. Follow it phase by phase — do not skip ahead, and
do not mark a phase done until it can actually be demonstrated (run + inspected).

---

## 0. What This Actually Is

An autonomous research agent that:
plans → selects tools → gathers evidence from multiple sources → validates and
deduplicates → recovers from an induced tool failure → produces a cited,
structured research report.

**LLM = planner/reasoner. Runtime = execution, validation, retry, safety, state.**
The LLM never controls arbitrary application behavior — it can only choose from a
registered set of tools with typed inputs/outputs.

Do not build a toy chatbot. Do not optimize for feature count. Optimize for:
clear architecture + reliable execution + visible recovery + good tests +
excellent documentation.

---

## 1. Assignment Requirements (verbatim source of truth)

The employer's assignment requires the agent to:

(a) Accept a high-level goal from a user
(b) Autonomously decompose the goal into a sequence of steps
(c) Use at least two distinct tools to accomplish sub-tasks
(d) Self-correct when a step fails or returns an unexpected result
(e) Produce a final, structured output/report summarizing what it did and why

It must also show a **visible planning trace** (intermediate plan before acting —
not hidden chain-of-thought) and handle **at least one deliberately induced
failure**, showing recovery or clear reporting of the failure.

**No dataset is provided.** The agent must source data live (web search / public
APIs) and generate its own synthetic test inputs. Any mocked/simulated component
must be clearly documented and justified.

**Deliverables:**
- Source code (GitHub repo) with a working README (setup + run instructions)
- An architecture diagram (image, hand-drawn or exported is fine)
- 2–3 sample run transcripts/logs (planning, tool calls, final output)
- A ≤1-page write-up: design decisions, limitations, what you'd do differently

**Evaluation rubric (weights — build to these, not to a longer feature list):**

| Criterion | What they're checking | Weight |
|---|---|---|
| Problem decomposition & planning | Sensible, legible plan before acting | 20% |
| Tool use & orchestration | Correct invocation, sane I/O, error checks | 20% |
| Robustness & error handling | Detects and recovers instead of silently breaking | 20% |
| Code quality & structure | Modular, readable, reasonably tested | 15% |
| Documentation & communication | Clear enough for someone else to run and understand | 15% |
| Creativity & initiative | Went beyond the minimum (logging, retries, self-eval) | 10% |

**Explicit constraint:** work must primarily be the candidate's own. Don't copy
repository implementations; implement and understand every component yourself;
keep the design explainable in an interview.

Suggested effort: 5–7 days part-time. Judgment and engineering quality matter
more than raw feature count.

---

## 2. Product Concept

Name: **ResearchPilot**

> An autonomous research agent that plans research tasks, selects appropriate
> tools, gathers evidence from multiple sources, validates and deduplicates
> findings, recovers from tool failures, and produces a cited research report.

Not a "magical autonomous superintelligence." Be plain about what it is and
isn't in the README.

---

## 3. Constraints From My Environment (apply these when choosing implementations)

- No GPU, 8GB RAM, Windows 11. Avoid anything that wants to load a local model
  or a heavy embedding model into memory. If similarity/dedup needs embeddings,
  call a hosted embeddings API sparingly (or skip embeddings entirely if a
  normalized-string/lexical approach is "genuinely sufficient" — the spec
  explicitly permits this, so default to it and document the tradeoff rather
  than reaching for a vector DB).
- Budget: $0–15/month total. This determines search tool and LLM provider:
  - Search: prefer a free-tier API (e.g. a provider with a no-cost quota) over
    anything metered per-query at scale. Pick one, implement it behind the
    common tool interface so it's swappable, and note the tradeoff in the README.
  - LLM: keep the LLM interface abstracted (Section 20 of the plan) so a
    cheap/free model can be swapped in for development and a stronger one used
    for the actual submission demo runs, without code changes.
- Keep dependencies minimal — every extra package is something that has to
  install cleanly on Windows without a GPU toolchain. Prefer `httpx`, `pydantic`,
  `tenacity`, stdlib `sqlite3`, and skip anything with heavy native deps.

---

## 4. Architecture

```
User
 ↓
Goal Parser
 ↓
Planner
 ↓
Plan Validator
 ↓
Agent State
 ↓
Tool Router
 ├── Web Search
 ├── Web Fetch
 ├── Calculator
 └── Failure Simulator
 ↓
Evidence Collector
 ↓
Deduplication / Relevance Filter
 ↓
Validation
 ↓
Synthesizer
 ↓
Structured Report

(Retry/Recovery loop wraps Tool Router)
(Observability/Event Log observes every stage)
(SQLite Memory sits beside Planner + Evidence Collector)
```

Directory layout:

```
research-agent/
├── app/
│   ├── main.py
│   ├── agent/        (graph.py, state.py, planner.py, executor.py,
│   │                   researcher.py, verifier.py, synthesizer.py)
│   ├── tools/         (base.py, web_search.py, webpage_fetch.py,
│   │                   calculator.py, failure_simulator.py)
│   ├── models/        (plan.py, evidence.py, report.py, events.py)
│   ├── memory/        (sqlite.py)
│   ├── reliability/    (retry.py, timeout.py, validation.py)
│   └── config.py
├── tests/ (unit/, integration/, evaluation/, fixtures/)
├── examples/ (sample_queries.md, sample_runs/)
├── reports/
├── docs/ (architecture.md, evaluation.md)
├── README.md, pyproject.toml, .env.example, .gitignore, LICENSE
```

Adjust structure only with a stated engineering reason.

---

## 5. Agent State (typed, not dicts)

Pydantic model with: `user_goal`, `normalized_goal`, `plan`, `current_step`,
`completed_steps`, `failed_steps`, `evidence`, `sources`, `tool_results`,
`retry_counts`, `warnings`, `execution_events`, `final_report`, `status`.

## 6. Planning

Planner converts goal → structured plan (`goal`, `steps: [{id, objective, tool,
expected_output}]`), validated before execution. Tool names must come from the
registered tool set — no arbitrary code generation. On invalid/malformed plan:
detect → reject → request corrected plan → bounded retries → fail clearly if
correction doesn't converge.

## 7. Tools (minimum 3, common interface: name, description, input schema,
output schema, execute(), timeout, error handling)

1. **Web Search** — structured results (title, URL, snippet, domain, timestamp
   if available). Never hand raw HTML to the LLM.
2. **Web Page Fetcher** — timeout-bounded fetch, HTTP error handling, text
   extraction, truncation, structured output.
3. **Calculator / deterministic analysis** — proves tool selection isn't
   decorative; never ask the LLM to do arithmetic a deterministic tool can do.
4. **Failure Simulator** — controllable (`failure_mode="timeout"` /
   `"invalid_response"` / `"temporary_error"`), clearly marked as a test/eval
   component, exists specifically to demonstrate recovery.

Planner picks the right tool per subtask (e.g., research question → search/
fetch/dedup/verify/synthesize; quantitative question → search/extract/
calculator/validate/report) — this demonstrates real orchestration, not tool
decoration.

## 8. Parallel Research

Independent research subtasks run concurrently via asyncio, merged afterward.
No race conditions. Final report stays deterministic enough to test.

## 9. Evidence Model

Per finding: `claim`, `source_url`, `source_title`, `extracted_text`,
`relevance_score`, `confidence`, `retrieved_at`, `evidence_id`. No unsupported
factual claims in the final report where attribution is expected. Synthesizer
consumes evidence objects, never raw web text blobs.

## 10. Deduplication

Deterministic: normalize titles, normalize URLs, drop duplicate URLs, compare
normalized claims. Add embeddings/similarity only if genuinely needed — default
to the simpler algorithm given the hardware/budget constraints above, and
document the tradeoff explicitly in `docs/architecture.md`.

## 11. Relevance Filtering

Combine source metadata + query-term relevance + (optional) LLM classification
+ deterministic validation. Don't trust the LLM blindly. Reject invalid/empty
evidence.

## 12. Failure Recovery (one of the most heavily weighted sections — 20%)

Required flow for an induced failure (e.g. `web_fetch` timeout):
detect → record failure event → increment retry counter → bounded exponential
backoff retry → on success continue → on exhausted retries mark source
unavailable → try an alternate source if possible → continue if enough evidence
remains → surface the failure in logs/report.

No infinite retries — hard max. Distinguish: transient failure, permanent
failure, malformed tool response, planner failure, validation failure.

Reproducible failure demo command, e.g.:
`research-agent --query "..." --simulate-failure timeout`

Transcript must visibly show: PLAN → TOOL CALL → FAILURE → RETRY → RECOVERY →
CONTINUE → FINAL REPORT. Make this one of the strongest parts of the submission.

## 13. Observability

Structured events (`timestamp`, `event`, `tool`, `step_id`, `status`, ...)
covering: plan_created, tool_selected, tool_started, tool_succeeded,
tool_failed, retry_started, evidence_added, evidence_deduplicated,
synthesis_started, report_generated. Readable from the CLI. Never log secrets.

## 14. Visible Planning Trace

CLI shows goal → numbered plan → `[n/N] doing X...` execution progress →
`[WARN]` / `[RETRY]` / `[RECOVERED]` lines on failure. Show operational plan and
events only — never hidden chain-of-thought reasoning.

## 15. Final Report (Markdown)

Sections: Research Question, Executive Summary, Key Findings (numbered, each
with a Source line), Important Evidence, Contradictions/Uncertainty, Actionable
Insights, Sources (numbered list), Execution Summary (sources searched/used/
rejected, failures, retries, duration). Auditable by design.

## 16. Source Quality

Prefer official docs, primary sources, reputable technical sources, papers,
official company/government sources. Don't treat every source as equally
reliable. On conflicting sources: keep both claims, flag the conflict, don't
silently pick one.

## 17. Memory (SQLite, optional per query)

Store query, timestamp, report path, source URLs, summary metadata. Use only
when it helps; don't make it mandatory. Old cached info is never treated as
automatically current — always show its age if reused.

## 18. LLM Abstraction

Small interface behind which at least one cloud provider (and optionally a
local/Ollama option) can sit. API keys via env vars only, never committed.
Ship `.env.example`.

## 19. Deterministic vs LLM Responsibilities

Deterministic code: validation, retries, timeouts, URL normalization, dedup,
schema validation, state transitions, logging, report formatting.
LLM: goal interpretation, plan generation, semantic relevance calls, synthesis.
State this split explicitly in the README — it's the core engineering signal
the rubric is checking for.

## 20. Testing

- **Unit:** planner schema validation, tool input validation, URL
  normalization, dedup, retry logic, failure classification, report generation.
- **Integration:** planner→executor, search→evidence, evidence→synthesis,
  failure→retry→recovery.
- **Failure tests (explicit):** timeout, invalid tool output, HTTP failure,
  malformed planner output, exhausted retries.

All tests deterministic; mock external network calls. Core suite must not
depend on live web APIs.

## 21. Evaluation Harness

5–10 synthetic research tasks (query, expected characteristics, required tool
types, expected report sections, failure scenario where relevant), stored e.g.
in `tests/fixtures/research_tasks.json`, clearly labeled synthetic. Measure:
plan validity, tool success rate, recovery rate, source coverage, duplicate
rate, report completeness, citation presence. Report only actually-measured
numbers — never invented metrics like "95% accurate."

## 22. CLI

`uv run research-agent --query "..." [--output ...] [--simulate-failure ...]
[--verbose] [--no-memory]`. Simple default experience.

## 23. README Structure

Title/one-liner → Problem → Architecture (diagram) → How It Works (flow) →
Reliability → Installation → Configuration → Usage (≥3 example commands) →
Example Run (real transcript) → Failure Recovery Demo → Testing → Evaluation
(and real measured results) → Design Decisions (why deterministic runtime
surrounds LLM decisions) → Limitations (honest) → Future Improvements.

## 24. Sample Transcripts (3, all real — never fabricated)

- **A — Normal:** goal, plan, tool calls, evidence, final report.
- **B — Failure:** goal, plan, tool call, timeout, retry, recovery, final report.
- **C — Ambiguous/difficult:** goal normalization, planning adjustment, multiple
  sources, conflicting/uncertain evidence, final report.

## 25. One-Page Write-up

Design decisions · Agent design (planning/execution separation) · Reliability
(detection/recovery) · Tool use (why each tool exists) · Evaluation (how
tested) · Limitations · What you'd improve with more time. No marketing
language — write like an engineer reviewing their own system.

## 26. Security

No secrets in git, `.env.example` present, URL validation, request timeouts,
response size limits, input validation, no arbitrary shell/Python execution,
safe file paths, bounded retries, no credentials in logs.

## 27. Engineering Rules

No giant files. Nothing dumped in `main.py`. No global mutable state. Typed
models throughout. Small functions. Orchestration separated from tools. LLM
logic separated from deterministic logic. Meaningful error classes. Tests for
failure paths, not just happy paths. Minimal dependencies. No copying from
public repos. No fake metrics. No claiming production-readiness. No hiding
failures. No hardcoded final answers. Don't let the system depend on one lucky
demo query.

---

## 28. Build Sequence — follow in order, checkpoint at each ✋

1. **Architecture only** — state model, tool interfaces, execution flow, test
   strategy. No code yet. ✋ *review before continuing*
2. **Core agent** — state, planner, executor, tool registry, structured
   outputs. Run tests.
3. **Research tools** — search, page fetch, calculator, evidence extraction.
   Run tests.
4. **Reliability** — timeout, retry, failure simulation, fallback, error
   classification. Add tests. ✋ *review before continuing — this section
   carries the most rubric weight and is easiest to fake*
5. **Evidence pipeline** — relevance filtering, dedup, provenance, validation.
6. **Synthesis** — structured report, source references, uncertainty handling.
7. **Memory** — lightweight SQLite.
8. **Evaluation** — synthetic cases + metrics.
9. **Documentation** — README, architecture diagram, transcripts, write-up.
10. **Final audit** — run full test suite, run all 3 demo scenarios, inspect
    generated reports, check git status for secrets/unnecessary files, and fill
    in the requirement matrix below honestly (only mark done what was actually
    demonstrated).

## 29. Requirement Matrix (fill in at Phase 10, not before)

| Requirement | Implemented | Evidence |
|---|---|---|
| High-level goal input | | |
| Autonomous planning | | |
| 2+ tools | | |
| Visible plan | | |
| Tool execution | | |
| Failure simulation | | |
| Recovery | | |
| Structured output | | |
| Tests | | |
| Architecture diagram | | |
| Sample transcripts | | |
| README | | |
| Write-up | | |

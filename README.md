# ResearchPilot

> An autonomous research agent that accepts a high-level goal, plans its own
> decomposition, gathers and validates evidence from the live web with
> multiple tools, recovers from tool failures, and produces a cited,
> structured Markdown report — with a **visible planning trace** and a
> **deterministic runtime wrapping every LLM decision**.

Deliverables: this README · [architecture diagram](docs/architecture.svg) ·
[design write-up](docs/writeup.md) ·
[3 real sample transcripts](examples/sample_runs/) ·
[full architecture doc](docs/architecture.md) ·
[evaluation results](docs/evaluation.md) · [evaluation fixture tasks](tests/fixtures/research_tasks.json)

---

## Problem

Build an agent that: (a) accepts a high-level goal, (b) autonomously
decomposes it into steps, (c) uses at least two distinct tools, (d)
self-corrects when a step fails or returns an unexpected result, and
(e) produces a final structured report — showing an intermediate plan before
acting, and handling at least one deliberately induced failure with visible
recovery.

**What this is not:** no dataset was provided, so all data is sourced live
(Tavily search + fetched pages); there is no hidden chain-of-thought shown to
the user (only the operational plan and events); and this is not a
production-hardened service — see [Limitations](#limitations).

---

## Architecture

Source of truth for details: [`docs/architecture.md`](docs/architecture.md)
(components, data contracts, deviations D1–D13). Diagram:
[`docs/architecture.svg`](docs/architecture.svg)
([mermaid source](docs/architecture.mmd) — rendered inline below for GitHub):

```
[architecture.pdf](https://github.com/user-attachments/files/32735893/architecture.pdf)

```

---

## How It Works

1. **Goal normalization** — deterministic string handling (trim, collapse).
2. **Planning (LLM)** — one call returns a structured plan: numbered steps,
   each with a registered tool name, *static literal arguments*, and an
   expected output. Placeholder/templated arguments are rejected by the prompt
   and by validation.
3. **Plan validation (deterministic)** — tools must exist in the registry,
   arguments must pass the tool's own input schema, ids must be unique, plan
   size capped. On failure: `plan_invalid` event → correction feedback → up to
   `RESEARCHPILOT_MAX_PLAN_ATTEMPTS` (3) → clear `planner_error` exit.
4. **Visible trace** — the plan is printed *before* any action:
   `[GOAL] → [PLAN] → [n/N] doing …` (§14). Events add the full stream under
   `--verbose`.
5. **Concurrent execution (asyncio)** — independent steps run concurrently;
   results are merged in **plan-step order** (not completion order), so
   reports stay deterministic.
6. **Research steps** — one `web_search` call is internally ranked
   (source-quality tier → dedup → query relevance), then `webpage_fetch` pulls
   candidate pages through the same reliability runner; extraction failures
   advance to the next candidate; exhausted candidates mark the source
   unavailable and the run continues.
7. **Evidence pipeline** — relevance floor, deterministic dedup (normalized
   URLs + normalized claims), provenance on every item, cross-source numeric
   conflict flags.
8. **Synthesis (LLM) + citation gate** — findings must cite evidence ids that
   actually exist; bad citations trigger a bounded re-synthesis; then a
   deterministic render produces the 8-section Markdown report.
9. **Memory & events** — optional SQLite history per query (age shown on
   reuse); structured events for every stage, readable on the console.

---

## Reliability

| Mechanism | Behavior |
|---|---|
| Failure classification | `transient`, `permanent`, `malformed_output`, `planner_error`, `validation_error` — one table, applied in `reliability/runner.py` |
| Tool retries | max 3 attempts, exponential backoff 0.5 s → 8 s, jitter; **hard cap, no infinite loops**; only `transient` retries |
| Timeouts | `web_search`/`webpage_fetch` 10 s, `calculator`/failure simulator 5 s, LLM 60 s — enforced with `asyncio.wait_for` |
| Plan correction | max 3 attempts with structured feedback, then fail clearly (`exit 1`, no report) |
| Synthesis | max 2 attempts (citation gate / schema failures re-prompt) |
| Candidate exhaustion | fetch retries exhausted → `source_unavailable` → next candidate → continue if evidence remains |
| Evidence gates | relevance floor, dedup, extraction validation — rejected evidence is *counted and shown*, never silently absorbed |
| Induced failures | `--simulate-failure {timeout,invalid_response,temporary_error}` injects **only the failure** via a one-shot `ArmedTool`; classification, backoff, retry, and recovery run the normal code path |
| Reporting | every failure appears in console (`[WARN]/[RETRY]/[RECOVERED]`), events, and the report's Execution Summary |

Exit codes: `0` report written · `1` failed, no report · `2` configuration /
usage error.

---

## Installation

Prerequisites: Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Sushit-prog/ResearchPilot.git
cd ResearchPilot
uv sync
```

The app reads **OS environment variables only** (`.env` is not auto-loaded).
Create a `.env` file with your keys — every supported variable is listed in
[Configuration](#configuration) — then export it before running:

```bash
# bash / zsh
set -a; source .env; set +a
```

```powershell
# PowerShell
Get-Content .env | ForEach-Object { if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { Set-Item -Path "env:$($matches[1])" -Value ($matches[2].Trim('"').Trim("'")) } }
```

Keys needed for live runs (both free tier):

- **Search:** `TAVILY_API_KEY` — free tier, ~1000 searches/month
  (https://app.tavily.com). Search runs behind the tool interface, so another
  provider is swappable in one module.
- **LLM:** `RESEARCHPILOT_LLM_API_KEY` + endpoint vars — any
  OpenAI-compatible API. The sample transcripts use
  [Groq](https://console.groq.com) (`openai/gpt-oss-120b`).

---

## Configuration

All settings are environment variables (defaults from `app/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `RESEARCHPILOT_LLM_PROVIDER` | `fake` | `openai_compatible` (live) / `fake` (offline, **rejected by CLI**) |
| `RESEARCHPILOT_LLM_BASE_URL` | — | e.g. `https://api.groq.com/openai/v1` |
| `RESEARCHPILOT_LLM_MODEL` | `fake-model` | e.g. `openai/gpt-oss-120b` |
| `RESEARCHPILOT_LLM_API_KEY` | — | provider key (never committed) |
| `TAVILY_API_KEY` | — | search key (`RESEARCHPILOT_SEARCH_API_KEY` overrides) |
| `RESEARCHPILOT_MAX_PLAN_ATTEMPTS` | `3` | plan correction budget |
| `RESEARCHPILOT_RETRY_MAX_ATTEMPTS` | `3` | attempts per transient tool call |
| `RESEARCHPILOT_RETRY_BASE_DELAY` / `_MAX_DELAY` | `0.5` / `8.0` | backoff bounds (seconds) |
| `RESEARCHPILOT_MIN_EVIDENCE` / `_MIN_DISTINCT_SOURCES` | `3` / `2` | synthesis thresholds (thin evidence is flagged, not fatal) |
| `RESEARCHPILOT_VERBOSE` / `RESEARCHPILOT_MEMORY` | `false` / `true` | event stream / SQLite history |

---

## Usage

```bash
# 1. Normal run — transcript A's exact command
uv run research-agent --query "What are the latest stable versions of the httpx and tenacity Python packages, and what timeout/retry features do they provide?" --output "reports/a"

# 2. Failure-recovery demo (induced timeout on the first network call) — transcript B's exact command
uv run research-agent --query "What are the main causes of HTTP 429 rate limit errors and standard client-side mitigation strategies?" --output "reports/b" --simulate-failure timeout

# 3. Full event stream — transcript C's exact command
uv run research-agent --query "How popular is Python really?" --output "reports/c" --verbose

# 4. Write reports elsewhere (illustrative)
uv run research-agent --query "..." --output reports/demo
```

Flags: `--query` (required) · `--output DIR` ·
`--simulate-failure {timeout,invalid_response,temporary_error}` · `--verbose`
· `--no-memory`.

---

## Example Run

Real capture (2026-09-28, Groq `openai/gpt-oss-120b`, Tavily) — abridged;
full transcript incl. report: [`transcript-a-normal.md`](examples/sample_runs/transcript-a-normal.md).

```text
[GOAL] What are the latest stable versions of the httpx and tenacity Python packages, ...
[PLAN] 2 steps:
  1. search_httpx [web_search] Identify the latest stable version of the httpx package and summarize its timeout capabilities.
  2. search_tenacity [web_search] Identify the latest stable version of the tenacity package and summarize its retry capabilities.
[1/2] doing Identify the latest stable version of the httpx package and summarize its timeout capabilities....
[2/2] doing Identify the latest stable version of the tenacity package and summarize its retry capabilities....
[SYNTHESIS] composing report from 4 evidence items
[REPORT] reports\a\what-are-the-latest-stable-versions-of-the-httpx-and-tenacit.md — 4 findings from 4 evidence items
  - Sources: 6 searched, 4 used, 2 rejected
  - Tool calls: 8 | Retries: 0
[DONE] completed — 2 steps, 0 failed steps, 0 retries, 11.5s
```

The plan appears **before any tool runs**, the two steps run concurrently,
and the report states honestly when crawled pages did not contain the exact
version numbers asked for instead of inventing them.

---

## Failure Recovery Demo

```bash
uv run research-agent --query "What are the main causes of HTTP 429 rate limit errors and standard client-side mitigation strategies?" --simulate-failure timeout
```

Real capture — abridged; full transcript incl. timing analysis and report:
[`transcript-b-failure-timeout.md`](examples/sample_runs/transcript-b-failure-timeout.md).

```text
[1/1] doing Find authoritative sources that explain the primary causes of HTTP 429 rate-limit errors ...
[WARN] web_search failed (transient): operation exceeded 10.0s
[RETRY] web_search attempt 2: operation exceeded 10.0s
[RECOVERED] web_search succeeded on attempt 2
[SYNTHESIS] composing report from 2 evidence items
  - Tool calls: 4 | Retries: 1
  - Failures: none
[DONE] completed — 1 steps, 0 failed steps, 1 retries, 17.2s
```

**Timing note (honesty about what is real):** the console has no per-line
timestamps, and stdout above is verbatim. There is a real **~10-second pause**
between `[1/1] doing …` and the first `[WARN]`. That pause is not a stall and
not a fake log line: `ArmedTool` injects *slowness only* (sleeps
`timeout + 1 s` on the run's first network call — a one-shot arm), while the
**wait itself is the runner's own `asyncio.wait_for` deadline (10 s) firing**,
followed by the untouched code path: classification as `transient` → backoff →
retry → `[RECOVERED]`. In this capture the arm was spent on the search call
and every later fetch succeeded first try, so `Failures: none` is accurate —
a candidate fetch that instead exhausts its retries lands under `Failures:`
with `source marked unavailable, run continued` (the execution-summary fix).
Corroboration: 18.2 s wall time vs 12.1 s for the normal run, and the
report's Execution Summary counts `Retries: 1`. Only the slowness is induced;
every handling step is real.

The other two modes exercise the same machinery with different injections:
`invalid_response` (malformed tool output) and `temporary_error` (explicit
transient error before any network I/O).

---

## Testing

```bash
uv run ruff check .
uv run pytest -q
```

**504 passed, 1 deselected in ~3.9 s** (deselected = the live-API marker; the
core suite performs **zero network calls** — `httpx.MockTransport` for HTTP,
scripted `FakeLLM` for planning/synthesis, fixed clock for retry/backoff).

Categories: `tests/unit/` (plan validation, tool I/O schemas, URL
normalization, dedup, retry/classification, report rendering, CLI, LLM
provider) · `tests/integration/` (planner→executor, search→evidence,
evidence→synthesis, failure→retry→recovery) · `tests/failure/` (timeout,
invalid tool output, HTTP failure, malformed planner output, exhausted
retries) · `tests/evaluation/` (synthetic-task harness, also regenerates
[`docs/evaluation.md`](docs/evaluation.md) deterministically).

---

## Evaluation

Synthetic-task harness (`tests/fixtures/research_tasks.json`, 6 tasks, fully
offline). Full definitions and per-task tables: [`docs/evaluation.md`](docs/evaluation.md).

| Metric | Raw counts | Rate |
|---|---|---|
| Plan validity (first pass, no correction needed) | 5/6 tasks | 83.3% |
| Plan validity (after bounded correction) | 6/6 tasks | 100.0% |
| Tool success rate | 31/31 calls | 100.0% |
| Failure recovery (induced) | 1/1 recovered | 100.0% |
| Source coverage | 18 distinct sources across 6 tasks | per task in doc |
| Duplicate rate (evidence) | 1/20 dropped | 5.0% |
| Report completeness | 48/48 sections | 100.0% |
| Citation presence | 19/19 findings | 100.0% (incl. 1 computed; scope caveat in doc) |

These numbers come from a deterministic stand-in LLM and mocked HTTP — they
measure the *runtime* (planning contract, tools, recovery, gates), not a real
model's quality. Real-model behavior is shown instead in the three live
transcripts.

The assignment's requirement-by-requirement status, each row with its
evidence: [`docs/requirement-matrix.md`](docs/requirement-matrix.md).

---

## Design Decisions

**Deterministic runtime, LLM at the edges (§19).** The LLM does exactly four
things: interpret the goal into a plan, semantic relevance calls, synthesize,
and produce findings. Everything else is deterministic code: validation,
retries, timeouts, URL normalization, dedup, schema checks, state transitions,
event emission, report rendering. The LLM never controls application flow —
it can only emit a plan whose steps name registered tools with static
arguments, and the validator is the gate.

- **One reliability seam** (`reliability/runner.py`): every tool call passes
  the same validate → timeout → retry → classify → emit path, so transcripts,
  tests, and the report all see one truth.
- **Injection-only failure demo:** `ArmedTool` wraps real tools and adds only
  a failure; nothing about failure *handling* is simulated.
- **Lexical dedup, no embeddings:** normalized URLs + normalized claim
  comparison. On 8 GB RAM / $0–15 budget, hosted embeddings for six synthetic
  tasks would add cost and nondeterminism for no measured gain; the seam
  (`dedup.py`) accepts a smarter comparer later.
- **OpenAI-compatible provider:** one thin HTTP adapter covers Groq, OpenAI,
  Together, vLLM/Ollama, and Gemini's compatible layer — swappable by four
  env vars; `fake` provider keeps tests offline.
- **Concurrent steps, plan-order merge:** parallelism for speed, deterministic
  ordering for testable reports.
- **Optional memory:** SQLite is a convenience, never a dependency — old
  cached info is shown with its age, never treated as current.
- **Plan-time static arguments:** fetches happen inside research steps, so the
  plan stays validate-before-act (no run-time argument templating).

---

## Limitations

Stated plainly, three structural limitations documented in
[`docs/architecture.md`](docs/architecture.md) (reproduced verbatim):

> **Cross-source derivation (v1):** deriving *brand-new* cross-source metrics
> whose operands are never co-stated ("compute YoY growth for A and B from
> separate filings, then ratio them") requires symbolic operand references
> over runtime extraction — option (b) reincarnated, with the same
> runtime-dependent plan validation already rejected. It is documented as a
> limitation rather than half-built; `verifier.py` is the seam where a typed
> derivation format would land later.

> **`depends_on` has no populated case in v1.** The planner guidance produces
> edge-free plans (antichains): data-flow edges are impossible under the
> plan-time invariant; narrative-sequencing edges cannot change any step's
> static arguments or the report while *coupling* failures — working against
> §12's "continue if enough evidence remains"; synthesis is pipeline Stage 10,
> not a plan step, so it cannot be an edge target; rate-limit serialization
> belongs to an executor semaphore, not the plan. The DAG machinery (topo
> sort, validation, `SKIPPED` propagation) exists and is unit-tested, but
> `SKIPPED` is unreachable in v1 production runs. The field is reserved for a
> future that would populate it honestly — evidence-conditioned replanning —
> and must not be made to look load-bearing before then.

> **`SourceStatus.REJECTED` has no assignment site in v1.** Sources are only
> ever constructed `AVAILABLE` (fetch succeeded) or `UNAVAILABLE` (retries +
> candidate exhaustion); `filter_evidence` drops `Evidence` objects, never
> `Source` rows — the member is reserved exactly as §4.5 reserves `depends_on`.
> Chosen semantics: `ExecutionSummary.sources_rejected = sources_searched −
> sources_used`, arithmetic over distinct normalized URLs vs. distinct
> evidence URLs, **not** a count of `REJECTED`-status sources (which would be
> structurally zero today).

Additional honest limitations:

- **Suite is offline.** Live web behavior is only covered by the three
  transcripts; no recorded-HIT cassette tests exist yet.
- **LLM provider errors are not retried.** A provider HTTP failure (e.g. a
  rate-limit 429) surfaces immediately as a clear `planner_error`/provider
  error and `exit 1`; bounded retry exists for *tools*, not for LLM calls
  (this actually occurred during transcript capture — see
  [`docs/writeup.md`](docs/writeup.md)).
- **Citation gate covers Key Findings only.** The executive summary and
  actionable insights are LLM-written *from* the evidence and can include
  general knowledge; only Key Findings are checked sentence-by-sentence
  against evidence URLs before the report ships.
- **Thin evidence happens.** JS-walled or irrelevant pages get rejected; the
  report then states only what the surviving evidence supports — transcript C
  rejected 5 of 9 sources (`no relevant passage` ×3, JS-walled ×2) and cites
  only what the remaining 4 evidence items carry.
- **Flattened text, no DOM structure.** Sentence selection sees whitespace
  only: on promo/JS-heavy pages a selected passage can open with site-chrome
  text glued to the first period (visible in transcript C's evidence
  excerpts). Run-on, length, and title-case guards remove most of it, but
  DOM-aware extraction would remove the rest.
- **Conflict detection is numeric-only** (same metric label, different
  values); qualitative contradictions rely on the synthesizer noticing them.
- **One search provider** (Tavily) behind the interface; quality of findings
  is bounded by source quality and by the chosen model's plan/synthesis
  quality.

---

## Future Improvements

- Evidence-conditioned replanning (populate `depends_on`, honest `SKIPPED`).
- Typed derivation format in `verifier.py` for cross-source computed metrics.
- Optional embeddings-based dedup behind the existing comparer seam.
- Recorded-HTTP cassette tests for live-search fixtures.
- LLM-call retry with provider-aware backoff (429/5xx).
- Additional search/fetch providers; `--export-events` JSONL for auditing.
- Confidence calibration from source tiers instead of fixed per-finding
  values.

---

## License

MIT — see [LICENSE](LICENSE).

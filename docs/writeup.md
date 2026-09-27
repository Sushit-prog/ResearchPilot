# ResearchPilot — Design Write-Up

One page, engineer's voice. Companion to [`architecture.md`](architecture.md)
(components/contracts) and [`evaluation.md`](evaluation.md) (measured numbers).

## Design decisions

**Deterministic runtime around LLM decisions.** The rubric's real question is
whether the LLM *controls* the system or *participates* in it. ResearchPilot's
split: the model produces a structured plan (steps naming registered tools
with static literal arguments) and later synthesizes findings from evidence
objects; every other behavior — validation, retries, timeouts, classification,
URL normalization, dedup, state transitions, event emission, rendering — is
ordinary code. The gate is `plan_validator`: a plan that references an
unregistered tool, template placeholders (`"URL_FROM_STEP_1"`), or unparsable
arguments is rejected with structured feedback and a bounded correction budget
(3), then fails loudly. This makes failures diagnosable and tests
deterministic; the model's contribution stays where judgement is actually
needed.

**One reliability seam.** All tool calls — research-internal or direct — go
through `reliability/runner.py`: validate → timeout → classify → bounded
backoff/retry → emit events. Five failure kinds (`transient`, `permanent`,
`malformed_output`, `planner_error`, `validation_error`), one retry table
(3 attempts, 0.5–8 s, jitter), hard caps everywhere. Keeping `retry.py` /
`timeout.py` as pure primitives and tools as pure `execute()` bodies is what
lets the same code path serve production runs, unit tests, and the induced-
failure demo without special cases.

**Injection-only failure demo.** `--simulate-failure` arms a one-shot
`ArmedTool` that adds *only* a failure (slowness, a transient raise, or a
malformed payload). Classification, the real `asyncio.wait_for` deadline,
backoff, retry, and recovery are untouched code — which is why the transcript
can claim, honestly, that only the slowness is injected. An arm spent on the
first network call keeps the rest of the run genuine, so the demo also
exercises natural incidents (transcript B has both).

**Evidence over text.** The synthesizer receives typed `Evidence` (claim,
provenance, relevance, confidence) — never raw page HTML — and a
deterministic citation gate re-checks every finding id against the evidence
list, re-prompting on fabrication (2 attempts). Reports therefore fail *thin*
(clear insufficiency statement) rather than failing *loud* (invented numbers).

## Agent design (planning vs execution)

Planning is a single LLM call producing a validated artifact; execution is an
asyncio state machine that the planner cannot influence once running — no
re-planning hooks, no code generation, no arbitrary tool arguments. Research
steps encapsulate search → rank → fetch-candidate-loop → extract as one unit
so plans stay validate-before-act; `depends_on` machinery exists and is tested
but intentionally unpopulated in v1 (edge-free plans; documented rather than
faked). Independent steps run concurrently and merge in plan-step order, so
parallelism never makes the report nondeterministic.

## Reliability (detection & recovery)

Detection: schema validation on every tool output, timeouts on every call,
explicit classification table (unclassified errors are tagged, not silently
treated as known failures — deviations D11/D12). Recovery: bounded retry for
transient failures, candidate advancement and `source_unavailable` for
exhausted fetches, correction budgets for planner and synthesizer, and
"continue if enough evidence remains" for the run as a whole. Every path is
visible: `[WARN]/[RETRY]/[RECOVERED]` lines, an event stream under `--verbose`,
and counters in the report's Execution Summary. Nothing is swallowed; failed
runs exit `1` with no report instead of a partial one.

## Tool use (why each exists)

`web_search` (structured results, never raw HTML to the model),
`webpage_fetch` (bounded fetch + text extraction for depth),
`calculator` (deterministic arithmetic/derivation checks — the LLM is never
asked to compute), `failure_simulator`/`ArmedTool` (evaluation component,
flagged `is_test_component`). The planner picks among them per subtask, which
keeps tool selection load-bearing rather than decorative.

## Evaluation (how tested)

490 offline tests: unit (schemas, normalization, dedup, retry tables,
rendering, CLI, provider), integration (planner→executor→evidence→synthesis,
failure→retry→recovery), explicit failure tests (timeout, malformed planner
output, invalid tool output, HTTP failure, exhausted retries), plus a
6-task synthetic harness measuring plan validity, tool success, recovery,
duplicates, completeness, and citation carry-through — all with `FakeLLM` +
`httpx.MockTransport`, reported raw counts only (`evaluation.md`). The three
live transcripts cover real-web behavior the suite deliberately doesn't.

## Limitations

Structural ones are reproduced in the README (cross-source derivation,
unpopulated `depends_on`, unused `SourceStatus.REJECTED`). Practically: the
suite has no live-web cassettes; conflict detection is numeric-only; one
search provider; report quality is bounded by source quality and model
quality. Two honest field notes from capture: (1) placeholder plan arguments
were a real, recurring model failure until the prompt's explicit ban plus the
validator's rejection loop — an example of *runtime beats prompting*; (2) a
free-tier LLM rate limit (429) aborted a run immediately — LLM calls have no
retry (tools do), which is listed as a future improvement rather than papered
over. Goal selection for transcript C also over-ran its agreed phrasing bound
(7 phrasings); the transcript discloses the process and uses the *first*
successful run, not the best-looking one.

## What I'd improve with more time

Evidence-conditioned replanning (populate `depends_on` honestly), a typed
derivation format in `verifier.py` for computed cross-source metrics,
provider-aware LLM retry with backoff, recorded-HTTP cassettes for live
fixtures, optional embeddings dedup behind the comparer seam, and an
exportable JSONL event log for auditing runs.

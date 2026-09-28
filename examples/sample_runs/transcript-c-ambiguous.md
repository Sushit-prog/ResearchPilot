# Transcript C — Ambiguous goal, verbose event stream, honest degradation

Real live run: Tavily search + `openai/gpt-oss-120b` on the Groq API, with
`--verbose` so the full event stream is visible. Goal is the **fixed, per
policy** `"How popular is Python really?"`. Fresh memory DB (no `[MEMORY]`
line). Captured 2026-09-28. Exit code `0`; wall time 10.3 s (agent-reported
duration 9.3 s). Post-capture edit: the `[DONE]` line's "N failures" was
changed to "N failed steps" to match a later CLI wording change; nothing
else was modified (byte-verified against the raw capture).

## Goal-selection disclosure (process, not cherry-picking)

Finding a goal phrasing that would *naturally* produce a plan-correction loop
or a cross-source conflict took **7 distinct phrasings / 8 runs** across two
builds — more than the ~3–4 phrasings originally bounded (an over-search that
was stopped). Breakdown: the first 6 runs all **failed** (5 with placeholder
plan arguments rejected 3× until the plan-attempt budget was exhausted,
1 already-valid plan then aborted by an LLM rate limit), on the pre-hardening
build; the prompt now explicitly bans placeholder/templated step arguments. The
**first successful run** of the fixed goal is this one — per the first-success
rule it is the transcript. A second phrasing (market-share + "why do estimates
differ") also succeeded and was discarded because it showed no effects either.

**Neither plan correction nor a cross-source conflict arose in this run**, and
the header says so rather than shopping for a better-looking run. What the
ambiguous goal *did* produce, and what this transcript therefore shows, is the
rest of the difficulty surface: two candidate pages rejected for no extractable
text (JS-walled), one duplicate claim dropped by dedup, two relevance-floor
rejections at `0.0 < 0.1`, and synthesis over **1 evidence item** — after which
the report states plainly that it cannot quantify Python's popularity instead
of inventing numbers (§15).

## Command

```powershell
# same environment as transcript A (TAVILY_API_KEY + Groq — see README Configuration)
uv run research-agent --query "How popular is Python really?" --output "reports/c" --verbose
```

## stdout (verbatim)

```text
[GOAL] How popular is Python really?
[PLAN] 2 steps:
  1. search_tiobe [web_search] Find recent rankings and statistics for Python in the TIOBE index.
  2. search_pypl [web_search] Find recent rankings and statistics for Python in the PYPL (PopularitY of Programming Language) index.
[1/2] doing Find recent rankings and statistics for Python in the TIOBE index....
[tool_selected] web_search step=search_tiobe
[tool_started] web_search step=search_tiobe attempt=1
[2/2] doing Find recent rankings and statistics for Python in the PYPL (PopularitY of Programming Language) index....
[tool_selected] web_search step=search_pypl
[tool_started] web_search step=search_pypl attempt=1
[tool_succeeded] web_search step=search_tiobe attempt=1
[tool_started] webpage_fetch step=search_tiobe attempt=1
[tool_succeeded] web_search step=search_pypl attempt=1
[tool_started] webpage_fetch step=search_pypl attempt=1
[tool_succeeded] webpage_fetch step=search_tiobe attempt=1
[evidence_added] webpage_fetch step=search_tiobe
[tool_started] webpage_fetch step=search_tiobe attempt=1
[tool_succeeded] webpage_fetch step=search_tiobe attempt=1
[evidence_added] webpage_fetch step=search_tiobe
[tool_started] webpage_fetch step=search_tiobe attempt=1
[tool_succeeded] webpage_fetch step=search_pypl attempt=1
[evidence_added] webpage_fetch step=search_pypl
[tool_started] webpage_fetch step=search_pypl attempt=1
[tool_succeeded] webpage_fetch step=search_tiobe attempt=1
[evidence_rejected] webpage_fetch step=search_tiobe no extractable text in fetched page
[tool_succeeded] webpage_fetch step=search_pypl attempt=1
[evidence_added] webpage_fetch step=search_pypl
[tool_started] webpage_fetch step=search_pypl attempt=1
[tool_succeeded] webpage_fetch step=search_pypl attempt=1
[evidence_rejected] webpage_fetch step=search_pypl no extractable text in fetched page
[evidence_deduplicated] step=search_pypl duplicate_claim
[evidence_rejected] step=search_tiobe relevance 0.0 below threshold 0.1
[evidence_rejected] step=search_pypl relevance 0.0 below threshold 0.1
[SYNTHESIS] composing report from 1 evidence items
[REPORT] reports\c\how-popular-is-python-really.md — 1 findings from 1 evidence items
  - Duration: 9.3s
  - Steps: 2 total, 2 succeeded, 0 failed
  - Sources: 6 searched, 1 used, 5 rejected
  - Tool calls: 8 | Retries: 0
  - Failures: none
[DONE] completed — 2 steps, 0 failed steps, 0 retries, 9.3s
```

Reading the trace: two independent steps run concurrently (the interleaved
`tool_*` lines belong to different steps). The ambiguous goal was normalized
into two **source-targeted** sub-questions (TIOBE, PYPL) — a deliberate plan
response to a vague ask. The pipeline then visibly exercises every rejection
path: `no extractable text` ×2 (candidate page had no crawlable body →
candidate advanced), `duplicate_claim` (deterministic dedup), relevance floor
`0.0 < 0.1` ×2, leaving 1 evidence item from 1 source — below the configured
`min_evidence = 3` — and the synthesis proceeds anyway while the report itself
flags the insufficiency.

## Report (verbatim — `reports/c/how-popular-is-python-really.md`)

```markdown
# ResearchPilot Report

## Research Question
How popular is Python really?

## Executive Summary
The only evidence provided is a Wikipedia page for the TIOBE index, but the extracted claim does not contain any specific data about Python's popularity. Consequently, no quantitative assessment can be drawn from the current evidence. To answer the research question, direct examination of the TIOBE index rankings or other language popularity surveys is required.

## Key Findings
1. The provided evidence does not contain specific popularity metrics for Python.
   Source: TIOBE index - Wikipedia — https://en.wikipedia.org/wiki/TIOBE_index (confidence 0.70)

## Important Evidence
- [search_tiobe:3] Jump to content Main menu Main menu move to sidebar hide Navigation Main page Contents Current events Random article About Wikipedia Contact us Contribute Help Learn to edit Community portal Recent changes Upload file Special pages Search…

## Contradictions / Uncertainty
- None identified.

## Actionable Insights
- Consult the latest TIOBE index page directly to retrieve current ranking and percentage share for Python.
- Cross‑reference other language popularity surveys (e.g., Stack Overflow Developer Survey, PYPL) for a more comprehensive view.

## Sources
1. TIOBE index - Wikipedia — https://en.wikipedia.org/wiki/TIOBE_index (en.wikipedia.org)

## Execution Summary
- Duration: 9.3s
- Steps: 2 total, 2 succeeded, 0 failed
- Sources: 6 searched, 1 used, 5 rejected
- Tool calls: 8 | Retries: 0
- Failures: none
```

Honesty check: the report's Key Finding is an explicit *absence* of the
requested numbers — the anti-hallucination behavior is visible in the wild,
not just in tests. `Failures: none` is accurate at the step level (both steps
succeeded); the five rejected sources are reported separately as
`1 used, 5 rejected`, never silently absorbed.

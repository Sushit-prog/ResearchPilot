# Sample queries

Goals used for the deliverable transcripts and useful for manual runs.
All are synthetic assignments (no dataset was provided).

| Query | Why it's interesting |
|---|---|
| What are the latest stable versions of the httpx and tenacity Python packages, and what timeout/retry features do they provide? | Two-entity comparison → two independent steps, version + feature facts ([transcript A](sample_runs/transcript-a-normal.md)) |
| What are the main causes of HTTP 429 rate limit errors and standard client-side mitigation strategies? | Failure demo target — run with `--simulate-failure timeout` ([transcript B](sample_runs/transcript-b-failure-timeout.md)) |
| How popular is Python really? | Ambiguous/vague goal — normalization and evidence gates under-specified asks ([transcript C](sample_runs/transcript-c-ambiguous.md)) |
| What is the market share of Python among programming languages, and why do estimates differ? | Two-clause stats shape; invites cross-source numeric conflict flags when values collide |
| Compare the retry-after semantics described by RFC 9110 and common API vendor docs | Primary-source (RFC) vs vendor docs; source-tier ranking |
| Does SQLite support concurrent writers, and what do official docs recommend for locking? | Official-docs-first goal; calculator step can verify PRAGMA/busy-timeout arithmetic |

Run any of them:

```bash
uv run research-agent --query "How popular is Python really?" --verbose
```

# ResearchPilot — Architecture (Phase 1)

**Status:** Phase 1 of the §28 build sequence in `AGENTS.md` — design only. No
implementation code exists yet; the Pydantic classes and interfaces below are
the data contracts every later phase must satisfy. `AGENTS.md` remains the
source of truth; this document resolves the design decisions needed before
Phase 2.

---

## 1. Directory structure

```
xiarch/                          # repo root = project root (see deviation D1)
├── app/
│   ├── __init__.py
│   ├── main.py                  # thin CLI entry: argparse → config → orchestrator → report
│   ├── config.py                # frozen pydantic settings from env vars (no pydantic-settings dep)
│   ├── errors.py                # exception hierarchy (deviation D2)
│   ├── observability.py         # EventSink protocol + console/JSONL sinks (deviation D3)
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── orchestrator.py      # renamed from spec's graph.py (deviation D4)
│   │   ├── state.py             # AgentState, StepRecord, AgentStatus
│   │   ├── planner.py           # normalize_goal() + LLM plan generation (deviation D5)
│   │   ├── executor.py          # DAG scheduling, parallel dispatch, step-level fallback
│   │   ├── researcher.py        # search→rank→fetch unit + evidence/numeric extraction
│   │   ├── verifier.py          # plan validation + evidence validation + numeric pass
│   │   └── synthesizer.py       # evidence → Report (LLM) + deterministic markdown renderer
│   ├── llm/                     # LLM abstraction (deviation D6)
│   │   ├── __init__.py
│   │   ├── base.py              # LLMProvider protocol
│   │   ├── openai_compat.py     # thin httpx client (OpenAI/Groq/OpenRouter/Ollama base_url)
│   │   └── fake.py              # FakeLLMProvider — scripted responses for tests/demos
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py              # Tool ABC, ToolRegistry, input/output parse helpers
│   │   ├── web_search.py
│   │   ├── webpage_fetch.py
│   │   ├── calculator.py        # whitelisted-AST expression parser, never eval()
│   │   └── failure_simulator.py # test/eval component (is_test_component = True)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── plan.py              # Plan, PlanStep, StepStatus
│   │   ├── evidence.py          # Evidence, NumericFact, EvidenceVerification, Source, ...
│   │   ├── report.py            # Report, Finding, SourceRef, ExecutionSummary, FailureRecord
│   │   ├── events.py            # ExecutionEvent, EventKind, FailureKind, ToolResult
│   │                            #   (grouped as "runtime execution records" — deviation D9)
│   │   └── tool_io.py           # tool I/O contracts (Phase 3 addition — deviation D10)
│   ├── memory/
│   │   ├── __init__.py
│   │   └── sqlite.py            # optional per-query run history (§17)
│   └── reliability/
│       ├── __init__.py
│       ├── runner.py            # THE seam: validate → timeout → retry → classify → emit (D7)
│       ├── retry.py             # tenacity policy wrapper (pure primitive)
│       ├── timeout.py           # asyncio.wait_for wrapper (pure primitive)
│       └── validation.py        # shared input/output/evidence schema validation helpers
├── tests/
│   ├── conftest.py              # shared fixtures: FakeLLM, MockTransport, fake clock
│   ├── unit/                    # §20 category 1
│   ├── integration/             # §20 category 2
│   ├── failure/                 # §20 category 3 (deviation D8)
│   ├── evaluation/              # synthetic-task harness (§21)
│   └── fixtures/                # canned HTTP payloads, research_tasks.json, HTML samples
├── examples/
│   ├── sample_queries.md
│   └── sample_runs/             # transcripts A/B/C (real runs, Phase 9)
├── reports/                     # generated reports — gitignored, .gitkeep only
├── docs/
│   ├── architecture.md          # this file
│   └── evaluation.md            # measured harness results (Phase 8)
├── README.md
├── pyproject.toml               # [project.scripts] research-agent = "app.main:main"
├── .env.example
├── .gitignore
└── LICENSE
```

### Deviations from AGENTS.md §4 (each with engineering reason)

| # | Deviation | Reason |
|---|---|---|
| D1 | No nested `research-agent/` wrapper dir | The repo root *is* the project root. A single-package `uv` project gains nothing from nesting; imports and tooling stay flat. |
| D2 | New `app/errors.py` | §27 requires meaningful error classes; the §4 layout gives the hierarchy no home. |
| D3 | New `app/observability.py` | The §4 *diagram* requires an Event Log observing every stage; the §4 *layout* allocates it no file. Holds the `EventSink` protocol and its console/JSONL implementations. |
| D4 | `agent/graph.py` → `agent/orchestrator.py` | No graph framework is used (§3 minimal-deps rule). It is a plain asyncio phase state machine, not a graph; the honest name avoids implying LangGraph-style machinery we deliberately didn't adopt. |
| D5 | Goal Parser folded into `planner.py` as `normalize_goal()` | A stage in the §4 diagram ≠ a mandatory file (§27: no file sprawl). Normalization is a pure function of the goal; keeping it adjacent to the planner keeps both modules small. |
| D6 | New `app/llm/` package | §18 requires an LLM abstraction interface; the §4 layout has no directory for it. Three focused modules (protocol, one provider, one fake) justify a package over a single file. |
| D7 | New `reliability/runner.py` | The single seam that composes *validate → timeout → retry → classify → emit events* around every tool call. Keeps `retry.py`/`timeout.py` as pure primitives and tools as pure `execute()` bodies — this is what makes §19's deterministic-runtime-around-the-LLM split real instead of aspirational. |
| D8 | New `tests/failure/` directory | §20 names failure tests as a third category; robustness carries 20% rubric weight and deserves a visible home next to `unit/` and `integration/`. |
| D9 | `EventKind` extends §13's list | §13 says events must *cover* the listed names — a floor, not a ceiling. Added: `goal_normalized`, `plan_invalid`, `step_failed`, `source_unavailable`, `candidate_advanced`, `evidence_rejected`, `evidence_conflict`, `run_completed`. Every extension exists to make a recovery or rejection path visible in transcripts. |
| D10 | New `app/models/tool_io.py` | Phase 3 turned §3's "tool schemas (contracts only)" into real models. They must be importable by the tools, by `researcher.py` in Phase 5, and by tests; `models/` is already the data-contract package, while duplicating the schemas inside each tool module would fragment them. |
| D11 | `ToolResult.unclassified: bool = False` + `runner.is_unclassified()` | A code bug (e.g. `IndexError`) that matches no row of `classify()`'s table still classifies `PERMANENT` — correct for retry safety, but it made a genuine bug indistinguishable from a known permanent failure like a 404. The additive field (default `False`) tags the ToolResult, the `tool_failed` event carries `data={"unclassified": true, "error_type": ...}` (free-form `data`, §13-sanctioned), and the console renders `[WARN] unexpected error type: ...` instead of the normal failure line. Taxonomy untouched: still §12's five kinds, still no retry-loop. |

`reports/` output is gitignored; the three deliverable transcripts live in
`examples/sample_runs/` instead (curated deliverables, not build artifacts).

---

## 2. Agent State model (data contract)

All models: pydantic v2, fully JSON-serializable (state can be dumped for
debugging without custom encoders). One `AgentState` instance is owned by the
orchestrator — no global mutable state (§27).

### Enums

```python
class AgentStatus(str, Enum):
    INIT = "init"
    PLANNING = "planning"
    EXECUTING = "executing"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"   # report produced — partial step failures surface in
                              # ExecutionSummary + warnings, NOT here
    FAILED = "failed"         # no report possible (unrecoverable planner/validation
                              # failure, or zero evidence)

class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"       # a depends_on prerequisite failed

class FailureKind(str, Enum):              # §12 taxonomy — maps 1:1 from errors.py
    TRANSIENT = "transient"                # timeout, 5xx, connection reset → retriable
    PERMANENT = "permanent"                # 404, DNS failure, blocked → not retriable
    MALFORMED_OUTPUT = "malformed_output"   # tool output failed its output schema
    PLANNER_ERROR = "planner_error"        # plan invalid after bounded correction
    VALIDATION_ERROR = "validation_error"  # structural failure (plan or evidence)

class FailureMode(str, Enum):              # failure_simulator input (§7)
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    TEMPORARY_ERROR = "temporary_error"

class StepRecord(BaseModel):               # compact account of a finished step
    step_id: str
    objective: str
    tool: str
    status: StepStatus
    result_ids: list[str] = []             # indexes into AgentState.tool_results
    error: str | None = None
    failure_kind: FailureKind | None = None
```

### Plan (`app/models/plan.py`)

```python
class PlanStep(BaseModel):
    id: str                            # unique within the plan, e.g. "r1", "q1"
    objective: str                     # human-readable; shown in the visible trace
    tool: str                          # MUST be a ToolRegistry name — validated (§6)
    arguments: dict[str, Any] = {}     # static per the plan-time invariant (§4.3)
    expected_output: str               # what this step must produce, LLM-declared
    depends_on: list[str] = []         # reserved — no populated v1 case (§4.5)
    status: StepStatus = StepStatus.PENDING   # mutated by the executor during the run

class Plan(BaseModel):
    goal: str
    steps: list[PlanStep]              # validator enforces: non-empty, ≤ config cap,
                                       # unique ids, known tools, args pass each tool's
                                       # input schema, depends_on refs exist, DAG acyclic
```

`arguments` + `depends_on` are the two additive fields beyond §6's literal
`{id, objective, tool, expected_output}`. `arguments` is justified by the
plan-time invariant (§4.3). `depends_on` is **not** a §8 requirement —
parallelism is the *absence* of edges — and no v1 plan populates it; it is
kept for schema completeness and future extensibility (see §4.5).

### Evidence (`app/models/evidence.py`)

```python
class NumericFact(BaseModel):          # runtime numeric extraction (§4.3 calculator path)
    value: float
    unit: str | None = None            # e.g. "USD_B", "percent", "million_users"
    metric_label: str | None = None    # e.g. "revenue", "yoy_growth"

class EvidenceVerification(BaseModel): # verifier.py numeric pass output
    recomputed: bool = False           # claim-internal arithmetic was checked
    consistent: bool | None = None     # None = not applicable / not checked
    conflict_with: list[str] = []      # evidence_ids holding a conflicting value
    note: str | None = None

class Evidence(BaseModel):             # §9 fields verbatim + typed extensions
    evidence_id: str
    claim: str
    source_url: str | None             # None ONLY for calculator-derived evidence;
                                       # web-derived evidence must be non-None (checked)
    source_title: str | None
    extracted_text: str                # bounded excerpt, never full raw HTML
    relevance_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    retrieved_at: datetime
    step_id: str | None = None         # provenance: which plan step produced it
    numeric: NumericFact | None = None # set only when extraction found a number
    verification: EvidenceVerification = Field(default_factory=EvidenceVerification)

class SourceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"        # set after retries + candidate exhaustion
    REJECTED = "rejected"              # filtered out (duplicate/irrelevant/low quality)

class SourceQuality(str, Enum):        # §16 tiers
    PRIMARY = "primary"                # official docs, papers, government/company sources
    REPUTABLE = "reputable"            # reputable technical/press sources
    UNKNOWN = "unknown"

class Source(BaseModel):
    url: str
    domain: str
    title: str | None = None
    quality: SourceQuality = SourceQuality.UNKNOWN
    status: SourceStatus = SourceStatus.AVAILABLE
    retrieved_at: datetime | None = None
    unavailable_reason: str | None = None
```

Calculator-derived evidence (goal-intrinsic arithmetic, §4.3) has
`source_url = None`; its report "Source" line renders as
`computed — <expression> = <value>` (deterministic, auditable). Every
web-derived finding keeps a real URL, satisfying §9's no-unsupported-attribution
rule.

### Runtime execution records (`app/models/events.py`)

```python
class ToolResult(BaseModel):
    result_id: str
    tool: str
    step_id: str
    call_ordinal: int                  # nth tool call within the step (the research
                                        #  unit makes several: search + k fetches)
    success: bool
    output: dict[str, Any] | None = None  # schema-validated output, serialized
    attempts: int = 1                  # runner attempts consumed
    failure_kind: FailureKind | None = None
    error: str | None = None
    duration_ms: int = 0
    started_at: datetime

class EventKind(str, Enum):
    # §13 floor
    PLAN_CREATED = "plan_created"
    TOOL_SELECTED = "tool_selected"
    TOOL_STARTED = "tool_started"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    RETRY_STARTED = "retry_started"
    EVIDENCE_ADDED = "evidence_added"
    EVIDENCE_DEDUPLICATED = "evidence_deduplicated"
    SYNTHESIS_STARTED = "synthesis_started"
    REPORT_GENERATED = "report_generated"
    # declared extensions (deviation D9)
    GOAL_NORMALIZED = "goal_normalized"
    PLAN_INVALID = "plan_invalid"
    STEP_FAILED = "step_failed"
    SOURCE_UNAVAILABLE = "source_unavailable"
    CANDIDATE_ADVANCED = "candidate_advanced"
    EVIDENCE_REJECTED = "evidence_rejected"
    EVIDENCE_CONFLICT = "evidence_conflict"
    RUN_COMPLETED = "run_completed"

class ExecutionEvent(BaseModel):
    timestamp: datetime                # injected clock, not time.time()
    event: EventKind
    tool: str | None = None
    step_id: str | None = None
    status: str | None = None          # free-form detail (e.g. "timeout")
    attempt: int | None = None
    message: str | None = None
    data: dict[str, Any] = {}          # structured detail; NEVER secrets (§26)
```

### Report (`app/models/report.py`) — §15 sections as fields

```python
class Finding(BaseModel):
    index: int                         # renders as "1." in Key Findings
    claim: str
    evidence_id: str                   # citation anchor — must resolve (checked)
    source_url: str | None             # mirrors evidence; None → "computed — …" line
    source_title: str | None = None
    confidence: float

class ImportantEvidence(BaseModel):
    evidence_id: str
    excerpt: str

class SourceRef(BaseModel):
    index: int
    url: str
    title: str | None = None
    domain: str

class FailureRecord(BaseModel):
    step_id: str
    tool: str
    failure_kind: FailureKind
    message: str

class ExecutionSummary(BaseModel):
    started_at: datetime
    finished_at: datetime
    duration_s: float
    steps_total: int
    steps_succeeded: int
    steps_failed: int
    sources_searched: int
    sources_used: int
    sources_rejected: int
    tool_calls: int
    retries: int
    failures: list[FailureRecord]      # §12: failures ALWAYS surface here

class Report(BaseModel):
    research_question: str
    executive_summary: str
    key_findings: list[Finding]        # numbered; each with a Source line
    important_evidence: list[ImportantEvidence]
    contradictions: list[str]          # §15/§16: conflicts kept and flagged, never hidden
    actionable_insights: list[str]
    sources: list[SourceRef]           # numbered list, deduplicated
    execution_summary: ExecutionSummary
```

### AgentState (`app/agent/state.py`)

```python
class AgentState(BaseModel):
    user_goal: str
    normalized_goal: str = ""
    plan: Plan | None = None
    current_step: PlanStep | None = None
    completed_steps: list[StepRecord] = []
    failed_steps: list[StepRecord] = []
    evidence: list[Evidence] = []
    sources: list[Source] = []
    tool_results: list[ToolResult] = []
    retry_counts: dict[str, int] = {}  # key = "{step_id}:{tool}:{call_ordinal}";
                                        # hard max enforced by the runner (§12)
    warnings: list[str] = []
    execution_events: list[ExecutionEvent] = []
    final_report: Report | None = None
    status: AgentStatus = AgentStatus.INIT
```

Field-for-field with §5's required list — only typed companions added.

---

## 3. Tool interfaces

### The `Tool` contract (`app/tools/base.py`)

```python
InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)

class Tool(ABC, Generic[InputT, OutputT]):
    name: ClassVar[str]                          # registry key, e.g. "web_search"
    description: ClassVar[str]                   # shown to the planner in its prompt
    input_model: ClassVar[type[BaseModel]]       # plan-arg validation + planner schema
    output_model: ClassVar[type[BaseModel]]      # never raw HTML to the LLM (§7)
    default_timeout_s: ClassVar[float]           # runner-enforced (§12)
    is_test_component: ClassVar[bool] = False    # failure_simulator → True

    @abstractmethod
    async def execute(self, params: InputT) -> OutputT:
        """Do the work. Raise ToolError subclasses for failures.
        NO timeout, retry, or event logic inside — the reliability runner owns
        all three (§19)."""

    def parse_input(self, raw: Mapping[str, Any]) -> InputT:
        """Validate raw (plan-supplied) args against input_model.
        Raises ToolInputValidationError → FailureKind.VALIDATION_ERROR."""

    def parse_output(self, raw: Any) -> OutputT:
        """Validate execute()'s raw return against output_model.
        Raises MalformedToolOutputError → FailureKind.MALFORMED_OUTPUT."""
```

Design choice: **ABC, not `Protocol`.** The registry needs shared schema
helpers and identity-based registration; structural typing buys nothing here.

### `ToolRegistry`

```python
class ToolRegistry:
    def register(self, tool: Tool[Any, Any]) -> None
    def get(self, name: str) -> Tool[Any, Any]      # unknown → ToolNotFoundError
    def names(self) -> list[str]                    # plan validator's allow-list (§6)
    def schemas(self) -> dict[str, dict[str, Any]]  # name → {description, input_schema}
                                                    # injected into the planner prompt
```

### Error hierarchy (`app/errors.py`) → `FailureKind` mapping

```
ResearchPilotError
├── ToolError (carries tool name + failure_kind)
│   ├── TransientToolError        → TRANSIENT        (retriable)
│   ├── PermanentToolError        → PERMANENT        (not retriable)
│   └── MalformedToolOutputError  → MALFORMED_OUTPUT (retriable, then source fallback)
├── ToolInputValidationError      → VALIDATION_ERROR
├── PlanValidationError           → VALIDATION_ERROR (plan-correction loop, not tool retry)
├── PlannerError                  → PLANNER_ERROR    (correction loop exhausted)
├── EvidenceValidationError       → VALIDATION_ERROR (evidence dropped + event, run continues)
├── ToolNotFoundError             → (no kind — internal invariant violation)
└── ConfigurationError            → run aborts before planning (clear CLI message)
```

Retriability is a property of the `FailureKind`, decided in one place
(`retry.py`) — not scattered through tools.

### Tool schemas (contracts only — no logic in this phase)

**1. `web_search`** — structured results only; raw HTML never reaches the LLM.

```python
class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=8, ge=1, le=20)

class SearchResultItem(BaseModel):
    title: str
    url: str                            # validated http/https, no embedded credentials
    snippet: str
    domain: str
    published_at: datetime | None = None

class SearchOutput(BaseModel):
    query: str
    results: list[SearchResultItem]
    provider: str                       # which backend answered — tradeoff noted in README
    retrieved_at: datetime
```

Provider (chosen in Phase 3): **Tavily** — free tier, 1,000 searches/month
(§3 budget), authenticated with `Authorization: Bearer <TAVILY_API_KEY>` via
`TAVILY_API_KEY` in `.env.example`; swappable behind this interface.
`published_at` stays `None` unless the provider returns `published_date`
(only for `topic="news"` queries) — absent metadata is never fabricated.

**2. `webpage_fetch`**

```python
class FetchInput(BaseModel):
    url: str                            # http/https only, validated (§26)
    max_chars: int = Field(default=20_000, ge=500, le=100_000)   # size cap (§26)

class FetchOutput(BaseModel):
    requested_url: str
    final_url: str                      # after redirects
    status_code: int
    title: str | None
    text: str                           # extracted main text, whitespace-normalized
    truncated: bool
    content_type: str | None
```

**3. `calculator`** — two call paths, one contract (§4.3).

```python
class CalculatorInput(BaseModel):
    expression: str                     # goal-intrinsic expression, OR an expression
                                        # composed in code by researcher/verifier
    unit: str | None = None

class CalculatorOutput(BaseModel):
    expression: str
    value: float
    unit: str | None = None
```

The parser is a **whitelisted-AST evaluator**: numbers, `+ - * / // % **`,
parentheses, unary minus, and an explicit allow-list of pure functions
(`sqrt`, `abs`, `min`, `max`). Name lookups, attribute access, calls outside
the allow-list, and inputs exceeding size/depth limits are rejected —
**never `eval()`/`exec()`** (§26). Division by zero raises
`PermanentToolError`: retrying identical arithmetic is pointless.

**4. `failure_simulator`** — test/eval component, `is_test_component = True`.

```python
class SimulatorInput(BaseModel):
    failure_mode: FailureMode           # timeout | invalid_response | temporary_error
    fail_times: int = Field(default=1, ge=0, le=10)  # fail first N invocations, then
                                                     # succeed → recovery is
                                                     # reproducible by construction
    payload: dict[str, Any] | None = None  # body returned in invalid_response mode

class SimulatorOutput(BaseModel):
    status: str                         # "ok" on post-failure success
    invocation: int
```

Behavior contract: `timeout` sleeps past the runner's deadline (the runner's
`wait_for` fires — exercises the *real* timeout path); `invalid_response`
*returns* a body that fails `output_model` (exercises the real
`parse_output` → `MALFORMED_OUTPUT` path, not a pre-raised fake);
`temporary_error` raises `TransientToolError`.

### LLM abstraction (`app/llm/base.py`) — §18

```python
class LLMProvider(Protocol):
    name: str
    async def complete(self, *, system: str, user: str,
                       temperature: float = 0.0) -> str
```

- `OpenAICompatProvider` (`llm/openai_compat.py`): one thin httpx client works
  with OpenAI, Groq, OpenRouter, and Ollama via `base_url` — cheap/free model
  in development, stronger model for submission runs, **no code changes**
  (§3). Keys from env vars only, never logged (§26).
- `FakeLLMProvider` (`llm/fake.py`): scripted response queue — drives
  deterministic tests and offline demos.
- Provider selection lives in `config.py`, not in agent code.

---

## 4. Execution flow walkthrough

```
User → Goal Parser → Planner(LLM) → Plan Validator → Agent State → Tool Router
        → [Web Search | Web Fetch | Calculator | Failure Simulator]
        → Evidence Collector → Dedup / Relevance Filter → Validation
        → Synthesizer(LLM) → Structured Report

(Reliability runner wraps every tool call: timeout → retry → classify → events)
(EventSink observes every stage; SQLite Memory sits beside planner + evidence)
```

Stages below are numbered Stage 0–11; subsection numbers are 4.N.

### 4.0 Stage 0 — CLI & config

`uv run research-agent --query "..." [--output ...]
[--simulate-failure timeout|invalid_response|temporary_error] [--verbose]
[--no-memory]`. Config = CLI flags + env vars → frozen model. Exit codes:
`0` report written (even with warnings), `1` no report, `2` invalid usage.
`--simulate-failure` **arms** the failure simulator for the first network tool
call of the run (`fail_times = 1`) — the real call then follows the normal
retry path, producing a reproducible PLAN → TOOL CALL → FAILURE → RETRY →
RECOVERY transcript without touching the network.

### 4.1 Stage 1 — Goal parsing (deterministic)

`normalize_goal()`: trim, collapse whitespace, NFC-normalize → emit
`goal_normalized` + the goal to the console. Optional memory lookup: if a
recent run has the same normalized goal, its result is *shown with its age*
("cached from 2 days ago — may be stale") and never silently reused (§17).
`--no-memory` skips read and write.

### 4.2 Stage 2 — Plan generation (LLM) with bounded correction

The planner prompt contains: normalized goal, `registry.schemas()` (name +
description + input JSON schema per tool), the plan JSON contract, and the
**plan-time invariant** (§4.3). Output is parsed into `Plan`; parse failure or
schema failure raises `PlanValidationError` → `plan_invalid` event → the error
text is fed back for one correction attempt. **Hard cap
(`config.max_plan_attempts`, default 3)**; exhausted → `PlannerError` → clear
CLI failure, exit 1. Two bounded loops exist — plan correction and tool retry —
with *separate* counters and *separate* budgets.

### 4.3 ★ Design decision — plan-time invariants (fetch *and* calculator)

> **Invariant:** `PlanStep.arguments` are functions of the **goal alone**,
> computable at plan time. Runtime-derived values (discovered URLs, reported
> numbers) never enter plan arguments — they stay inside deterministic runtime
> units that own that data's lifecycle.

One principle resolves both tools — the "carve-out" was never a carve-out, it
is the goal-intrinsic column:

| | Goal-intrinsic data (plan-time) | Runtime-derived data |
|---|---|---|
| **webpage_fetch** | URL literally in the goal → planned step, static `arguments.url`, direct through runner | Discovered URL → `researcher.py` owns **search → rank → fetch → advance-to-next-candidate** as one unit, invisible to the planner and to `depends_on` |
| **calculator** | Operands in the goal → planned step, static `arguments.expression`, direct through runner | Operands in evidence → deterministic units own **extract → compute → validate** (below) |

**Why not reference-bearing plans** (option (b): `{"url_from": "r1", "rank": 0}`
resolved by the executor): §6 guarantees plans are *validated before
execution*, and a runtime reference cannot be validated at plan time; it also
builds a templating DSL the spec never asked for, adds a third distributed
retry mechanism (nothing bounds rank advancement in the plan schema), and
transports a decision that is deterministic in *both* options (the planner
never sees search output) through the LLM layer for no reason (§19).

**Calculator's runtime path** (scoped to the domain we're actually building —
general research summarization, §28 phases):

1. **Typed numeric extraction** (`researcher.py`): fetched text → `Evidence`
   carrying `numeric: {value, unit, metric_label}` alongside the prose claim.
2. **Claim-internal recomputation** (`verifier.py`, Validation stage): claims
   embedding their own arithmetic ("up 12% to $8.9B") are recomputed in code;
   mismatch → confidence adjustment + `evidence_conflict` + a Contradictions
   entry. All operands travel with the claim, so the expression is composed in
   code — no intent plumbing, no reference resolution.
3. **Cross-source numeric consistency** (`verifier.py`): same metric label,
   different values across sources → deterministic conflict flag → §15
   Contradictions / §16 "keep both, flag the conflict."

All three invoke the `calculator` tool through the reliability runner — same
timeout, classification, and events — so tool selection stays non-decorative
and the LLM never does arithmetic (§7).

**Stated limitation (v1):** deriving *brand-new* cross-source metrics whose
operands are never co-stated ("compute YoY growth for A and B from separate
filings, then ratio them") requires symbolic operand references over runtime
extraction — option (b) reincarnated, with the same runtime-dependent plan
validation already rejected. It is documented as a limitation rather than
half-built; `verifier.py` is the seam where a typed derivation format would
land later.

### 4.4 Stage 3 — Plan validation & visible trace (`verifier.py`)

Before anything executes: steps non-empty and ≤ cap; unique ids; `tool ∈
registry.names()`; `arguments` pass `tool.parse_input` — fully static now,
which is exactly what the invariant buys; `depends_on` references exist and
the DAG is acyclic; `expected_output` present. Failure → `plan_invalid` event
→ back to Stage 2's correction budget. On success → `plan_created` and the
**visible trace** (§14): goal → numbered plan → `[n/N] doing X...` progress.
Operational plan and events only — never hidden chain-of-thought.

### 4.5 Stage 4 — Execution scheduling (`executor.py`)

Topological order over `depends_on` (for v1's edge-free plans this reduces to
"dispatch every step concurrently"); every step whose prerequisites are
`SUCCEEDED` dispatches via `asyncio.gather` (§8). **No shared
mutation:** tasks *return* `(StepRecord, list[ToolResult], list[Evidence],
list[Source])`; the orchestrator merges them in plan-step order — never
completion order — so the final report is deterministic enough to test (§8).
`current_step` is tracked for display ("most recently started step"). A step
whose prerequisite failed → `SKIPPED` (dependency-propagated, distinct from
its own failure).

**`depends_on` has no populated case in v1.** The planner guidance produces
edge-free plans (antichains): data-flow edges are impossible under the
plan-time invariant (that would be option (b), §4.3); narrative-sequencing
edges ("first survey, then deep-dive") cannot change any step's static
arguments or the report (merge is plan-step order, so execution order is
invisible downstream) while *coupling* failures — a failed prerequisite would
`SKIPPED` an otherwise-independent step, working against §12's "continue if
enough evidence remains"; synthesis is pipeline Stage 10, not a plan step, so
it cannot be an edge target; rate-limit serialization belongs to an executor
semaphore, not the plan. The DAG machinery (topological sort, dangling/cyclic
validation, `SKIPPED` propagation) exists and is unit-tested (§5), but
`SKIPPED` is unreachable in v1 production runs. The field is reserved for a
future that would populate it honestly — evidence-conditioned replanning —
and must not be made to look load-bearing before then.

### 4.6 Stage 5 — Tool router & step dispatch

Two step flavors:

- **Research step** (`tool = web_search`, static `query` argument): executed
  by `researcher.py` as *one unit* — (i) run `web_search` through the runner;
  (ii) deterministically rank results (§16 quality tiers → dedup'd URLs →
  query-term relevance); (iii) for the top-`k` candidates in order, run
  `webpage_fetch` through the runner; (iv) on candidate exhaustion → mark
  source unavailable → return what was gathered; (v) extract `Evidence` (with
  provenance) per successful fetch. The plan contains *no* fetch steps; every
  internal call still appends `ToolResult` and emits `tool_*` events, so
  orchestration remains fully visible in transcripts and `tool_results`.
- **Direct steps** (`webpage_fetch` with a goal-given URL, `calculator` with a
  goal-intrinsic expression, `failure_simulator`): `tool_selected` event →
  straight through the runner.

### 4.7 Stage 6 — Reliability runner (`reliability/runner.py`) — the attempt layer

Every tool call passes through exactly one code path:

```
with_timeout(tool.default_timeout_s)        # timeout.py, asyncio.wait_for
  → retry(policy, sleep=injected)           # retry.py, tenacity, hard max
  → classify(exception) → FailureKind      # one mapping table
  → emit tool_started / retry_started / tool_failed / tool_succeeded
  → parse_output → ToolResult appended (success or classified failure)
```

- **Retry policy** (`config.retry`): `max_attempts = 3` (hard — never
  infinite, §12), exponential backoff `0.5s * 2^n`, capped at 8s, full jitter;
  `sleep` is injectable so tests never actually wait.
- **Retriability is kind-driven:** `TRANSIENT` and `MALFORMED_OUTPUT` retry;
  `PERMANENT` fails the call immediately (a 404 does not become a 200).
- **Unclassified exceptions stay visible (D11):** an exception type matching
  no row of the `classify()` table still returns `PERMANENT` (never
  retry-loop on a bug), but the runner sets `ToolResult.unclassified = True`,
  tags the `tool_failed` event `data={"unclassified": true, "error_type": ...}`,
  and the console renders `[WARN] unexpected error type: IndexError in <tool> …`
  — so a code bug never wears the same face as a known 404.
- **Runner exhaustion ≠ step failure.** The runner returns a classified failed
  `ToolResult` to its caller; what happens next belongs to the layer above.

### 4.8 Three-layer recovery (§12 — the rubric's heaviest section)

| Layer | Owner | Bound | Mechanism |
|---|---|---|---|
| **Attempt** | reliability runner | `max_attempts = 3` per tool call, keyed in `retry_counts` | timeout → classify → exponential backoff → `retry_started` / `tool_failed` events |
| **Source** | `researcher.py` | `k` candidates (config, default 3) from the *same* search output | runner exhausted one URL → `source_unavailable` → `candidate_advanced` → fetch next ranked result |
| **Step** | `executor.py` | 1 failure record per step | all candidates failed → `failed_steps` + `step_failed` → evidence-sufficiency check → continue with warning, or degrade |

Sufficiency means: at least `min_evidence` evidence items **and** at least
`min_distinct_sources` distinct URL-bearing (real) sources. Calculator-derived
evidence (`source_url = None`, §4.9) counts toward the evidence total but never
toward source diversity — arithmetic checks a claim's internal consistency; it
does not corroborate a source.

Required §12 sequence, fully mapped to events and CLI lines:

```
detect → tool_failed event → retry counter++ → bounded backoff   [RETRY]
  → success? continue → [RECOVERED]
  → exhausted? classify
      TRANSIENT / MALFORMED_OUTPUT
          → source_unavailable → candidate_advanced              [WARN]
              → next candidate succeeds → continue
              → k exhausted → step_failed
                  → evidence sufficient? → continue with warnings
                  → insufficient? → degrade (synthesize what exists)
                                 or FAILED (zero evidence → exit 1)
  → PERMANENT → skip attempt retry → straight to source layer
→ EVERY failure lands in Report.execution_summary.failures + CLI summary
```

§12's five classes each get their own handler, none silently swallowed:
`TRANSIENT` vs `PERMANENT` from exception type + HTTP status;
`MALFORMED_OUTPUT` from `parse_output`; `PLANNER_ERROR` from Stage 2's loop;
`VALIDATION_ERROR` from plan/evidence validators.

---

### 4.9 Stage 7 — Evidence collection

Realized inside the research unit for research steps (the §4 diagram's
"Evidence Collector" is a logical stage, not a separate dispatch pass):

- Successful `webpage_fetch` output → bounded excerpt → `claim` phrased from
  the excerpt + `extracted_text` + provenance (`source_url`, `source_title`,
  `step_id`, `retrieved_at`) + seed `relevance_score` → `evidence_added` event.
- Numeric extraction (§4.3 item 1) runs as part of this pass.
- Goal-URL fetch steps and calculator outputs convert the same way (calculator
  → evidence with `source_url = None`, marked derived).
- Nothing without a source and an excerpt becomes evidence — invalid/empty
  candidates are rejected with `evidence_rejected` here, before filtering.

### 4.10 Stage 8 — Deduplication (deterministic, §10)

Order: normalize URLs (scheme/host case, trailing slash, sorted query params,
`utm_*` stripped, fragment dropped) → drop duplicate URLs → normalize titles →
compare normalized claims (casefolded, punctuation/whitespace collapsed,
token-set overlap ≥ threshold) → `evidence_deduplicated` event for every drop.

**Tradeoff (§10, explicit):** lexical/normalized-string dedup over
embeddings — no local model on 8 GB/no-GPU hardware, no per-query embedding
cost against the $0–15/month budget, fully testable offline, and the spec
expressly permits it. Revisit embeddings only if real runs show duplicate
pairs this method misses; the seam is one function signature.

### 4.11 Stage 9 — Relevance filter + validation (`verifier.py`)

Per evidence: query-term overlap score (deterministic) + optional LLM
classification as a **non-authoritative** signal (disagreement triggers a
warning, never blind trust — §11) + structural checks (non-empty claim, valid
URL, source exists, scores in range). Rejects → `evidence_rejected` + warning;
invalid evidence never reaches synthesis. Then the numeric pass (§4.3 items
2–3): claim recomputation + cross-source conflict detection →
`evidence_conflict` events populate `Report.contradictions`.

### 4.12 Stage 10 — Synthesis (`synthesizer.py`)

Input to the LLM: **evidence objects only** (claims, provenance, scores) —
never raw web text (§9). `synthesis_started` event. Output parsed into
`Report`, then two deterministic gates:

1. **Citation check:** every `key_findings[i].evidence_id` must resolve in
   `state.evidence`; its `source_url` must match the evidence (None allowed
   only for calculator-derived). Violations → one bounded correction with the
   violation list fed back; findings that still don't resolve are **dropped
   with a warning** (never emitted). No unsupported attribution leaves this
   stage (§9).
2. **Section completeness:** all §15 sections present and typed → else the
   same bounded correction loop (`max_synthesis_attempts`, default 2).

Then `render_markdown(report)` — a pure function → `reports/<slug>.md` (slug
derived from goal, path-traversal-safe, §26) → `report_generated` event →
console prints the execution summary.

### 4.13 Stage 11 — Memory & closeout

Unless `--no-memory`: insert one run row (query, normalized goal, timestamp,
status, report path, source URLs, evidence count, duration) via stdlib
`sqlite3`. Stale rows are only ever shown *with their age* (§17). Final
`run_completed` event; exit code per §4.0.

### Hook points — where cross-cutting concerns live

| Concern | Hook |
|---|---|
| **Timeouts** | Tool boundary only — runner (Stage 6, `default_timeout_s`, per-call override); planner/LLM calls get their own single timeout in `llm/openai_compat.py`; SQLite ops are local and bounded |
| **Retries** | Exactly three bounded loops: tool retry (runner, `retry_counts`, hard max 3), plan correction (Stage 2, `max_plan_attempts`), synthesis citation/section correction (Stage 10). Never inside tools, never unbounded |
| **Event log** | `EventSink` (protocol in `observability.py`) injected into orchestrator, planner, runner, researcher, verifier, synthesizer; sinks = in-memory list (`state.execution_events`) + console renderer (always; `--verbose` adds per-call detail) + optional JSONL file. Secrets redacted before any sink (§26, tested) |
| **SQLite memory** | Stages 1 (read) and 11 (write) only; never consulted mid-run — old data is never treated as current (§17) |
| **Visible trace** | Stages 3–6 print plan + `[n/N]` progress; runner prints `[RETRY]` / `[RECOVERED]` / `[WARN]` lines as events arrive (§14) |

### Deterministic vs LLM responsibilities (§19)

| Deterministic code | LLM |
|---|---|
| Validation (plan, tool I/O, evidence, citations) | Goal interpretation (`normalize_goal` is deterministic; *understanding* is not) |
| Retries, timeouts, backoff, failure classification | Plan generation + bounded plan correction |
| URL normalization, dedup, ranking, relevance scoring | Optional relevance *classification* (non-authoritative) |
| Numeric extraction / recomputation / conflict detection | Synthesis of findings from evidence objects |
| State transitions, DAG scheduling, deterministic parallel merge | |
| Event emission, logging, report formatting/rendering | |

This split is the core engineering signal the rubric checks; it is enforced
structurally (runner around tools, `FakeLLMProvider` seam, pure renderer),
not just documented.

---

## 5. Test strategy (§20)

Runner: `pytest` + `pytest-asyncio` (`asyncio_mode = auto`); `ruff` for lint.
**The core suite never touches the live web** and must pass offline,
deterministically, on a cold machine.

### Mocking approach

| Dependency | Seam | Technique |
|---|---|---|
| HTTP (search, fetch) | tools accept an injected `httpx.MockTransport`/`Client` (constructor param; default = real client) | `httpx.MockTransport` routing to canned fixtures in `tests/fixtures/` — **zero extra dependencies** (chosen over `respx` to honor §3 minimal-deps) |
| LLM | `LLMProvider` interface | `FakeLLMProvider` with a scripted response queue: valid plan, invalid plan, synthesis payload, malformed JSON. Call count asserted |
| Time/clock | runner `sleep` param, injected `clock` in orchestrator | tenacity with injectable sleep — backoff tests assert the *schedule* without waiting; timestamps come from the fake clock |
| Failures | the real `failure_simulator` tool | no sabotaged mocks — induced failures exercise the production code path |
| Calculator | pure function | no mocks needed |

### Unit tests (`tests/unit/`)

- **Plan schema:** valid plan accepted; unknown tool rejected; empty steps /
  over-cap rejected; duplicate ids rejected; missing `expected_output`
  rejected; cyclic `depends_on` rejected; dangling `depends_on` rejected;
  `arguments` failing the tool's input schema rejected; `normalize_goal`
  whitespace/case handling.
- **Tool input validation:** bad URL schemes (`file://`, `javascript:`),
  embedded-credential URLs, `max_results` out of range, empty query,
  oversized `max_chars`, `fail_times` bounds.
- **Calculator parser:** precedence/associativity; division by zero →
  `PERMANENT` classification; rejection of `__import__`, attribute access,
  name lookups, string/lambda payloads — *eval-payload regression tests*.
- **URL normalization:** scheme/host case, trailing slash, query sorting,
  `utm_*` stripping, fragment drop; non-http rejected.
- **Dedup:** duplicate URLs; duplicate normalized claims; title collapse;
  near-identical-but-distinct claims preserved.
- **Retry logic:** exact backoff schedule (fake sleep records delays), hard
  max reached → no further attempt, `retry_counts` keys incremented per call
  ordinal, retriable-vs-not mapping per `FailureKind`.
- **Failure classification:** exception/status → `FailureKind` mapping table
  (5xx → transient, 404 → permanent, schema violation → malformed_output).
- **Report generation:** all eight §15 sections present; deterministic
  markdown render (golden string); citation check rejects unknown
  `evidence_id`; execution-summary counts match inputs.
- **Numeric pass:** extraction ("up 12% to $8.9B" → fact; prose → none);
  claim recomputation consistent vs inconsistent; cross-source conflict
  detection.
- **Events:** each stage transition emits its expected `EventKind` in order;
  serialized event payloads never contain a configured API key (§26).

### Integration tests (`tests/integration/`)

- **planner → executor:** FakeLLM canned plan (including a `calculator` step)
  → real executor + fake tools → state transitions and step statuses correct.
- **search → evidence:** MockTransport search fixture → researcher →
  `Evidence` with correct provenance (`source_url`, `step_id`, timestamps).
- **evidence → synthesis:** canned evidence → FakeLLM synthesis payload →
  `Report` with resolvable citations → markdown file written.
- **failure → retry → recovery:** tool fails twice (`temporary_error`) →
  succeeds on attempt 3 → `SUCCEEDED`, retry schedule + event order asserted.
- **candidate advancement:** search OK → fetch fails permanently on candidate
  0 → researcher advances → candidate 1 succeeds → evidence from candidate 1,
  source 0 marked unavailable, `candidate_advanced` emitted.
- **full pipeline (offline):** goal → FakeLLM plan → MockTransport search +
  fetch → dedup/filter → FakeLLM synthesis → report file + memory row
  (assert `--no-memory` skips the row).

### Failure tests (`tests/failure/` — explicit §20 category)

- **Timeout:** `failure_simulator` mode `timeout` → runner `wait_for` fires →
  transient classification → bounded backoff → recovery; a second scenario
  exhausts attempts → source layer engaged.
- **Invalid tool output:** mode `invalid_response` → `parse_output` fails →
  `MALFORMED_OUTPUT` → retry/fallback, never a crash.
- **HTTP failure:** MockTransport 500 → transient, retried; 404 → permanent,
  *not* retried (classification boundary).
- **Malformed planner output:** FakeLLM returns non-JSON → plan-invalid
  correction loop → bounded → `PlannerError`, clean CLI failure, exit 1.
- **Unknown tool in plan:** FakeLLM plans `shell_exec` → rejected → bounded
  correction → clear failure (never executes arbitrary names, §26).
- **Exhausted retries:** all attempts + all `k` candidates fail → step lands
  in `failed_steps`, run continues if evidence suffices, failure appears in
  `execution_summary.failures`, warnings present, report still valid.
- **Zero evidence:** everything fails → `FAILED`, exit 1, no partial report
  presented as complete.

### Evaluation harness (`tests/evaluation/`, §21)

5–10 synthetic tasks in `tests/fixtures/research_tasks.json` (clearly labeled
synthetic): query, expected characteristics, required tool types, expected
report sections, optional failure scenario. Runs against `FakeLLMProvider` +
MockTransport (deterministic), measuring: plan validity (first-pass and
post-correction), tool success rate, recovery rate (induced failures
recovered / induced), source coverage, duplicate rate, report completeness,
citation presence. Results recorded in `docs/evaluation.md` — **only
actually-measured numbers** (§21).

A separate `@pytest.mark.live` smoke test (excluded from the default `pytest`
run) can hit a real search/LLM endpoint manually — never part of CI.

---

## 6. Dependencies & configuration

**Runtime:** `pydantic>=2`, `httpx`, `tenacity`. **Dev/test:** `pytest`,
`pytest-asyncio`, `ruff`. Nothing with heavy native deps (§3).

**Config surface (`config.py`, frozen):** LLM provider/base_url/model +
API key (env only), search provider + key (Phase 3), `max_plan_attempts=3`,
`retry.max_attempts=3` / backoff params, `default_timeout_s` per tool,
`candidate_cap k=3`, evidence-sufficiency thresholds (`min_evidence=3`,
`min_distinct_sources=2`), `max_steps=12`, report output dir, memory path,
verbose/memory flags. `.env.example` documents every variable; no secret ever
enters git, events, or logs (§26).

**Python floor:** `requires-python = ">=3.11"` — explicit, not incidental: the
only 3.11+ construct is stdlib `typing.Self` (`plan.py`), kept over a
`typing_extensions` dependency per §3 minimal-deps. AGENTS.md states no Python
version; the environment runs CPython 3.14.

---

*Phase 1 complete pending review (✋ per §28). Phase 2 (core agent — state,
planner, executor, tool registry, structured outputs) does not begin until this
document is approved.*


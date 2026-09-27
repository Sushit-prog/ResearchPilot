from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.events import FailureKind


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

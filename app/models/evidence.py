from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class NumericFact(BaseModel):          # runtime numeric extraction (§4.3 calculator path)
    value: float
    unit: str | None = None            # e.g. "USD_B", "percent", "million_users"
    metric_label: str | None = None    # e.g. "revenue", "yoy_growth"


class EvidenceVerification(BaseModel): # verifier.py numeric pass output
    recomputed: bool = False           # claim-internal arithmetic was checked
    consistent: bool | None = None     # None = not applicable / not checked
    conflict_with: list[str] = Field(default_factory=list)  # conflicting evidence_ids
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

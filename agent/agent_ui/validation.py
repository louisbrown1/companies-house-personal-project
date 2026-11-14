"""Reject malformed evidence and reports before they reach the UI."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .evidence_models import EvidenceGap, validate_fact


class Source(BaseModel):
    url: HttpUrl
    kind: Literal["companies_house_api", "review_site", "trade_press", "search_result", "other"]


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(pattern=r"^ev_[0-9]+$")
    claim: str = Field(min_length=1)
    source: Source
    verbatim_excerpt: str
    structured_fact: dict
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_typed_fact(self):
        if set(self.structured_fact) != {self.claim}:
            raise ValueError("Evidence claim must identify its structured payload")
        for claim, payload in self.structured_fact.items():
            validate_fact(claim, payload)
        return self

    @property
    def typed_fact(self):
        return validate_fact(self.claim, self.structured_fact.get(self.claim))


class EvidenceBundle(BaseModel):
    evidence: list[EvidenceItem]
    uncertainty: list[str]
    gaps: list[EvidenceGap] = Field(default_factory=list)


class ReportValidationError(ValueError):
    """Model output failed checks; never publish it as a report."""


def parse_model_json(value):
    """Accept a single JSON fence, but never extract JSON from mixed prose."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    return json.loads(fence.group(1) if fence else text)


def validate_report(report: str, state: dict) -> None:
    try:
        _validate_report(report, state)
    except (ValueError, TypeError) as exc:
        raise ReportValidationError(str(exc)) from exc


def _validate_report(report: str, state: dict) -> None:
    for key in ("competition", "quality_signals"):
        value = state.get(key)
        value = parse_model_json(value)
        if not isinstance(value, dict):
            raise ValueError(f"Missing or invalid {key} output")
    raw = state.get("evidence")
    evidence = EvidenceBundle.model_validate(parse_model_json(raw))
    sources = {item.evidence_id: str(item.source.url) for item in evidence.evidence}
    if len(sources) != len(evidence.evidence):
        raise ValueError("Duplicate evidence IDs")
    headings = [
        "Business Model Summary",
        "Competition (Structured)",
        "Company Quality (Structured)",
        "Explicit Citation Block",
        "Uncertainty and Limitations",
    ]
    positions = [report.find("## " + title) for title in headings]
    if any(pos < 0 for pos in positions) or positions != sorted(positions):
        raise ValueError("Report is missing required sections or section order")
    block = report[positions[3] : positions[4]]
    pairs = re.findall(r"^- (ev_\d+) -> (https?://\S+)\s*$", block, re.MULTILINE)
    citations = dict(pairs)
    if len(citations) != len(pairs):
        raise ValueError("Duplicate report citations")
    referenced = set(re.findall(r"\bev_\d+\b", report))
    for evidence_id in referenced:
        if evidence_id not in sources or evidence_id not in citations:
            raise ValueError("Report references unknown or uncited evidence")
        if str(HttpUrl(citations[evidence_id])) != sources[evidence_id]:
            raise ValueError("Report citation differs from evidence source")
    if evidence.evidence and not referenced:
        raise ValueError("Report does not cite collected evidence")

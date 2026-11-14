"""Typed financial evidence and machine-readable gaps at report boundaries."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .peer_models import PeerComparison


class SourceFact(BaseModel):
    # Preserve extractor provenance and diagnostics during the compatibility migration.
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class FinancialFact(SourceFact):
    value: Decimal
    scale: int = Field(gt=0, strict=True)
    currency: Literal["GBP"]
    period: date
    scope: str = Field(min_length=1)
    page: int = Field(ge=1, strict=True)
    excerpt: str = ""


class RegulatoryRatio(SourceFact):
    metric: Literal[
        "cet1", "lcr", "nsfr", "total_capital_ratio", "leverage_ratio", "regulatory_capital"
    ]
    value: Decimal = Field(ge=0)
    scale: int = Field(gt=0, strict=True)
    unit: Literal["%", "GBP"]
    period: date
    scope: str = Field(min_length=1)
    page: int = Field(ge=1, strict=True)
    reporting_basis: Literal["point_in_time", "average", "unspecified", "period_end_unspecified"]
    averaging_window: str | None = None
    excerpt: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_basis(self):
        if self.metric == "regulatory_capital":
            if self.unit != "GBP":
                raise ValueError("Capital amount must use GBP")
        elif self.unit != "%" or self.scale != 1:
            raise ValueError("Regulatory ratios require percentage units and scale 1")
        if self.reporting_basis != "average" and self.averaging_window is not None:
            raise ValueError("Only averaged measurements may have an averaging window")
        return self


class Disclosure(SourceFact):
    topic: str = Field(min_length=1)
    period: date
    page: int = Field(ge=1, strict=True)
    scope: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    basis: str = Field(min_length=1)


class ReviewRating(SourceFact):
    status: Literal["confirmed_on_profile", "confirmed_via_tavily_extract"] = "confirmed_on_profile"
    provider: str = Field(min_length=1)
    rating: Decimal
    scale_min: Decimal = Decimal(0)
    scale_max: Decimal = Field(gt=0)
    review_count: int = Field(ge=0, strict=True)
    scope: str = Field(min_length=1)
    entity_match: str = Field(min_length=1)

    @model_validator(mode="after")
    def within_scale(self):
        if (
            not self.scale_min < self.scale_max
            or not self.scale_min <= self.rating <= self.scale_max
        ):
            raise ValueError("Review rating outside published scale")
        return self


class UnavailableReviewRating(SourceFact):
    provider: str = Field(min_length=1)
    status: Literal[
        "ambiguous_aggregate",
        "aggregate_unavailable",
        "unsafe_profile_url",
        "page_unavailable",
        "unsupported_content",
        "page_too_large",
        "redirect_limit",
        "fetch_failed",
        "tavily_extraction_failed",
    ]
    url: HttpUrl
    checked_at: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    rating: None = None
    scale_min: None = None
    scale_max: None = None
    review_count: None = None


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal["missing_metric", "conflicting_metric", "uncertain_transcription", "coverage"]
    message: str = Field(min_length=1)
    metric: str | None = None
    period: date | None = None
    scope: str | None = None
    page: int | None = Field(default=None, ge=1)
    status: Literal["open", "resolved"] = "open"

    @model_validator(mode="after")
    def requires_context(self):
        if self.code in ("missing_metric", "conflicting_metric") and (
            not self.metric or not self.period
        ):
            raise ValueError("Metric gaps need metric and period")
        if self.code == "uncertain_transcription" and self.page is None:
            raise ValueError("Transcription gaps need a source page")
        return self


FINANCIAL_METRICS = frozenset(
    "revenue profit_before_tax profit_after_tax operating_profit interest_income effective_interest_income interest_expense net_interest_income total_income total_assets total_liabilities total_equity customer_deposits customer_loans cash cash_central_banks operating_cash_flow cost_of_sales gross_profit administrative_expenses current_assets current_liabilities".split()
)
# Only code-produced registry/search metadata may retain their provider payload.
METADATA_CLAIMS = frozenset(
    "company_name company_number company_status type date_of_creation sic_codes "
    "registered_office_address accounts confirmation_statement has_charges "
    "has_insolvency_history undeliverable_registered_office_address officers_counts "
    "psc_counts charges_counts filing_history_recent_counts active_registry_matches "
    "unverified_web_search_result preliminary_review_sentiment accounts_filing "
    "financial_observation".split()
)


class PairedNote(SourceFact):
    current: Decimal | None
    previous: Decimal | None
    period: date
    prior_period: date
    currency: Literal["GBP"]
    scale: int = Field(gt=0, strict=True)
    page: int = Field(ge=1, strict=True)
    excerpt: str = Field(min_length=1)


def validate_fact(claim, payload):
    """Validate known evidence variants without changing legacy JSON serialization."""
    if claim == "platform_customer_rating" and isinstance(payload, dict):
        if payload.get("status") not in (
            None,
            "confirmed_on_profile",
            "confirmed_via_tavily_extract",
        ):
            return UnavailableReviewRating.model_validate(payload)
    model = {
        "banking_regulatory_metric": RegulatoryRatio,
        "banking_note_disclosure": Disclosure,
        "platform_customer_rating": ReviewRating,
        "peer_comparison": PeerComparison,
    }.get(claim)
    if model is None and (
        claim in FINANCIAL_METRICS
        or isinstance(payload, dict)
        and "currency" in payload
        and "value" in payload
    ):
        model = FinancialFact
    if model is None and (claim.startswith("note_") or claim == "group_funding_counterparty"):
        model = PairedNote
    if model is not None:
        return model.model_validate(payload)
    if claim not in METADATA_CLAIMS:
        raise ValueError("Unsupported evidence claim")
    return None


def resolve_metric_gaps(notes, metrics):
    """Resolve by metric, period and requested scope, never by English wording."""
    records = [EvidenceGap.model_validate(g) for g in notes.get("gap_records", [])]
    values = [RegulatoryRatio.model_validate(m) for m in metrics]
    for gap in records:
        if gap.code != "missing_metric" or gap.status != "open":
            continue
        matches = [
            m
            for m in values
            if m.metric == gap.metric
            and m.period == gap.period
            and (gap.scope is None or m.scope == gap.scope)
        ]
        by_context = {}
        for m in matches:
            key = (m.scope, m.reporting_basis, m.averaging_window)
            by_context.setdefault(key, set()).add((m.value, m.scale, m.unit))
        if matches and all(len(v) == 1 for v in by_context.values()):
            gap.status = "resolved"
    removed = {g.message for g in records if g.status == "resolved"}
    notes["gaps"] = [text for text in notes["gaps"] if text not in removed]
    notes["gap_records"] = [g.model_dump(mode="json") for g in records]

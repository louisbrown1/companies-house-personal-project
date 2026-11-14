"""Source-bearing contracts for bounded financial peer comparisons."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .peer_discovery_models import DiscoveryAudit

Metric = Literal["loans_to_deposits", "equity_to_assets", "current_ratio", "cet1", "lcr", "nsfr"]
RATIOS = {
    "loans_to_deposits": ("customer_loans", "customer_deposits", Decimal(100)),
    "equity_to_assets": ("total_equity", "total_assets", Decimal(100)),
    "current_ratio": ("current_assets", "current_liabilities", Decimal(1)),
}


class PeerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PeerPolicy(PeerModel):
    sector: Literal["bank", "retail", "trading"]
    metrics: list[Metric] = Field(min_length=1)
    max_period_gap_days: int = Field(default=93, ge=0, le=93)
    max_candidates: int = Field(default=20, ge=1, le=20)
    max_profiles: int = Field(default=6, ge=1, le=6)
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_peers: int = Field(default=2, ge=1, le=2)
    budget_seconds: int = Field(default=600, ge=1, le=900)
    max_asset_multiple: int = Field(default=10, ge=1, le=20)
    discovery_query: str | None = Field(default=None, min_length=10, max_length=250)


class PeerSelection(PeerModel):
    company_numbers: list[Annotated[str, Field(pattern=r"^(?:[0-9]{8}|[A-Z]{2}[0-9]{6})$")]] = (
        Field(default_factory=list, max_length=2)
    )


class PeerIdentity(PeerModel):
    company_number: str = Field(pattern=r"^(?:[0-9]{8}|[A-Z]{2}[0-9]{6})$")
    company_name: str = Field(min_length=1)
    sic_codes: list[str]
    registry_url: HttpUrl
    basis: Literal["registry_identity_and_sector_accounts"] = (
        "registry_identity_and_sector_accounts"
    )


class MetricSource(PeerModel):
    url: HttpUrl
    page: int = Field(ge=1, strict=True)
    excerpt: str = Field(min_length=1)
    field: str
    value: Decimal
    scale: int = Field(gt=0, strict=True)
    unit: Literal["GBP", "%"]
    period: date
    scope: Literal["company", "group"]


class PeerMeasurement(PeerModel):
    company_number: str = Field(pattern=r"^(?:[0-9]{8}|[A-Z]{2}[0-9]{6})$")
    metric: Metric
    value: Decimal
    unit: Literal["%", "times"]
    period: date
    scope: Literal["company", "group"]
    reporting_basis: Literal["point_in_time", "average"]
    averaging_window: str | None = None
    sources: list[MetricSource] = Field(min_length=1)
    formula: str

    @model_validator(mode="after")
    def validate_calculation(self):
        if any(s.period != self.period or s.scope != self.scope for s in self.sources):
            raise ValueError("Source periods and scopes must match the measurement")
        if self.reporting_basis != "average" and self.averaging_window is not None:
            raise ValueError("Only averages may have a window")
        if self.reporting_basis == "average" and not self.averaging_window:
            raise ValueError("Averaged ratios need an explicit window")
        if self.metric in RATIOS:
            numerator, denominator, multiplier = RATIOS[self.metric]
            if len(self.sources) != 2:
                raise ValueError("Derived ratios need both inputs")
            a, b = self.sources
            if (a.field, b.field, a.unit, b.unit) != (numerator, denominator, "GBP", "GBP"):
                raise ValueError("Wrong ratio inputs")
            if b.value <= 0:
                raise ValueError("Denominator must be positive; sign is not inferred")
            expected = a.value * a.scale / (b.value * b.scale) * multiplier
            if self.value != expected or self.reporting_basis != "point_in_time":
                raise ValueError("Invalid derived ratio")
        elif len(self.sources) != 1 or (
            self.sources[0].field != self.metric
            or self.sources[0].unit != "%"
            or self.sources[0].scale != 1
            or self.value != self.sources[0].value
        ):
            raise ValueError("Regulatory ratio must preserve its published value")
        if self.unit != ("times" if self.metric == "current_ratio" else "%"):
            raise ValueError("Incorrect measurement unit")
        return self


class PeerComparison(PeerModel):
    subject: PeerMeasurement
    peer: PeerMeasurement
    difference: Decimal
    difference_unit: Literal["percentage_points", "times"]
    period_gap_days: int = Field(ge=0, le=93)

    @model_validator(mode="after")
    def consistent_comparison(self):
        a, b = self.subject, self.peer
        if a.company_number == b.company_number:
            raise ValueError("Cannot compare a company with itself")
        if (a.metric, a.unit, a.scope, a.reporting_basis, a.averaging_window) != (
            b.metric,
            b.unit,
            b.scope,
            b.reporting_basis,
            b.averaging_window,
        ):
            raise ValueError("Incompatible comparison contexts")
        if self.period_gap_days != abs((a.period - b.period).days):
            raise ValueError("Incorrect period separation")
        if self.difference != a.value - b.value:
            raise ValueError("Incorrect comparison arithmetic")
        expected = "percentage_points" if a.unit == "%" else "times"
        if self.difference_unit != expected:
            raise ValueError("Incorrect difference unit")
        return self


class PeerReport(PeerModel):
    status: Literal["completed", "partial", "unavailable"] = "unavailable"
    sector: str
    runbook_sha256: str
    selection_mode: Literal["automatic", "explicit"] = "automatic"
    discovery: DiscoveryAudit | None = None
    candidates_considered: int = 0
    peers: list[PeerIdentity] = Field(default_factory=list)
    comparisons: list[PeerComparison] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

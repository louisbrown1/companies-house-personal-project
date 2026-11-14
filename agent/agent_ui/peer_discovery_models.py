"""Typed, auditable web discovery; quoted pages support model interpretations."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class DiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Quote(DiscoveryModel):
    source_id: str
    quote: str = Field(min_length=12, max_length=700)


class BusinessFacet(Quote):
    code: Literal[
        "savings",
        "mortgages",
        "residential_mortgages",
        "buy_to_let",
        "business_lending",
        "consumer_credit",
        "asset_finance",
        "corporate_banking",
        "retail_goods",
        "wholesale_goods",
        "manufacturing",
        "professional_services",
        "software",
        "individuals",
        "landlords",
        "smes",
        "corporates",
        "uk",
        "international",
    ]


class ProfileDraft(DiscoveryModel):
    facets: list[BusinessFacet] = Field(max_length=10)
    uncertainties: list[str] = Field(default_factory=list, max_length=5)


class ProfileSelection(DiscoveryModel):
    selected_ids: list[str] = Field(
        max_length=10,
        description="IDs of supported business-profile statements; choose products, customers and operating geography where evidenced.",
    )


class PageSource(DiscoveryModel):
    source_id: str
    url: HttpUrl
    text: str = Field(max_length=6000)
    kind: Literal["search_snippet", "page_content"]
    retrieved_at: str


class AssetContext(DiscoveryModel):
    amount_gbp: Decimal = Field(gt=0)
    period: date
    source_url: HttpUrl
    page: int = Field(ge=1)
    excerpt: str = Field(min_length=1)


class BusinessProfile(ProfileDraft):
    company_name: str
    company_number: str
    identity_quotes: list[Quote]
    sources: list[PageSource]
    assets: AssetContext | None = None


class CandidateProposal(Quote):
    name: str = Field(
        min_length=3,
        max_length=120,
        description="Copy a candidate business/brand name appearing in the cited source; do not invent a legal suffix.",
    )


class CandidateDecision(DiscoveryModel):
    name: str
    company_number: str | None = None
    status: Literal["shortlisted", "excluded", "included"]
    reasons: list[str]
    discovery_evidence: Quote | None = None
    profile: BusinessProfile | None = None
    shared_facets: list[str] = Field(default_factory=list)


class DiscoveryAudit(DiscoveryModel):
    status: Literal["completed", "partial", "unavailable"] = "unavailable"
    subject: BusinessProfile | None = None
    candidates: list[CandidateDecision] = Field(default_factory=list)
    discovery_sources: list[PageSource] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    searches: list[str] = Field(default_factory=list)

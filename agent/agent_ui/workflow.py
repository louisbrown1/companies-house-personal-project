"""Provider-independent orchestration: tools own evidence, models select source IDs."""

import asyncio
import os

from pydantic import BaseModel, ConfigDict, Field

from .local_report import generate_local_report
from .registry_context import compact_registry_context


class ApprovedCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_number: str = Field(pattern=r"^(?:[0-9]{8}|[A-Z]{2}[0-9]{6})$")
    company_name: str | None = None


class CandidateCompany(BaseModel):
    model_config = ConfigDict(extra="allow")
    company_number: str = Field(pattern=r"^(?:[0-9]{8}|[A-Z]{2}[0-9]{6})$")
    title: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)


class CompanyResolution(BaseModel):
    model_config = ConfigDict(extra="allow")
    selected_company_number: str | None = None
    confidence: float = Field(ge=0, le=1)
    rationale: list[str] = Field(default_factory=list)
    candidates: list[CandidateCompany]


async def resolve_company(query, registry_tool):
    result = await registry_tool("resolve_company", {"identifier_or_name": query})
    CompanyResolution.model_validate(result)
    return result


async def generate_report(approved, user_text, registry_tool):
    deadline = asyncio.get_running_loop().time() + int(os.getenv("REPORT_TIMEOUT_SEC", "600"))
    company = ApprovedCompany.model_validate(approved)
    pack = await registry_tool("underwriting_pack", {"company_number": company.company_number})
    profile = pack.get("profile", {})
    if (
        pack.get("company_number") != company.company_number
        or profile.get("company_number") != company.company_number
    ):
        raise ValueError("Registry returned a different company")
    if not isinstance(profile.get("company_name"), str) or not profile["company_name"].strip():
        raise ValueError("Registry returned no company name")
    return await generate_local_report(
        compact_registry_context(pack), user_text, registry_tool, deadline=deadline
    )

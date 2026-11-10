# ch_mcp_server.py
import asyncio
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

CH_BASE_URL = "https://api.company-information.service.gov.uk"

# ----------------------------
# Models
# ----------------------------


class CandidateCompany(BaseModel):
    company_number: str
    title: str
    company_status: Optional[str] = None
    date_of_creation: Optional[str] = None
    address_snippet: Optional[str] = None
    score: float = 0.0
    score_rationale: List[str] = Field(default_factory=list)


class ResolveResult(BaseModel):
    selected_company_number: Optional[str]
    confidence: float
    rationale: List[str]
    candidates: List[CandidateCompany]


# ----------------------------
# Helpers
# ----------------------------


def _api_key() -> str:
    key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not key:
        raise RuntimeError("Missing COMPANIES_HOUSE_API_KEY env var.")
    return key


def _looks_like_company_number(s: str) -> bool:
    # CH company numbers are commonly 8 digits, but can include letters (e.g., SC, NI, OC, LP...)
    return bool(re.fullmatch(r"[A-Z]{0,2}\d{6,8}", s.strip().upper()))


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and (exc.response.status_code == 429 or exc.response.status_code >= 500)
    )


@retry(
    retry=retry_if_exception(_retryable),
    reraise=True,
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(4),
)
async def _get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    auth = (_api_key(), "")  # Basic auth: key as username, blank password.
    async with httpx.AsyncClient(base_url=CH_BASE_URL, auth=auth, timeout=30) as client:
        resp = await client.get(path, params=params)
        if resp.status_code == 429:
            # CH rate limit: 600/5min; treat 429 as retryable.
            raise httpx.HTTPStatusError("Rate limited (429).", request=resp.request, response=resp)
        resp.raise_for_status()
        return resp.json()


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _score_candidate(query: str, item: Dict[str, Any]) -> Tuple[float, List[str]]:
    rationale: List[str] = []
    score = 0.0
    qn = _norm_name(query)
    tn = _norm_name(item.get("title", ""))

    if tn == qn:
        score += 0.65
        rationale.append("Exact normalized name match.")
    elif qn and qn in tn:
        score += 0.35
        rationale.append("Query is substring of company name.")

    status = (item.get("company_status") or "").lower()
    if status == "active":
        score += 0.25
        rationale.append("Company status is active.")
    elif status:
        score += 0.10
        rationale.append(f"Company status is {status} (less preferred).")

    if item.get("date_of_creation"):
        score += 0.05
        rationale.append("Has incorporation date present.")

    return min(score, 0.99), rationale


# ----------------------------
# Internal API impl functions
# (Never call @mcp.tool functions internally; they become FunctionTool objects.)
# ----------------------------


async def _search_companies_impl(query: str, items_per_page: int = 10) -> Dict[str, Any]:
    return await _get_json(
        "/search/companies", params={"q": query, "items_per_page": items_per_page}
    )


async def _company_profile_impl(company_number: str) -> Dict[str, Any]:
    return await _get_json(f"/company/{company_number}")


async def _registered_office_address_impl(company_number: str) -> Dict[str, Any]:
    return await _get_json(f"/company/{company_number}/registered-office-address")


async def _list_officers_impl(company_number: str, items_per_page: int = 100) -> Dict[str, Any]:
    return await _get_json(
        f"/company/{company_number}/officers", params={"items_per_page": items_per_page}
    )


async def _filing_history_impl(
    company_number: str, items_per_page: int = 100, category: Optional[str] = None
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"items_per_page": items_per_page}
    if category:
        params["category"] = category
    return await _get_json(f"/company/{company_number}/filing-history", params=params)


async def _charges_impl(company_number: str, items_per_page: int = 100) -> Dict[str, Any]:
    return await _get_json(
        f"/company/{company_number}/charges", params={"items_per_page": items_per_page}
    )


async def _insolvency_impl(company_number: str) -> Dict[str, Any]:
    return await _get_json(f"/company/{company_number}/insolvency")


async def _psc_impl(company_number: str, items_per_page: int = 100) -> Dict[str, Any]:
    return await _get_json(
        f"/company/{company_number}/persons-with-significant-control",
        params={"items_per_page": items_per_page},
    )


async def _advanced_company_search_impl(
    sic_codes: Optional[List[str]] = None,
    location: Optional[str] = None,
    company_status: Optional[List[str]] = None,
    size: int = 20,
    start_index: int = 0,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"size": size, "start_index": start_index}
    if sic_codes:
        params["sic_codes"] = ",".join(sic_codes)
    if location:
        params["location"] = location
    if company_status:
        params["company_status"] = ",".join(company_status)
    return await _get_json("/advanced-search/companies", params=params)


# ----------------------------
# MCP Server
# ----------------------------

mcp = FastMCP(name="CompaniesHouseMCP")


@mcp.tool
async def search_companies(query: str, items_per_page: int = 10) -> Dict[str, Any]:
    """Search Companies House by company name (GET /search/companies)."""
    return await _search_companies_impl(query, items_per_page)


@mcp.tool
async def company_profile(company_number: str) -> Dict[str, Any]:
    """Get company profile (GET /company/{companyNumber})."""
    return await _company_profile_impl(company_number)


@mcp.tool
async def registered_office_address(company_number: str) -> Dict[str, Any]:
    """Get registered office address (GET /company/{companyNumber}/registered-office-address)."""
    return await _registered_office_address_impl(company_number)


@mcp.tool
async def list_officers(company_number: str, items_per_page: int = 100) -> Dict[str, Any]:
    """List officers (GET /company/{company_number}/officers)."""
    return await _list_officers_impl(company_number, items_per_page)


@mcp.tool
async def filing_history(
    company_number: str, items_per_page: int = 100, category: Optional[str] = None
) -> Dict[str, Any]:
    """List filing history (GET /company/{company_number}/filing-history)."""
    return await _filing_history_impl(company_number, items_per_page, category)


@mcp.tool
async def charges(company_number: str, items_per_page: int = 100) -> Dict[str, Any]:
    """List charges (GET /company/{company_number}/charges)."""
    return await _charges_impl(company_number, items_per_page)


@mcp.tool
async def insolvency(company_number: str) -> Dict[str, Any]:
    """Get insolvency data (GET /company/{company_number}/insolvency)."""
    return await _insolvency_impl(company_number)


@mcp.tool
async def persons_with_significant_control(
    company_number: str, items_per_page: int = 100
) -> Dict[str, Any]:
    """List PSC (GET /company/{company_number}/persons-with-significant-control)."""
    return await _psc_impl(company_number, items_per_page)


@mcp.tool
async def advanced_company_search(
    sic_codes: Optional[List[str]] = None,
    location: Optional[str] = None,
    company_status: Optional[List[str]] = None,
    size: int = 20,
    start_index: int = 0,
) -> Dict[str, Any]:
    """Advanced search (GET /advanced-search/companies) for competition sizing."""
    return await _advanced_company_search_impl(
        sic_codes, location, company_status, size, start_index
    )


@mcp.tool
async def resolve_company(identifier_or_name: str, max_candidates: int = 10) -> Dict[str, Any]:
    """
    Resolve a user-provided UK company name or company number to an intended legal entity.
    Returns a selected company_number, confidence, and ranked alternatives.
    """
    raw = identifier_or_name.strip()
    rationale: List[str] = []

    # 1) Direct company number path
    if _looks_like_company_number(raw):
        cn = raw.upper()
        try:
            profile = await _company_profile_impl(cn)
            return ResolveResult(
                selected_company_number=cn,
                confidence=0.98,
                rationale=["Input looks like a company number and profile lookup succeeded."],
                candidates=[
                    CandidateCompany(
                        company_number=cn,
                        title=profile["company_name"],
                        company_status=profile.get("company_status"),
                        date_of_creation=profile.get("date_of_creation"),
                        score=0.98,
                        score_rationale=["Direct company_number resolution."],
                    )
                ],
            ).model_dump()
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            rationale.append(
                f"Looked like a company number, but profile lookup failed: {type(e).__name__}"
            )

    # 2) Name search
    sr = await _search_companies_impl(raw, items_per_page=max_candidates)
    items = sr.get("items", []) or []

    candidates: List[CandidateCompany] = []
    for it in items:
        sc, why = _score_candidate(raw, it)
        candidates.append(
            CandidateCompany(
                company_number=it.get("company_number"),
                title=it.get("title"),
                company_status=it.get("company_status"),
                date_of_creation=it.get("date_of_creation"),
                address_snippet=it.get("address_snippet"),
                score=sc,
                score_rationale=why,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)

    if not candidates:
        return ResolveResult(
            selected_company_number=None,
            confidence=0.0,
            rationale=rationale + ["No candidates returned from /search/companies."],
            candidates=[],
        ).model_dump()

    top = candidates[0]
    confidence = min(0.90, max(0.40, top.score))
    rationale = (
        rationale + ["Selected best-scoring Companies House search result."] + top.score_rationale
    )

    return ResolveResult(
        selected_company_number=top.company_number,
        confidence=confidence,
        rationale=rationale,
        candidates=candidates,
    ).model_dump()


@mcp.tool
async def underwriting_pack(company_number: str) -> Dict[str, Any]:
    """
    Convenience tool: fetches a bundle of underwriting-relevant registry data in one call
    (profile, officers, PSC, charges, insolvency, recent filings).
    """
    profile, roa, offs, psc, chg = await asyncio.gather(
        _company_profile_impl(company_number),
        _registered_office_address_impl(company_number),
        _list_officers_impl(company_number),
        _psc_impl(company_number),
        _charges_impl(company_number),
    )

    # insolvency endpoint may 404 if none; treat as empty
    try:
        ins = await _insolvency_impl(company_number)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        ins = {"status": "not_found", "note": "No insolvency record returned (404)."}

    filings = await _filing_history_impl(company_number, items_per_page=50)

    return {
        "company_number": company_number,
        "profile": profile,
        "registered_office_address": roa,
        "officers": offs,
        "psc": psc,
        "charges": chg,
        "insolvency": ins,
        "filing_history_recent": filings,
        "sources": {
            "profile": f"{CH_BASE_URL}/company/{company_number}",
            "roa": f"{CH_BASE_URL}/company/{company_number}/registered-office-address",
            "officers": f"{CH_BASE_URL}/company/{company_number}/officers",
            "psc": f"{CH_BASE_URL}/company/{company_number}/persons-with-significant-control",
            "charges": f"{CH_BASE_URL}/company/{company_number}/charges",
            "insolvency": f"{CH_BASE_URL}/company/{company_number}/insolvency",
            "filings": f"{CH_BASE_URL}/company/{company_number}/filing-history",
        },
    }


if __name__ == "__main__":
    # Local dev default: stdio
    # Cloud Run: set MCP_TRANSPORT=http
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        mcp.run()
    else:
        port = int(os.getenv("PORT", "8080"))
        # transport="http" enables Streamable HTTP over /mcp by default
        mcp.run(transport="http", host="0.0.0.0", port=port)

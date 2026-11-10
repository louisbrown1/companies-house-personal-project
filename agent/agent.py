# agent.py
import os
import sys
from pathlib import Path

from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.parallel_agent import ParallelAgent
from google.adk.agents.sequential_agent import SequentialAgent
from google.adk.tools import google_search
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from mcp import StdioServerParameters

from agent_ui.model_config import agent_model, model_provider, unavailable_web_research
from agent_ui.validation import EvidenceBundle

# -----------------------
# Helpers
# -----------------------


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


# -----------------------
# MCP toolset wiring
# -----------------------


def companies_house_toolset(tool_filter: list[str]) -> McpToolset:
    """
    Create an MCP toolset for Companies House with explicit tool filtering and
    configurable timeouts for remote Streamable HTTP connections.
    """
    mcp_timeout = float(os.getenv("CH_MCP_TIMEOUT_SEC", "120"))
    mcp_sse_read_timeout = float(os.getenv("CH_MCP_SSE_READ_TIMEOUT_SEC", "180"))

    # Option A: local stdio subprocess
    if os.getenv("CH_MCP_MODE", "stdio") == "stdio":
        return McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=sys.executable,
                    args=[
                        os.getenv(
                            "CH_MCP_SERVER_PATH",
                            str(
                                Path(__file__).resolve().parent.parent
                                / "mcp_server"
                                / "ch_mcp_server.py"
                            ),
                        )
                    ],
                    env={**os.environ},
                ),
                timeout=mcp_timeout,
            ),
            tool_filter=tool_filter,
        )

    # Option B: remote Streamable HTTP MCP service (Cloud Run)
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=os.environ["CH_MCP_URL"],  # e.g. https://.../mcp
            timeout=mcp_timeout,
            sse_read_timeout=mcp_sse_read_timeout,
            headers={"Authorization": f"Bearer {os.environ['CH_MCP_AUTH']}"}
            if os.getenv("CH_MCP_AUTH")
            else None,
        ),
        tool_filter=tool_filter,
    )


# -----------------------
# Agent factory
# -----------------------


def build_root_agent(*, approved_context: bool = False):
    """
    Build a fresh root agent with fresh MCP toolsets.
    Use this in app.py when rebuilding runtime after MCP session termination.
    """

    # Create smaller MCP toolsets per sub-agent (reduces discovery load/timeouts)
    ch_resolver_tools = companies_house_toolset(["resolve_company"])
    ch_registry_tools = companies_house_toolset(["underwriting_pack"])
    ch_competition_tools = companies_house_toolset(["advanced_company_search"])

    # -----------------------
    # Sub-agents
    # -----------------------

    entity_resolver = LlmAgent(
        name="EntityResolver",
        model=agent_model(),
        instruction=(
            "Resolve the user's input to a single UK legal entity.\n"
            "You MUST call resolve_company first.\n"
            "Return ONLY JSON (no markdown, no prose) with this exact shape:\n"
            "{\n"
            '  "selected_company_number": "string|null",\n'
            '  "selected_company_name": "string|null",\n'
            '  "confidence": 0.0,\n'
            '  "rationale": ["..."],\n'
            '  "candidates": [\n'
            "    {\n"
            '      "company_number": "string",\n'
            '      "title": "string",\n'
            '      "company_status": "string|null",\n'
            '      "date_of_creation": "string|null",\n'
            '      "address_snippet": "string|null",\n'
            '      "score": 0.0\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- If confidence >= 0.7, set selected_company_number and selected_company_name.\n"
            "- If confidence < 0.7, set selected_company_number and selected_company_name to null and keep candidates.\n"
            "- Do not invent fields. Use tool output only."
        ),
        tools=[ch_resolver_tools],
        output_key="entity_resolution",
    )

    registry_collector = LlmAgent(
        name="RegistryPackCollector",
        model=agent_model(),
        instruction=(
            "Use entity_resolution to fetch registry data.\n"
            "Input state:\n{entity_resolution?}\n\n"
            "If entity_resolution.selected_company_number is null, return ONLY JSON:\n"
            '{"error":"entity_not_resolved"}\n'
            "Otherwise call underwriting_pack with the selected company number.\n"
            "Return ONLY the raw underwriting_pack JSON from the tool (do not summarize, do not remove fields)."
        ),
        tools=[ch_registry_tools],
        output_key="registry_pack",
    )

    company_context_builder = LlmAgent(
        name="CompanyContextBuilder",
        model=agent_model(),
        instruction=(
            "Build a canonical company context for web search from the following state.\n"
            "entity_resolution:\n{entity_resolution?}\n\n"
            "registry_pack:\n{registry_pack?}\n\n"
            "Return ONLY JSON with this exact shape:\n"
            "{\n"
            '  "resolved": true,\n'
            '  "company_number": "string|null",\n'
            '  "company_name": "string|null",\n'
            '  "search_names": ["string"],\n'
            '  "location_hint": "string|null",\n'
            '  "website_hint": "string|null",\n'
            '  "sic_codes": ["string"],\n'
            '  "uncertainty": ["..."]\n'
            "}\n"
            "Rules:\n"
            "- Use registry_pack.profile.company_name as primary company_name when available.\n"
            "- Use registry_pack.profile.sic_codes when available.\n"
            "- If unresolved or registry data missing, set resolved=false and explain in uncertainty.\n"
            "- Do not invent website URLs."
        ),
        output_key="company_context",
    )

    competition_researcher = LlmAgent(
        name="CompetitionResearcher",
        model=agent_model(),
        instruction=(
            "Estimate competition using Companies House SIC codes + geography.\n"
            "Input state:\n"
            "registry_pack:\n{registry_pack?}\n\n"
            "company_context:\n{company_context?}\n\n"
            "If registry_pack has no profile or no sic_codes, return ONLY JSON:\n"
            '{"competition_level":"unknown","basis":{"sic_codes":[],"location":null},"density_proxy":null,"named_competitors":[],"uncertainty":["Missing SIC codes in registry_pack."]}\n'
            "Otherwise:\n"
            "- Use registry_pack.profile.sic_codes and registered office/location hints.\n"
            "- Call advanced_company_search to estimate active companies in same SIC/location.\n"
            "- Request size=1 and use the API's total_results or hits field for the count, never the returned page length.\n"
            "Return ONLY JSON with this exact shape:\n"
            "{\n"
            '  "competition_level": "low|medium|high|unknown",\n'
            '  "basis": {\n'
            '    "sic_codes": ["string"],\n'
            '    "location": "string|null",\n'
            '    "method": "string"\n'
            "  },\n"
            '  "density_proxy": {\n'
            '    "active_company_count_estimate": 0,\n'
            '    "source_url": "string|null",\n'
            '    "confidence": 0.0\n'
            "  } | null,\n"
            '  "named_competitors": [],\n'
            '  "uncertainty": ["..."]\n'
            "}\n"
            "Use only supported tool outputs. Do not invent named competitors."
        ),
        tools=[ch_competition_tools],
        output_key="competition",
    )

    review_harvester = LlmAgent(
        name="ReviewHarvester",
        model=agent_model(),
        instruction=(
            "You are collecting public company quality signals from web sources.\n"
            "Input state:\n"
            "company_context:\n{company_context?}\n\n"
            "entity_resolution:\n{entity_resolution?}\n\n"
            "If company_context.resolved is false OR company_context.company_name is null, return ONLY JSON:\n"
            "{"
            '"review_sources":[],"trade_press_mentions":[],"positives":[],"negatives":[],"uncertainty":["Company not resolved; cannot search accurately."]'
            "}\n\n"
            "You MUST use Google Search at least once before answering.\n"
            "Search strategy:\n"
            "- Use exact company name in quotes.\n"
            "- Use the Companies House number when disambiguation is needed.\n"
            "- Prioritize official review pages, reputable trade publications, and recent sources.\n"
            "Return ONLY JSON with this exact shape:\n"
            "{\n"
            '  "review_sources": [\n'
            "    {\n"
            '      "site": "string",\n'
            '      "rating": 0.0,\n'
            '      "review_count": 0,\n'
            '      "url": "https://...",\n'
            '      "snippet": "string",\n'
            '      "as_of": "YYYY-MM-DD|null"\n'
            "    }\n"
            "  ],\n"
            '  "trade_press_mentions": [\n'
            "    {\n"
            '      "publisher": "string",\n'
            '      "headline": "string",\n'
            '      "date": "YYYY-MM-DD|null",\n'
            '      "url": "https://...",\n'
            '      "summary": "string",\n'
            '      "sentiment": "positive|neutral|negative|mixed"\n'
            "    }\n"
            "  ],\n"
            '  "positives": [\n'
            '    {"theme":"string","evidence_urls":["https://..."]}\n'
            "  ],\n"
            '  "negatives": [\n'
            '    {"theme":"string","evidence_urls":["https://..."]}\n'
            "  ],\n"
            '  "uncertainty": ["..."]\n'
            "}\n"
            "Rules:\n"
            "- Every factual item MUST include a source URL.\n"
            "- If rating/review_count cannot be verified, omit that source rather than guessing.\n"
            "- Do not fabricate quotes."
        ),
        tools=[google_search] if model_provider() == "gemini" else [],
        before_agent_callback=unavailable_web_research if model_provider() == "gemma" else None,
        output_key="quality_signals",
    )

    evidence_extractor = LlmAgent(
        name="EvidenceExtractor",
        model=agent_model(),
        instruction=(
            "Normalize the following into evidence items for underwriting.\n\n"
            "registry_pack:\n{registry_pack?}\n\n"
            "competition:\n{competition?}\n\n"
            "quality_signals:\n{quality_signals?}\n\n"
            "Return ONLY JSON with this exact shape:\n"
            "{\n"
            '  "evidence": [\n'
            "    {\n"
            '      "evidence_id": "ev_001",\n'
            '      "claim": "string",\n'
            '      "source": {\n'
            '        "url": "https://...",\n'
            '        "kind": "companies_house_api|review_site|trade_press|search_result|other"\n'
            "      },\n"
            '      "verbatim_excerpt": "string",\n'
            '      "structured_fact": {},\n'
            '      "confidence": 0.0\n'
            "    }\n"
            "  ],\n"
            '  "uncertainty": ["..."]\n'
            "}\n"
            "Rules:\n"
            "- EVERY evidence item MUST include source.url.\n"
            "- Use Companies House source URLs from registry_pack.sources where possible.\n"
            "- If a claim has no URL, do NOT include it as evidence; instead add an uncertainty note.\n"
            "- Keep evidence_ids unique and stable within the response."
            "\n- Carry registry_pack.scope.limitations into uncertainty when present."
        ),
        output_key="evidence",
        output_schema=EvidenceBundle,
    )

    underwriting_synthesizer = LlmAgent(
        name="UnderwritingSynthesizer",
        model=agent_model(synthesis=True),
        instruction=(
            "Using evidence only, produce an explainable underwriting intelligence report.\n"
            "Evidence input:\n{evidence?}\n\n"
            "Do not invent facts. If evidence is missing, say what would resolve it.\n\n"
            "Output MUST be markdown and MUST include all sections below in this order:\n"
            "## Business Model Summary\n"
            "- Summarize what the company does using evidence IDs in-line (e.g., [ev_001]).\n\n"
            "## Competition (Structured)\n"
            "- Sector basis (SIC / registry basis)\n"
            "- Geographic basis\n"
            "- Competition level\n"
            "- Confidence assessment\n"
            "- Rationale with evidence IDs\n\n"
            "## Company Quality (Structured)\n"
            "- Review sources summary\n"
            "- Trade press summary\n"
            "- Positive themes\n"
            "- Negative themes\n"
            "- Confidence assessment\n"
            "- Rationale with evidence IDs\n\n"
            "## Explicit Citation Block\n"
            "Provide a bullet list mapping EVERY referenced evidence_id to URL in this exact format:\n"
            "- ev_001 -> https://...\n"
            "- ev_002 -> https://...\n\n"
            "## Uncertainty and Limitations\n"
            "- List evidence gaps, ambiguity, or weak coverage.\n\n"
            "Rules:\n"
            "- Every material claim must reference at least one evidence_id.\n"
            "- Every evidence_id you reference must appear in the Explicit Citation Block.\n"
            "- If no evidence exists for a requested claim, state that explicitly instead of inferring."
        ),
        output_key="report",
    )

    # -----------------------
    # Orchestration
    # -----------------------

    use_parallel = _bool_env("USE_PARALLEL", default=True)

    if use_parallel:
        # registry_collector runs before this, so competition/reviews can run in parallel.
        parallel_research = ParallelAgent(
            name="ParallelResearch",
            sub_agents=[competition_researcher, review_harvester],
        )

        root = SequentialAgent(
            name="UnderwritingPipeline",
            sub_agents=[
                *(
                    []
                    if approved_context
                    else [entity_resolver, registry_collector, company_context_builder]
                ),
                parallel_research,
                evidence_extractor,
                underwriting_synthesizer,
            ],
        )
    else:
        # Debug mode: fully sequential to reduce timeout/concurrency issues
        root = SequentialAgent(
            name="UnderwritingPipeline",
            sub_agents=[
                *(
                    []
                    if approved_context
                    else [entity_resolver, registry_collector, company_context_builder]
                ),
                competition_researcher,
                review_harvester,
                evidence_extractor,
                underwriting_synthesizer,
            ],
        )

    return root


# Backwards-compatible export for code that imports `root_agent` directly.
# If  app.py supports runtime rebuilds, prefer importing build_root_agent().
# Lazy export avoids opening unused toolsets when the UI imports the factory.
def __getattr__(name):
    if name == "root_agent":
        return build_root_agent()
    raise AttributeError(name)

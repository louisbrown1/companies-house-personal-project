import json
from types import SimpleNamespace

import pytest

from agent_ui import local_report as local
from agent_ui.validation import ReportValidationError


def pack():
    return {
        "company_number": "01357507",
        "profile": {
            "company_number": "01357507",
            "company_name": "Example Ltd",
            "sic_codes": ["47190"],
            "registered_office_address": {"locality": "Liverpool"},
        },
        "charges": {"total_count": 0},
    }


ARGS = {"sic_codes": ["47190"], "location": "Liverpool", "company_status": ["active"], "size": 1}


def test_registry_count_uses_total_not_page_length_and_canonical_source():
    bundle, groups = local.build_evidence(pack(), {"hits": 123, "items": [{}]}, ARGS)
    report = local.render_report(bundle, groups, ["ev_001"])
    assert '"count": 123' in report
    assert "&#" not in report
    assert "/advanced-search/companies?" in report
    assert "/company/01357507/advanced-search" not in report
    assert "Competition level: unknown" in report
    citations = [line for line in report.splitlines() if " -> " in line]
    assert len(citations) == len(bundle.evidence) == len(set(citations))


@pytest.mark.parametrize("response", [None, {"items": [{}]}, {"hits": True}, {"hits": -1}])
def test_missing_count_is_not_invented_or_inferred_from_items(response):
    bundle, groups = local.build_evidence(pack(), response, ARGS)
    assert groups["competition"] == []
    assert "count unavailable" in local.render_report(bundle, groups, [])


def test_zero_count_is_preserved():
    bundle, groups = local.build_evidence(pack(), {"hits": 0}, ARGS)
    assert '"count": 0' in local.render_report(bundle, groups, [])


@pytest.mark.parametrize(
    "selection",
    [
        {"selected_ids": ["ev_999"]},
        {"selected_ids": ["ev_001"], "claim": "fabricated"},
        {"selected_ids": "ev_001"},
    ],
)
def test_model_cannot_supply_claims_or_unknown_citations(selection):
    with pytest.raises(ReportValidationError):
        local.validate_selection(json.dumps(selection), ["ev_001"])


def test_duplicate_highlights_are_collapsed_without_inventing_ids():
    assert local.validate_selection('{"selected_ids":["ev_001","ev_001"]}', ["ev_001"]) == [
        "ev_001"
    ]
    bundle, groups = local.build_evidence(pack(), None, ARGS)
    report = local.render_report(bundle, groups, ["ev_001", "ev_001"])
    assert report.count("- ev_001 ->") == 1


@pytest.mark.asyncio
async def test_live_search_arguments_and_explicit_failure_gap(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    calls = []

    async def search(name, args):
        calls.append((name, args))
        raise RuntimeError("Unavailable")

    async def select(bundle, prompt):
        return ["ev_001"]

    monkeypatch.setattr(local, "select_highlights", select)
    report = await local.generate_local_report(pack(), "test", search)
    assert calls[0][0] == "advanced_company_search"
    assert calls[0][1]["size"] == 1
    assert calls[0][1]["sic_codes"] == ["47190"]
    assert "count unavailable" in report
    assert "not a lending recommendation" in report


@pytest.mark.asyncio
async def test_highlight_request_uses_litert_compatible_schema(monkeypatch):
    class Model:
        async def generate_content_async(self, request, stream):
            schema = request.config.response_schema.model_json_schema()
            assert schema["type"] == "object"
            assert schema["properties"]["selected_ids"]["maxItems"] == 5
            assert "ev_001" in schema["properties"]["selected_ids"]["items"]["enum"]
            yield SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(
                    parts=[SimpleNamespace(text='{"selected_ids":["ev_001"]}')]
                ),
            )

    monkeypatch.setattr(local, "agent_model", lambda: Model())
    bundle, _ = local.build_evidence(pack(), None, ARGS)
    assert await local.select_highlights(bundle, "test") == ["ev_001"]


@pytest.mark.asyncio
async def test_financial_observations_render_even_when_not_selected(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    async def registry(*args):
        return {"hits": 1}

    async def accounts(*args):
        filings = []
        for period, revenue, profit in [("2026-03-28", "200", "10"), ("2025-03-29", "100", "20")]:
            figures = {
                k: {
                    "value": v,
                    "scale": 1000000,
                    "currency": "GBP",
                    "scope": "company",
                    "period": period,
                    "page": 1,
                    "excerpt": k + " " + v,
                }
                for k, v in [("revenue", revenue), ("operating_profit", profit)]
            }
            filings.append(
                {
                    "period": period,
                    "filed": period,
                    "description": "full",
                    "url": "https://example.com/accounts",
                    "figures": figures,
                }
            )
        filings[0]["comparatives"] = filings[1]["figures"]
        return {"filings": filings, "gaps": []}

    calls = []
    prompts = []

    async def select(bundle, prompt):
        calls.append(bundle)
        prompts.append(prompt)
        return [bundle.evidence[0].evidence_id]

    monkeypatch.setattr(local, "collect_accounts", accounts)
    monkeypatch.setattr(local, "select_highlights", select)
    report = await local.generate_local_report(pack(), "test", registry)
    assert len(calls) == 2
    assert "Retail accounts interpretation" in prompts[1]
    assert "Banking accounts interpretation" not in prompts[1]
    assert "not company evidence" in prompts[1]
    assert all(item.claim == "financial_observation" for item in calls[1].evidence)
    assert "## Financial Health" in report
    assert "Other calculated observations" in report
    assert "Operating profit decreased 50.00%" in report
    assert "operating margin 5.00%" in report
    assert "#page=1" in report

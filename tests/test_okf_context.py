import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from agent_ui import hybrid_accounts
from agent_ui.okf_context import company_context_type, load_context, prompt_context

TODAY = date(2026, 11, 7)


@pytest.mark.parametrize(
    "codes,expected",
    [
        (["64191"], "bank"),
        (["64192"], "bank"),
        (["47190"], "retail"),
        (["62020"], "trading"),
        ([], "trading"),
        (["47190", "64191"], "bank"),
    ],
)
def test_business_context_selection(codes, expected):
    assert company_context_type(codes) == expected


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("bank", ["common", "banking"]),
        ("retail", ["common", "retail"]),
        ("trading", ["common"]),
        ("../../elsewhere", ["common"]),
    ],
)
def test_bundled_context_is_bounded_scoped_and_honest_about_trust(kind, expected):
    context = load_context(kind, today=TODAY)
    assert [c["id"] for c in context["manifest"]["concepts"]] == expected
    assert all(c["trust"] == "unverified" for c in context["manifest"]["concepts"])
    assert len(context["text"]) <= 6000
    assert not context["manifest"]["excluded"]
    assert "not company evidence" in prompt_context(context)


def concept(root, front="type: Playbook", body="Local interpretation guidance"):
    (root / "common.md").write_text(f"---\n{front}\n---\n{body}", encoding="utf-8")


@pytest.mark.parametrize(
    "front",
    [
        "type: Playbook\nstatus: draft",
        "type: Playbook\nstatus: deprecated",
        "type: Playbook\nstale_after: 2026-11-06",
        "type: Playbook\nstale_after: not-a-date",
        "title: No type",
        "type: Playbook\nverified: someone",
        "!!python/object/apply:os.system [echo unsafe]",
    ],
)
def test_unusable_concepts_are_excluded_without_executing_or_blocking(front, tmp_path):
    concept(tmp_path, front)
    context = load_context("trading", root=tmp_path, today=TODAY)
    assert context["text"] == ""
    assert context["manifest"]["excluded"] == ["common"]


def test_expiry_boundary_missing_files_and_unknown_metadata(tmp_path):
    concept(tmp_path, "type: Playbook\nstale_after: 2026-11-07\ncustom: preserved-by-author")
    assert load_context("trading", root=tmp_path, today=TODAY)["text"]
    assert load_context("trading", root=tmp_path, today=date(2026, 11, 8))["text"] == ""
    assert load_context("bank", root=tmp_path, today=TODAY)["manifest"]["excluded"] == ["banking"]


def test_oversized_concept_is_not_loaded(tmp_path):
    concept(tmp_path, body="a" * 9000)
    assert load_context("trading", root=tmp_path, today=TODAY)["text"] == ""


def test_context_enters_model_request_and_metadata_changes_invalidate_cache(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    concept(
        root,
        'type: Playbook\nsources: [{resource: "repo:original"}]',
        body="Unique banking definition",
    )
    cache = tmp_path / "cache"
    monkeypatch.setattr(hybrid_accounts, "CACHE", cache)
    requests = []

    class Model:
        async def generate_content_async(self, request, stream):
            requests.append(request)
            yield SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text='{"mappings":[{"row_id":"r1","metric":"total_assets"}]}'
                        )
                    ]
                ),
            )

    monkeypatch.setattr(hybrid_accounts, "agent_model", lambda: Model())
    rows = [
        {
            "row_id": "r1",
            "table_title": "Balance sheet",
            "label": "total assets",
            "allowed_metrics": ["total_assets"],
            "cells": [
                {
                    "source_text": "10",
                    "scale": 1000,
                    "period": "2026-06-30",
                    "scope": "company",
                    "cell_id": "c1",
                }
            ],
        }
    ]
    context = load_context("trading", root=root, today=TODAY)
    result = asyncio.run(hybrid_accounts.model_mappings(rows, "trading", context))
    assert result == ({"r1": "total_assets"}, "completed")
    assert "Unique banking definition" in requests[0].contents[0].parts[0].text
    assert "Permitted metrics for this row" in requests[0].contents[0].parts[0].text
    schema = requests[0].config.response_schema.model_json_schema()
    assert schema["$defs"]["SourceMapping"]["properties"]["row_id"]["const"] == "r1"
    assert asyncio.run(hybrid_accounts.model_mappings(rows, "trading", context))[1] == "cached"
    concept(
        root,
        'type: Playbook\nsources: [{resource: "repo:revised"}]',
        body="Unique banking definition",
    )
    revised = load_context("trading", root=root, today=TODAY)
    assert asyncio.run(hybrid_accounts.model_mappings(rows, "trading", revised))[1] == "completed"
    assert len(requests) == 2

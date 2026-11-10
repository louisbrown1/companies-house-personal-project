import asyncio
import json
from pathlib import Path

import pytest

from agent_ui import page_selection as ps
from agent_ui.account_fields import fields_for
from agent_ui.document_navigation import heading_candidates, note_ranges

INDEX = {
    "page_count": 100,
    "sections": [
        {"section_id": "s001", "title": "Capital adequacy", "start_page": 80, "end_page": 90},
        {"section_id": "s002", "title": "Liquidity risk", "start_page": 91, "end_page": 95},
    ],
}


def payload(**changes):
    item = {
        "field_id": "cet1",
        "section_ids": ["s001"],
        "reason": "Published capital note",
        "priority": "high",
    }
    item.update(changes)
    return json.dumps({"selections": [item], "unresolved": []})


@pytest.mark.parametrize(
    "changes",
    [
        {"field_id": "invented"},
        {"section_ids": ["invented"]},
        {"section_ids": ["s001", "s001"]},
        {"priority": "urgent"},
        {"pages": [2]},
    ],
)
def test_rejects_untrusted_plan(changes):
    with pytest.raises(ValueError):
        ps.validate_plan(payload(**changes), INDEX, fields_for("bank"))


def test_budget_and_truncation_are_explicit():
    plan = ps.validate_plan(payload(), INDEX, fields_for("bank"))
    pages, audit = ps.allocate_pages(plan, INDEX)
    assert pages == list(range(80, 88))
    assert audit[0]["range_truncated"]
    pages, audit = ps.allocate_pages(plan, INDEX, budget=4)
    assert pages == [] and audit[0]["budget_skipped"]
    assert "lcr" in plan.unresolved


def test_invalid_source_range_rejected():
    plan = ps.validate_plan(payload(), INDEX, fields_for("bank"))
    bad = {**INDEX, "sections": [{**INDEX["sections"][0], "end_page": 101}]}
    with pytest.raises(ValueError):
        ps.allocate_pages(plan, bad)


def test_date_and_body_text_are_not_headings():
    def box(text):
        return {"text": text, "score": 1, "box": [[0, 200], [300, 200], [300, 230], [0, 230]]}

    found = heading_candidates(
        {
            "page": 282,
            "boxes": [
                box("31 March 2025. Tier 1 capital increased"),
                box("funding sources. Through active liquidity management,"),
                box("63. Capital management and allocation"),
            ],
        }
    )
    assert [h["title"] for h in found] == ["63. Capital management and allocation"]


def test_notes_use_printed_offset_and_next_chapter():
    entries = [
        {"title": "Notes to the financial statements", "printed_page": 148},
        {"title": "Notes to risk and capital management", "printed_page": 240},
        {"title": "Sustainability", "printed_page": 290},
    ]
    assert note_ranges(entries, 361, 2) == [(149, 242), (241, 292)]


def test_field_definitions_and_runbooks_are_separate():
    bank = {f.field_id: f for f in fields_for("bank")}
    assert "not loans/deposits" in bank["lcr"].description
    assert "leases" in {f.field_id for f in fields_for("retail")}
    context = ps.load_context("bank", root=ps.RUNBOOKS)
    assert not context["manifest"]["excluded"]
    assert {c["id"] for c in context["manifest"]["concepts"]} == {"common", "banking"}


def test_followup_uses_extraction_feedback_and_cleanup(monkeypatch, tmp_path):
    raw = json.loads((Path(__file__).parent / "fixtures/investec_statements_2026.json").read_text())
    from agent_ui.document_context import bind_document

    doc = bind_document(raw, "INVESTEC BANK PLC", "00489604", "2026-03-31")
    index = {**INDEX, "sha256": "indexhash", "index_pages": [80, 91], "index_truncated": False}
    monkeypatch.setattr(ps, "build_index", lambda *args: index)
    monkeypatch.setattr(ps.accounts, "CACHE", tmp_path)
    monkeypatch.setattr(
        ps, "read_pages", lambda path, document, pages: [{"page": p, "boxes": []} for p in pages]
    )
    calls = []

    async def propose(index, fields, runbook, feedback):
        calls.append((index, feedback))
        sid = "s001" if len(calls) == 1 else "s002"
        return ps.validate_plan(payload(section_ids=[sid]), index, fields), {
            "status": "test",
            "batches": [],
        }

    monkeypatch.setattr(ps, "propose_plan", propose)
    result, audit = asyncio.run(
        ps.select_note_pages(None, doc, "bank", evidence_check=lambda d: ["cet1"])
    )
    assert len(calls) == 2
    assert calls[1][1]["unresolved_fields"]
    assert "s001" not in {s["section_id"] for s in calls[1][0]["sections"]}
    assert 80 in audit["selected_pages"] and 91 in audit["selected_pages"]
    assert audit["rounds"][0]["missing_validated_fields"] == ["cet1"]
    assert result["identity"]["verified"]


def test_ranking_keeps_all_locations_and_prioritises_field_descriptions():
    fields = [f for f in fields_for("bank") if f.field_id in ("cet1", "lcr", "nsfr")]
    index = {
        **INDEX,
        "sections": [
            {"section_id": "irrelevant", "title": "Operating costs", "start_page": 1, "end_page": 2}
        ]
        + INDEX["sections"],
    }
    ranked = ps.ranked_sections(index, fields)
    assert ranked[-1]["section_id"] == "irrelevant"
    assert {s["section_id"] for s in ranked} == {s["section_id"] for s in index["sections"]}


def test_conflicting_model_assignment_is_rejected():
    raw = json.loads(payload())
    raw["unresolved"] = ["cet1", "cet1"]
    with pytest.raises(ValueError):
        ps.validate_plan(json.dumps(raw), INDEX, fields_for("bank"))


def test_separate_number_and_heading_boxes_are_joined():
    def box(text, x, y):
        return {
            "text": text,
            "score": 1,
            "box": [[x, y], [x + 100, y], [x + 100, y + 20], [x, y + 20]],
        }

    page = {
        "page": 99,
        "boxes": [
            box("26", 50, 649),
            box("Contingent liabilities and guarantees", 180, 649),
            box("52 weeks ended 28 March 2026", 50, 220),
            box("1 Aug 23", 50, 300),
        ],
    }
    titles = [h["title"] for h in heading_candidates(page)]
    assert "26 Contingent liabilities and guarantees" in titles
    assert not any(t.startswith(("52", "1 Aug")) for t in titles)


def test_retail_definitions_distinguish_costs_and_maturities_from_policies():
    fields = {f.field_id: f for f in fields_for("retail")}
    assert "liquidity risk" in fields["leases"].description
    assert "do not supply a maturity schedule" in fields["leases"].description
    assert "Employee remuneration" in fields["employee_costs"].description


def test_financial_review_and_prefixed_note_headings_are_indexed():
    entries = [
        {"title": "Chief Financial Officer's review", "printed_page": 19},
        {"title": "People", "printed_page": 25},
    ]
    assert note_ranges(entries, 180, 0) == [(18, 25)]
    page = {
        "page": 24,
        "boxes": [
            {"text": t, "score": 1, "box": [[60, y], [360, y], [360, y + 20], [60, y + 20]]}
            for t, y in [("Capital", 650), ("Liquidity", 900), ("Note 26. Financial assets", 550)]
        ],
    }
    assert {h["title"] for h in heading_candidates(page)} == {
        "Capital",
        "Liquidity",
        "26 Financial assets",
    }


def stub_model(monkeypatch, tmp_path, respond):
    from types import SimpleNamespace

    from google.genai import types

    calls = []

    class Model:
        async def generate_content_async(self, request, stream=False):
            assert request.config.response_mime_type == "application/json"
            assert (
                request.config.response_schema.model_json_schema()["additionalProperties"] is False
            )
            prompt = request.contents[0].parts[0].text
            data, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"fields":') :])
            fields = tuple(f["field_id"] for f in data["fields"])
            calls.append(fields)
            result = respond(fields, calls.count(fields))
            if isinstance(result, BaseException):
                raise result
            text, finish_reason = result if isinstance(result, tuple) else (result, None)
            yield SimpleNamespace(
                error_code=None,
                finish_reason=finish_reason,
                content=types.Content(parts=[types.Part(text=text)]),
            )

    monkeypatch.setattr(ps.accounts, "CACHE", tmp_path)
    monkeypatch.setattr(ps, "agent_model", lambda: Model())
    return calls


def valid_batch(fields, section="s001"):
    return json.dumps(
        {
            "selections": [
                {
                    "field_id": f,
                    "section_ids": [section],
                    "reason": "Relevant indexed note",
                    "priority": "high",
                }
                for f in fields
            ],
            "unresolved": [],
        }
    )


def test_failed_last_batch_retries_without_regenerating_successes(monkeypatch, tmp_path):
    calls = stub_model(
        monkeypatch,
        tmp_path,
        lambda fields, n: (
            '{"selections":' if fields == ("securitisation",) and n == 1 else valid_batch(fields)
        ),
    )
    plan, audit = asyncio.run(
        ps.propose_plan(INDEX, fields_for("bank"), ps.load_context("bank", root=ps.RUNBOOKS))
    )
    assert len(plan.selections) == 10 and not plan.unresolved
    assert len(calls) == 5 and calls[-2:] == [("securitisation",)] * 2
    assert audit["status"] == "completed"
    assert [a["status"] for a in audit["batches"][-1]["attempts"]] == ["failed", "completed"]
    assert len(list(tmp_path.glob("*selection-plan.json"))) == 4


def test_exhausted_batch_preserves_other_batches_and_is_not_cached(monkeypatch, tmp_path):
    calls = stub_model(
        monkeypatch,
        tmp_path,
        lambda fields, n: "not JSON" if "cash_flow" in fields else valid_batch(fields),
    )
    runbook = ps.load_context("bank", root=ps.RUNBOOKS)
    plan, audit = asyncio.run(ps.propose_plan(INDEX, fields_for("bank"), runbook))
    assert len(plan.selections) == 7
    assert set(plan.unresolved) == {"profit_explanation", "cash_flow", "related_parties"}
    assert audit["status"] == "partial_failure" and len(calls) == 6
    assert len(list(tmp_path.glob("*selection-plan.json"))) == 3
    calls.clear()
    again, audit = asyncio.run(ps.propose_plan(INDEX, fields_for("bank"), runbook))
    assert again == plan and len(calls) == ps.MAX_BATCH_ATTEMPTS
    assert sum(b["status"] == "cached" for b in audit["batches"]) == 3


@pytest.mark.parametrize(
    "invalid",
    [
        '{"selections":',
        payload(section_ids=["invented"]),
        payload(section_ids=["s001", "s001"]),
        payload(field_id="invented"),
        payload(reason=123),
        payload(pages=[91]),
        json.dumps({"selections": [], "unresolved": []}),
        json.dumps({"selections": json.loads(payload())["selections"] * 2, "unresolved": []}),
        json.dumps({"selections": json.loads(payload())["selections"], "unresolved": ["cet1"]}),
    ],
)
def test_strict_validation_retries_invalid_json_and_assignments(monkeypatch, tmp_path, invalid):
    calls = stub_model(
        monkeypatch, tmp_path, lambda fields, n: invalid if n == 1 else valid_batch(fields)
    )
    fields = [f for f in fields_for("bank") if f.field_id == "cet1"]
    plan, audit = asyncio.run(
        ps.propose_plan(INDEX, fields, ps.load_context("bank", root=ps.RUNBOOKS))
    )
    assert len(calls) == 2 and len(plan.selections) == 1
    assert audit["batches"][0]["attempts"][0]["status"] == "failed"
    assert len(list(tmp_path.glob("*selection-plan.json"))) == 1


def test_invalid_cache_is_revalidated_and_replaced(monkeypatch, tmp_path):
    calls = stub_model(monkeypatch, tmp_path, lambda fields, n: valid_batch(fields))
    fields = [f for f in fields_for("bank") if f.field_id == "cet1"]
    runbook = ps.load_context("bank", root=ps.RUNBOOKS)
    asyncio.run(ps.propose_plan(INDEX, fields, runbook))
    cache = next(tmp_path.glob("*selection-plan.json"))
    cache.write_text(payload(section_ids=["invented"]))
    plan, audit = asyncio.run(ps.propose_plan(INDEX, fields, runbook))
    assert len(calls) == 2 and plan.selections[0].section_ids == ["s001"]
    assert audit["batches"][0]["cache_invalid"] is True
    assert ps.validate_plan(cache.read_text(), INDEX, fields) == plan


def test_cancellation_propagates_without_retry(monkeypatch, tmp_path):
    calls = stub_model(monkeypatch, tmp_path, lambda fields, n: asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            ps.propose_plan(INDEX, fields_for("bank"), ps.load_context("bank", root=ps.RUNBOOKS))
        )
    assert len(calls) == 1 and not list(tmp_path.iterdir())


def test_transient_timeout_is_retried(monkeypatch, tmp_path):
    calls = stub_model(
        monkeypatch, tmp_path, lambda fields, n: TimeoutError() if n == 1 else valid_batch(fields)
    )
    fields = [f for f in fields_for("bank") if f.field_id == "cet1"]
    plan, audit = asyncio.run(
        ps.propose_plan(INDEX, fields, ps.load_context("bank", root=ps.RUNBOOKS))
    )
    assert len(calls) == 2 and plan.selections
    assert audit["batches"][0]["attempts"][0]["error_type"] == "TimeoutError"


def test_retrieval_keeps_successful_pages_when_final_batch_exhausts_retries(monkeypatch, tmp_path):
    from agent_ui.document_context import bind_document

    raw = json.loads((Path(__file__).parent / "fixtures/investec_statements_2026.json").read_text())
    doc = bind_document(raw, "INVESTEC BANK PLC", "00489604", "2026-03-31")
    monkeypatch.setattr(
        ps,
        "build_index",
        lambda *args: {
            **INDEX,
            "sha256": "indexhash",
            "index_pages": [80, 91],
            "index_truncated": False,
        },
    )
    monkeypatch.setattr(
        ps, "read_pages", lambda path, document, pages: [{"page": p, "boxes": []} for p in pages]
    )
    calls = stub_model(
        monkeypatch,
        tmp_path,
        lambda fields, n: "broken" if fields == ("securitisation",) else valid_batch(fields),
    )
    result, audit = asyncio.run(ps.select_note_pages(None, doc, "bank"))
    assert audit["selected_pages"] == list(range(80, 88))
    assert 80 in [p["page"] for p in result["pages"]]
    assert audit["failed_fields"] == ["securitisation"]
    assert audit["unresolved_fields"] == ["securitisation"]
    assert audit["status"] == "partial_failure"
    assert calls.count(("securitisation",)) == 2 * ps.MAX_BATCH_ATTEMPTS


def test_setup_failure_is_isolated_to_its_batch(monkeypatch):
    async def batch(index, fields, runbook, feedback):
        if fields[0].field_id == "cet1":
            raise ValueError("Prompt exceeds budget")
        return ps.validate_plan(valid_batch([f.field_id for f in fields]), index, fields), {
            "field_ids": [f.field_id for f in fields],
            "status": "completed",
            "attempts": [],
        }

    monkeypatch.setattr(ps, "propose_batch", batch)
    plan, audit = asyncio.run(ps.propose_plan(INDEX, fields_for("bank"), {}))
    assert len(plan.selections) == 7 and set(plan.unresolved) == {"cet1", "lcr", "nsfr"}
    assert audit["status"] == "partial_failure"


def test_truncated_response_is_retried_even_when_json_parses(monkeypatch, tmp_path):
    from google.genai import types

    calls = stub_model(
        monkeypatch,
        tmp_path,
        lambda fields, n: (
            (valid_batch(fields), types.FinishReason.MAX_TOKENS) if n == 1 else valid_batch(fields)
        ),
    )
    fields = [f for f in fields_for("bank") if f.field_id == "cet1"]
    plan, audit = asyncio.run(
        ps.propose_plan(INDEX, fields, ps.load_context("bank", root=ps.RUNBOOKS))
    )
    assert len(calls) == 2 and plan.selections
    assert audit["batches"][0]["attempts"][0]["finish_reason"] == "MAX_TOKENS"


def test_failed_field_can_recover_in_followup_round(monkeypatch, tmp_path):
    from agent_ui.document_context import bind_document

    raw = json.loads((Path(__file__).parent / "fixtures/investec_statements_2026.json").read_text())
    doc = bind_document(raw, "INVESTEC BANK PLC", "00489604", "2026-03-31")
    monkeypatch.setattr(
        ps,
        "build_index",
        lambda *args: {
            **INDEX,
            "sha256": "indexhash",
            "index_pages": [80, 91],
            "index_truncated": False,
        },
    )
    monkeypatch.setattr(
        ps, "read_pages", lambda path, document, pages: [{"page": p, "boxes": []} for p in pages]
    )

    def respond(fields, n):
        if fields == ("securitisation",):
            return "broken" if n <= ps.MAX_BATCH_ATTEMPTS else valid_batch(fields, "s002")
        return valid_batch(fields)

    stub_model(monkeypatch, tmp_path, respond)
    _, audit = asyncio.run(ps.select_note_pages(None, doc, "bank"))
    assert audit["status"] == "completed" and not audit["failed_fields"]
    assert not audit["unresolved_fields"]
    assert audit["rounds"][0]["status"] == "partial_failure"
    assert audit["rounds"][1]["status"] == "completed"
    assert audit["selected_pages"] == list(range(80, 88)) + list(range(91, 96))


def companion_index(titles):
    return {
        "page_count": 100,
        "sections": [
            {
                "section_id": f"n{i}",
                "title": title,
                "start_page": 10 + i * 10,
                "end_page": 11 + i * 10,
                "source": "observed_pdf_heading",
            }
            for i, title in enumerate(titles)
        ],
    }


def test_companion_coverage_finds_other_assets_without_company_rules():
    index = companion_index(["12 Other assets", "29 Related party transactions"])
    pages, decisions = ps.companion_pages(index, fields_for("bank"), already=[20, 21])
    assert pages == [10, 11, 20, 21]
    assert decisions[0]["field_id"] == "related_parties"
    assert decisions[0]["selection_method"] == "companion_heading"
    assert decisions[0]["outcome"] == "selected"
    assert ps.companion_pages(index, fields_for("retail"), already=[20, 21])[0] == pages


def test_companion_limits_and_skips_are_explicit():
    index = companion_index(["1 Other assets", "2 Other receivables", "3 Other liabilities"])
    pages, decisions = ps.companion_pages(index, fields_for("bank"))
    assert pages == [10, 11, 20, 21]
    assert decisions[-1]["outcome"] == "section_limit" and decisions[-1]["budget_skipped"]
    pages, decisions = ps.companion_pages(index, fields_for("bank"), already=[1, 2, 3], budget=4)
    assert pages == [1, 2, 3] and all(d["outcome"] == "page_budget" for d in decisions)


def test_companion_deduplication_and_no_speculative_heading_matches():
    index = companion_index(
        ["Note 12. OTHER ASSETS", "13 Other assets accounting policy", "14 Customer loans"]
    )
    pages, decisions = ps.companion_pages(index, fields_for("bank"), already=[10, 11])
    assert pages == [10, 11] and len(decisions) == 1
    assert decisions[0]["outcome"] == "already_retrieved"
    index["sections"][0]["source"] = "search_snippet"
    assert ps.companion_pages(index, fields_for("bank")) == ([], [])


def test_companion_invalid_ranges_and_long_sections_remain_bounded():
    index = companion_index(["Other assets", "Other liabilities"])
    index["sections"][0]["end_page"] = 110
    index["sections"][1]["end_page"] = 90
    pages, decisions = ps.companion_pages(index, fields_for("bank"))
    assert pages == list(range(20, 28))
    assert decisions[0]["outcome"] == "invalid_indexed_range"
    assert decisions[1]["range_truncated"]


def test_retrieval_adds_companion_even_when_model_selects_only_primary(monkeypatch, tmp_path):
    from agent_ui.document_context import bind_document

    raw = json.loads((Path(__file__).parent / "fixtures/investec_statements_2026.json").read_text())
    doc = bind_document(raw, "INVESTEC BANK PLC", "00489604", "2026-03-31")
    index = companion_index(["12 Other assets", "29 Related party transactions"])
    index.update(sha256="test-index", index_pages=[10, 20], index_truncated=False)
    monkeypatch.setattr(ps, "build_index", lambda *args: index)
    monkeypatch.setattr(
        ps, "read_pages", lambda path, doc, pages: [{"page": p, "boxes": []} for p in pages]
    )
    stub_model(monkeypatch, tmp_path, lambda fields, n: valid_batch(fields, "n1"))
    result, audit = asyncio.run(ps.select_note_pages(None, doc, "bank"))
    assert audit["selected_pages"] == [10, 11, 20, 21]
    assert 10 in [p["page"] for p in result["pages"]]
    companion = [
        d
        for d in audit["rounds"][0]["retrieval"]
        if d.get("selection_method") == "companion_heading"
    ]
    assert len(companion) == 1 and companion[0]["section_id"] == "n0"

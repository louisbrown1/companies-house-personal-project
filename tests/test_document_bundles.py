import asyncio
import json
from pathlib import Path

import pytest

from agent_ui import hybrid_accounts as hybrid
from agent_ui.document_bundle import DocumentBundle
from agent_ui.document_context import bind_document


def document():
    raw = json.loads((Path(__file__).parent / "fixtures/investec_statements_2026.json").read_text())
    return bind_document(raw, "INVESTEC BANK PLC", "00489604", "2026-03-31")


@pytest.mark.parametrize("fail", [False, True])
def test_temporary_bundle_cleanup_and_audit(fail):
    path = None
    try:
        with DocumentBundle(document()) as bundle:
            path = bundle.path
            content = bundle.candidates(hybrid.candidate_rows(document(), "bank"))
            assert "Extraction candidates" in content
            assert "Unverified source evidence" in content
            assert (path / "page-125.md").exists()
            assert bundle.manifest["pdf_sha256"] == document()["sha256"]
            assert bundle.manifest["candidate_sha256"]
            if fail:
                raise RuntimeError("test cancellation/failure cleanup")
    except RuntimeError:
        assert fail
    assert not path.exists()


def test_investec_cover_year_does_not_bind_comparative_period():
    doc = document()
    assert doc["identity"]["verified"]
    assert not bind_document(doc, "INVESTEC BANK PLC", "00489604", "2025-03-31")["identity"][
        "verified"
    ]


def test_shared_units_scopes_spaces_and_interest_subtotal(monkeypatch):
    async def select(rows, kind, context):
        assert "Extraction candidates" in context["document_candidates"]
        return {r["row_id"]: r["allowed_metrics"][0] for r in rows}, "test"

    monkeypatch.setattr(hybrid, "model_mappings", select)
    facts, diag = asyncio.run(hybrid.extract_hybrid(document(), "bank"))
    for scope, metric, value in [
        ("group", "profit_before_tax", "439641"),
        ("group", "profit_after_tax", "353551"),
        ("group", "net_interest_income", "733205"),
        ("group", "interest_income", "1735860"),
        ("group", "effective_interest_income", "1636947"),
        ("company", "customer_loans", "13683100"),
        ("company", "customer_deposits", "20496140"),
        ("company", "cash", "3716428"),
        ("company", "operating_cash_flow", "-681789"),
    ]:
        fact = facts[scope, "2026-03-31", metric]
        assert fact["value"] == value
        assert fact["currency"] == "GBP" and fact["scale"] == 1000
        assert fact["cell_id"] and fact["source_box"]
    assert not diag["conflicts"]
    assert diag["document_bundle"]["candidate_sha256"]


def test_ambiguous_shared_scope_and_currency_withheld():
    from agent_ui.shared_headers import shared_column_headers

    doc = document()
    page = next(p for p in doc["pages"] if p["page"] == 129)
    assert len(shared_column_headers(page["boxes"], 182)) == 4
    boxes = [b for b in page["boxes"] if b["text"] != "Company"]
    assert shared_column_headers(boxes, 182) == []
    boxes = [{**b, "text": b["text"].replace("£", "?")} for b in page["boxes"]]
    assert shared_column_headers(boxes, 182) == []

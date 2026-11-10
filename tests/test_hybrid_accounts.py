import asyncio
import json
from pathlib import Path

import pytest

from agent_ui import hybrid_accounts as hybrid
from agent_ui.document_context import bind_document, page_markdown, section_scope


def document():
    raw = json.loads(
        (Path(__file__).parent / "fixtures/aldermore_statements_2026.json").read_text()
    )
    return bind_document(raw, "ALDERMORE BANK PLC", "00947662", "2026-06-30")


def test_cover_identity_allows_statement_without_repeated_name():
    doc = document()
    assert doc["identity"]["verified"]
    assert section_scope(doc, 52) == "company"
    assert len(hybrid.candidate_rows(doc, "bank")) >= 15
    assert "PDF page 52" in page_markdown(doc["pages"][3])


@pytest.mark.parametrize(
    "name,number,period",
    [
        ("OTHER BANK PLC", "00947662", "2026-06-30"),
        ("ALDERMORE BANK PLC", "09964966", "2026-06-30"),
        ("ALDERMORE BANK PLC", "00947662", "2025-06-30"),
    ],
)
def test_identity_mismatch_withholds_candidates(name, number, period):
    doc = bind_document(document(), name, number, period)
    assert not doc["identity"]["verified"]
    assert hybrid.candidate_rows(doc, "bank") == []
    assert section_scope(doc, 52) is None


@pytest.mark.parametrize(
    "mapping",
    [
        {"row_id": "invented", "metric": "total_assets"},
        {"row_id": "r1", "metric": "revenue"},
        {"row_id": "r1", "metric": "total_assets", "value": 100},
    ],
)
def test_model_cannot_invent_source_cells_or_facts(mapping):
    with pytest.raises(ValueError):
        hybrid.validate_mappings(
            json.dumps({"mappings": [mapping]}),
            [{"row_id": "r1", "allowed_metrics": ["total_assets"]}],
        )


def test_aldermore_values_dates_units_and_source_cells(monkeypatch):
    async def choose(rows, company_type, context=None):
        return {r["row_id"]: r["allowed_metrics"][0] for r in rows}, "test_mapping"

    monkeypatch.setattr(hybrid, "model_mappings", choose)
    facts, diagnostics = asyncio.run(hybrid.extract_hybrid(document(), "bank"))
    for metric, value in {
        "profit_before_tax": "159.2",
        "profit_after_tax": "113.8",
        "net_interest_income": "399.4",
        "total_assets": "22597.6",
        "total_liabilities": "21083.5",
        "total_equity": "1514.1",
        "cash": "2235.6",
        "customer_deposits": "19110.9",
    }.items():
        f = facts["company", "2026-06-30", metric]
        assert f["value"] == value
        assert f["scale"] == 1000000 and f["currency"] == "GBP"
        assert f["cell_id"] and f["source_box"] and f["source_text"]
    assert facts["company", "2025-06-30", "profit_before_tax"]["value"] == "220.7"
    assert not diagnostics["conflicts"]


def fact(value, scale=1000000):
    return {
        "value": value,
        "scale": scale,
        "currency": "GBP",
        "scope": "company",
        "period": "2026-06-30",
    }


def test_conflicting_values_are_withheld_but_equal_unit_conversions_agree():
    merged, conflicts = hybrid.merge_facts({"cash": fact("1")}, {"cash": fact("1000", 1000)})
    assert merged and not conflicts
    merged, conflicts = hybrid.merge_facts({"cash": fact("1")}, {"cash": fact("2")})
    assert not merged and conflicts == ["cash"]


def test_reconciliation_accepts_rounding_and_quarantines_inconsistency():
    figures = {
        "total_assets": fact("100.0"),
        "total_liabilities": fact("80.0"),
        "total_equity": fact("20.1"),
    }
    assert hybrid.reconcile_figures(figures)[0] == figures
    figures["total_equity"] = fact("30.0")
    figures["cash"] = fact("5.0")
    output, failed = hybrid.reconcile_figures(figures)
    assert set(output) == {"cash"}
    assert len(failed) == 3


def test_model_failure_preserves_known_labels(monkeypatch):
    async def fail(*args):
        raise TimeoutError()

    monkeypatch.setattr(hybrid, "model_mappings", fail)
    facts, diagnostics = asyncio.run(hybrid.extract_hybrid(document(), "bank"))
    assert facts["company", "2026-06-30", "profit_before_tax"]["value"] == "159.2"
    assert diagnostics["model_status"] == "failed:TimeoutError"


@pytest.mark.parametrize("page_count", [17, 361])
def test_native_pdf_discovery_preserves_page_geometry(tmp_path, monkeypatch, page_count):
    from reportlab.pdfgen import canvas

    from agent_ui import accounts

    path = tmp_path / "native.pdf"
    pdf = canvas.Canvas(str(path))
    for text in [
        "Table of contents",
        "Company financial statements",
        "Company information for a fictional test company only",
    ]:
        pdf.drawString(
            45,
            750
            - [
                "Table of contents",
                "Company financial statements",
                "Company information for a fictional test company only",
            ].index(text)
            * 25,
            text,
        )
    pdf.drawString(500, 725, "8")
    pdf.showPage()
    for n in range(1, page_count):
        if n == 7:
            pdf.drawString(45, 780, "Company financial statements")
        pdf.drawString(
            45,
            750,
            "Native PDF page with sufficient searchable text to exercise the text layer extraction.",
        )
        pdf.drawString(
            45,
            725,
            "Additional test data, containing no real financial information or company claims.",
        )
        pdf.showPage()
    pdf.save()
    monkeypatch.setattr(accounts, "CACHE", tmp_path / "cache")

    def no_ocr(*args):
        raise AssertionError("Native text must not require OCR")

    monkeypatch.setattr(accounts, "_engine", no_ocr)
    doc = accounts.read_statement_pages(path)
    assert doc["discovery_status"] == "located"
    assert doc["page_count"] == page_count
    assert len(doc["pages"]) <= 18  # Long documents do not trigger a whole-PDF scan.
    assert 8 in doc["statement_pages"]
    assert doc["sections"][0]["printed_page"] == 8
    assert all(b["method"] == "PDF text layer" for p in doc["pages"] for b in p["boxes"])


def test_high_resolution_retry_reuses_verified_discovery(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from reportlab.pdfgen import canvas

    from agent_ui import accounts, document_discovery

    path = tmp_path / "retry.pdf"
    pdf = canvas.Canvas(str(path))
    for _ in range(30):
        pdf.drawString(45, 750, "A test page with no contents page or statement heading.")
        pdf.showPage()
    pdf.save()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache = tmp_path / "cache"
    cache.mkdir()
    original = {
        "page": 1,
        "boxes": [{"text": "Verified cover", "score": 1, "box": [[1, 1], [2, 1], [2, 2], [1, 2]]}],
    }
    baseline = {
        "sha256": digest,
        "page_count": 30,
        "statement_pages": [20],
        "sections": [],
        "pages": [original] + [{"page": p, "boxes": []} for p in (20, 28, 29, 30)],
        "discovery": {
            "contents": [],
            "printed_page_offset": 0,
            "fallback_header_pages": list(range(9, 31)),
        },
    }
    (cache / (digest + "-ocr-v5.json")).write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(accounts, "CACHE", cache)
    calls = []

    def ocr(picture):
        calls.append(picture.size)
        return SimpleNamespace(txts=[], boxes=[], scores=[])

    monkeypatch.setattr(accounts, "_engine", ocr)

    def no_rediscovery(*args):
        raise AssertionError("Retry must not repeat the cold heading scan")

    monkeypatch.setattr(document_discovery, "discover", no_rediscovery)
    result = accounts.read_statement_pages(path, high_resolution=True)
    assert result["statement_pages"] == [20]
    assert result["pages"][0] == original
    assert result["discovery"]["reused_for_high_resolution"]
    assert len(calls) == 1

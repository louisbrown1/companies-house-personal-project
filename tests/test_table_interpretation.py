import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent_ui import table_interpretation as tables


def cell(text, x, y, width=40, score=1):
    return {
        "text": text,
        "score": score,
        "box": [[x, y], [x + width, y], [x + width, y + 14], [x, y + 14]],
    }


def document(complex=False):
    boxes = [cell("Consolidated income statement", 20, 30, 300)]
    columns = (
        [(300, "2026"), (400, "2025")]
        if not complex
        else [
            (300, "2026"),
            (400, "2026"),
            (500, "2026"),
            (600, "2025"),
            (700, "2025"),
            (800, "2025"),
        ]
    )
    for i, (x, year) in enumerate(columns):
        boxes += [cell(year, x, 75), cell("GBP000", x, 120)]
        if complex:
            boxes.append(
                cell(["Excluding exceptional", "Exceptional", "Total"][i % 3], x - 30, 98, 70)
            )
    for ri, label in enumerate(
        ["Revenue", "Profit before income tax", "Profit for the financial year"]
    ):
        boxes.append(cell(label, 20, 160 + ri * 30, 250))
        for i, (x, _) in enumerate(columns):
            boxes.append(cell(str(1000 + 100 * ri + i), x, 160 + ri * 30))
    return {
        "sha256": "synthetic",
        "page_count": 1,
        "statement_pages": [1],
        "identity": {"verified": True, "company_name": "Example Limited", "period": "2026-06-30"},
        "pages": [{"page": 1, "boxes": boxes}],
    }


def proposal(table, metric="profit_before_tax", column=0):
    c = table["columns"][column]
    r = next(r for r in table["rows"] if "before" in r["label"])
    return {
        "columns": [
            {
                "column_id": c["id"],
                "period": c["periods"][0],
                "scope_id": c["scopes"][0]["id"],
                "unit_id": c["units"][0]["id"],
                "basis_ids": c["total_ids"],
            }
        ],
        "rows": [{"row_id": r["id"], "metric": metric}],
    }


def test_model_maps_unfamiliar_label_but_amount_is_copied_from_source():
    t = tables.build_tables(document(), ["2026-06-30", "2025-06-30"])[0]
    facts, audit = tables.validate_plan(proposal(t), t)
    f = facts["group", "2026-06-30", "profit_before_tax"]
    assert f["value"] == "1100" and f["source_text"] == "1100"
    assert (
        f["scale"] == 1000 and f["source_box"] and f["cell_id"] and f["pdf_sha256"] == "synthetic"
    )
    assert not audit["rejected"]


@pytest.mark.parametrize(
    "metric,label",
    [
        ("customer_deposits", "Movement in customer deposits"),
        ("customer_loans", "Movement in loans and advances to customers"),
        ("cash", "Effect of exchange rates on cash and cash equivalents"),
        ("cash", "Cash and cash equivalents at beginning of year"),
    ],
)
def test_movements_are_not_closing_balances(metric, label):
    assert not tables.metric_compatible(metric, label)


def test_six_columns_require_statutory_total_not_adjusted_amount():
    t = tables.build_tables(document(True), ["2026-06-30", "2025-06-30"])[0]
    assert len(t["columns"]) == 6
    assert not tables.validate_plan(proposal(t, column=0), t)[0]
    facts, _ = tables.validate_plan(proposal(t, column=2), t)
    assert facts["group", "2026-06-30", "profit_before_tax"]["value"] == "1102"


def test_repeated_consistent_unit_row_can_corroborate_a_weak_glyph():
    d = document(True)
    for b in d["pages"][0]["boxes"]:
        if b["text"] == "GBP000" and b["box"][0][0] == 500:
            b["score"] = 0.8
    t = tables.build_tables(d, ["2026-06-30", "2025-06-30"])[0]
    facts, _ = tables.validate_plan(proposal(t, column=2), t)
    assert facts["group", "2026-06-30", "profit_before_tax"]["scale"] == 1000
    assert (
        "repeated"
        in facts["group", "2026-06-30", "profit_before_tax"]["interpretation_evidence"]["unit"][
            "binding"
        ]
    )


def test_mixed_unit_row_cannot_override_a_low_confidence_column_unit():
    d = document()
    for b in d["pages"][0]["boxes"]:
        if b["text"] == "GBP000":
            if b["box"][0][0] == 300:
                b["score"] = 0.8
            else:
                b["text"] = "GBPm"
    t = tables.build_tables(d, ["2026-06-30", "2025-06-30"])[0]
    assert not tables.validate_plan(proposal(t), t)[0]


@pytest.mark.parametrize(
    "change",
    [
        "unknown_column",
        "unknown_row",
        "invented_amount",
        "wrong_period",
        "wrong_scope",
        "wrong_unit",
        "wrong_metric",
        "duplicate_row",
        "low_confidence",
    ],
)
def test_unsupported_model_claims_are_rejected(change):
    t = tables.build_tables(document(), ["2026-06-30", "2025-06-30"])[0]
    p = proposal(t)
    if change == "unknown_column":
        p["columns"][0]["column_id"] = "invented"
    if change == "unknown_row":
        p["rows"][0]["row_id"] = "invented"
    if change == "invented_amount":
        p["rows"][0]["value"] = "999999"
    if change == "wrong_period":
        p["columns"][0]["period"] = "2025-06-30"
    if change == "wrong_scope":
        p["columns"][0]["scope_id"] = "company"
    if change == "wrong_unit":
        p["columns"][0]["unit_id"] = "millions"
    if change == "wrong_metric":
        p["rows"][0]["metric"] = "profit_after_tax"
    if change == "duplicate_row":
        p["rows"].append(deepcopy(p["rows"][0]))
    if change == "low_confidence":
        r = next(r for r in t["rows"] if r["id"] == p["rows"][0]["row_id"])
        r["cells"][p["columns"][0]["column_id"]]["score"] = 0.7
    try:
        assert not tables.validate_plan(p, t)[0]
    except (ValueError, ValidationError):
        pass


def test_reversed_years_follow_source_headers_not_column_order():
    d = document()
    for b in d["pages"][0]["boxes"]:
        if b["text"] == "2026":
            b["text"] = "2025"
        elif b["text"] == "2025":
            b["text"] = "2026"
    t = tables.build_tables(d, ["2026-06-30", "2025-06-30"])[0]
    facts, _ = tables.validate_plan(proposal(t), t)
    assert ("group", "2025-06-30", "profit_before_tax") in facts


@pytest.mark.parametrize(
    "suffix,accepted", [("", True), (" and its subsidiaries (the Group)", False)]
)
def test_audit_scope_requires_the_exact_issuer_and_no_group_perimeter(suffix, accepted):
    d = document()
    d["pages"].append(
        {
            "page": 2,
            "boxes": [
                cell(
                    "We have audited the financial statements of Example Limited (the Company)"
                    + suffix
                    + " for the year ended 30 June 2026. Basis for opinion",
                    20,
                    40,
                    900,
                )
            ],
        }
    )
    assert bool(tables.opinion_scope(d)) == accepted
    d["identity"]["company_name"] = "Different Limited"
    assert not tables.opinion_scope(d)


def test_cover_name_alone_never_assigns_company_scope():
    d = document()
    d["pages"][0]["boxes"][0]["text"] = "Income statement"
    t = tables.build_tables(d, ["2026-06-30", "2025-06-30"])[0]
    assert all(not c["scopes"] for c in t["columns"])


def test_explicit_header_date_conflict_blocks_column():
    d = document()
    d["pages"][0]["boxes"].append(cell("As at 31 December 2026", 20, 55, 250))
    t = tables.build_tables(d, ["2026-06-30", "2025-06-30"])[0]
    assert all("2026-06-30" not in c["periods"] for c in t["columns"])


def test_document_identity_failure_never_calls_model(monkeypatch):
    d = document()
    d["identity"]["verified"] = False

    async def fail(*args):
        raise AssertionError("Model must not see an unverified filing")

    monkeypatch.setattr(tables, "interpret_table", fail)
    facts, audit = asyncio.run(tables.interpret_document(d, ["2026-06-30"]))
    assert not facts and audit["tables"] == []


def test_model_failure_is_reported_without_inventing_values(monkeypatch):
    async def fail(*args):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(tables, "interpret_table", fail)
    facts, audit = asyncio.run(tables.interpret_document(document(), ["2026-06-30"]))
    assert not facts and audit["tables"][0]["status"] == "failed:TimeoutError"


def test_conflict_cannot_be_revived_by_a_later_ocr_pass(monkeypatch, tmp_path):
    from agent_ui import hybrid_accounts as hybrid

    fact = {
        "value": "10",
        "scale": 1000,
        "currency": "GBP",
        "scope": "company",
        "period": "2026-06-30",
    }

    async def base(*args):
        return {("company", "2026-06-30", "profit_before_tax"): fact}, {"conflicts": []}

    async def extra(*args):
        return {}, {"conflicts": []}

    monkeypatch.setattr(hybrid, "extract_hybrid", base)
    monkeypatch.setattr(tables, "interpret_document", extra)
    monkeypatch.setattr(hybrid, "CACHE", tmp_path)
    filing = {"period": "2026-06-30", "figures": {"profit_before_tax": {**fact, "value": "20"}}}
    asyncio.run(hybrid.enrich_filing(filing, document(), "trading"))
    assert not filing["figures"] and filing["extraction_conflicts"]
    asyncio.run(hybrid.enrich_filing(filing, document(), "trading"))
    assert not filing["figures"]


def test_compact_model_selection_is_validated_and_cached(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from agent_ui import model_config

    t = tables.build_tables(document(), ["2026-06-30", "2025-06-30"])[0]
    row = next(r for r in t["rows"] if "before" in r["label"])
    calls = []

    class Model:
        async def generate_content_async(self, request, stream=False):
            calls.append(request)
            payload = {
                "columns": [c["id"] for c in t["columns"]],
                "rows": [{"row_id": row["id"], "metric": "profit_before_tax"}],
            }
            yield SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(parts=[SimpleNamespace(text=json.dumps(payload))]),
            )

    monkeypatch.setattr(model_config, "agent_model", lambda: Model())
    monkeypatch.setattr(tables.accounts, "CACHE", tmp_path)
    facts, audit = asyncio.run(tables.interpret_table(t, document()["identity"]))
    assert len(facts) == 2 and audit["status"] == "completed"
    assert facts["group", "2026-06-30", "profit_before_tax"]["value"] == "1100"
    cached, second = asyncio.run(tables.interpret_table(t, document()["identity"]))
    assert cached == facts and second["status"] == "cached" and len(calls) == 1
    monkeypatch.setattr(tables, "VERSION", tables.VERSION + "-new-validation")
    refreshed, third = asyncio.run(tables.interpret_table(t, document()["identity"]))
    assert refreshed == facts and third["status"] == "completed" and len(calls) == 2


@pytest.mark.parametrize(
    "metric,label",
    [
        ("net_interest_income", "Net increase in interest income receivable"),
        ("net_interest_income", "Net decrease in interest income receivable"),
        ("net_interest_income", "Net interest income receivable"),
        ("net_interest_income", "Net interest income margin"),
        ("interest_income", "Interest income received"),
        ("interest_expense", "Increase in interest payable"),
        ("total_income", "Changes in total income"),
        ("profit_before_tax", "Adjustments to profit before tax"),
        ("revenue", "Revenue growth percentage"),
    ],
)
def test_movements_and_ratios_cannot_be_published_as_financial_totals(metric, label):
    t = tables.build_tables(document(), ["2026-06-30", "2025-06-30"])[0]
    p = proposal(t, metric=metric)
    row = next(r for r in t["rows"] if r["id"] == p["rows"][0]["row_id"])
    row["label"] = label
    facts, audit = tables.validate_plan(p, t)
    assert not facts
    assert audit["rejected"][0]["reason"] == "incompatible accounting meaning"


@pytest.mark.parametrize(
    "metric,label",
    [
        ("net_interest_income", "Net interest income"),
        ("interest_income", "Interest income"),
        ("interest_expense", "Interest payable and similar expenses"),
        (
            "effective_interest_income",
            "Interest income calculated using the effective interest method",
        ),
        ("total_income", "Total operating income"),
        ("operating_cash_flow", "Net cash from operating activities"),
        ("profit_before_tax", "Profit before tax"),
        ("customer_deposits", "Deposits from customers"),
    ],
)
def test_genuine_statement_totals_remain_eligible(metric, label):
    assert tables.metric_compatible(metric, label)

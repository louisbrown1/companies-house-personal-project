import asyncio
import json
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


def test_missing_comparative_cell_does_not_hide_valid_current_period_cell():
    doc = document()
    doc["pages"][0]["boxes"] = [b for b in doc["pages"][0]["boxes"] if b["text"] != "1101"]
    table = tables.build_tables(doc, ["2026-06-30", "2025-06-30"])[0]
    facts, audit = tables.validate_plan(proposal(table), table)
    assert facts["group", "2026-06-30", "profit_before_tax"]["value"] == "1100"
    assert not audit["rejected"]
    other, _ = tables.validate_plan(proposal(table, column=1), table)
    assert not other


@pytest.mark.parametrize(
    "label",
    [
        "Total liabilities and equity",
        "Total liabilitles and equity",
        "Total liabililies and equity",
    ],
)
def test_combined_total_with_ocr_damage_cannot_become_equity(label):
    assert not tables.metric_compatible("total_equity", label)
    assert tables.metric_compatible("total_equity", "Total equity")


def test_fiscal_year_headers_and_apostrophe_millions_need_explicit_date_anchor():
    doc = document()
    boxes = doc["pages"][0]["boxes"]
    for b in boxes:
        if b["text"] in {"2026", "2025"}:
            b["text"] = "FY" + b["text"][-2:]
        if b["text"] == "GBP000":
            b["text"] = "£’m"
    assert tables.build_tables(doc, ["2026-06-30", "2025-06-30"]) == []
    boxes.append(cell("For the year ended 30 June 2026", 20, 55, 250))
    table = tables.build_tables(doc, ["2026-06-30", "2025-06-30"])[0]
    assert [c["periods"] for c in table["columns"]] == [["2026-06-30"], ["2025-06-30"]]
    assert all(c["units"][0]["scale"] == 1000000 for c in table["columns"])
    assert not tables.fiscal_header_years("25", ["2025-06-30"], ["2025-06-30"])
    assert not tables.fiscal_header_years("FY25", ["2025-03-31"], ["2025-06-30"])
    assert tables.unit_scale("E'm") is None


def test_high_resolution_retains_stronger_literal_unit_from_same_pdf(monkeypatch, tmp_path):
    standard = document()
    high = deepcopy(standard)
    for b in high["pages"][0]["boxes"]:
        if b["text"] == "GBP000":
            b["score"] = 0.80
    (tmp_path / "synthetic-ocr-v5.json").write_text(json.dumps(standard), encoding="utf-8")
    monkeypatch.setattr(tables.accounts, "CACHE", tmp_path)
    table = tables.build_tables(high, ["2026-06-30", "2025-06-30"])[0]
    facts, _ = tables.validate_plan(proposal(table), table)
    assert facts["group", "2026-06-30", "profit_before_tax"]["scale"] == 1000
    assert (
        "standard-unit"
        in facts["group", "2026-06-30", "profit_before_tax"]["interpretation_evidence"]["unit"][
            "id"
        ]
    )


def test_merged_auditor_contents_heading_triggers_bounded_context_retrieval(monkeypatch):
    from agent_ui import document_navigation

    doc = document()
    doc["page_count"] = 150
    doc["discovery"] = {"contents": [{"title": "Independentauditor'sreport", "printed_page": 129}]}
    requested = []

    def read(path, document, pages):
        requested.extend(pages)
        return [{"page": p, "boxes": []} for p in pages]

    monkeypatch.setattr(document_navigation, "read_pages", read)
    result = tables.prepare_context(None, doc)
    assert requested == [127, 128, 129, 130, 131] and result["table_context_pages"] == requested


@pytest.mark.parametrize(
    "heading,accepted",
    [
        ("Independent auditors' report. Report on the audit of the financial statements.", True),
        ("Directors' report.", False),
    ],
)
def test_named_company_opinion_requires_audit_context_when_body_ocr_is_damaged(heading, accepted):
    doc = document()
    text = (
        heading
        + " In our opinion, Example Limited's financial statements give a true and fair view of the company's affairs. We have audited the financial staterments. Basis for opinion"
    )
    doc["pages"].append({"page": 2, "boxes": [cell(text, 20, 30, 800)]})
    assert bool(tables.opinion_scope(doc)) is accepted


@pytest.mark.parametrize(
    "issuer,extra,accepted",
    [
        ("Example Limited", "", True),
        ("Other Limited", "", False),
        ("Example Limited", "These consolidated statements cover subsidiaries.", False),
    ],
)
def test_opinion_first_scope_requires_named_issuer_company_and_no_group(issuer, extra, accepted):
    doc = document()
    text = (
        f"In our opinion, {issuer}'s financial statements give a true and fair view of the company's affairs. "
        f"We have audited the financial statements included in the annual report. {extra} Basis for opinion"
    )
    doc["pages"].append({"page": 2, "boxes": [cell(text, 20, 30, 800)]})
    assert bool(tables.opinion_scope(doc)) is accepted


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

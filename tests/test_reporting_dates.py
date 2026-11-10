import pytest
from test_document_discovery import bind, box, document
from test_table_interpretation import document as table_document

from agent_ui import table_interpretation as tables
from agent_ui.reporting_dates import full_dates, standalone_date


@pytest.mark.parametrize(
    "text",
    [
        "31/03/2026",
        "31-03-2026",
        "31.03.2026",
        "2026-03-31",
        "31 / 3 / 2026",
        "31 March 2026",
        "31 Mar 2026",
        "31 Mar. 2026",
    ],
)
def test_supported_explicit_dates(text):
    assert full_dates("Year ended " + text) == ["2026-03-31"]
    assert standalone_date(text) == "2026-03-31"
    d = document()
    d["pages"][1]["boxes"][1]["text"] = "Year ended " + text
    assert bind(d)["verified"]
    assert not bind(d, "2025-03-31")["verified"]


@pytest.mark.parametrize(
    "text",
    ["31/02/2026", "29/02/2025", "03/31/2026", "31/03/26", "312/2026", "131/03/2026", "31/03-2026"],
)
def test_invalid_or_unsupported_dates_are_not_guessed(text):
    assert full_dates(text) == []
    assert standalone_date(text) is None


def test_numeric_cover_receipt_stamp_is_not_a_reporting_period():
    d = document()
    d["pages"][0]["boxes"].append(box("COMPANIES HOUSE 22/04/2026", y=250))
    assert bind(d)["verified"]
    d["pages"] = d["pages"][:1]
    assert not bind(d)["verified"]


def test_explicit_numeric_cover_period_conflict_is_rejected():
    d = document()
    d["pages"][0]["boxes"].append(box("Year ended 31/12/2025", y=250))
    assert "different_cover_period" in bind(d)["contradictions"]


def test_numeric_approval_date_is_not_a_statement_period():
    d = document()
    d["pages"][1]["boxes"].append(box("Approved on 20/05/2026", 600, 100, 300))
    assert bind(d)["verified"]


def test_numeric_table_headers_bind_columns_and_reject_wrong_day():
    d = table_document()
    for b in d["pages"][0]["boxes"]:
        if b["text"] in ("2026", "2025"):
            b["text"] = "30/06/" + b["text"]
    t = tables.build_tables(d, ["2026-06-30", "2025-06-30"])[0]
    assert [c["periods"] for c in t["columns"]] == [["2026-06-30"], ["2025-06-30"]]
    t = tables.build_tables(d, ["2026-06-29", "2025-06-30"])[0]
    assert all("2026-06-29" not in c["periods"] for c in t["columns"])


def test_profit_or_loss_heading_exposes_numeric_period():
    d = document()
    d["pages"][1]["boxes"][0]["text"] = "Statement of profit or loss and other comprehensive income"
    d["pages"][1]["boxes"][1]["text"] = "Year ended 31/03/2026"
    assert bind(d)["verified"]

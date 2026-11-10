from copy import deepcopy

import pytest

from agent_ui.document_context import bind_document, canonical_number, section_scope
from agent_ui.document_discovery import contents_entries, discover, heading_matches


def box(text, x=40, y=50, width=200):
    return {
        "text": text,
        "score": 1,
        "box": [[x, y], [x + width, y], [x + width, y + 15], [x, y + 15]],
    }


@pytest.mark.parametrize("before", [True, False])
def test_multicolumn_contents_and_continuation(before):
    boxes = []
    for row in range(4):
        for col in range(2):
            x = 40 + col * 400
            y = 80 + row * 30
            boxes.extend(
                [
                    box("Company balance sheet", x + (40 if before else 0), y, 230),
                    box(str(20 + row + col * 10), x + (0 if before else 290), y, 25),
                ]
            )
    entries = contents_entries({"page_count": 100, "pages": [{"page": 3, "boxes": boxes}]})
    assert len(entries) == 8
    assert all(e["kind"] == "statement" for e in entries)
    assert {e["printed_page"] for e in entries} == {20, 21, 22, 23, 30, 31, 32, 33}


def test_offsets_are_verified_at_destination_and_scope_is_not_interchangeable():
    sources = {i: [box("Ordinary report text")] for i in range(30)}
    sources[2] = [
        box("IN THIS REPORT"),
        box("Company balance sheet", 40, 100),
        box("20", 400, 100, 25),
        box("1", 500, 800, 20),
    ]
    sources[3] = [box("Ordinary report text"), box("2", 500, 800, 20)]
    sources[19] = [box("Consolidated balance sheet")]
    sources[21] = [box("Company balance sheet")]
    pages = {}

    def read(i, headers_only=False):
        if not headers_only:
            pages[i] = sources[i]
        return sources[i]

    result = discover(pages, read, 30)
    assert result["printed_page_offset"] == 2
    assert result["targets"] == {21}
    assert result["sections"][0]["pdf_page"] == 22
    assert not heading_matches("Company balance sheet", "Consolidated balance sheet")


def test_header_fallback_is_bounded_and_rereads_full_statement():
    pages = {}
    calls = []

    def read(i, headers_only=False):
        calls.append((i, headers_only))
        values = [box("Company income statement" if i == 90 else "Ordinary report text")]
        if not headers_only:
            pages[i] = values
        return values

    result = discover(pages, read, 400)
    assert result["targets"] == {90}
    assert (90, True) in calls and (90, False) in calls
    assert len(result["fallback_header_pages"]) == 200
    assert result["fallback_truncated"]
    assert len(pages) == 9


def document(number="140269"):
    return {
        "page_count": 40,
        "statement_pages": [20],
        "pages": [
            {
                "page": 1,
                "boxes": [
                    box("Example Trading", y=40),
                    box("Limited", y=65),
                    box("Company registration number " + number, y=110),
                    box("Report and Accounts 2026", y=160),
                ],
            },
            {
                "page": 20,
                "boxes": [
                    box("Company income statement", y=50),
                    box("Year ended 31 March", y=100),
                    box("2026", 300, 130, 40),
                    box("2025", 400, 129, 40),
                    box("GBP000", 300, 160, 40),
                ],
            },
        ],
    }


def bind(d, period="2026-03-31"):
    return bind_document(d, "EXAMPLE TRADING LIMITED", "00140269", period)["identity"]


def test_multiline_name_short_number_and_shared_statement_date():
    identity = bind(document())
    assert identity["verified"]
    assert identity["evidence"]["source_numbers"] == ["00140269"]
    assert not bind(document(), "2025-03-31")["verified"]


def test_number_prefix_is_preserved():
    assert canonical_number("SC123456") == "SC123456"
    assert canonical_number("123456") == "00123456"
    assert not bind(document("SC140269"))["verified"]


def test_registry_provenance_can_resolve_missing_number_but_never_contradictions():
    d = document()
    d["pages"][0]["boxes"].pop(2)
    assert not bind(d)["verified"]
    d["registry_binding"] = {
        "source": "companies_house_filing_history",
        "company_number": "00140269",
        "period": "2026-03-31",
    }
    assert bind(d)["verified"]
    for bad in [
        box("Company registration number 99999999", y=110),
        box("Other Business Limited", y=20),
    ]:
        changed = deepcopy(d)
        changed["pages"][0]["boxes"].append(bad)
        assert not bind(changed)["verified"]
    changed = deepcopy(d)
    changed["pages"][1]["boxes"][2]["text"] = "2027"
    assert not bind(changed)["verified"]


def test_report_year_alone_is_not_a_period_binding():
    d = document()
    d["pages"] = d["pages"][:1]
    assert not bind(d)["verified"]


def test_equity_label_does_not_turn_approval_date_into_statement_period():
    d = document()
    d["pages"][1]["boxes"].extend(
        [box("Profit and loss account", y=800), box("Approved on 13 October 2026", y=900)]
    )
    assert bind(d)["verified"]


def test_adjacent_approval_column_is_not_a_reporting_period():
    d = document()
    d["pages"][1]["boxes"].append(box("Approved by the Board on 26 May 2026", 600, 100, 300))
    assert bind(d)["verified"]


def test_week_based_periods_and_separate_equity_comparative_are_not_mixed():
    d = document()
    d["pages"][0]["boxes"][-1]["text"] = "Report and Accounts 2025"
    d["pages"][1]["boxes"][1]["text"] = "AT 27 DECEMBER2025 (2024: 28 DECEMBER 2024)"
    d["pages"][1]["boxes"][2]["text"] = "2025"
    d["pages"][1]["boxes"][3]["text"] = "2024"
    d["statement_pages"].append(21)
    d["pages"].append(
        {
            "page": 21,
            "boxes": [
                box("Statement of changes in equity"),
                box("52 weeks ended 28 December 2024", y=100),
                box("GBP000", y=150),
            ],
        }
    )
    assert bind(d, "2025-12-27")["verified"]
    assert not bind(d, "2024-12-28")["verified"]


def test_cross_reference_does_not_establish_group_scope():
    d = {
        "identity": {"verified": True},
        "pages": [
            {
                "page": 10,
                "boxes": [
                    box("Refer to Note 13 of the", y=100),
                    box("Consolidated Financial Statements", y=120),
                    box("(page 146)", y=140),
                ],
            }
        ],
    }
    assert section_scope(d, 20) is None
    d["pages"] = [{"page": 10, "boxes": [box("Company financial statements")]}]
    assert section_scope(d, 20) == "company"


def test_joined_ocr_legal_name_and_number_keep_exact_entity_matching():
    d = document()
    d["pages"][0]["boxes"] = [
        box("Example TradingLimited", y=40),
        box("CompanyNumber00140269", 400, y=40),
        box("Report and Accounts 2026", y=160),
    ]
    assert bind(d)["verified"]
    d["pages"][0]["boxes"][1]["text"] = "CompanyNumber99999999"
    assert not bind(d)["verified"]


def test_real_allica_retry_corrects_source_date_without_assuming_expected_period():
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/allica_identity_ratios.json").read_text()
    )
    doc = fixture["original"]
    assert not bind_document(doc, "ALLICA BANK LIMITED", "07706156", "2024-12-31")["identity"][
        "verified"
    ]
    doc["pages"] = fixture["refined_pages"]
    assert bind_document(doc, "ALLICA BANK LIMITED", "07706156", "2024-12-31")["identity"][
        "verified"
    ]
    assert not bind_document(doc, "ALLICA BANK LIMITED", "07706156", "2023-12-31")["identity"][
        "verified"
    ]
    assert not bind_document(doc, "OTHER BANK LIMITED", "07706156", "2024-12-31")["identity"][
        "verified"
    ]


def test_other_registration_types_do_not_veto_company_identity():
    d = document()
    d["pages"].append(
        {
            "page": 4,
            "boxes": [
                box(
                    "Regulated by the Financial Conduct Authority under registration number 740551"
                ),
                box("Registered Charity Number: 1003859", y=130),
                box("VAT registration number 12345678", y=170),
            ],
        }
    )
    identity = bind(d)
    assert identity["verified"]
    assert identity["evidence"]["source_numbers"] == ["00140269"]


def test_genuine_company_number_conflict_is_still_rejected():
    d = document()
    d["pages"].append({"page": 4, "boxes": [box("Company registration number 99999999")]})
    assert "different_registration_number" in bind(d)["contradictions"]

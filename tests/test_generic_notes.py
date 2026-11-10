import copy

import pytest
from test_accounts_notes import doc, extract, maturity
from test_banking_notes import document as bank_document

from agent_ui.banking_notes import extract_banking_notes


@pytest.mark.parametrize("unit,scale", [("£'000", 1000), ("GBP", 1), ("£ million", 1000000)])
def test_alternative_name_reverse_years_units_and_labels(unit, scale):
    d = doc(
        [
            [(800, "2025"), (1000, "2026")],
            [(800, unit), (1000, unit)],
            [(140, "Salaries and wages"), (800, "125"), (1000, "150")],
        ]
    )
    d["pages"][0]["boxes"][0]["text"] = "Unseen Manufacturing plc"
    r = __import__("agent_ui.accounts_notes", fromlist=["extract_notes"]).extract_notes(
        d, "2026-03-28", "2025-03-29", "UNSEEN MANUFACTURING PLC"
    )
    assert r["pairs"]["wages_and_salaries"]["current"] == "150"
    assert r["pairs"]["wages_and_salaries"]["previous"] == "125"
    assert r["pairs"]["wages_and_salaries"]["scale"] == scale


@pytest.mark.parametrize(
    "heading",
    [
        "The overall Acme corporate Group position at each period end",
        "Consolidated results",
        "Group and Company",
    ],
)
def test_any_group_table_is_withheld(heading):
    d = doc(
        [
            [(140, heading)],
            [(800, "2026"), (1000, "2025")],
            [(800, "£m"), (1000, "£m")],
            [(140, "Wages and salaries"), (800, "15"), (1000, "12")],
        ]
    )
    assert not extract(d)["pairs"]


def test_document_identity_can_replace_repeated_company_header():
    d = doc(
        [
            [(800, "2026"), (1000, "2025")],
            [(800, "£m"), (1000, "£m")],
            [(140, "Pension costs"), (800, "15"), (1000, "12")],
        ]
    )
    d["pages"][0]["boxes"][0]["text"] = "Notes continued"
    assert not extract(d)["pairs"]
    d["identity"] = {"verified": True, "company_name": "EXAMPLE LIMITED", "period": "2026-03-28"}
    assert extract(d)["pairs"]["pension_costs"]["current"] == "15"
    d["identity"]["period"] = "2025-03-29"
    assert not extract(d)["pairs"]


def test_alternative_maturity_caption_and_buckets():
    d = maturity(1429)
    for b in d["pages"][0]["boxes"]:
        if "following table" in b["text"]:
            b["text"] = "Contractual maturities of lease liabilities"
        if "Within 1 year" in b["text"]:
            b["text"] = (
                "Less than one year One to two years Two to five years Over five years Total"
            )
        if b["text"] == "£'m":
            b["text"] = "£'000"
    r = extract(d)["maturities"]
    assert r[0]["total"] == "1429" and r[0]["scale"] == 1000
    for b in d["pages"][0]["boxes"]:
        if "Less than one" in b["text"]:
            b["text"] = (
                "Over five years Two to five years One to two years Less than one year Total"
            )
    assert not extract(d)["maturities"]


def test_mixed_units_and_conflicting_values_are_withheld():
    rows = [
        [(800, "2026"), (1000, "2025")],
        [(800, "£m"), (1000, "£000")],
        [(140, "Wages and salaries"), (800, "15"), (1000, "12")],
    ]
    assert not extract(doc(rows))["pairs"]
    rows[1] = [(800, "£m"), (1000, "£m")]
    d = doc(rows)
    other = copy.deepcopy(d["pages"][0])
    other["page"] = 13
    for b in other["boxes"]:
        if b["text"] == "15":
            b["text"] = "16"
    d["pages"].append(other)
    assert not extract(d)["pairs"]


@pytest.mark.parametrize(
    "sentence,topic",
    [
        (
            "Liquidity buffers are maintained and liquidity stress tests are performed monthly.",
            "liquidity_management",
        ),
        (
            "An intercompany funding facility from the parent is repayable on demand.",
            "group_funding",
        ),
        ("The shareholder has not committed to a capital contribution.", "capital_support"),
        ("Securitised assets retain credit risk under the transaction terms.", "securitisation"),
    ],
)
def test_bank_disclosures_use_concepts_and_preserve_negation(sentence, topic):
    d = bank_document(82)
    d["identity"] = {"verified": True, "company_name": "UNSEEN BANK PLC", "period": "2025-03-31"}
    d["pages"][0]["boxes"] = [
        {"text": sentence, "score": 1, "box": [[145, 600], [1080, 600], [1080, 620], [145, 620]]}
    ]
    notes = extract_banking_notes(d, "2025-03-31", "UNSEEN BANK PLC")
    assert any(x["topic"] == topic and sentence in x["excerpt"] for x in notes["disclosures"])
    assert not notes["metrics"]


def test_report_converts_thousands_before_adding_employee_costs():
    from agent_ui.financial_health import add_notes

    keys = ["wages_and_salaries", "social_security_costs", "share_based_payments", "pension_costs"]
    pairs = {
        k: {
            "current": "250",
            "previous": "125",
            "scale": 1000,
            "currency": "GBP",
            "scope": "company",
            "period": "2026-03-28",
            "prior_period": "2025-03-29",
            "page": 12,
            "excerpt": k,
        }
        for k in keys
    }
    groups = {"financial": [], "financial_notes": []}
    output = []

    def add(label, fact, *args, **kwargs):
        eid = str(len(output))
        output.append((label, fact))
        groups["financial"].append(eid)
        return eid

    add_notes(
        {
            "notes": {"pairs": pairs, "maturities": [], "excerpts": []},
            "figures": {},
            "url": "https://example.com",
        },
        {},
        add,
        groups,
    )
    text = next(v["observation"] for k, v in output if k == "financial_observation")
    assert "GBP 1.00m versus 0.500m" in text
    assert "1000m" not in text

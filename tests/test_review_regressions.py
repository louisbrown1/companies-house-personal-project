import asyncio

import pytest
from test_accounts_notes import doc
from test_table_interpretation import cell

from agent_ui import hybrid_accounts
from agent_ui.accounts_notes import extract_notes
from agent_ui.banking_notes import extract_banking_notes
from agent_ui.executive_summary import render_executive_summary
from agent_ui.financial_health import add_financial_evidence
from agent_ui.validation import EvidenceBundle


@pytest.mark.parametrize(
    "label,amount,expected",
    [
        ("Loss before taxation", "100", "-100"),
        ("Loss before taxation", "(100)", "-100"),
        ("Profit before taxation", "100", "100"),
        ("Loss profit before tax", "100", "100"),
    ],
)
def test_hybrid_sign_and_literal_source_are_preserved(label, amount, expected, monkeypatch):
    boxes = [cell("Company income statement", 20, 30, 240), cell("Year ended 30 June", 20, 55, 200)]
    for x, year in [(300, "2026"), (400, "2025")]:
        boxes.extend([cell(year, x, 75), cell("GBPm", x, 120), cell(amount, x, 160)])
    boxes.append(cell(label, 20, 160, 200))
    document = {
        "sha256": "synthetic",
        "identity": {"verified": True},
        "statement_pages": [1],
        "pages": [{"page": 1, "boxes": boxes}],
    }

    async def no_model(rows, *args):
        assert not rows  # Known labels must not require inference to fix their sign.
        return {}, "not_needed"

    monkeypatch.setattr(hybrid_accounts, "model_mappings", no_model)
    facts, _ = asyncio.run(hybrid_accounts.extract_hybrid(document, "bank"))
    fact = facts["company", "2026-06-30", "profit_before_tax"]
    assert fact["value"] == expected
    assert fact["source_text"] == amount


@pytest.mark.parametrize(
    "header,accepted",
    [
        ("Year ended 31 December", False),
        ("Year ended 31 December 2026 and 31 December 2025", False),
        ("Year ended 28 March 2026 and 29 March 2025", True),
        ("Years ended", True),
    ],
)
def test_note_dates_cannot_override_explicit_header(header, accepted):
    document = doc(
        [
            [(140, header)],
            [(800, "2026"), (1000, "2025")],
            [(800, "GBPm"), (1000, "GBPm")],
            [(140, "Wages and salaries"), (800, "15"), (1000, "12")],
        ]
    )
    document["identity"] = {
        "verified": True,
        "company_name": "EXAMPLE LIMITED",
        "period": "2026-03-28",
    }
    notes = extract_notes(document, "2026-03-28", "2025-03-29", "EXAMPLE LIMITED")
    assert bool(notes["pairs"]) == accepted


def test_banking_notes_keep_distinct_later_disclosures_with_a_bound():
    policy = "Intercompany transactions are eliminated on consolidation."
    substantive = (
        "Related party transactions include management fees of GBP 90 million paid to the parent."
    )
    texts = [
        policy,
        policy,
        "Note 29. Related party transactions",
        substantive,
        "Related party transactions include fees of GBP 20 million paid to a subsidiary.",
        "Related party transactions include fees of GBP 30 million paid to a subsidiary.",
    ]
    document = {
        "identity": {
            "verified": True,
            "company_name": "EXAMPLE BANK LIMITED",
            "period": "2025-12-31",
        },
        "pages": [
            {"page": i + 1, "boxes": [cell(text, 100, 300, 900)]} for i, text in enumerate(texts)
        ],
    }
    notes = extract_banking_notes(document, "2025-12-31", "EXAMPLE BANK LIMITED")
    disclosures = [d for d in notes["disclosures"] if d["topic"] == "related_party_transactions"]
    assert len(disclosures) == 3
    assert sum(d["excerpt"] == policy for d in disclosures) == 1
    assert any(d["excerpt"] == substantive and d["page"] == 4 for d in disclosures)
    assert all(d["excerpt"] != "Note 29. Related party transactions" for d in disclosures)


def test_summary_accepts_bank_scope_without_claiming_known_perimeter():
    metric = {
        "metric": "lcr",
        "period": "2025-12-31",
        "scope": "bank (regulatory perimeter unspecified)",
        "reporting_basis": "point_in_time",
        "averaging_window": None,
        "value": "207.7",
        "scale": 1,
        "unit": "%",
        "page": 24,
        "excerpt": "Bank LCR 207.7%",
    }
    filing = {
        "period": "2025-12-31",
        "filed": "2026-03-01",
        "description": "full",
        "url": "https://example.com/accounts",
        "figures": {},
        "banking": True,
        "banking_notes": {"metrics": [metric], "disclosures": [], "gaps": []},
    }
    bundle, groups = EvidenceBundle(evidence=[], uncertainty=[]), {}
    add_financial_evidence(bundle, groups, {"filings": [filing], "gaps": []})
    summary = render_executive_summary(bundle, groups)
    liquidity = next(line for line in summary if line.startswith("- **Liquidity"))
    followup = next(line for line in summary if line.startswith("- **Follow-up"))
    assert "207.7%" in liquidity and "perimeter unspecified" in liquidity
    assert "CET1, NSFR" in followup and "LCR" not in followup

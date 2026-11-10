import copy
import json
from pathlib import Path

import pytest

from agent_ui.banking_notes import add_banking_note_evidence, extract_banking_notes
from agent_ui.validation import EvidenceBundle


def document(page=None):
    doc = json.loads((Path(__file__).parent / "fixtures/chetwood_banking_notes.json").read_text())
    if page:
        doc["pages"] = [p for p in doc["pages"] if p["page"] == page]
    return doc


def extract(doc):
    return extract_banking_notes(doc, "2025-03-31", "CHETWOOD FINANCIAL LIMITED")


def observations(notes):
    bundle = EvidenceBundle(evidence=[], uncertainty=[])
    groups = {}
    add_banking_note_evidence(
        bundle,
        groups,
        [{"period": "2025-03-31", "url": "https://example.com/filing", "banking_notes": notes}],
    )
    by_id = {item.evidence_id: item for item in bundle.evidence}
    for eid in groups["banking_note_observations"]:
        assert all(
            i in by_id for i in by_id[eid].structured_fact["financial_observation"]["inputs"]
        )
    return [
        item.structured_fact[item.claim]
        for item in bundle.evidence
        if item.claim == "financial_observation"
    ]


def ratio_document(
    label="Liquidity coverage ratio",
    current_header="Average for year ended 31 March",
    prior_header=None,
):
    doc = document(4)
    for b in doc["pages"][0]["boxes"]:
        if "Common equity tier" in b["text"]:
            b["text"] = label
        elif b["text"] == "16%":
            b["text"] = "160%" if label == "Liquidity coverage ratio" else "16%"
        elif b["text"] == "21%":
            b["text"] = "170%" if label == "Liquidity coverage ratio" else "21%"
        elif "Year ended 31 March" in b["text"]:
            b["text"] = current_header if b["box"][0][0] < 900 else prior_header or current_header
    return doc


def test_real_chetwood_metrics_keep_scope_year_units_and_citations():
    notes = extract(document())
    values = {
        (m["metric"], m["period"]): (m["value"], m["scale"], m["scope"]) for m in notes["metrics"]
    }
    assert values[("cet1", "2025-03-31")] == ("16", 1, "group")
    assert values[("cet1", "2024-03-31")] == ("21", 1, "group")
    assert values[("regulatory_capital", "2025-03-31")] == ("193329", 1000, "group")
    assert values[("regulatory_capital", "2024-03-31")] == ("171791", 1000, "group")
    obs = observations(notes)
    assert any("-5 percentage points" in o["observation"] for o in obs)
    assert any("GBP 193.329m" in o["observation"] for o in obs)
    assert any("Liquidity coverage ratio" in g for g in notes["gaps"])
    assert any(d["topic"] == "liquidity_management" for d in notes["disclosures"])


def test_average_liquidity_ratio_preserves_basis_and_window():
    notes = extract(ratio_document())
    assert all(
        m["reporting_basis"] == "average" and m["averaging_window"] == "annual"
        for m in notes["metrics"]
    )
    text = observations(notes)[0]["observation"]
    assert "annual average for the period ended 2025-03-31" in text
    assert "-10 percentage points" in text
    assert "LCR) at " not in text


@pytest.mark.parametrize(
    "current,prior",
    [
        ("Average for year ended 31 March", "As at 31 March"),
        ("Average for year ended 31 March", "Average for quarter ended 31 March"),
        ("Average as at 31 March", "Average as at 31 March"),
    ],
)
def test_incompatible_or_unknown_windows_withhold_movement(current, prior):
    text = observations(extract(ratio_document(current_header=current, prior_header=prior)))[0][
        "observation"
    ]
    assert "movement withheld" in text
    assert "percentage points" not in text


def test_total_regulatory_capital_ratio_is_not_a_monetary_amount():
    notes = extract(ratio_document("Total regulatory capital ratio", "As at 31 March"))
    assert len(notes["metrics"]) == 2
    assert {m["metric"] for m in notes["metrics"]} == {"total_capital_ratio"}
    assert all(m["unit"] == "%" and m["scale"] == 1 for m in notes["metrics"])


def test_decimal_gbp_million_capital_matches_integer_thousands():
    doc = document(82)
    for b in doc["pages"][0]["boxes"]:
        if b["text"] in ("£'000", "£000"):
            b["text"] = "£m"
        elif b["text"] == "193,329":
            b["text"] = "193.329"
        elif b["text"] == "171,791":
            b["text"] = "171.791"
    notes = extract(doc)
    assert {(m["value"], m["scale"]) for m in notes["metrics"]} == {
        ("193.329", 1000000),
        ("171.791", 1000000),
    }
    assert "GBP 193.329m" in observations(notes)[0]["observation"]


@pytest.mark.parametrize(
    "sentence",
    [
        "The PRA has not granted a solo-consolidation waiver; capital is reported for the company only.",
        "A solo-consolidation waiver application remains pending; the reporting perimeter is unchanged.",
    ],
)
def test_waiver_mention_never_asserts_approval_or_group_scope(sentence):
    doc = document(82)
    doc["pages"][0]["boxes"] = [b for b in doc["pages"][0]["boxes"] if b["box"][0][1] < 150]
    doc["pages"][0]["boxes"].append(
        {"text": sentence, "score": 1, "box": [[145, 600], [1080, 600], [1080, 620], [145, 620]]}
    )
    notes = extract(doc)
    assert sentence in notes["disclosures"][0]["excerpt"]
    text = observations(notes)[0]["observation"]
    assert "whether it was granted" in text
    assert "does not establish group-level reporting" in text
    assert "and group-level regulatory capital reporting" not in text


def test_conflicting_values_and_wrong_table_dates_are_withheld():
    doc = document(4)
    duplicate = copy.deepcopy(doc["pages"][0])
    for b in duplicate["boxes"]:
        if b["text"] == "16%":
            b["text"] = "17%"
    doc["pages"].append(duplicate)
    assert not any(m["period"] == "2025-03-31" for m in extract(doc)["metrics"])
    assert not extract(
        ratio_document(current_header="As at 31 December", prior_header="As at 31 December")
    )["metrics"]


def narrative_document(text):
    return {
        "identity": {
            "verified": True,
            "company_name": "Example Bank Limited",
            "period": "2025-12-31",
        },
        "pages": [
            {
                "page": 24,
                "boxes": [
                    {
                        "text": text,
                        "score": 1,
                        "box": [[600, 650], [1100, 650], [1100, 670], [600, 670]],
                    }
                ],
            }
        ],
    }


def test_explicit_narrative_ratios_preserve_scope_date_and_comparative():
    doc = narrative_document(
        "The Group remained well capitalised. As at 31 December 2025, the CET1 ratio was 13.4% (2024: 14.5%)."
    )
    result = extract_banking_notes(doc, "2025-12-31", "Example Bank Limited")
    assert [(m["value"], m["scope"], m["period"]) for m in result["metrics"]] == [
        ("13.4", "group", "2025-12-31"),
        ("14.5", "group", "2024-12-31"),
    ]
    doc = narrative_document(
        "The Bank's liquidity is measured by our Liquidity Coverage Ratio (LCR). At the year end, the ratio was 207.7% (2024: 253.0%). Above the minimum requirement of 100%."
    )
    result = extract_banking_notes(doc, "2025-12-31", "Example Bank Limited")
    assert len(result["metrics"]) == 2
    assert result["metrics"][0]["scope"] == "bank (regulatory perimeter unspecified)"
    assert result["metrics"][0]["value"] == "207.7"


@pytest.mark.parametrize(
    "text",
    [
        "As at 31 December 2025, the CET1 ratio was 13.4%.",
        "The Group and Company: As at 31 December 2025, the CET1 ratio was 13.4%.",
        "The Group: As at 31 December 2024, the CET1 ratio was 13.4%.",
        "The Group: As at 31 December 2025, the minimum CET1 ratio was 13.4%.",
        "The Group: As at 31 December 2025, the average CET1 ratio was 13.4%.",
        "The Group: the CET1 ratio was 13.4%.",
    ],
)
def test_ambiguous_narrative_ratios_are_withheld(text):
    assert not extract_banking_notes(
        narrative_document(text), "2025-12-31", "Example Bank Limited"
    )["metrics"]


def test_narrative_low_confidence_or_unverified_document_is_withheld():
    doc = narrative_document("The Group: As at 31 December 2025, the CET1 ratio was 13.4%.")
    doc["pages"][0]["boxes"][0]["score"] = 0.8
    assert not extract_banking_notes(doc, "2025-12-31", "Example Bank Limited")["metrics"]
    doc["pages"][0]["boxes"][0]["score"] = 1
    doc["identity"]["verified"] = False
    assert not extract_banking_notes(doc, "2025-12-31", "Example Bank Limited")["metrics"]


def test_real_allica_narrative_keeps_regulatory_perimeters_separate():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/allica_identity_ratios.json").read_text()
    )
    doc = narrative_document("")
    doc["pages"] = [fixture["ratios_page"]]
    notes = extract_banking_notes(doc, "2025-12-31", "Example Bank Limited")
    values = {(m["metric"], m["period"]): (m["value"], m["scope"]) for m in notes["metrics"]}
    assert values[("cet1", "2025-12-31")] == ("13.4", "group")
    assert values[("lcr", "2025-12-31")] == ("207.7", "bank (regulatory perimeter unspecified)")
    assert values[("cet1", "2024-12-31")][0] == "14.5"
    assert values[("lcr", "2024-12-31")][0] == "253.0"
    assert not any(m["metric"] == "nsfr" for m in notes["metrics"])


def test_explicit_nsfr_is_extractable_without_substituting_other_ratios():
    doc = narrative_document(
        "The Group: As at 31 December 2025, the net stable funding ratio was 135.2% (2024: 130.0%)."
    )
    notes = extract_banking_notes(doc, "2025-12-31", "Example Bank Limited")
    assert [(m["metric"], m["value"]) for m in notes["metrics"]] == [
        ("nsfr", "135.2"),
        ("nsfr", "130.0"),
    ]

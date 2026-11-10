import json
from pathlib import Path

from agent_ui.banking_notes import extract_banking_notes


def extract(name):
    data = json.loads(
        (Path(__file__).parent / "fixtures/bank_disclosure_layouts.json").read_text(
            encoding="utf-8"
        )
    )[name]
    return extract_banking_notes(data, data["identity"]["period"], data["identity"]["company_name"])


def test_aldermore_numeric_table_labels_are_not_narrative():
    notes = extract("aldermore")
    assert not notes["disclosures"]


def test_chetwood_high_confidence_malformed_currency_is_retried():
    notes = extract("chetwood")
    assert {5, 82, 83} <= set(notes["retry_pages"])
    assert not any(
        token in d["excerpt"] for d in notes["disclosures"] for token in ["£2Om", "£O.2m", "£8OOk"]
    )


def test_chetwood_securitisation_keeps_distinct_balances_not_contained_excerpt():
    notes = extract("chetwood")
    excerpts = [d["excerpt"] for d in notes["disclosures"] if d["topic"] == "securitisation"]
    assert len(excerpts) == 2
    assert any("£1,120.8m" in e for e in excerpts)
    assert any("£8,374k" in e for e in excerpts)
    assert not any(a in b for a in excerpts for b in excerpts if a != b)

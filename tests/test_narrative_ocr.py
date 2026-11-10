import json
from pathlib import Path

from agent_ui.banking_notes import extract_banking_notes


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/allica_narrative_pages.json").read_text())


def extract(document):
    return extract_banking_notes(document, "2025-12-31", "ALLICA BANK LIMITED")


def test_real_column_layout_does_not_merge_unrelated_prose():
    notes = extract(fixture())
    risk = next(d["excerpt"] for d in notes["disclosures"] if d["page"] == 38)
    policy = next(d["excerpt"] for d in notes["disclosures"] if d["page"] == 95)
    assert "Market positions" in risk
    assert "contributes to our overall capital" not in risk
    assert "Intercompany transactions" in policy
    assert "in the Group are eliminated on consolidation" in policy
    assert "going concern basis" not in policy
    assert "Revenue recognition" not in policy


def test_low_confidence_continuation_requests_retry_and_is_not_published():
    notes = extract(fixture())
    assert 165 in notes["retry_pages"]
    assert any("Page 165: low-confidence" in gap for gap in notes["gaps"])
    assert not any("Durir" in d["excerpt"] for d in notes["disclosures"])
    assert any(d["page"] == 95 for d in notes["disclosures"])


def test_source_retry_recovers_interest_without_inventing_text():
    document = fixture()
    document["pages"] = [
        p if p["page"] != 165 else document["refined_page"] for p in document["pages"]
    ]
    notes = extract(document)
    assert 165 not in notes["retry_pages"]
    excerpt = next(d["excerpt"] for d in notes["disclosures"] if "£0.9m" in d["excerpt"])
    assert "subordinated loan" in excerpt and "Warwick Capital Partners LLP" in excerpt
    assert "£2.5m" in excerpt and "£4.4m" in excerpt
    assert "£2.7m" in excerpt and "£5.4m" in excerpt
    source = " ".join(b["text"] for b in document["refined_page"]["boxes"])
    assert " ".join(excerpt.split()) in " ".join(source.split())


def test_unseen_three_columns_and_failed_retry_remain_separate():
    def box(text, x, y, score=1):
        return {
            "text": text,
            "score": score,
            "box": [[x, y], [x + 180, y], [x + 180, y + 12], [x, y + 12]],
        }

    document = {
        "identity": {
            "verified": True,
            "company_name": "ALLICA BANK LIMITED",
            "period": "2025-12-31",
        },
        "pages": [
            {
                "page": 7,
                "boxes": [
                    box("Liquidity management includes daily stress tests.", 30, 300),
                    box("This remains subject to oversight.", 35, 316),
                    box("Unrelated middle column information.", 300, 300),
                    box("Related party transactions are reviewed annually.", 600, 300),
                    box("Unreadable repayment qualifications.", 600, 316, 0.5),
                ],
            }
        ],
    }
    for _ in range(2):  # A still-bad retry must not become publishable.
        notes = extract(document)
        assert notes["retry_pages"] == [7]
        assert len(notes["disclosures"]) == 1
        assert "oversight" in notes["disclosures"][0]["excerpt"]
        assert "middle" not in notes["disclosures"][0]["excerpt"]

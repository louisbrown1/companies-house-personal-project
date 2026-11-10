from agent_ui.accounts_notes import extract_notes


def box(text, x, y, score=1):
    return {
        "text": text,
        "score": score,
        "box": [[x, y], [x + 40, y], [x + 40, y + 20], [x, y + 20]],
    }


def doc(rows):
    boxes = [box("Example Limited", 140, 80), box("Notes to the financial statements", 140, 140)]
    for index, row in enumerate(rows):
        for x, text in row:
            boxes.append(box(text, x, 220 + index * 30))
    return {"notes_start": 10, "pages": [{"page": 12, "boxes": boxes}]}


def extract(document):
    return extract_notes(document, "2026-03-28", "2025-03-29", "EXAMPLE LIMITED")


def test_note_columns_and_blank_comparatives():
    d = doc(
        [
            [(800, "2026"), (1000, "2025")],
            [(800, "£'m"), (1000, "£'m")],
            [(140, "Wages and salaries"), (800, "618"), (1000, "591")],
            [(140, "Social security costs"), (800, "65")],
        ]
    )
    result = extract(d)
    assert result["pairs"]["wages_and_salaries"]["current"] == "618"
    assert result["pairs"]["wages_and_salaries"]["previous"] == "591"
    assert "social_security_costs" not in result["pairs"]


def test_notes_reject_wrong_company_years_and_units():
    rows = [
        [(800, "2026"), (1000, "2025")],
        [(800, "£'m"), (1000, "£'m")],
        [(140, "Wages and salaries"), (800, "618"), (1000, "591")],
    ]
    d = doc(rows)
    d["pages"][0]["boxes"][0]["text"] = "Other Limited"
    assert not extract(d)["pairs"]
    rows[0] = [(800, "2025"), (1000, "2025")]
    assert not extract(doc(rows))["pairs"]
    rows[0] = [(800, "2026"), (1000, "2025")]
    rows[1] = [(800, "?"), (1000, "?")]
    assert not extract(doc(rows))["pairs"]


def test_group_table_is_not_company_profit():
    d = doc(
        [
            [(140, "The overall B&M corporate Group position at each period end was as follows")],
            [(800, "2026"), (1000, "2025")],
            [(800, "£'m"), (1000, "£'m")],
            [(140, "Adjusted operating profit"), (800, "500"), (1000, "700")],
        ]
    )
    assert not extract(d)["pairs"]


def maturity(total):
    return doc(
        [
            [(140, "The following table shows the liquidity risk maturity of the debt:")],
            [
                (
                    140,
                    "Within 1 year Between 1 and 2 years Between 2 and 5 years More than 5 years Total",
                )
            ],
            [(600, "£'m")],
            [(140, "28 March 2026")],
            [
                (140, "Lease liabilities"),
                (600, "219"),
                (700, "212"),
                (800, "506"),
                (900, "492"),
                (1000, str(total)),
            ],
        ]
    )


def test_maturity_total_reconciliation():
    result = extract(maturity(1429))
    assert result["maturities"][0]["within_one_year"] == "219"
    assert result["maturities"][0]["total"] == "1429"
    assert not extract(maturity(1400))["maturities"]


def test_counterparty_balances_preserve_blank_values():
    d = doc(
        [
            [(800, "2026"), (1000, "2025")],
            [(800, "£'m"), (1000, "£'m")],
            [(140, "Non-interest-bearing intercompany loan receivables")],
            [(140, "Parent Limited"), (800, "132")],
        ]
    )
    result = extract(d)["counterparties"][0]
    assert result["current"] == "132"
    assert result["previous"] is None
    assert result["type"] == "receivable"

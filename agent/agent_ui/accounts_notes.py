"""Selected accounts-note evidence, preserving scope and comparative columns."""

import re
from datetime import date

from .accounts import normalise, number, rows_from_boxes
from .reporting_dates import MONTH_NUMBERS, MONTH_PATTERN, date_matches

PAIRED_LABELS = {
    "salaries and wages": "wages_and_salaries",
    "social security contributions": "social_security_costs",
    "share based payments": "share_based_payments",
    "share based payment expenses": "share_based_payments",
    "defined contribution pension costs": "pension_costs",
    "defined contribution pension contributions": "pension_costs",
    "pension costs": "pension_costs",
    "wages and salaries": "wages_and_salaries",
    "social security costs": "social_security_costs",
    "share based payment expense": "share_based_payments",
    "pensions defined contributions plans": "pension_costs",
    "adjusted operating profit": "adjusted_operating_profit",
}


def gbp_scale(text):
    value = re.sub(r"[\s'’]", "", text.lower())
    return {
        "£": 1,
        "gbp": 1,
        "£000": 1000,
        "gbp000": 1000,
        "£thousands": 1000,
        "£m": 1000000,
        "gbpm": 1000000,
        "£million": 1000000,
    }.get(value)


def centre(cell):
    return sum(p[0] for p in cell["box"]) / 4


def note_dates_match(header, period, prior_period):
    """Year-only columns may inherit filing dates; explicit contradictions may not."""
    expected = {p[:4]: p for p in (period, prior_period)}
    matches = list(date_matches(header))
    if any(value[:4] in expected and expected[value[:4]] != value for _, value in matches):
        return False
    # Remove complete dates before checking a shared day/month header.
    for match, _ in reversed(matches):
        header = header[: match.start()] + " " * len(match.group()) + header[match.end() :]
    partial = re.findall(r"\b(\d{1,2})\s+(" + MONTH_PATTERN + r")\b", header, re.I)
    return all(
        (int(day), MONTH_NUMBERS[month.lower()])
        == (date.fromisoformat(p).day, date.fromisoformat(p).month)
        for day, month in partial
        for p in (period, prior_period)
    )


def extract_notes(document, period, prior_period, company_name):
    result = {"pairs": {}, "excerpts": [], "maturities": [], "counterparties": [], "gaps": []}
    if not document.get("notes_start"):
        result["gaps"].append("Notes contents location not found; note coverage unavailable.")
        return result
    conflicts = set()
    for page in document["pages"]:
        boxes = page["boxes"]
        header = " ".join(b["text"] for b in boxes if b["box"][0][1] < 180)
        identity = document.get("identity", {})
        verified = (
            identity.get("verified") is True
            and identity.get("company_name") == company_name
            and identity.get("period") == period
        )
        if not verified and normalise(company_name) not in normalise(header):
            continue
        rows = rows_from_boxes(boxes)
        lines = [" | ".join(b["text"] for b in row["cells"]) for row in rows]
        full = "\n".join(lines)
        # Withhold mixed/wider-group tables from company-only numeric extraction.
        group_scope = bool(
            re.search(
                r"\bconsolidated\b|\bgroup\s+(?:and company|position|results|financial statements)|\b(?:overall|wider)\b[^\n]{0,90}\bgroup\b",
                full,
                re.I,
            )
            or any(normalise(b["text"]) == "group" for b in boxes)
        )
        if page["page"] >= document["notes_start"] and not group_scope:
            columns, monetary, section, lending_type = None, False, "", None
            scale = None
            for i, row in enumerate(rows):
                cells = row["cells"]
                if "future operating lease commitments" in lines[i].lower():
                    columns, monetary, lending_type = None, False, None
                    continue
                years = [c for c in cells if re.fullmatch(r"20\d{2}", c["text"].strip())]
                if years:
                    columns, monetary, lending_type = None, False, None
                    if (
                        len(years) == 2
                        and {c["text"].strip() for c in years} == {period[:4], prior_period[:4]}
                        and period[:4] != prior_period[:4]
                        and note_dates_match(
                            "\n".join(lines[max(0, i - 5) : i + 1]), period, prior_period
                        )
                    ):
                        by_year = {c["text"].strip(): centre(c) for c in years}
                        columns = [by_year[period[:4]], by_year[prior_period[:4]]]
                    continue
                if columns is None:
                    continue
                unit_cells = [[c for c in cells if abs(centre(c) - x) < 65] for x in columns]
                if all(len(c) == 1 and gbp_scale(c[0]["text"]) is not None for c in unit_cells):
                    scales = {gbp_scale(c[0]["text"]) for c in unit_cells}
                    monetary = len(scales) == 1
                    scale = next(iter(scales)) if monetary else None
                    continue
                if not monetary:
                    continue
                label = normalise(
                    " ".join(c["text"] for c in cells if c["box"][0][0] < min(columns) - 100)
                )
                if label in (
                    "non interest bearing intercompany loan receivables",
                    "non interest bearing intercompany loan payables",
                ):
                    lending_type = "receivable" if label.endswith("receivables") else "payable"
                if lending_type and re.search(r"\b(ltd|limited)$", label) and len(label) < 100:
                    candidates = [[c for c in cells if abs(centre(c) - x) < 65] for x in columns]
                    try:
                        values = [
                            str(number(c[0]["text"]))
                            if len(c) == 1 and c[0]["score"] >= 0.96
                            else None
                            for c in candidates
                        ]
                    except ValueError:
                        values = [None, None]
                    if any(v is not None for v in values) and all(
                        c["score"] >= 0.94 for c in cells
                    ):
                        result["counterparties"].append(
                            {
                                "counterparty": " ".join(
                                    c["text"] for c in cells if c["box"][0][0] < min(columns) - 100
                                ),
                                "type": lending_type,
                                "current": values[0],
                                "previous": values[1],
                                "period": period,
                                "prior_period": prior_period,
                                "currency": "GBP",
                                "scale": scale,
                                "page": page["page"],
                                "excerpt": lines[i],
                                "limitation": "Balances, not cash transfers. Blank comparative cells are unknown, not zero.",
                            }
                        )
                if label in ("current", "non current"):
                    section = label
                metric = PAIRED_LABELS.get(label)
                if label == "non interest bearing intergroup funding":
                    if section == "non current" and "trade and other receivables" in full.lower():
                        metric = "intergroup_loan_receivables"
                    elif section == "current" and "trade and other payables" in full.lower():
                        metric = "intergroup_loan_payables"
                if not metric or metric in conflicts:
                    continue
                if any(c["score"] < 0.94 for c in cells):
                    continue
                candidates = [[c for c in cells if abs(centre(c) - x) < 65] for x in columns]
                if not all(len(c) == 1 and c[0]["score"] >= 0.96 for c in candidates):
                    continue
                try:
                    values = [str(number(c[0]["text"])) for c in candidates]
                except ValueError:
                    continue
                entry = {
                    "current": values[0],
                    "previous": values[1],
                    "period": period,
                    "prior_period": prior_period,
                    "currency": "GBP",
                    "scale": scale,
                    "page": page["page"],
                    "excerpt": lines[i],
                    "scope": "company",
                    "method": "OCR; source review required",
                }
                existing = result["pairs"].get(metric)
                if existing and (existing["current"], existing["previous"], existing["scale"]) != (
                    *values,
                    scale,
                ):
                    result["pairs"].pop(metric)
                    conflicts.add(metric)
                    result["gaps"].append(f"Conflicting note values for {metric}; withheld.")
                else:
                    result["pairs"][metric] = entry
        # Require explicit maturity header, five numeric cells, exact date and an additive total.
        if not group_scope and re.search(
            r"(?:liquidity|contractual|undiscounted|lease).*maturit|maturit.*(?:lease|liabilit|debt)",
            full,
            re.I,
        ):
            date_label = None
            for row, line in zip(rows, lines):
                compact = normalise(line).replace(" ", "")
                for end in (period, prior_period):
                    day = date.fromisoformat(end)
                    if compact == normalise(f"{day.day} {day:%B %Y}").replace(" ", ""):
                        date_label = end
                cells = row["cells"]
                if (
                    date_label
                    and normalise(cells[0]["text"]) == "lease liabilities"
                    and len(cells) == 6
                    and all(c["score"] >= 0.96 for c in cells)
                ):
                    try:
                        values = [number(c["text"]) for c in cells[1:]]
                    except ValueError:
                        continue
                    if sum(values[:4]) != values[4] or any(v < 0 for v in values):
                        result["gaps"].append(
                            "Lease maturity schedule did not reconcile; withheld."
                        )
                        continue
                    heading = normalise(" ".join(lines[:10]))
                    for word, digit in (("one", "1"), ("two", "2"), ("five", "5")):
                        heading = re.sub(r"\b" + word + r"\b", digit, heading)
                    heading = re.sub(r"(?:less than|up to) 1 year", "within 1 year", heading)
                    heading = re.sub(r"\b1 (?:to|–|-) 2 years", "between 1 and 2 years", heading)
                    heading = re.sub(r"\b2 (?:to|–|-) 5 years", "between 2 and 5 years", heading)
                    heading = re.sub(r"(?:over|after) 5 years", "more than 5 years", heading)
                    scales = {
                        gbp_scale(b["text"]) for b in boxes if gbp_scale(b["text"]) is not None
                    }
                    positions = [
                        heading.find(s)
                        for s in ("within 1", "between 1", "between 2", "more than", "total")
                    ]
                    if (
                        any(p < 0 for p in positions)
                        or positions != sorted(positions)
                        or "5 years" not in heading
                        or len(scales) != 1
                    ):
                        continue
                    result["maturities"].append(
                        {
                            "period": date_label,
                            "within_one_year": str(values[0]),
                            "one_to_two_years": str(values[1]),
                            "two_to_five_years": str(values[2]),
                            "after_five_years": str(values[3]),
                            "total": str(values[4]),
                            "currency": "GBP",
                            "scale": next(iter(scales)),
                            "page": page["page"],
                            "excerpt": line,
                            "scope": "company",
                        }
                    )
        # Preserve whole OCR paragraphs, including qualifying text; do not ask Gemma to invent causes.
        paragraphs = []
        current = []
        last_y = None
        for row in rows:
            if last_y is not None and row["y"] - last_y > 35 and current:
                paragraphs.append(" ".join(current))
                current = []
            current.append(" ".join(c["text"] for c in row["cells"]))
            last_y = row["y"]
        if current:
            paragraphs.append(" ".join(current))
        patterns = {
            "management_explanation": r"(?:profit|margin|cost|revenue|expense|sales).*(?:due to|driven by|reflects?|because|result of)|(?:increase|decrease|decline|growth).*(?:profit|margin|cost|revenue)",
            "lease_terms": r"(?:lease|right.of.use).*(?:covenant|extension|termination|renewal)|(?:extension|termination).*(?:lease|clause)",
            "group_funding_terms": r"(?:intercompany|intergroup|intragroup|related.party).*(?:interest|repay|demand|terms|funding)",
            "group_guarantees": r"(?:guarantor|guarantees?).*(?:group|parent|subsidiar|loan|borrow|notes|debt)",
            "cash_comparability": r"(?:deposit|cash equivalent|money.market).*(?:matured|maturity|reclassif)",
        }
        for paragraph in paragraphs:
            for topic, pattern in patterns.items():
                if re.search(pattern, paragraph, re.I) and 50 <= len(paragraph) <= 1800:
                    # Current-period guarantee paragraph only; retain prior counterpart in the linked page.
                    if topic == "group_guarantees" and period[:4] not in paragraph:
                        continue
                    if topic == "group_guarantees" and normalise(company_name).replace(
                        " limited", " ltd"
                    ) not in normalise(paragraph).replace(" limited", " ltd"):
                        continue
                    if sum(e["topic"] == topic for e in result["excerpts"]) < (
                        6 if topic == "management_explanation" else 3
                    ):
                        result["excerpts"].append(
                            {
                                "topic": topic,
                                "page": page["page"],
                                "excerpt": paragraph,
                                "attribution": "Company disclosure, OCR transcription; not independently verified",
                            }
                        )
    for key in (
        "wages_and_salaries",
        "social_security_costs",
        "intergroup_loan_receivables",
        "intergroup_loan_payables",
    ):
        if key not in result["pairs"]:
            result["gaps"].append(f"Note detail unavailable or ambiguous: {key}.")
    if not result["maturities"]:
        result["gaps"].append(
            "Supported lease maturity schedule not found; future commitments remain incomplete."
        )
    result["gaps"].append(
        "Selected notes and management commentary from the latest filing only; not a complete audit or covenant review. Note comparisons use the latest filing's comparative columns. Blank cells are not assumed to be zero. Cost-note details overlap statement expenses and must not be added again to the profit bridge."
    )
    return result

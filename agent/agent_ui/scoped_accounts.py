"""Explicit scope/period columns for group and banking financial statements."""

import re
import unicodedata
from datetime import date

from .accounts import LABELS, STATEMENT, normalise, number, rows_from_boxes

BANK_LABELS = {
    "interest income calculated using the effective interest rate method": "interest_income",
    "interest payable and similar charges": "interest_expense",
    "net interest income": "net_interest_income",
    "total income": "total_income",
    "loans and advances to customers": "customer_loans",
    "customer deposits": "customer_deposits",
    "customers accounts": "customer_deposits",
    "amounts due to banks": "credit_institution_funding",
    "impairment charge release on loans and advances to customers": "loan_impairment_expense",
    "investment in debt securities": "debt_securities",
    "total equity": "total_equity",
    "amounts owed to credit institutions": "credit_institution_funding",
    "loss before taxation": "profit_before_tax",
    "profit before taxation": "profit_before_tax",
    "loss after tax and loss for the year": "profit_after_tax",
    "profit after tax and profit for the year": "profit_after_tax",
    "impairment of loans and advances to customers": "loan_impairment_expense",
    "net cash from operating activities": "operating_cash_flow",
    "net cash used in operating activities": "operating_cash_flow",
    "closing cash and cash equivalents": "cash",
    "loss profit before tax": "profit_before_tax",
    "profit loss before tax": "profit_before_tax",
}


def clean(text):
    return normalise(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode())


def right(box):
    return max(p[0] for p in box["box"])


def column_headers(boxes, heading_y, header_padding=20):
    """Anchor numeric columns to their printed units, not assumed column order."""
    headers = [b for b in boxes if heading_y + header_padding < b["box"][0][1] < heading_y + 210]
    units = []
    for b in headers:
        text = b["text"].lower().replace(" ", "").replace("o", "0")
        if re.fullmatch(r"(?:£|gbp)?['’]?(?:000|0000|m)", text):
            units.append(
                (
                    b,
                    1000000 if text.endswith("m") else 1000,
                    "GBP" if "£" in text or "gbp" in text else None,
                )
            )
    if (
        len(units) not in (2, 4)
        or max(b["box"][0][1] for b, _, _ in units) - min(b["box"][0][1] for b, _, _ in units) > 18
    ):
        return []
    units.sort(key=lambda u: right(u[0]))
    if len({s for _, s, _ in units}) != 1 or not any(c for _, _, c in units):
        return []
    spacing = min(right(units[i + 1][0]) - right(units[i][0]) for i in range(len(units) - 1))
    if spacing < 50:
        return []
    tolerance = min(60, spacing * 0.48)
    result = []
    for unit, scale, _ in units:
        x = right(unit)
        above = [b for b in headers if b["box"][0][1] < unit["box"][0][1]]
        years = []
        for b in above:
            for match in re.finditer(r"\b20\d{2}\b", b["text"]):
                # OCR sometimes merges adjacent date cells. Retain token geometry.
                end = b["box"][0][0] + (right(b) - b["box"][0][0]) * match.end() / len(b["text"])
                if abs(end - x) < tolerance:
                    years.append(match.group())
        scopes = [
            clean(b["text"])
            for b in above
            if clean(b["text"]) in ("group", "company") and abs(right(b) - x) < tolerance
        ]
        # Shared date text is accepted only when all explicit day/month labels agree.
        dates = re.findall(
            r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b",
            " ".join(b["text"] for b in above),
            re.I,
        )
        if not dates:
            # Split lines such as "Year ended 31" / "March 2025".
            joined = " ".join(
                b["text"]
                for b in sorted(above, key=lambda b: b["box"][0][1])
                if b["box"][0][0] < x and right(b) > x - spacing
            )
            dates = re.findall(r"\b(\d{1,2})\s+(March)\b", joined, re.I)
        dates = {(int(day), month.lower()) for day, month in dates}
        if len(set(years)) != 1 or len(dates) != 1 or len(set(scopes)) > 1:
            return []
        result.append(
            {
                "x": x,
                "year": years[0],
                "day_month": next(iter(dates)),
                "scope": scopes[0] if scopes else None,
                "scale": scale,
                "y": max(p[1] for p in unit["box"]),
                "tolerance": min(28, spacing * 0.35),
            }
        )
    return result


def extract_scoped_figures(document, period, company_name, scope):
    figures, gaps, conflicts = {}, [], set()
    expected = date.fromisoformat(period)
    for page in document["pages"]:
        boxes = page["boxes"]
        header = " ".join(b["text"] for b in boxes if b["box"][0][1] < 180)
        titles = [
            b
            for b in boxes
            if b["box"][0][1] < 220
            and STATEMENT.search(b["text"])
            and "annual report" not in b["text"].lower()
        ]
        if (
            normalise(company_name) not in normalise(header)
            or len(titles) != 1
            or "contents" in header.lower()
        ):
            continue
        title = titles[0]["text"].lower()
        columns = column_headers(boxes, titles[0]["box"][0][1])
        combined = "company" in title and "consolidated" in title
        fixed = "group" if "consolidated" in title else "company" if "company" in title else None
        if not columns or (combined and any(c["scope"] is None for c in columns)):
            gaps.append(
                f"Page {page['page']}: explicit scope/date/unit columns could not be resolved."
            )
            continue
        matches = [
            c
            for c in columns
            if (c["scope"] if combined else fixed) == scope
            and c["year"] == str(expected.year)
            and c["day_month"] == (expected.day, expected.strftime("%B").lower())
        ]
        if len(matches) != 1:
            continue
        col = matches[0]
        note_boxes = [b for b in boxes if clean(b["text"]) == "notes" and b["box"][0][1] < col["y"]]
        boundary = min(b["box"][0][0] for b in note_boxes) if note_boxes else columns[0]["x"] - 100
        for row in rows_from_boxes(boxes):
            if row["y"] <= col["y"]:
                continue
            labels = [b for b in row["cells"] if right(b) < boundary]
            label = clean(" ".join(b["text"] for b in labels))
            metric = BANK_LABELS.get(label, LABELS.get(label))
            values = [b for b in row["cells"] if abs(right(b) - col["x"]) < col["tolerance"]]
            if (
                not metric
                or metric in conflicts
                or len(values) != 1
                or values[0]["score"] < 0.96
                or any(b["score"] < 0.94 for b in labels)
            ):
                continue
            try:
                value = number(values[0]["text"])
            except ValueError:
                continue
            if label.startswith("loss after tax") or label == "loss before taxation":
                value = -abs(value)
            fact = {
                "value": str(value),
                "scale": col["scale"],
                "currency": "GBP",
                "scope": scope,
                "period": period,
                "page": page["page"],
                "excerpt": " | ".join(b["text"] for b in row["cells"]),
                "method": "OCR with explicit scope/date/unit columns; source review required",
            }
            if metric in figures and figures[metric]["value"] != fact["value"]:
                figures.pop(metric)
                conflicts.add(metric)
                gaps.append(f"{scope} {metric}: conflicting values withheld.")
            else:
                figures[metric] = fact
    return figures, list(dict.fromkeys(gaps))


def is_banking_document(document):
    return any(
        clean(b["text"]) == "customer deposits" for p in document["pages"] for b in p["boxes"]
    )

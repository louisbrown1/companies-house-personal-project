"""Filing identity and page-aware evidence, independent of repeated page headers."""

import re
from datetime import date

from .accounts import normalise, rows_from_boxes
from .reporting_dates import full_dates, labelled_period_dates

MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
)


def canonical_number(value):

    value = re.sub(r"\s+", "", value).upper()

    if re.fullmatch(r"\d{1,8}", value):
        return value.zfill(8)

    if re.fullmatch(r"[A-Z]{2}\d{6}", value):
        return value

    return None


def canonical_name(value):

    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = normalise(value)

    return re.sub(r"\bltd\b", "limited", value)


def identity_lines(page):

    rows = rows_from_boxes(page["boxes"])
    lines = []

    for i, row in enumerate(rows):
        cells = row["cells"]
        text = " ".join(b["text"] for b in cells)

        lines.append(text)

        combined = text

        for nxt in rows[i + 1 : i + 3]:
            if nxt["y"] - row["y"] > max(
                160,
                3 * max(max(p[1] for p in b["box"]) - min(p[1] for p in b["box"]) for b in cells),
            ):
                break

            if abs(nxt["cells"][0]["box"][0][0] - cells[0]["box"][0][0]) > 80:
                break

            combined += " " + " ".join(b["text"] for b in nxt["cells"])
            lines.append(combined)

    return lines


def statement_dates(document):

    from .document_discovery import page_headings, top

    evidence = []

    for page in document["pages"]:
        if page["page"] not in document.get("statement_pages", []):
            continue

        headings = sorted(
            (h for h in page_headings(page["boxes"]) if h["kind"] == "statement"),
            key=lambda h: min(p[1] for p in h["box"]),
        )
        # Body labels such as a balance-sheet 'Profit and loss account' are not
        # a second primary statement and must not bind a later approval date.
        for heading in headings[:1]:
            # Equity roll-forwards can present the comparative year as a separate
            # first table; they cannot establish the document's current period.
            if "changes in equity" in normalise(heading["title"]):
                continue

            y = min(p[1] for p in heading["box"])

            region = [b for b in page["boxes"] if y - 30 <= top(b) <= y + 310]
            # Approval/signature text can sit beside a statement header in a
            # separate column; its date is not the reporting period.
            region = [
                b
                for b in region
                if not re.search(
                    r"\b(?:approved|approval|authorised|authorized|signed|issued)\b",
                    b["text"],
                    re.I,
                )
            ]

            units = [
                top(b)
                for b in region
                if re.fullmatch(r"(?:£|GBP)?['’]?(?:000|m)", b["text"].replace(" ", ""), re.I)
            ]

            if units:
                region = [b for b in region if top(b) <= min(units) + 12]

            text = " ".join(b["text"] for b in sorted(region, key=top))
            text = re.sub(r"(?<=[A-Za-z])(?=20\d{2}\b)", " ", text)

            days = re.findall(r"\b(\d{1,2})\s+(" + MONTHS + r")\b", text, re.I)

            years = re.findall(r"\b(20\d{2})\b", text)

            dates = full_dates(text)

            import calendar

            if len(set((d, m.lower()) for d, m in days)) == 1 and years:
                day, month = days[0]

                try:
                    dates.append(
                        date(
                            max(map(int, years)),
                            list(calendar.month_name).index(month.capitalize()),
                            int(day),
                        ).isoformat()
                    )

                except ValueError:
                    pass

            if dates:
                evidence.append(
                    {
                        "page": page["page"],
                        "latest_header_date": max(dates),
                        "heading": heading["title"],
                    }
                )

    return evidence


def registration_label(text, opening_page=False):
    """Company IDs are distinct from regulator, charity and tax registrations."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", text)
    if re.search(
        r"\b(?:charity|charitable|charty|FCA|PRA|authority|regulator|regulatory|VAT|tax)\b",
        text,
        re.I,
    ):
        return None
    pattern = r"\bcompany\s+(?:registration\s+)?(?:number|no\.?)\s*:?\s*"
    if opening_page:
        pattern = r"\b(?:company\s+(?:registration\s+)?|registered\s+|registration\s+)(?:number|no\.?)\s*:?\s*"
    return re.search(pattern, text, re.I)


def bind_document(document, company_name, company_number, period):
    """Combine source identity, registry provenance and statement dates; contradictions veto."""

    expected = date.fromisoformat(period)
    expected_number = canonical_number(company_number)
    name = canonical_name(company_name)

    identity_pages = [
        p
        for p in document["pages"]
        if p["page"] <= 8 or p["page"] in document.get("identity_pages", [])
    ]

    name_pages = []
    number_pages = []
    numbers = set()
    cover_names = set()

    for page in identity_pages:
        lines = identity_lines(page) + [
            b["text"] for b in page["boxes"] if b.get("score", 1) >= 0.96
        ]

        for text in lines:
            normalized = canonical_name(text)

            if re.fullmatch(
                re.escape(name) + r"(?: (?:annual|report|financial statements)\b.*)?", normalized
            ):
                name_pages.append(page["page"])

        if page["page"] == 1:
            for b in page["boxes"]:
                candidate = canonical_name(b["text"])

                if (
                    re.fullmatch(r"[a-z][a-z0-9 ]{2,80} (?:limited|plc|llp)", candidate)
                    and b.get("score", 1) >= 0.96
                ):
                    cover_names.add(candidate)

        for b in page["boxes"]:
            text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", b["text"])
            score = b.get("score", 1)

            label = registration_label(text, page["page"] <= 3)
            tokens = []
            if label:
                token = re.match(
                    r"(?:[A-Za-z]{2}\s*\d{6}|\d{1,8})(?![A-Za-z0-9])", text[label.end() :]
                )
                if token:
                    tokens = [token.group()]
            elif re.fullmatch(r"(?:[A-Za-z]{2}\s*\d{6}|\d{1,8})", text.strip()):
                y = min(p[1] for p in b["box"])
                nearby = any(
                    registration_label(other["text"], page["page"] <= 3)
                    and 0 <= y - min(p[1] for p in other["box"]) < 65
                    and min(p[0] for p in other["box"]) - 40
                    <= min(p[0] for p in b["box"])
                    <= max(p[0] for p in other["box"]) + 100
                    for other in page["boxes"]
                )
                if nearby or (
                    page["page"] <= 3
                    and re.fullmatch(r"(?:[A-Za-z]{2}\d{6}|\d{6,8})", text.strip())
                ):
                    tokens = [text.strip()]

            for token in tokens:
                number = canonical_number(token)

                if number and score >= 0.90:
                    numbers.add(number)

                    if number == expected_number:
                        number_pages.append(page["page"])

    front = " ".join(
        " ".join(b["text"] for b in p["boxes"]) for p in identity_pages if p["page"] <= 3
    )

    cover = " ".join(
        " ".join(b["text"] for b in p["boxes"]) for p in identity_pages if p["page"] == 1
    )

    report_years = re.findall(
        r"(?:annual report(?: and accounts)?|report and accounts|financial statements|accounts)\s+(20\d{2})",
        front,
        re.I,
    )

    cover_dates = re.findall(r"\b(\d{1,2})\s+(" + MONTHS + r")\s+(20\d{2})\b", cover, re.I)

    expected_text = normalise(f"{expected.day} {expected:%B %Y}").replace(" ", "")

    cover_match = expected_text in normalise(cover).replace(" ", "")

    labelled_dates = labelled_period_dates(cover)
    cover_match = cover_match or period in labelled_dates

    dated = statement_dates(document)

    statement_match = any(e["latest_header_date"] == period for e in dated)

    # Other dated primary statements are contradictions, not a licence to use a comparative column.

    contradictions = []

    if numbers and numbers != {expected_number}:
        contradictions.append("different_registration_number")

    if cover_names and name not in cover_names:
        contradictions.append("different_cover_entity")

    if report_years and set(report_years) != {str(expected.year)}:
        contradictions.append("different_report_year")

    if (cover_dates and not cover_match) or any(d != period for d in labelled_dates):
        contradictions.append("different_cover_period")

    if dated and any(e["latest_header_date"] != period for e in dated):
        contradictions.append("different_statement_period")

    provenance = document.get("registry_binding", {})

    linked = (
        provenance.get("company_number") == company_number
        and provenance.get("period") == period
        and provenance.get("source") == "companies_house_filing_history"
    )

    name_ok = bool(name_pages)
    number_ok = bool(number_pages)

    period_ok = (cover_match or statement_match) and not any(
        "period" in c or "year" in c for c in contradictions
    )

    # A registry-linked filing plus source legal name and dated statements can

    # resolve an absent/unreadable number, but never override a conflicting number.

    identity_ok = name_ok and (number_ok or (linked and not numbers and statement_match))

    verified = identity_ok and period_ok and not contradictions

    identity = {
        "verified": verified,
        "company_name": company_name,
        "company_number": company_number,
        "period": period,
        "checks": {
            "cover_name": name_ok,
            "company_number": number_ok,
            "period": period_ok,
            "registry_link": linked,
        },
        "evidence": {
            "name_pages": sorted(set(name_pages)),
            "number_pages": sorted(set(number_pages)),
            "source_numbers": sorted(numbers),
            "statement_dates": dated,
            "report_years": sorted(set(report_years)),
        },
        "contradictions": contradictions,
    }

    return {**document, "identity": identity}


def section_scope(document, page):
    """Inherit scope only from an explicit company/group financial-statements section."""

    if not document.get("identity", {}).get("verified"):
        return None

    from statistics import median

    from .document_discovery import height, left, top

    starts = []

    for p in document["pages"]:
        if p["page"] > page:
            continue

        text_boxes = [b for b in p["boxes"] if re.search(r"[A-Za-z]{3}", b["text"])]
        typical = median([height(b) for b in text_boxes]) if text_boxes else 0
        first_y = min((top(b) for b in text_boxes), default=0)
        for b in p["boxes"]:
            title = normalise(b["text"])

            if not re.fullmatch(
                r"(?:company|group|consolidated) financial statements|notes to (?:the )?financial statements",
                title,
            ):
                continue
            nearby = [
                other
                for other in text_boxes
                if other is not b
                and abs(left(other) - left(b)) < 45
                and abs(top(other) - top(b)) < max(50, height(b) * 2.5)
            ]
            reference = any(
                (
                    top(other) < top(b)
                    and re.search(
                        r"(?:of the|to the|in the|refer to|see)$", normalise(other["text"])
                    )
                )
                or (top(other) > top(b) and re.match(r"pages?\s+\d", normalise(other["text"])))
                for other in nearby
            )
            prominent = len(text_boxes) <= 8 or (
                height(b) >= typical * 1.4 and top(b) <= first_y + 120
            )
            confirmed = any(
                s.get("verified")
                and s.get("pdf_page") == p["page"]
                and s.get("kind") == "financial_section"
                and normalise(s["title"]) == title
                for s in document.get("sections", [])
            )
            if reference or not (prominent or (confirmed and top(b) <= first_y + 60)):
                continue

            if re.fullmatch(r"(company|group|consolidated) financial statements", title):
                starts.append((p["page"], "company" if title.startswith("company") else "group"))

            if re.fullmatch(r"notes to (?:the )?financial statements", title):
                starts.append((p["page"], None))

    if not starts:
        return None

    latest = max(p for p, _ in starts)

    scopes = {s for p, s in starts if p == latest}

    return next(iter(scopes)) if len(scopes) == 1 else None


def page_markdown(page):

    lines = [
        f"## PDF page {page['page']}",
        "| Row | Cells in left-to-right order |",
        "| --- | --- |",
    ]

    for i, row in enumerate(rows_from_boxes(page["boxes"])):
        cells = [" ".join(c["text"].split()).replace("|", "\\|") for c in row["cells"]]

        lines.append(f"| p{page['page']}r{i} | {' / '.join(cells)} |")

    return "\n".join(lines)

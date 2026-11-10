"""Published banking ratios and bounded note disclosures, with explicit scope."""

import re
from datetime import date
from decimal import Decimal

from .accounts import normalise, number, rows_from_boxes
from .scoped_accounts import right

RATIOS = {
    "cet1": r"common equity tier 1.*capital ratio|cet1 (?:capital )?ratio",
    "lcr": r"liquidity coverage ratio(?: lcr)?",
    "nsfr": r"net stable funding ratio(?: nsfr)?",
    "total_capital_ratio": r"total (?:regulatory )?capital ratio",
    "leverage_ratio": r"(?:regulatory )?leverage ratio",
}
NAMES = {
    "cet1": "CET1 ratio",
    "lcr": "Liquidity coverage ratio (LCR)",
    "nsfr": "Net stable funding ratio (NSFR)",
    "total_capital_ratio": "Total capital ratio",
    "leverage_ratio": "Leverage ratio",
    "regulatory_capital": "Total regulatory capital",
}


def narrative_columns(boxes):
    """Keep prose reading order within left-aligned columns, not page-wide rows.

    A small indentation tolerance joins wrapped paragraph lines. Monetary table
    cells remain the responsibility of the separate geometry-based metric parser.
    """
    # Detect table rows before separating columns, while numeric siblings are visible.
    table_boxes = set()
    for row in rows_from_boxes(boxes):
        numeric = [
            b for b in row["cells"] if re.fullmatch(r"[£$€]?[()\dOo.,%−–+\-]+", b["text"].strip())
        ]
        labels = [b for b in row["cells"] if b not in numeric]
        if (
            len(numeric) >= 2
            and labels
            and all(min(n["box"][0][0] for n in numeric) > right(b) for b in labels)
        ):
            table_boxes.update(id(b) for b in row["cells"])
    columns = []
    for box in sorted(boxes, key=lambda b: min(p[0] for p in b["box"])):
        if id(box) in table_boxes or not re.search(r"[A-Za-z]{2}", box["text"]):
            continue
        x = min(p[0] for p in box["box"])
        column = next((c for left, c in columns if abs(left - x) < 45), None)
        if column is None:
            column = []
            columns.append((x, column))
        column.append(box)
    return [rows_from_boxes(column) for _, column in columns]


def context(boxes, cell, y, period):
    x = right(cell)
    above = [b for b in boxes if y - 220 < b["box"][0][1] < y - 5]
    years = []
    for b in above:
        if b["score"] < 0.96:
            continue
        for m in re.finditer(r"\b20\d{2}\b", b["text"]):
            token_right = b["box"][0][0] + (right(b) - b["box"][0][0]) * m.end() / len(b["text"])
            if abs(token_right - x) < 35:
                years.append((b["box"][0][1], m.group()))
    scopes = [
        (b["box"][0][1], normalise(b["text"]))
        for b in above
        if normalise(b["text"]) in ("group", "company")
        and b["score"] >= 0.96
        and abs(right(b) - x) < 35
    ]
    if not years or not scopes:
        return None
    near_years = {value for yy, value in years if yy > max(y for y, _ in years) - 10}
    if len(near_years) != 1:
        return None
    year = next(iter(near_years))
    if int(year) not in (int(period[:4]), int(period[:4]) - 1):
        return None
    # Reject a table dated differently from the filing period (e.g. December data
    # inside March accounts); a year alone must never imply a reporting date.
    column_text = " ".join(
        b["text"]
        for b in sorted(above, key=lambda b: b["box"][0][1])
        if right(b) >= x - 35 and b["box"][0][0] < x + 10
    )
    months = re.findall(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
        column_text,
        re.I,
    )
    expected = date.fromisoformat(period)
    days = re.findall(
        r"\b(?:at|ended)\s+(\d{1,2})\b|\b(\d{1,2})\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
        column_text,
        re.I,
    )
    if (
        not months
        or {m.lower() for m in months} != {expected.strftime("%B").lower()}
        or not days
        or {int(a or b) for a, b in days} != {expected.day}
    ):
        return None
    basis = (
        "average"
        if re.search(r"\baverag(?:e|ed|es|ing)\b", column_text, re.I)
        else "point_in_time"
        if re.search(r"\bas at\b", column_text, re.I)
        else "period_end_unspecified"
    )
    window = None
    if basis == "average":
        for pattern, name in [
            (r"\b(?:year|12 months|twelve months)\b", "annual"),
            (r"\b(?:quarter|3 months|three months)\b", "quarterly"),
            (r"\b(?:month|monthly)\b", "monthly"),
        ]:
            if re.search(pattern, column_text, re.I):
                window = name
                break
    return {
        "period": year + period[4:],
        "scope": max(scopes)[1],
        "reporting_basis": basis,
        "averaging_window": window,
        "header_excerpt": column_text,
    }


def narrative_ratios(page, identity, period, company_name):
    """Copy explicit ratios from bounded, single-column paragraphs; never infer scope."""
    from .reporting_dates import full_dates

    if not (
        identity.get("verified")
        and identity.get("company_name") == company_name
        and identity.get("period") == period
    ):
        return []
    columns = []
    for box in sorted(page["boxes"], key=lambda b: b["box"][0][0]):
        x = box["box"][0][0]
        column = next((c for c in columns if abs(c[0]["box"][0][0] - x) < 45), None)
        if column is None:
            columns.append([box])
        else:
            column.append(box)
    paragraphs = []
    for column in columns:
        paragraph = []
        for box in sorted(column, key=lambda b: b["box"][0][1]):
            if paragraph:
                prev = paragraph[-1]
                height = max(p[1] for p in prev["box"]) - min(p[1] for p in prev["box"])
                if box["box"][0][1] - prev["box"][0][1] > max(22, height * 1.4):
                    paragraphs.append(paragraph)
                    paragraph = []
            paragraph.append(box)
        if paragraph:
            paragraphs.append(paragraph)
    patterns = {
        "cet1": r"(?:CET1|common equity tier 1)(?: capital)? ratio",
        "lcr": r"liquidity coverage ratio(?:\s*\(LCR\))?",
        "nsfr": r"net stable funding ratio(?:\s*\(NSFR\))?",
        "total_capital_ratio": r"total capital ratio",
    }
    result = []
    for paragraph in paragraphs:
        text = " ".join(b["text"] for b in paragraph)
        if len(text) > 1800:
            continue
        scopes = set(re.findall(r"\b(group|company|bank)\b", text, re.I))
        scopes = {s.lower() for s in scopes}
        if len(scopes) != 1:
            continue
        scope = next(iter(scopes))
        if scope == "bank":
            scope = "bank (regulatory perimeter unspecified)"
        dates = full_dates(text)
        year_end = bool(re.search(r"\bat (?:the )?year end\b", text, re.I))
        if dates and set(dates) != {period}:
            continue
        if not dates and not year_end:
            continue
        for metric, pattern in patterns.items():
            # Either the named ratio directly precedes the amount, or it is
            # followed by a year-end sentence explicitly referring to that ratio.
            match = re.search(
                pattern
                + r"(?:\s+(?:was|is|of|stood at)\s*|[.]?\s+At (?:the )?year end,?\s+the ratio was\s*)(\d+(?:\.\d+)?)\s*%",
                text,
                re.I,
            )
            if not match:
                continue
            if re.search(r"\b(?:average|minimum|target)\b[^.]{0,60}$", text[: match.start()], re.I):
                continue
            if any(b.get("score", 1) < 0.94 for b in paragraph):
                continue
            value = Decimal(match[1])
            if not 0 <= value <= (10000 if metric in ("lcr", "nsfr") else 100):
                continue
            current = {
                "metric": metric,
                "period": period,
                "scope": scope,
                "reporting_basis": "point_in_time",
                "averaging_window": None,
                "header_excerpt": text,
                "value": str(value),
                "scale": 1,
                "unit": "%",
                "page": page["page"],
                "excerpt": text,
                "basis": "Explicit filing narrative; source scope retained; no independent compliance assessment.",
            }
            result.append(current)
            prior = re.match(r"\s*\((20\d{2}):\s*(\d+(?:\.\d+)?)\s*%\)", text[match.end() :])
            if prior and int(prior[1]) == int(period[:4]) - 1:
                result.append(
                    {**current, "period": prior[1] + period[4:], "value": str(Decimal(prior[2]))}
                )
    return result


def extract_banking_notes(document, period, company_name):
    result = {"metrics": [], "disclosures": [], "gaps": [], "retry_pages": []}
    seen_excerpts = {}
    narrative_retries = set()
    expected = date.fromisoformat(period)
    date_text = normalise(f"{expected.day} {expected:%B} {expected.year}")
    for page in document["pages"]:
        boxes = page["boxes"]
        header = " ".join(b["text"] for b in boxes if b["box"][0][1] < 180)
        identity = document.get("identity", {})
        document_verified = (
            identity.get("verified") is True
            and identity.get("company_name") == company_name
            and identity.get("period") == period
        )
        if not document_verified and (
            normalise(company_name) not in normalise(header)
            or date_text.replace(" ", "") not in normalise(header).replace(" ", "")
        ):
            continue
        narrative = narrative_ratios(page, identity, period, company_name)
        result["metrics"].extend(narrative)
        page_text = " ".join(b["text"] for b in boxes)
        if re.search(r"CET1|liquidity coverage ratio|net stable funding ratio", page_text, re.I):
            result["retry_pages"].append(page["page"])
        rows = rows_from_boxes(boxes)
        for row in rows:
            line = " | ".join(b["text"] for b in row["cells"])
            candidates = [
                b for b in row["cells"] if re.fullmatch(r"\d+(?:\.\d+)?\s*%", b["text"].strip())
            ]
            label = normalise(" ".join(b["text"] for b in row["cells"] if b not in candidates))
            metric = next(
                (key for key, pattern in RATIOS.items() if re.fullmatch(pattern, label)), None
            )
            amounts = [
                b
                for b in row["cells"]
                if b["box"][0][0] > 600
                and re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", b["text"].strip())
            ]
            amount_label = normalise(" ".join(b["text"] for b in row["cells"] if b not in amounts))
            capital = metric is None and amount_label == "total regulatory capital"
            if capital:
                metric = "regulatory_capital"
                candidates = amounts
            if not metric:
                continue
            for cell in candidates:
                ctx = context(boxes, cell, row["y"], period)
                if (
                    not ctx
                    or cell["score"] < 0.96
                    or any(b["score"] < 0.94 for b in row["cells"] if b not in candidates)
                ):
                    result["retry_pages"].append(page["page"])
                    continue
                scale = 1
                if capital:
                    units = [
                        b
                        for b in boxes
                        if row["y"] - 160 < b["box"][0][1] < row["y"]
                        and abs(right(b) - right(cell)) < 45
                        and re.fullmatch(r"£['’]?(?:000|m)", b["text"].replace(" ", ""), re.I)
                    ]
                    if len(units) != 1:
                        result["retry_pages"].append(page["page"])
                        continue
                    scale = 1000000 if units[0]["text"].lower().endswith("m") else 1000
                value = number(cell["text"].replace("%", ""))
                if value < 0 or (
                    not capital and value > (10000 if metric in ("lcr", "nsfr") else 100)
                ):
                    continue
                result["metrics"].append(
                    {
                        "metric": metric,
                        **ctx,
                        "value": str(value),
                        "scale": scale,
                        "unit": "GBP" if capital else "%",
                        "page": page["page"],
                        "excerpt": line,
                        "basis": "Published filing value; OCR source review required; reporting basis as disclosed, not independently verified.",
                    }
                )
        # Copy bounded contiguous paragraphs; numbers are quoted evidence, never parsed by Gemma.
        # Topic selection requires explicit phrases, not generic references to capital/liquidity.
        topics = {
            "capital_scope": r"solo.consolidation waiver",
            "liquidity_management": r"liquidity.*(?:stress test|buffer|management|contingency)|(?:treasury|stress test|contingency funding).*liquidity",
            "related_party_transactions": r"transactions with related parties|purchase gross loans and advances|(?:related.party|intercompany|intragroup).*(?:purchase|sale|transfer|transaction)",
            "group_funding": r"(?:facility|loan|funding).*(?:parent|subsidiary|sister company|related.party)|(?:intercompany|intragroup|related.party).*(?:facility|loan|funding)",
            "securitisation": r"retains the risk and reward|securiti[sz].*(?:risk|retained|transfer|asset)|(?:risk|reward).*(?:securiti[sz]|transferred assets)",
            "capital_support": r"(?:investor|shareholder|parent).*(?:inject|capital contribution|capital support|subscrib)|capital.*(?:inject|contribution|support).*shareholder",
            "credit_risk": r"forward.looking information.*expected credit|expected credit losses.*forward.looking",
        }
        for prose_rows in narrative_columns(boxes):
            lines = [" ".join(b["text"] for b in row["cells"]) for row in prose_rows]
            for i, line in enumerate(lines):
                # Process each column independently, including the confidence of
                # every continuation line rather than just the topic match.
                _collect_disclosures(
                    lines,
                    prose_rows,
                    i,
                    topics,
                    seen_excerpts,
                    narrative_retries,
                    result,
                    page["page"],
                    period,
                )
    # Duplicate or contradictory metrics do not silently override earlier evidence.
    grouped = {}
    for metric in result["metrics"]:
        grouped.setdefault((metric["metric"], metric["scope"], metric["period"]), []).append(metric)
    result["metrics"] = []
    for key, values in grouped.items():
        if (
            len(
                {
                    (v["value"], v["scale"], v["reporting_basis"], v["averaging_window"])
                    for v in values
                }
            )
            != 1
        ):
            result["gaps"].append(f"Conflicting {key[0]} values for {key[1]} {key[2]}; withheld.")
        else:
            result["metrics"].append(values[0])
    for key in ("cet1", "lcr", "nsfr", "regulatory_capital"):
        if not any(m["metric"] == key and m["period"] == period for m in result["metrics"]):
            result["gaps"].append(
                f"{NAMES[key]}: no reliable, explicitly scoped value extracted for {period}. This does not establish that it is undisclosed elsewhere."
            )
    result["retry_pages"] = (
        sorted(narrative_retries) + sorted(set(result["retry_pages"]) - narrative_retries)
    )[:6]
    result["gaps"].extend(
        f"Page {page}: low-confidence note transcription withheld; source review required."
        for page in sorted(narrative_retries)
    )
    result["gaps"].append(
        "Published ratios and management compliance statements do not establish current regulatory compliance. Firm-specific requirements, buffers, reporting basis and Pillar 3 disclosures require review. No LCR/NSFR proxy is calculated."
    )
    return result


def _collect_disclosures(lines, rows, i, topics, seen_excerpts, retries, result, page, period):
    line = lines[i]
    for topic, pattern in topics.items():
        if not re.search(pattern, line, re.I):
            continue
        if topic == "related_party_transactions" and re.fullmatch(
            r"(?:note\s+)?(?:\d+\s+)?related party transactions", normalise(line)
        ):
            continue
        start = max(0, i - 2) if topic == "capital_scope" else i
        end = i + 1
        section_heading = normalise(line) == "transactions with related parties"
        if section_heading:
            start = i + 1
            end = min(start + 1, len(rows))
        while (
            end < len(rows)
            and end < start + 8
            and rows[end]["y"] - rows[end - 1]["y"] < (32 if section_heading else 24)
        ):
            if len(lines[end].split()) <= 4 and not re.search(r"[.!?£\d]", lines[end]):
                break
            end += 1
        excerpt = "\n".join(lines[start:end])[:1800]
        if len(excerpt.split()) < 5:
            continue  # Bare table labels and headings are not narrative evidence.
        # High OCR confidence does not make malformed monetary tokens valid.
        # Retry from the source image; never silently replace O/I with digits.
        suspicious_currency = re.search(
            r"[£$€]\s*[\dOoIl.,]*[OoIl][\dOoIl.,]*(?:[mkb]\b|\b)", excerpt
        )
        if suspicious_currency or any(
            b.get("score", 1) < 0.94 and not b.get("fallback", {}).get("verified")
            for row in rows[start:end]
            for b in row["cells"]
        ):
            retries.add(page)
            continue
        signature = normalise(excerpt)
        if any(signature in prior for prior in seen_excerpts.get(topic, set())):
            continue
        contained = {prior for prior in seen_excerpts.get(topic, set()) if prior in signature}
        if not contained and len(seen_excerpts.get(topic, set())) >= 3:
            continue
        if contained:
            result["disclosures"] = [
                d
                for d in result["disclosures"]
                if d["topic"] != topic or normalise(d["excerpt"]) not in contained
            ]
            seen_excerpts[topic].difference_update(contained)
        result["disclosures"].append(
            {
                "topic": topic,
                "period": period,
                "page": page,
                "scope": "as stated in excerpt; may include group, company and related entities",
                "excerpt": excerpt,
                "basis": "Attributed filing disclosure; OCR source review required.",
            }
        )
        seen_excerpts.setdefault(topic, set()).add(signature)


def add_banking_note_evidence(bundle, groups, filings):
    from .validation import EvidenceItem

    groups["banking_note_observations"] = []
    groups["regulatory_metrics"] = []
    interpretations = {
        "capital_scope": "The filing discusses a solo-consolidation waiver. Review the quoted disclosure to establish whether it was granted and which regulatory reporting perimeter applies; mention of a waiver alone does not establish group-level reporting.",
        "liquidity_management": "Management describes internal liquidity management processes. This narrative does not provide a numeric LCR or NSFR or independently prove liquidity adequacy.",
        "related_party_transactions": "The quoted disclosure discusses transactions or transfers. Verify the related-party relationship, direction, terms and amounts; a transaction amount is not necessarily a closing exposure.",
        "group_funding": "The quoted disclosure discusses funding involving related entities. Verify the relationship, drawn balances, facility limits and repayment terms separately.",
        "securitisation": "The quoted disclosure discusses securitisation or asset-transfer risks. Determine whether risks are retained or transferred from the complete terms; do not infer derecognition from a legal sale alone.",
        "capital_support": "The quoted disclosure discusses capital support. Establish whether support is historical, proposed, conditional or committed; a mention does not establish an unconditional future commitment.",
        "credit_risk": "The filing describes forward-looking expected-credit-loss estimation. Impairment allowances depend on assumptions and scenarios; they are not guarantees against future losses.",
    }

    def add(claim, fact, url, excerpt, group):
        eid = f"ev_{len(bundle.evidence) + 1:03d}"
        bundle.evidence.append(
            EvidenceItem(
                evidence_id=eid,
                claim=claim,
                structured_fact={claim: fact},
                source={"url": url, "kind": filing.get("source_kind", "companies_house_api")},
                verbatim_excerpt=excerpt,
                confidence=0.8,
            )
        )
        groups.setdefault(group, []).append(eid)
        return eid

    for filing in filings:
        notes = filing.get("banking_notes")
        if not notes:
            continue
        bundle.uncertainty.extend(notes["gaps"])
        metrics = notes["metrics"]
        metric_ids = {}
        for m in metrics:
            url = filing["url"] + f"#page={m['page']}"
            source = add("banking_regulatory_metric", m, url, m["excerpt"], "financial_notes")
            groups["regulatory_metrics"].append(source)
            metric_ids[(m["metric"], m["scope"], m["period"])] = source
        for m in metrics:
            url = filing["url"] + f"#page={m['page']}"
            source = metric_ids[(m["metric"], m["scope"], m["period"])]
            if m["period"] != filing["period"]:
                continue
            value = Decimal(m["value"])
            display = (
                f"GBP {value * m['scale'] / 1000000:,.3f}m" if m["unit"] == "GBP" else f"{value:g}%"
            )
            basis = m["reporting_basis"]
            timing = (
                f"{m['averaging_window'] or 'unspecified-window'} average for the period ended"
                if basis == "average"
                else "at"
                if basis == "point_in_time"
                else "reported for the period ended"
            )
            text = f"{m['scope'].capitalize()}: published {NAMES[m['metric']]} ({timing} {m['period']}): {display}."
            if basis == "unspecified":
                text += " The source does not specify whether this is a period-end or averaged ratio, or its averaging window."
            inputs = [source]
            priors = [
                p
                for p in metrics
                if p["metric"] == m["metric"]
                and p["scope"] == m["scope"]
                and p["period"] < m["period"]
            ]
            if len(priors) == 1:
                p = priors[0]
                inputs.append(metric_ids[(p["metric"], p["scope"], p["period"])])
                prior_value = Decimal(p["value"])
                # Source row includes both published columns; retain explicit comparative context.
                comparable = (
                    basis == p["reporting_basis"]
                    and basis != "unspecified"
                    and m["averaging_window"] == p["averaging_window"]
                    and (basis != "average" or m["averaging_window"] is not None)
                )
                if not comparable:
                    text += " Comparative movement withheld because reporting bases or averaging windows are different or unclear."
                elif m["unit"] == "%":
                    text += f" Same-filing comparative {p['period']}: {prior_value:g}%; movement {value - prior_value:+g} percentage points."
                elif m["scale"] == p["scale"]:
                    text += f" Same-filing comparative {p['period']}: GBP {prior_value * p['scale'] / 1000000:,.3f}m."
            text += " No conclusion about regulatory headroom or compliance is inferred."
            eid = add(
                "financial_observation",
                {
                    "observation": text,
                    "inputs": inputs,
                    "period": m["period"],
                    "scope": m["scope"],
                    "metric": m["metric"],
                    "category": "banking_notes",
                },
                url,
                m["excerpt"],
                "financial_observations",
            )
            groups["banking_note_observations"].append(eid)
        for d in notes["disclosures"]:
            url = filing["url"] + f"#page={d['page']}"
            source = add("banking_note_disclosure", d, url, d["excerpt"], "financial_notes")
            eid = add(
                "financial_observation",
                {
                    "observation": interpretations[d["topic"]],
                    "inputs": [source],
                    "period": d["period"],
                    "category": "banking_notes",
                    "metric": d["topic"],
                },
                url,
                d["excerpt"],
                "financial_observations",
            )
            groups["banking_note_observations"].append(eid)

"""Conservative KM1 table interpretation with explicit issuer and column bindings."""

import re
from decimal import Decimal

from .reporting_dates import MONTH_NUMBERS, full_dates


def km1_metrics(pages, company_name, period):
    opening = " ".join(" ".join(p.split()) for p in pages[:5])
    name = re.escape(company_name)
    # A mention in a subsidiary list is insufficient: the introduction must name
    # the bank as a subject of these disclosures, with an explicit reporting date.
    dual = re.search(
        r"This document comprises .{1,150}? and "
        + name
        + r"[’']s\s*\([“\"]the Bank[”\"]\) Pillar 3 disclosures.{0,100}?as at ([^.]+)",
        opening,
        re.I,
    )
    single = re.search(
        r"This document provides the Pillar 3 disclosures.{0,150}?required of "
        + name
        + r",?\s*(?:trading as .{1,80}? )?as at ([^.]+)",
        opening,
        re.I,
    )
    binding = dual or single
    if not binding or full_dates(binding[1]) != [period]:
        return []
    metrics = []
    previous_km1 = False
    for page_number, page in enumerate(pages, 1):
        lines = [" ".join(line.split()) for line in page.splitlines() if line.strip()]
        flat = " ".join(lines)
        km1 = bool(re.search(r"\bKM1\b", flat))
        continuation = previous_km1 and bool(
            re.search(r"^17 Liquidity coverage ratio", page, re.M | re.I)
        )
        previous_km1 = km1
        if not (km1 or continuation):
            continue
        header = flat.split("Available own funds", 1)[0].split("Liquidity Coverage Ratio", 1)[0]
        dates = []
        scope = "bank (regulatory perimeter unspecified)"
        start, count = 0, 1
        if dual:
            # Two explicit column groups, repeated date sequences, and the
            # legal-name-to-Bank binding above are all required. Omit parent data.
            if not re.search(r"\bGroup Bank\b", header):
                continue
            for day, month, year in re.findall(r"(\d{1,2})-\s*([A-Za-z]{3})-(\d{2})\b", header):
                month_number = MONTH_NUMBERS.get(month.lower())
                if month_number:
                    dates.extend(full_dates(f"{day}/{month_number}/20{year}"))
            if not dates or len(dates) % 2:
                continue
            count = len(dates)
            start = count // 2
            if (
                dates[:start] != dates[start:]
                or dates[start] != period
                or len(set(dates[:start])) != start
            ):
                continue
            scope = "company"
        else:
            # A sole year column is bound only by the explicit as-at statement.
            years = re.findall(r"^20\d{2}$", "\n".join(lines), re.M)
            if years != [period[:4]]:
                continue
            dates = [period]
        patterns = {
            "cet1": r"5 Common Equity Tier 1(?: \(CET1\))? ratio",
            "total_capital_ratio": r"7 Total capital ratio",
            "lcr": r"17 Liquidity coverage ratio",
            "nsfr": r"20 (?:NSFR|Net stable funding) ratio",
        }
        for line in lines:
            for metric, pattern in patterns.items():
                row = re.fullmatch(pattern + r" \(%\)\s+(.+)", line, re.I)
                if not row or not re.fullmatch(
                    r"\d+(?:\.\d+)?\s*%?(?:\s+\d+(?:\.\d+)?\s*%?)*", row[1]
                ):
                    continue
                values = re.findall(r"\d+(?:\.\d+)?", row[1])
                if len(values) != count:
                    continue
                basis, window = "point_in_time", None
                if metric in {"lcr", "nsfr"}:
                    basis = "unspecified"
                    title = (
                        "Liquidity Coverage Ratio"
                        if metric == "lcr"
                        else "Net Stable Funding Ratio"
                    )
                    foot = re.search(title + r"\s+([¹²³⁴⁵⁶⁷⁸⁹])", flat, re.I)
                    if foot:
                        number = str("¹²³⁴⁵⁶⁷⁸⁹".index(foot[1]) + 1)
                        note = re.search(
                            r"\b"
                            + number
                            + r" Figures presented are for the (12 month|4 quarter) average to the period end\.",
                            flat,
                            re.I,
                        )
                        if note:
                            basis, window = "average", note[1].lower().replace(" ", "-")
                for value, date in zip(values[start:], dates[start:]):
                    if not 0 <= Decimal(value) <= (10000 if metric in {"lcr", "nsfr"} else 100):
                        continue
                    metrics.append(
                        {
                            "metric": metric,
                            "scope": scope,
                            "period": date,
                            "value": value,
                            "scale": 1,
                            "unit": "%",
                            "page": page_number,
                            "reporting_basis": basis,
                            "averaging_window": window,
                            "header_excerpt": header,
                            "excerpt": line,
                            "scope_excerpt": binding[0],
                            "basis": "Issuer-published KM1 table; literal PDF text. "
                            + ("Averaging basis not specified. " if basis == "unspecified" else "")
                            + "No independent compliance assessment.",
                        }
                    )
    grouped = {}
    for metric in metrics:
        grouped.setdefault((metric["metric"], metric["scope"], metric["period"]), []).append(metric)
    return [
        rows[0]
        for rows in grouped.values()
        if len({(r["value"], r["reporting_basis"], r["averaging_window"]) for r in rows}) == 1
    ]

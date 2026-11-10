"""Explicit shared GBP units with two/four dated columns and labelled scope bands."""

import re

from .scoped_accounts import clean, right


def shared_column_headers(boxes, heading_y):
    headers = [b for b in boxes if heading_y + 20 < b["box"][0][1] < heading_y + 210]
    units = [
        b
        for b in headers
        if re.fullmatch(r"(?:£|GBP)['’]?(?:000|m)", b["text"].replace(" ", ""), re.I)
    ]
    if len(units) != 1:
        return []
    unit = units[0]
    years = []
    for b in headers:
        if abs(b["box"][0][1] - unit["box"][0][1]) > 22 or b["score"] < 0.96:
            continue
        match = re.fullmatch(r"(?:(\d{1,2})\s+(\w+)\s+)?(20\d{2})\*?", b["text"].strip())
        if match:
            years.append((b, match))
    years.sort(key=lambda pair: right(pair[0]))
    if len(years) not in (2, 4):
        return []
    shared_dates = re.findall(
        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b",
        " ".join(b["text"] for b in headers),
        re.I,
    )
    dates = {(int(d), m.lower()) for d, m in shared_dates}
    if len(dates) != 1:
        return []
    scopes = sorted(
        [b for b in headers if clean(b["text"]) in ("group", "company") and b["score"] >= 0.96],
        key=right,
    )
    if len(scopes) > 2 or (
        len(scopes) == 2
        and (len(years) != 4 or {clean(b["text"]) for b in scopes} != {"group", "company"})
    ):
        return []
    if len(years) == 4 and len(scopes) != 2:
        return []
    if len(scopes) == 2:
        for i, s in enumerate(scopes):
            centre = (s["box"][0][0] + right(s)) / 2
            pair = years[2 * i : 2 * i + 2]
            if not pair[0][0]["box"][0][0] - 50 < centre < right(pair[1][0]) + 50:
                return []
    spacing = min(right(years[i + 1][0]) - right(years[i][0]) for i in range(len(years) - 1))
    if spacing < 50:
        return []
    return [
        {
            "x": right(b),
            "year": m.group(3),
            "day_month": next(iter(dates)),
            "scope": clean(scopes[i // 2]["text"])
            if len(scopes) == 2
            else clean(scopes[0]["text"])
            if scopes
            else None,
            "scale": 1000000 if unit["text"].lower().endswith("m") else 1000,
            "y": max(max(p[1] for p in b["box"]), max(p[1] for p in unit["box"])),
            "tolerance": min(28, spacing * 0.35),
        }
        for i, (b, m) in enumerate(years)
    ]

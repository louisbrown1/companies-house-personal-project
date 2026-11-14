"""Geometry-based document navigation. Locations are discovered, never issuer-specific."""

import re

from .accounts import normalise, rows_from_boxes

STATEMENT_TITLE = re.compile(
    r"^(?:(?:company|group|consolidated)(?: and company)? )?(?:income statements?|statements? of (?:total comprehensive income|comprehensive income|profit or loss(?: and other comprehensive income)?|financial position|cash flows|changes in equity)|balance sheets?|cash flow statements?|profit and loss accounts?)$"
)
NOTES_TITLE = re.compile(
    r"^notes to (?:the )?(?:(?:company|group|consolidated) )?(?:financial statements|accounts)|^notes to risk and capital",
    re.I,
)
FINANCIAL_SECTION = re.compile(r"^(?:(?:company|group|consolidated) )?financial statements$")
MAX_DISCOVERY_PAGES = 40
MAX_FALLBACK_HEADERS = 200


def left(b):
    return min(p[0] for p in b["box"])


def right(b):
    return max(p[0] for p in b["box"])


def top(b):
    return min(p[1] for p in b["box"])


def height(b):
    return max(p[1] for p in b["box"]) - top(b)


def title_kind(text):
    title = normalise(text)
    if re.fullmatch(STATEMENT_TITLE.pattern.replace(" ", r"\s*"), title):
        return "statement"
    if re.search(NOTES_TITLE.pattern.replace(" ", r"\s*"), title, re.I):
        return "notes"
    if re.fullmatch(FINANCIAL_SECTION.pattern.replace(" ", r"\s*"), title):
        return "financial_section"
    return None


def contents_entries(document):
    """Pair aligned page-number cells with nearby titles, independent of heading spelling."""
    found = []
    for page in document["pages"]:
        if page["page"] > 12:
            continue
        rows = rows_from_boxes(page["boxes"])
        entries = []
        numbered_rows = [
            r["cells"]
            for r in rows
            if any(re.fullmatch(r"\d{1,3}", b["text"].strip()) for b in r["cells"])
            and any(re.search(r"[A-Za-z]{3}", b["text"]) for b in r["cells"])
        ]
        after = sum(bool(re.fullmatch(r"\d{1,3}", r[-1]["text"].strip())) for r in numbered_rows)
        before = sum(bool(re.fullmatch(r"\d{1,3}", r[0]["text"].strip())) for r in numbered_rows)
        numbers_after = after >= before
        for ri, row in enumerate(rows):
            cells = row["cells"]
            for number in cells:
                token = number["text"].strip(" .")
                if (
                    not re.fullmatch(r"\d{1,3}", token)
                    or number.get("score", 1) < 0.85
                    or not 1 <= int(token) <= document["page_count"]
                ):
                    continue
                labels = [
                    b for b in cells if b is not number and re.search(r"[A-Za-z]{3}", b["text"])
                ]
                if not labels:
                    continue

                def distance(b):
                    return max(left(b) - right(number), left(number) - right(b), 0)

                directional = [
                    b
                    for b in labels
                    if (right(b) <= left(number) if numbers_after else left(b) >= right(number))
                ]
                if not directional:
                    continue
                label = min(directional, key=distance)
                # Stop at another numeric column, rather than joining both sides of a contents spread.
                title = label["text"].strip(" .")
                if ri and not title_kind(title):
                    previous = rows[ri - 1]
                    above = [
                        b
                        for b in previous["cells"]
                        if abs(left(b) - left(label)) < 35 and re.search(r"[A-Za-z]{3}", b["text"])
                    ]
                    if len(above) == 1 and row["y"] - previous["y"] < max(45, height(label) * 2.4):
                        joined = above[0]["text"] + " " + title
                        if title_kind(joined) and re.search(
                            r"\b(?:of|the|financial|company|consolidated)$",
                            normalise(above[0]["text"]),
                        ):
                            title = joined
                entries.append(
                    {
                        "title": title,
                        "printed_page": int(token),
                        "contents_page": page["page"],
                        "kind": title_kind(title),
                    }
                )
        # A single isolated number beside prose is not a contents table.
        identified = any(
            re.fullmatch(r"(?:table of )?contents|in this report|index", normalise(b["text"]))
            for b in page["boxes"]
        )
        if identified or len(entries) >= 4:
            found.extend(entries)
    return list({(e["title"], e["printed_page"]): e for e in found}.values())


def page_number_anchors(pages):
    anchors = []
    for page in pages:
        boxes = page["boxes"]
        if not boxes:
            continue
        ymin = min(top(b) for b in boxes)
        ymax = max(top(b) + height(b) for b in boxes)
        for b in boxes:
            token = b["text"].strip()
            if not re.fullmatch(r"\d{1,3}", token) or b.get("score", 1) < 0.9:
                continue
            printed = int(token)
            offset = page["page"] - printed
            edge = (
                top(b) < ymin + max(30, (ymax - ymin) * 0.08) or top(b) > ymin + (ymax - ymin) * 0.9
            )
            if edge and -4 <= offset <= 20 and printed > 0:
                anchors.append(
                    {"pdf_page": page["page"], "printed_page": printed, "offset": offset}
                )
    return anchors


def infer_offset(pages):
    anchors = page_number_anchors(pages)
    votes = {}
    for a in anchors:
        votes.setdefault(a["offset"], set()).add(a["pdf_page"])
    ranked = sorted(((len(v), k) for k, v in votes.items()), reverse=True)
    if ranked and ranked[0][0] >= 2 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return ranked[0][1]
    return 0


def page_headings(boxes):
    """Exact heading evidence, including headings split over adjacent OCR rows."""
    found = []
    ordered = sorted(boxes, key=lambda b: (top(b), left(b)))
    for i, b in enumerate(ordered):
        if b.get("score", 1) < 0.85:
            continue
        text = b["text"]
        kind = title_kind(text)
        if kind:
            found.append({"title": text, "kind": kind, "box": b["box"]})
        if i + 1 < len(ordered):
            other = ordered[i + 1]
            if abs(left(b) - left(other)) < 45 and 0 <= top(other) - top(b) < max(
                height(b) * 2.5, 45
            ):
                joined = text + " " + other["text"]
                kind = title_kind(joined)
                if kind:
                    found.append({"title": joined, "kind": kind, "box": b["box"]})
    return found


def heading_matches(expected, actual):
    a, b = normalise(expected), normalise(actual)
    if a.replace(" ", "") == b.replace(" ", ""):
        return True

    def scope(s):
        return (
            "group"
            if re.search(r"\b(?:group|consolidated)\b", s)
            else "company"
            if re.search(r"\bcompany\b", s)
            else None
        )

    def kind(s):
        if "cash flow" in s:
            return "cash"
        if "changes in equity" in s:
            return "equity"
        if "balance sheet" in s or "financial position" in s:
            return "position"
        if "income" in s or "profit and loss" in s:
            return "income"
        return None

    return bool(kind(a)) and kind(a) == kind(b) and (not scope(a) or scope(a) == scope(b))


def discover(pages, read, page_count):
    """read(index, headers_only=False) is supplied by the bounded PDF reader."""
    opening = min(8, page_count)
    for index in range(opening):
        read(index)

    def snapshot():
        return [{"page": i + 1, "boxes": b} for i, b in sorted(pages.items())]

    entries = contents_entries({"pages": snapshot(), "page_count": page_count})
    offset = infer_offset(snapshot())
    targets = set()
    verified = []
    rejected = []
    relevant = [e for e in entries if e["kind"] in ("statement", "financial_section")]
    # Prefer specific statements over broad chapter starts, then verify destination headings.
    relevant.sort(key=lambda e: (e["kind"] != "statement", e["printed_page"]))
    for entry in relevant:
        if len(targets) >= MAX_DISCOVERY_PAGES:
            break
        predicted = entry["printed_page"] + offset
        candidates = [
            p
            for p in (predicted, predicted - 1, predicted + 1, predicted - 2, predicted + 2)
            if 1 <= p <= page_count
        ]
        matched = None
        for p in candidates:
            if len(set(pages) - set(range(opening))) >= MAX_DISCOVERY_PAGES and p - 1 not in pages:
                break
            boxes = read(p - 1)
            headings = page_headings(boxes)
            wanted = normalise(entry["title"])
            hits = [h for h in headings if heading_matches(wanted, h["title"])]
            if hits:
                matched = p
                verified.append(
                    {**entry, "pdf_page": p, "offset": p - entry["printed_page"], "verified": True}
                )
                break
        if matched:
            if entry["kind"] == "statement":
                targets.add(matched - 1)
            else:
                # Chapter starts are evidence of a section, not proof of a numeric table.
                for p in range(matched, min(page_count, matched + 6) + 1):
                    if (
                        len(set(pages) - set(range(opening))) >= MAX_DISCOVERY_PAGES
                        and p - 1 not in pages
                    ):
                        break
                    read(p - 1)
                    targets.add(p - 1)
        else:
            rejected.append(entry)
    fallback = []
    if not targets:
        # Bounded heading scan can find statements even without a parseable contents page.
        for index in range(opening, min(page_count, opening + MAX_FALLBACK_HEADERS)):
            boxes = read(index, headers_only=True)
            fallback.append(index + 1)
            if any(h["kind"] == "statement" for h in page_headings(boxes)):
                read(index)
                targets.add(index)
                if len(targets) >= 12:
                    break
    # Small accounts and legacy broad-chapter discovery remain bounded candidates.
    if not targets and page_count <= 16:
        for index in range(page_count):
            read(index)
            targets.add(index)
    return {
        "targets": targets,
        "sections": verified,
        "contents": entries,
        "printed_page_offset": offset,
        "page_number_anchors": page_number_anchors(snapshot()),
        "rejected_sections": rejected,
        "fallback_header_pages": fallback,
        "fallback_truncated": bool(fallback) and fallback[-1] < page_count,
    }

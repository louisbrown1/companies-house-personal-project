"""Build a bounded, evidence-only map of actual PDF headings and page ranges."""

import hashlib
import json
import re

from . import accounts

VERSION = "navigation-v7"
MAX_INDEX_PAGES = 200
MAX_RETRIEVAL_PAGES = 60


def cached_pages(document):
    pages = {p["page"]: p for p in document["pages"]}
    for suffix in (
        "-notes-v1.json",
        "-notes-v3.json",
        "-notes-v4.json",
        "-bank-notes-v1.json",
        "-bank-notes-v2.json",
        "-bank-notes-v3.json",
    ):
        path = accounts.CACHE / (document["sha256"] + suffix)
        if path.exists():
            for page in json.loads(path.read_text(encoding="utf-8"))["pages"]:
                pages.setdefault(page["page"], page)
    for path in accounts.CACHE.glob(document["sha256"] + "-selected-*-v1.json"):
        page = json.loads(path.read_text(encoding="utf-8"))
        pages.setdefault(page["page"], page)
    return pages


def contents_entries(document):
    from .document_discovery import contents_entries as discover_contents

    return document.get("discovery", {}).get("contents") or discover_contents(document)


def infer_offset(pages):
    from .document_discovery import infer_offset as discover_offset

    return discover_offset(list(pages.values()))


def note_ranges(entries, page_count, offset):
    starts = [
        e
        for e in entries
        if re.search(
            r"notes to (?:the )?(?:(?:company|group|consolidated) )?(?:financial statements|accounts)|notes to risk and capital|chief financial officer|financial review|capital (?:adequacy|management)|liquidity|risk appetite",
            e["title"],
            re.I,
        )
    ]
    ranges = []
    for entry in starts:
        start = entry["printed_page"] + offset
        later = [
            e["printed_page"] + offset for e in entries if e["printed_page"] > entry["printed_page"]
        ]
        end = min(later, default=page_count + 1) - 1
        if start <= end:
            ranges.append((max(1, start - 1), min(end + 1, page_count)))
    return ranges


def read_pages(path, document, page_numbers, *, headers_only=False):
    """Headers for navigation are never passed to extraction as complete pages."""
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    requested = sorted(set(page_numbers))
    limit = MAX_INDEX_PAGES if headers_only else MAX_RETRIEVAL_PAGES
    if len(requested) > limit or any(n < 1 or n > document["page_count"] for n in requested):
        raise ValueError("Page request exceeds navigation/retrieval budget")
    existing = cached_pages(document)
    result = []
    with accounts.OCR_LOCK:
        with pdfium.PdfDocument(path) as pdf:
            for n in requested:
                if n in existing:
                    result.append(existing[n])
                    continue
                cache = (
                    accounts.CACHE
                    / f"{document['sha256']}-{'heading' if headers_only else 'selected'}-{n}-{'v2' if headers_only else 'v1'}.json"
                )
                if cache.exists():
                    result.append(json.loads(cache.read_text(encoding="utf-8")))
                    continue
                page = pdf[n - 1]
                try:
                    if accounts._engine is None:
                        accounts._engine = RapidOCR(
                            params={
                                "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                            }
                        )
                    bitmap = page.render(scale=2)
                    try:
                        picture = bitmap.to_pil()
                        if headers_only:
                            picture = picture.crop(
                                (
                                    0,
                                    0,
                                    picture.width,
                                    min(max(620, int(picture.height * 0.60)), picture.height),
                                )
                            )
                        ocr = accounts._engine(picture)
                    finally:
                        bitmap.close()
                    item = {
                        "page": n,
                        "boxes": [
                            {"box": b.tolist(), "text": t, "score": float(s)}
                            for b, t, s in zip(ocr.boxes, ocr.txts, ocr.scores)
                        ]
                        if ocr.txts
                        else [],
                    }
                finally:
                    page.close()
                cache.write_text(json.dumps(item), encoding="utf-8")
                result.append(item)
    return result


def heading_candidates(page):
    result = []
    boxes = list(page["boxes"])
    # Some layouts put the note number in a separate OCR box from its heading.
    for row in accounts.rows_from_boxes(page["boxes"]):
        cells = row["cells"]
        if (
            len(cells) == 2
            and re.fullmatch(r"\d{1,2}\.?", cells[0]["text"])
            and re.match(r"[A-Za-z]", cells[1]["text"])
        ):
            boxes.append({**cells[1], "text": cells[0]["text"] + " " + cells[1]["text"]})
    for b in boxes:
        text = re.sub(r"^Note\s+(\d+)[.:]?\s*", r"\1 ", " ".join(b["text"].split()), flags=re.I)
        if not 100 < b["box"][0][1] < 1500 or b["score"] < 0.90 or not 5 < len(text) < 125:
            continue
        if re.match(
            r"^\d{1,2}\s+(?:Jan\w*|Feb\w*|Mar\w*|Apr\w*|May|Jun\w*|Jul\w*|Aug\w*|Sep\w*|Oct\w*|Nov\w*|Dec\w*|weeks?|months?|years?|to)\b",
            text,
            re.I,
        ):
            continue
        numbered = re.match(r"^\d{1,2}[.\s]+[A-Za-z]", text)
        thematic = (
            len(text) < 65
            and not re.search(r"[,.;]", text)
            and re.match(
                r"^(?:capital(?:$| (?:adequacy|management)\b)|liquidity(?:$| (?:risk|coverage)\b)|funding (?:risk|strategy)|related parties|leases|commitments|contingent liabilities|securiti[sz]ation|net stable funding)",
                text,
                re.I,
            )
        )
        if numbered or thematic:
            result.append({"title": text, "page": page["page"]})
    return result


def build_index(path, document):
    cache = accounts.CACHE / f"{document['sha256']}-{VERSION}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    pages = cached_pages(document)
    entries = contents_entries(document)
    offset = document.get("discovery", {}).get("printed_page_offset", infer_offset(pages))
    ranges = note_ranges(entries, document["page_count"], offset)
    wanted = sorted({p for start, end in ranges for p in range(start, end + 1)})
    scanned = wanted[:MAX_INDEX_PAGES]
    headings = []
    for page in read_pages(path, document, scanned, headers_only=True):
        headings.extend(heading_candidates(page))
    # Repeated continuation headings belong to one section, not fresh discoveries.
    unique = {}
    for heading in headings:
        key = accounts.normalise(re.sub(r"\(?continued\)?", "", heading["title"], flags=re.I))
        unique.setdefault(key, heading)
    headings = sorted(unique.values(), key=lambda h: (h["page"], h["title"]))
    sections = []
    for i, heading in enumerate(headings):
        following = [h["page"] for h in headings if h["page"] > heading["page"]]
        range_end = max(
            (end for start, end in ranges if start <= heading["page"] <= end),
            default=heading["page"],
        )
        end = min(min(following, default=range_end + 1) - 1, range_end)
        slug = re.sub(
            r"[^a-z0-9]+", "_", re.sub(r"^\d+[.\s]+", "", heading["title"].lower())
        ).strip("_")[:65]
        sections.append(
            {
                "section_id": f"p{heading['page']}_{slug}",
                "title": heading["title"],
                "start_page": heading["page"],
                "end_page": max(heading["page"], end),
                "source": "observed_pdf_heading",
            }
        )
    result = {
        "pdf_sha256": document["sha256"],
        "page_count": document["page_count"],
        "sections": sections,
        "index_pages": scanned,
        "index_truncated": len(wanted) > len(scanned),
        "printed_page_offset": offset,
        "statement_pages": document.get("statement_pages", []),
    }
    result["sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    cache.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result

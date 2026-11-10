"""Bounded Companies House PDF ingestion and conservative financial extraction.

Only recognisable standalone-company, two-column statements are calculated.
Unsupported layouts produce evidence gaps, never model-invented figures.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

CACHE = Path(__file__).resolve().parents[2] / ".data" / "accounts"
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 600  # Document length; OCR remains bounded to discovered sections.
OCR_LOCK = threading.Lock()  # PDFium and the shared OCR engine are not thread-safe.
_engine = None
logger = logging.getLogger(__name__)
STATEMENT = re.compile(
    r"(statement (?:of|af) (?:total comprehensive income|comprehensive income|financial position|cash\s*flows)|balance sheet|profit and loss account)",
    re.I,
)
LABELS = {
    "revenue": "revenue",
    "turnover": "revenue",
    "operating profit": "operating_profit",
    "operating loss": "operating_loss",
    "profit before tax": "profit_before_tax",
    "profit for the period": "profit_after_tax",
    "profit for the year": "profit_after_tax",
    "cash and cash equivalents": "cash",
    "total assets": "total_assets",
    "total liabilities": "total_liabilities",
    "net cash flows from operating activities": "operating_cash_flow",
    "net cash generated from operating activities": "operating_cash_flow",
}
CORE_METRICS = set(LABELS.values()) - {"operating_loss"}
LABELS.update(
    {
        "cost of sales": "cost_of_sales",
        "gross profit": "gross_profit",
        "administrative expenses": "administrative_expenses",
        "finance costs in relation to right of use assets": "lease_interest_expense",
        "repayment of the principal in relation to lease liabilities": "lease_principal_paid",
        "payment of interest in relation to lease liabilities": "lease_interest_paid",
        "dividends paid": "dividends_paid",
        "net funds received in respect of intragroup investing activities": "intragroup_investing_cash_flow",
        "net funds received paid in respect of intragroup investing activities": "intragroup_investing_cash_flow",
        "net funds paid received in respect of intragroup financing": "intragroup_financing_cash_flow",
        "net funds received paid in respect of intragroup financing activities": "intragroup_financing_cash_flow",
    }
)


def latest_periods(items):
    """Latest filing for each distinct made-up date, ordered by reporting date."""
    periods = {}
    for item in items:
        period = item.get("description_values", {}).get("made_up_date", "")
        try:
            date.fromisoformat(period)
        except (ValueError, TypeError):
            continue
        if item.get("category") != "accounts" or not item.get("links", {}).get("document_metadata"):
            continue
        if period not in periods or item.get("date", "") > periods[period].get("date", ""):
            periods[period] = item
    return [periods[p] for p in sorted(periods, reverse=True)[:2]]


def safe_document_url(url, redirect=False):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    valid = host == "document-api.company-information.service.gov.uk"
    if redirect:
        valid = (
            valid
            or host == "s3.eu-west-2.amazonaws.com"
            or host.endswith(".s3.amazonaws.com")
            or host.endswith(".s3.eu-west-2.amazonaws.com")
        )
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not valid
    ):
        raise ValueError("Untrusted document URL")
    if not redirect and not re.fullmatch(r"/document/[A-Za-z0-9_-]+", parsed.path):
        raise ValueError("Invalid document path")
    return url


async def download_document(url):
    url = safe_document_url(url)
    key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not key:
        raise ValueError("Companies House key not configured")
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".pdf")
    if target.exists() and 0 < target.stat().st_size <= MAX_BYTES:
        return target
    # Never put Basic auth on the client: signed storage redirects must not receive it.
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        metadata = await client.get(url, auth=(key, ""))
        metadata.raise_for_status()
        if "application/pdf" not in metadata.json().get("resources", {}):
            raise ValueError("PDF unavailable")
        request_url, auth = url + "/content", (key, "")
        for _ in range(3):
            async with client.stream(
                "GET", request_url, auth=auth, headers={"Accept": "application/pdf"}
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    request_url = safe_document_url(response.headers["location"], redirect=True)
                    auth = None
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Accounts PDF exceeds size limit")
                if not data.startswith(b"%PDF"):
                    raise ValueError("Invalid accounts PDF")
                # Atomic replacement prevents another request reading a partial download.
                import tempfile

                with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as tmp:
                    tmp.write(data)
                    temporary = Path(tmp.name)
                temporary.replace(target)
                return target
    raise ValueError("Too many document redirects")


def rows_from_boxes(boxes):
    """Retain row alignment; plain OCR token order loses the table columns."""
    rows = []
    for box in sorted(boxes, key=lambda b: sum(p[1] for p in b["box"]) / 4):
        y = sum(p[1] for p in box["box"]) / 4
        height = max(p[1] for p in box["box"]) - min(p[1] for p in box["box"])
        if not rows or abs(rows[-1]["y"] - y) > max(7, height * 0.55):
            rows.append({"y": y, "cells": []})
        rows[-1]["cells"].append(box)
    for row in rows:
        row["cells"].sort(key=lambda b: b["box"][0][0])
    return rows


def read_statement_pages(path, include_notes=False, high_resolution=False, banking_notes=False):
    """OCR a contents page and its statement pages; no whole-report model prompt."""
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    global _engine
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache = CACHE / (
        digest
        + (
            "-bank-notes-v3.json"
            if banking_notes
            else "-notes-v4.json"
            if include_notes
            else "-ocr-hi-v4.json"
            if high_resolution
            else "-ocr-v5.json"
        )
    )
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    with OCR_LOCK:
        if _engine is None:
            _engine = RapidOCR(
                params={
                    "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                }
            )
        with pdfium.PdfDocument(path) as doc:
            if len(doc) > MAX_PAGES:
                raise ValueError("Accounts PDF exceeds page limit")
            seed = CACHE / (digest + "-ocr-v1.json")
            if (CACHE / (digest + "-ocr-v2.json")).exists():
                seed = CACHE / (digest + "-ocr-v2.json")
            if banking_notes and (CACHE / (digest + "-notes-v3.json")).exists():
                seed = CACHE / (digest + "-notes-v3.json")
            pages = (
                {
                    p["page"] - 1: p["boxes"]
                    for p in json.loads(seed.read_text(encoding="utf-8"))["pages"]
                }
                if seed.exists()
                else {}
            )
            # Reuse prior work across discovery versions, including notes pages.
            for suffix in (
                "-ocr-v3.json",
                "-ocr-v4.json",
                "-notes-v1.json",
                "-notes-v3.json",
                "-bank-notes-v1.json",
                "-bank-notes-v2.json",
            ):
                previous = CACHE / (digest + suffix)
                if previous.exists():
                    for p in json.loads(previous.read_text(encoding="utf-8"))["pages"]:
                        pages.setdefault(p["page"] - 1, p["boxes"])

            located = None
            baseline = CACHE / (digest + "-ocr-v5.json")
            if high_resolution and baseline.exists() and not (include_notes or banking_notes):
                candidate = json.loads(baseline.read_text(encoding="utf-8"))
                if (
                    candidate.get("sha256") == digest
                    and candidate.get("page_count") == len(doc)
                    and candidate.get("statement_pages")
                    and candidate.get("discovery")
                    and all(
                        isinstance(n, int) and 1 <= n <= len(doc)
                        for n in candidate["statement_pages"]
                    )
                ):
                    located = candidate
                    pages.update({p["page"] - 1: p["boxes"] for p in candidate["pages"]})

            def read(index, headers_only=False):
                if index not in pages:
                    page = doc[index]
                    try:
                        textpage = page.get_textpage()
                        try:
                            if not high_resolution and len(textpage.get_text_range().strip()) > 100:
                                native = []
                                for r in range(textpage.count_rects()):
                                    left, bottom, right, top = textpage.get_rect(r)
                                    text = textpage.get_text_bounded(
                                        left, bottom, right, top
                                    ).strip()
                                    if text:
                                        y1, y2 = (
                                            (page.get_height() - top) * 2,
                                            (page.get_height() - bottom) * 2,
                                        )
                                        native.append(
                                            {
                                                "text": text,
                                                "score": 1.0,
                                                "method": "PDF text layer",
                                                "box": [
                                                    [left * 2, y1],
                                                    [right * 2, y1],
                                                    [right * 2, y2],
                                                    [left * 2, y2],
                                                ],
                                            }
                                        )
                                if native:
                                    if not headers_only:
                                        pages[index] = native
                                    return native
                        finally:
                            textpage.close()
                        render_scale = 3 if high_resolution and index >= 6 else 2
                        bitmap = page.render(scale=render_scale)
                        try:
                            picture = bitmap.to_pil()
                            if headers_only:
                                picture = picture.crop(
                                    (0, 0, picture.width, int(picture.height * 0.72))
                                )
                            result = _engine(picture)
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                    extracted = (
                        [
                            {"box": (b * (2 / render_scale)).tolist(), "text": t, "score": float(s)}
                            for b, t, s in zip(result.boxes, result.txts, result.scores)
                        ]
                        if result.txts
                        else []
                    )
                    if headers_only:
                        return extracted
                    pages[index] = extracted
                return pages[index]

            from .document_discovery import discover

            if located is not None:
                discovery = {
                    **located["discovery"],
                    "targets": {n - 1 for n in located["statement_pages"]},
                    "sections": located["sections"],
                    "reused_for_high_resolution": True,
                }
            else:
                discovery = discover(pages, read, len(doc))
            targets = discovery["targets"]
            sections = discovery["sections"]
            if high_resolution:
                for index in sorted(targets):
                    if index >= 6:
                        pages.pop(index, None)
                        read(index)
            # Bounded company-information search, including statutory end matter.
            identity_pages = set(range(max(0, len(doc) - 3), len(doc)))
            for entry in discovery["contents"]:
                if re.search(
                    r"company information|directors and advisers|secretary and advisers|registered office",
                    entry["title"],
                    re.I,
                ):
                    n = entry["printed_page"] + discovery["printed_page_offset"] - 1
                    if 0 <= n < len(doc):
                        identity_pages.add(n)
            identity_pages = set(sorted(identity_pages)[:6])
            for index in sorted(identity_pages):
                read(index)
            notes_start = None
            risk_notes_start = None
            if include_notes:
                for index in range(min(6, len(doc))):
                    for row in rows_from_boxes(read(index)):
                        cells = row["cells"]
                        page_number = re.fullmatch(r"[.\s]*(\d{1,3})", cells[-1]["text"])
                        if (
                            re.search(
                                r"notes to (?:the )?financial statements",
                                " ".join(b["text"] for b in cells),
                                re.I,
                            )
                            and page_number
                        ):
                            notes_start = int(page_number.group(1))
                        if (
                            banking_notes
                            and re.search(
                                r"notes to risk and capital management",
                                " ".join(b["text"] for b in cells),
                                re.I,
                            )
                            and page_number
                        ):
                            risk_notes_start = int(page_number.group(1))
                if notes_start and 1 <= notes_start <= len(doc):
                    width = (
                        6
                        if len(doc) > 200
                        else 43
                        if risk_notes_start
                        else 88
                        if banking_notes
                        else 47
                    )
                    for index in range(max(0, notes_start - 2), min(len(doc), notes_start + width)):
                        read(index)
                if risk_notes_start and 1 <= risk_notes_start <= len(doc):
                    for index in range(
                        max(0, risk_notes_start - 2),
                        min(len(doc), risk_notes_start + (6 if len(doc) > 200 else 43)),
                    ):
                        read(index)
            result = {
                "sha256": digest,
                "page_count": len(doc),
                "statement_pages": [i + 1 for i in sorted(targets)],
                "sections": sections,
                "discovery": {k: v for k, v in discovery.items() if k != "targets"},
                "identity_pages": [i + 1 for i in sorted(identity_pages)],
                "discovery_status": "located" if targets else "no_statement_sections_located",
                "notes_start": notes_start,
                "notes_scan_limit": (16 if banking_notes and risk_notes_start else 8)
                if include_notes and len(doc) > 200
                else (90 if banking_notes else 49)
                if include_notes
                else 0,
                "risk_notes_start": risk_notes_start,
                "pages": [{"page": i + 1, "boxes": boxes} for i, boxes in sorted(pages.items())],
            }
    CACHE.mkdir(parents=True, exist_ok=True)
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=CACHE, delete=False
    ) as temporary:
        json.dump(result, temporary)
        temporary_path = Path(temporary.name)
    temporary_path.replace(cache)
    return result


def normalise(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("'", "").replace("’", "")).strip()


def refine_note_pages(path, document, page_numbers):
    """Retry at most six uncertain ratio or narrative pages, retaining source geometry."""
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    global _engine
    pages = {p["page"]: p for p in document["pages"]}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with OCR_LOCK:
        if _engine is None:
            _engine = RapidOCR(
                params={
                    "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                }
            )
        with pdfium.PdfDocument(path) as doc:
            for n in sorted(set(page_numbers))[:6]:
                if not 1 <= n <= len(doc):
                    continue
                target = CACHE / f"{digest}-note-page-{n}-hi-v4.json"
                if target.exists():
                    pages[n] = json.loads(target.read_text(encoding="utf-8"))
                    continue
                page = doc[n - 1]
                try:
                    bitmap = page.render(scale=3)
                    try:
                        result = _engine(bitmap.to_pil(), use_det=True, use_cls=True, use_rec=True)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                pages[n] = {
                    "page": n,
                    "boxes": [
                        {"box": (b * 2 / 3).tolist(), "text": t, "score": float(s)}
                        for b, t, s in zip(result.boxes, result.txts, result.scores)
                    ]
                    if result.txts
                    else [],
                }
                from .ocr_fallback import refine_crops

                page = doc[n - 1]
                try:
                    bitmap = page.render(scale=4)
                    try:
                        pages[n] = refine_crops(pages[n], bitmap.to_pil(), _engine)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                import tempfile

                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=CACHE, delete=False
                ) as temporary:
                    json.dump(pages[n], temporary)
                    temp_path = Path(temporary.name)
                temp_path.replace(target)
    return {**document, "pages": [pages[n] for n in sorted(pages)]}


def number(text):
    value = text.strip().replace(",", "").replace(" ", "")
    if value in ("-", "–", "—"):
        return Decimal(0)
    if not re.fullmatch(r"(?:-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\))", value):
        raise ValueError("Unrecognised financial value")
    return Decimal("-" + value[1:-1] if value.startswith("(") else value)


def extract_figures(document, period, company_name, column=0):
    figures, gaps, conflicts = {}, [], set()
    for page in document["pages"]:
        boxes = page["boxes"]
        header = [b for b in boxes if b["box"][0][1] < 240]
        text = " ".join(b["text"] for b in header)
        if not STATEMENT.search(text) or "contents" in text.lower():
            continue
        # Never collapse group and company columns or infer entity from a brand name.
        if re.search(r"\b(group|consolidated)\b", text, re.I) or normalise(
            company_name
        ) not in normalise(text):
            gaps.append(
                f"Page {page['page']}: entity scope unsupported or ambiguous; figures withheld."
            )
            continue
        rows = rows_from_boxes(boxes)
        year_rows = [
            r
            for r in rows
            if r["y"] < 500 and any(b["text"].strip() == period[:4] for b in r["cells"])
        ]
        if not year_rows:
            continue
        yr = year_rows[0]
        years = [b for b in yr["cells"] if re.fullmatch(r"20\d{2}", b["text"].strip())]
        if len(years) != 2 or years[column]["text"].strip() != period[:4]:
            gaps.append(f"Page {page['page']}: unsupported year columns.")
            continue
        x = sum(p[0] for p in years[column]["box"]) / 4
        label_boundary = sum(p[0] for p in years[0]["box"]) / 4 - 220
        date_parts = "".join(
            b["text"]
            for r in rows
            if yr["y"] - 48 < r["y"] < yr["y"]
            for b in r["cells"]
            if abs(sum(p[0] for p in b["box"]) / 4 - x) < 80
        )
        expected = date.fromisoformat(period)
        if normalise(f"{expected.day} {expected:%B}").replace(" ", "") not in normalise(
            date_parts
        ).replace(" ", ""):
            gaps.append(
                f"Page {page['page']}: accounting date could not be confirmed; figures withheld."
            )
            continue
        units = [
            b["text"]
            for r in rows
            if yr["y"] < r["y"] < yr["y"] + 45
            for b in r["cells"]
            if abs(sum(p[0] for p in b["box"]) / 4 - x) < 65
        ]
        unit = " ".join(units).lower().replace(" ", "")
        scale = (
            1000000
            if re.search(r"(?:['’]?m|million)$", unit)
            else 1000
            if ("000" in unit or "thousand" in unit)
            else None
        )
        currency = "GBP" if "£" in unit or "gbp" in unit else "EUR" if "€" in unit else None
        if scale is None:
            gaps.append(f"Page {page['page']}: monetary scale unclear; figures withheld.")
            continue
        if currency is None:
            gaps.append(
                f"Page {page['page']}: currency symbol not reliably recognised by OCR; values retain the printed scale only."
            )
        section = None
        for row in rows:
            cells = row["cells"]
            label_cells = [b for b in cells if b["box"][0][0] < label_boundary]
            label = normalise(" ".join(b["text"] for b in label_cells))
            candidates = [b for b in cells if abs(sum(p[0] for p in b["box"]) / 4 - x) < 65]
            if label in (
                "current assets",
                "current liabilities",
                "non current assets",
                "non current liabilities",
                "equity",
            ):
                section = label
            metric = LABELS.get(label)
            if label == "lease liabilities" and section in (
                "current liabilities",
                "non current liabilities",
            ):
                metric = section.replace(" ", "_") + "_leases"
            if not label and section in ("current assets", "current liabilities"):
                metric = section.replace(" ", "_")
            if (
                metric is None
                or metric in conflicts
                or len(candidates) != 1
                or candidates[0]["score"] < 0.96
            ):
                continue
            if any(b["score"] < 0.94 for b in label_cells):
                continue
            try:
                value = number(candidates[0]["text"])
            except (ValueError, InvalidOperation):
                continue
            fact = {
                "value": str(value),
                "scale": scale,
                "currency": currency,
                "period": period,
                "scope": "company",
                "page": page["page"],
                "excerpt": " | ".join(b["text"] for b in cells),
                "method": "OCR; source review required",
            }
            if metric in figures and figures[metric]["value"] != fact["value"]:
                figures.pop(metric)
                conflicts.add(metric)
                gaps.append(f"{metric}: conflicting values withheld.")
            else:
                figures[metric] = fact
    if not figures:
        gaps.append(
            "No supported financial table could be extracted. No financial conclusion inferred."
        )
    else:
        missing = sorted(CORE_METRICS - set(figures))
        if missing:
            gaps.append(
                "Unavailable or unrecognised figures: "
                + ", ".join(missing)
                + ". These are not treated as zero."
            )
    return figures, list(dict.fromkeys(gaps))


async def collect_accounts(company_number, company_name, registry_tool, company_type=None):
    result = {"filings": [], "gaps": []}
    try:
        history = await registry_tool(
            "filing_history",
            {"company_number": company_number, "category": "accounts", "items_per_page": 100},
        )
        selected = latest_periods(history.get("items", []))
        if history.get("total_count", 0) > 100:
            result["gaps"].append("Selection is limited to the latest 100 accounts filing records.")
        if len(selected) < 2:
            result["gaps"].append(
                "Fewer than two distinct accounts reporting periods are available."
            )
        for item in selected:
            period = item["description_values"]["made_up_date"]
            transaction = item.get("transaction_id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", transaction):
                raise ValueError("Invalid filing identifier")
            filing = {
                "period": period,
                "filed": item.get("date"),
                "description": item.get("description"),
                "url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}/filing-history/{transaction}/document?format=pdf&download=0",
                "figures": {},
            }
            result["filings"].append(filing)
            document_memory = None
            try:
                path = await download_document(item["links"]["document_metadata"])
                document = await asyncio.to_thread(read_statement_pages, path)
                filing["sha256"] = document["sha256"]
                filing["banking"] = company_type == "bank"
                from .document_context import bind_document
                from .hybrid_accounts import enrich_filing

                document = {
                    **document,
                    "registry_binding": {
                        "source": "companies_house_filing_history",
                        "company_number": company_number,
                        "period": period,
                    },
                }
                document = bind_document(document, company_name, company_number, period)
                if not document["identity"]["verified"]:
                    # Re-read the actual contradictory statement headers as well as
                    # the cover. Never repair a date by assuming the expected year.
                    retry_pages = sorted(
                        {1, 2, 3}
                        | {
                            e["page"]
                            for e in document["identity"]["evidence"]["statement_dates"]
                            if e["latest_header_date"] != period
                        }
                    )[:6]
                    document = await asyncio.to_thread(
                        refine_note_pages, path, document, retry_pages
                    )
                    document = bind_document(document, company_name, company_number, period)
                filing["document_identity"] = document["identity"]
                filing["document_discovery"] = {
                    "statement_pages": document.get("statement_pages", []),
                    "verified_sections": document.get("sections", []),
                    "fallback_truncated": document.get("discovery", {}).get(
                        "fallback_truncated", False
                    ),
                }
                if not document["identity"]["verified"]:
                    result["gaps"].append(
                        f"{period}: document cover identity/period could not be verified; extraction withheld."
                    )
                    continue
                from .table_interpretation import prepare_context

                if os.getenv("DOCUMENT_MEMORY_ENABLED", "0") == "1":
                    from .document_memory import DocumentMemory

                    document_memory = DocumentMemory(
                        pdf_sha256=document["sha256"], company_name=company_name, period=period
                    )
                try:
                    document = await asyncio.to_thread(prepare_context, path, document)
                except Exception as exc:
                    logger.warning("Table scope context unavailable (%s)", type(exc).__name__)
                prior_period = (
                    selected[1]["description_values"]["made_up_date"]
                    if item is selected[0] and len(selected) == 2
                    else None
                )
                from .scoped_accounts import extract_scoped_figures, is_banking_document

                if (
                    company_type == "bank"
                    or is_banking_document(document)
                    or "group" in filing["description"]
                ):
                    filing["banking"] = company_type == "bank" or is_banking_document(document)
                    filing["figures"], gaps = extract_scoped_figures(
                        document, period, company_name, "company"
                    )
                    filing["group_figures"], group_gaps = extract_scoped_figures(
                        document, period, company_name, "group"
                    )
                    result["gaps"].extend(f"{period}: {gap}" for gap in gaps + group_gaps)
                    if item is selected[0] and len(selected) == 2:
                        prior = selected[1]["description_values"]["made_up_date"]
                        filing["comparatives"], _ = extract_scoped_figures(
                            document, prior, company_name, "company"
                        )
                        filing["group_comparatives"], _ = extract_scoped_figures(
                            document, prior, company_name, "group"
                        )
                    await enrich_filing(
                        filing,
                        document,
                        "bank" if filing["banking"] else company_type or "trading",
                        prior_period,
                        document_memory,
                    )
                    required = {"profit_before_tax", "profit_after_tax", "total_assets"} | (
                        {"interest_income", "customer_deposits"}
                        if filing["banking"]
                        else {"revenue", "operating_profit"}
                    )
                    current = set(filing["figures"]) | set(filing["group_figures"])
                    comparative = set(filing.get("comparatives", {})) | set(
                        filing.get("group_comparatives", {})
                    )
                    needs_retry = not required <= current or (
                        prior_period and not required <= comparative
                    )
                    if needs_retry and document.get("statement_pages"):
                        refined = await asyncio.to_thread(
                            read_statement_pages, path, high_resolution=True
                        )
                        # Keep the independently verified, potentially re-OCR'd cover.
                        refined = {
                            **refined,
                            "registry_binding": document.get("registry_binding", {}),
                            "pages": [p for p in document["pages"] if p["page"] <= 3]
                            + [p for p in refined["pages"] if p["page"] > 3],
                        }
                        context_pages = {
                            p["page"]: p
                            for p in document["pages"]
                            if p["page"] in document.get("table_context_pages", [])
                        }
                        present = {p["page"] for p in refined["pages"]}
                        refined["pages"] = [
                            context_pages.get(p["page"], p) for p in refined["pages"]
                        ]
                        refined["pages"].extend(
                            p for n, p in context_pages.items() if n not in present
                        )
                        refined["table_context_pages"] = list(context_pages)
                        refined = bind_document(refined, company_name, company_number, period)
                        filing["refined_document_identity"] = refined["identity"]
                        if not refined["identity"]["verified"]:
                            filing["document_identity"] = refined["identity"]
                            for field in (
                                "figures",
                                "group_figures",
                                "comparatives",
                                "group_comparatives",
                            ):
                                filing[field] = {}
                            result["gaps"].append(
                                f"{period}: higher-resolution source identity/period conflicts; financial extraction withheld."
                            )
                            continue
                        if refined["identity"]["verified"]:
                            # Preserve validated first-pass facts. The merger quarantines
                            # disagreements instead of overwriting them with a retry.
                            await enrich_filing(
                                filing,
                                refined,
                                "bank" if filing["banking"] else company_type or "trading",
                                prior_period,
                                document_memory,
                            )
                    if not filing["figures"] and not filing["group_figures"]:
                        result["gaps"].append(
                            f"{period}: No validated statement facts after section discovery and model-assisted mapping."
                        )
                    # Banking disclosures have their own scope/unit rules.
                    if filing["banking"] and item is selected[0]:
                        try:
                            from .banking_notes import extract_banking_notes
                            from .page_selection import select_note_pages

                            def missing_regulatory(candidate):
                                found = {
                                    m["metric"]
                                    for m in extract_banking_notes(candidate, period, company_name)[
                                        "metrics"
                                    ]
                                    if m["period"] == period
                                }
                                return sorted({"cet1", "lcr", "nsfr"} - found)

                            notes_doc, filing["page_selection"] = await select_note_pages(
                                path, document, "bank", evidence_check=missing_regulatory
                            )
                            notes = extract_banking_notes(notes_doc, period, company_name)
                            if notes["retry_pages"]:
                                notes_doc = await asyncio.to_thread(
                                    refine_note_pages, path, notes_doc, notes["retry_pages"]
                                )
                                notes = extract_banking_notes(notes_doc, period, company_name)
                            filing["banking_notes"] = notes
                            result["gaps"].append(
                                f"{period}: Targeted note retrieval selected {len(filing['page_selection']['selected_pages'])} PDF pages; selection is not evidence that every requested field was validated. Unselected disclosures remain unreviewed."
                            )
                        except Exception as exc:
                            logger.warning(
                                "Banking notes extraction failed (%s)", type(exc).__name__
                            )
                            result["gaps"].append(
                                "Banking notes extraction failed; capital and regulatory liquidity coverage remain incomplete."
                            )
                    elif not filing["banking"]:
                        result["gaps"].append(f"{period}: Group notes require further review.")
                    continue
                filing["figures"], gaps = extract_figures(document, period, company_name)
                if item is selected[0] and len(selected) == 2:
                    prior = selected[1]["description_values"]["made_up_date"]
                    filing["comparatives"], _ = extract_figures(
                        document, prior, company_name, column=1
                    )
                await enrich_filing(
                    filing, document, company_type or "trading", prior_period, document_memory
                )
                result["gaps"].extend(f"{period}: {gap}" for gap in gaps)
                if item is selected[0]:
                    try:
                        from .accounts_notes import extract_notes
                        from .page_selection import select_note_pages

                        prior = (
                            selected[1]["description_values"]["made_up_date"]
                            if len(selected) > 1
                            else ""
                        )

                        def missing_trading(candidate):
                            if not prior:
                                return []
                            found = extract_notes(candidate, period, prior, company_name)
                            missing = {}
                            if not found["maturities"]:
                                missing["leases"] = (
                                    "No validated maturity schedule: inspect liquidity risk or financial risk management for undiscounted lease liability maturities; accounting policies are insufficient."
                                )
                            if not found["counterparties"] and not any(
                                k.startswith("intergroup_") for k in found["pairs"]
                            ):
                                missing["related_parties"] = (
                                    "No validated intercompany funding amounts: inspect related-party and receivable/payable notes."
                                )
                            if not all(
                                k in found["pairs"]
                                for k in (
                                    "wages_and_salaries",
                                    "social_security_costs",
                                    "pension_costs",
                                    "share_based_payments",
                                )
                            ):
                                missing["employee_costs"] = (
                                    "Employee-cost amounts remain incomplete: inspect employee remuneration or staff-cost disclosures."
                                )
                            return missing

                        notes_document, filing["page_selection"] = await select_note_pages(
                            path,
                            document,
                            company_type or "trading",
                            evidence_check=missing_trading,
                        )
                        if prior:
                            filing["notes"] = extract_notes(
                                notes_document, period, prior, company_name
                            )
                            result["gaps"].extend(
                                f"{period}: {gap}" for gap in filing["notes"]["gaps"]
                            )
                        else:
                            result["gaps"].append(
                                "Comparative period unavailable; notes comparison not attempted."
                            )
                    except Exception as exc:
                        logger.warning("Accounts notes extraction failed (%s)", type(exc).__name__)
                        result["gaps"].append(
                            "Notes extraction failed; statement figures remain available. Lease commitments and group exposures are incomplete."
                        )
            except Exception as exc:
                logger.warning(
                    "Accounts extraction failed for period %s (%s)", period, type(exc).__name__
                )
                result["gaps"].append(
                    f"{period}: accounts download or extraction failed; financial figures unavailable."
                )
            finally:
                if document_memory is not None:
                    document_memory.entries.clear()
    except Exception as exc:
        logger.warning("Accounts filing selection failed (%s)", type(exc).__name__)
        result["gaps"].append("Accounts filing history unavailable; financial health not assessed.")
    result["gaps"].append(
        "Accounts are historical filings. OCR figures and note excerpts need source review. Only selected notes are analysed; audit opinions, covenant compliance, complete guarantees, restatements and post-balance-sheet events have not been independently assessed. No credit score or lending decision is inferred."
    )
    return result

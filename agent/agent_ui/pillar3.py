"""Bounded discovery of issuer-hosted, text-based Pillar 3 disclosures."""

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

import httpx

from .document_context import canonical_name
from .pillar3_tables import km1_metrics
from .reporting_dates import full_dates
from .research_requests import post_json

MAX_BYTES = 5_000_000
MAX_PDF_BYTES = 25_000_000


def publisher_matches(url, company_name):
    host = urlsplit(url).hostname or ""
    words = set(canonical_name(company_name).split()) - {
        "limited",
        "plc",
        "bank",
        "financial",
        "group",
        "holdings",
        "the",
        "company",
        "services",
    }
    return any(
        len(w) >= 4 and w in host.split(".")[1 if host.startswith("www.") else 0] for w in words
    )


async def fetch_public(client, url, host):
    """No credentials, private addresses, cross-host redirects or unbounded downloads."""
    for _ in range(3):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").removeprefix("www.") != host.removeprefix("www.")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Unsupported disclosure URL")
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Non-public disclosure address")
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            pdf = (
                parsed.path.lower().endswith(".pdf")
                or response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "application/pdf"
            )
            limit = MAX_PDF_BYTES if pdf else MAX_BYTES
            declared_size = response.headers.get("content-length", "")
            if declared_size.isdigit() and int(declared_size) > limit:
                raise ValueError("Disclosure exceeds size budget")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("Disclosure exceeds size budget")
            return bytes(data), url
    raise ValueError("Too many disclosure redirects")


def extract_pillar3(pages, company_name, period):
    """Require an explicit issuer/perimeter and dated table, not search snippets."""
    text = " ".join(" ".join(p.split()) for p in pages)
    name = re.escape(canonical_name(company_name))
    normal = canonical_name(text)
    if not re.search(r"consolidated pillar 3 disclosures of " + name + r"\b", normal):
        return km1_metrics(pages, company_name, period)
    # The scope paragraph must explicitly bind these disclosures to this year-end.
    scope = re.search(
        r"consolidated Pillar 3 disclosures of.{1,300}?year[- ]ended\s+(.{1,35})", text, re.I
    )
    if (
        not scope
        or not full_dates(scope[1].split(". ", 1)[0])
        or full_dates(scope[1].split(". ", 1)[0])[0] != period
    ):
        return []
    patterns = {
        "cet1": r"common equity tier 1(?: cet1)? ratio",
        "total_capital_ratio": r"total capital ratio",
        "lcr": r"liquidity coverage ratio",
        "nsfr": r"(?:net stable funding|nsfr) ratio",
    }
    metrics = []
    for page_number, page in enumerate(pages, 1):
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        dates = []
        for line in lines:
            found = full_dates(line)
            if len(found) >= 2:
                dates = found if len(set(found)) == len(found) and found[0] == period else []
                continue
            if not dates:
                continue
            amounts = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", line)
            label = canonical_name(re.sub(r"\d+(?:\.\d+)?\s*%", "", line))
            label = re.sub(r"^\d+\s+", "", label).strip()
            metric = next((k for k, pat in patterns.items() if re.fullmatch(pat, label)), None)
            if not metric or len(amounts) != len(dates):
                continue
            basis, window = "point_in_time", None
            if metric in {"lcr", "nsfr"}:
                title = (
                    "liquidity coverage ratio" if metric == "lcr" else "net stable funding ratio"
                )
                footnote = re.search(
                    title + r" is calculated based on (?:a )?(12-month|4-quarter) average",
                    text,
                    re.I,
                )
                if not footnote:
                    continue
                basis = "average"
                window = "12-month" if footnote[1].lower() == "12-month" else "4-quarter"
            for value, date in zip(amounts, dates):
                if not 0 <= Decimal(value) <= (10000 if metric in {"lcr", "nsfr"} else 100):
                    continue
                metrics.append(
                    {
                        "metric": metric,
                        "scope": "group",
                        "period": date,
                        "value": value,
                        "scale": 1,
                        "unit": "%",
                        "page": page_number,
                        "reporting_basis": basis,
                        "averaging_window": window,
                        "header_excerpt": " | ".join(dates),
                        "excerpt": line,
                        "basis": "Issuer-published consolidated Pillar 3 disclosure; literal PDF text; no independent compliance assessment.",
                    }
                )
    # Conflicting repeated disclosures are withheld, never resolved by ordering.
    grouped = {}
    for metric in metrics:
        grouped.setdefault((metric["metric"], metric["period"]), []).append(metric)
    return [
        rows[0]
        for rows in grouped.values()
        if len({(r["value"], r["reporting_basis"], r["averaging_window"]) for r in rows}) == 1
    ]


def pdf_pages(data):
    import pypdfium2 as pdfium

    if not data.startswith(b"%PDF"):
        raise ValueError("Not a PDF")
    pages = []
    with pdfium.PdfDocument(data) as pdf:
        if len(pdf) > 160:
            raise ValueError("Supplement exceeds page budget")
        for page in pdf:
            try:
                text = page.get_textpage()
                try:
                    pages.append(text.get_text_range())
                finally:
                    text.close()
            finally:
                page.close()
    return pages


def page_links(markup, landing):
    """Use anchor descriptions, not just filenames, to discover disclosure PDFs."""
    links = []
    for href, body in re.findall(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", markup, re.I | re.S
    ):
        url = urljoin(landing, html.unescape(href))
        label = html.unescape(re.sub(r"<[^>]+>", " ", body))
        links.append((url, label))
    # Hydrated sites may keep download URLs in serialized JSON rather than anchors.
    # Decode string literals only; never execute page scripts. All candidates still
    # pass publisher, public-network and document identity validation downstream.
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", markup, re.I | re.S):
        fragments = [script]
        for literal in re.findall(r'"(?:\\.|[^"\\])*"', script):
            try:
                fragments.append(json.loads(literal))
            except ValueError:
                continue
        for fragment in fragments:
            for value in re.findall(
                r'"(?:url|href)"\s*:\s*"([^"<>]+\.pdf(?:\?[^"<>]*)?)"', fragment, re.I
            ):
                links.append((urljoin(landing, html.unescape(value)), ""))
    return links


def publisher_identity(markup, company_name, company_number):
    plain = html.unescape(
        re.sub(
            r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", markup, flags=re.I | re.S
        )
    )
    plain = re.sub(r"<[^>]+>", " ", plain)
    if canonical_name(company_name) not in canonical_name(plain):
        return False
    # Zero padding is formatting, not a fuzzy identifier match. Require an
    # explicit company label so FCA/PRA reference numbers cannot authenticate it.
    numbers = re.findall(
        r"\bcompany\s+(?:registration\s+)?(?:number|no\.?)\s*:?\s*([A-Z]{2}\d{6}|\d{6,8})\b",
        plain,
        re.I,
    )
    # Bind this common legal formulation directly to the named company; a
    # nearby FCA/PRA number or another entity's registration is insufficient.
    legal_name = re.escape(company_name).replace(r"\ ", r"\s+")
    numbers.extend(
        re.findall(
            r"\b" + legal_name + r"\s+(?:is\s+)?(?:a\s+)?company\s+"
            r"(?:registered|incorporated)\s+in\s+"
            r"(?:England(?:\s+and\s+Wales)?|Wales|Scotland|Northern\s+Ireland)\s*"
            r"(?:\(\s*)?(?:under\s+)?(?:company\s+)?(?:registration\s+)?"
            r"(?:number|no\.?)\s*:?\s*([A-Z]{2}\d{6}|\d{6,8})\b",
            plain,
            re.I,
        )
    )
    return company_number.upper() in {n.upper().zfill(8) for n in numbers}


async def collect_pillar3(company_name, company_number, period, known_sources=()):
    from .browser_research import discover, enabled

    key = os.getenv("TAVILY_API_KEY", "")
    if not key and not enabled():
        return [], "Pillar 3 discovery unavailable: search provider not configured."
    failures = set()
    try:
        async with (
            asyncio.timeout(240),
            httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client,
        ):
            candidates = [u for u in known_sources if publisher_matches(u, company_name)]
            brand = " ".join(
                w
                for w in canonical_name(company_name).split()
                if w not in {"limited", "plc", "financial", "bank", "group"}
            )
            for query in [
                f'"{company_name}" "Pillar 3" "{period[:4]}"',
                f'"{brand}" investor annual reports disclosures',
            ]:
                if not key:
                    failures.add("search_not_configured")
                    break
                try:
                    response = await post_json(
                        client,
                        "https://api.tavily.com/search",
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "query": query,
                            "max_results": 5,
                            "search_depth": "basic",
                            "include_answer": False,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("results"), list
                    ):
                        raise ValueError("Invalid discovery response")
                    candidates.extend(
                        r["url"]
                        for r in payload["results"]
                        if isinstance(r, dict)
                        and isinstance(r.get("url"), str)
                        and publisher_matches(r["url"], company_name)
                    )
                except (httpx.HTTPError, ValueError):
                    failures.add("search_failed")
            browser_pages = []
            document_publishers = {}
            if not candidates or failures:
                recovered = await discover(
                    company_name, company_number, "pillar3", period, seeds=candidates
                )
                browser_pages = recovered.pages
                candidates.extend(p.url for p in browser_pages)
                candidates.extend(u for u in recovered.links if publisher_matches(u, company_name))
                verified_pages = {
                    p.url
                    for p in browser_pages
                    if publisher_identity(p.text, company_name, company_number)
                }
                for document in recovered.documents:
                    if document.get("publisher_page") in verified_pages:
                        candidates.append(document["url"])
                        document_publishers[document["url"]] = document["publisher_page"]
                if recovered.status != "disabled":
                    failures.add("browser_" + recovered.status)
            # Direct PDF results are retained, but their publisher must first be
            # identified on a same-host page containing the legal name and CRN.
            direct = [u for u in candidates if urlsplit(u).path.lower().endswith(".pdf")]
            queue = list(
                dict.fromkeys(
                    [u for u in candidates if u not in direct]
                    + [urljoin(u, "/") for u in candidates]
                )
            )
            queue.sort(key=lambda u: 0 if re.search(r"invest|report|disclos", u, re.I) else 1)
            verified_hosts = {}
            for page in browser_pages:
                if publisher_identity(page.text, company_name, company_number):
                    verified_hosts[urlsplit(page.url).hostname] = page.url
            pdfs = list(dict.fromkeys(direct))
            visited = set()
            for _ in range(6):
                if not queue:
                    break
                landing = queue.pop(0)
                if landing in visited:
                    continue
                visited.add(landing)
                host = urlsplit(landing).hostname
                try:
                    body, landing = await fetch_public(client, landing, host)
                    markup = body.decode("utf-8", errors="replace")
                    host = urlsplit(landing).hostname
                    if publisher_identity(markup, company_name, company_number):
                        verified_hosts[host] = landing
                    for url, label in page_links(markup, landing):
                        if urlsplit(url).hostname != host:
                            if (
                                publisher_matches(url, company_name)
                                and urlsplit(url).scheme == "https"
                                and url not in visited
                                and url not in queue
                            ):
                                queue.append(url)
                            continue
                        context = url + " " + label
                        if urlsplit(url).path.lower().endswith(".pdf") and re.search(
                            r"pillar[\s_-]*(?:3|iii|three)", context, re.I
                        ):
                            pdfs.append(url)
                        elif (
                            not urlsplit(url).path.lower().endswith(".pdf")
                            and re.search(
                                r"investor|annual.report|financial.report|pillar|disclosure|results.and.presentations|legal",
                                context,
                                re.I,
                            )
                            and url not in visited
                            and url not in queue
                        ):
                            queue.append(url)
                except (httpx.HTTPError, ValueError, OSError):
                    failures.add("publisher_fetch_failed")
            pdfs = list(dict.fromkeys(pdfs))
            pdfs.sort(
                key=lambda u: (
                    period[:4] not in u,
                    not bool(re.search(r"pillar[\s_-]*(?:3|iii)", u, re.I)),
                )
            )
            for url in pdfs[:4]:
                host = urlsplit(url).hostname
                identity_url = (
                    document_publishers.get(url)
                    or verified_hosts.get(host)
                    or verified_hosts.get(host.removeprefix("www."))
                    or verified_hosts.get("www." + host)
                )
                if not identity_url:
                    failures.add("publisher_identity_unverified")
                    continue
                try:
                    data, url = await fetch_public(client, url, host)
                    pages = await asyncio.to_thread(pdf_pages, data)
                    metrics = extract_pillar3(pages, company_name, period)
                    if not metrics:
                        failures.add("document_identity_period_or_layout_unsupported")
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    for metric in metrics:
                        metric.update(
                            document_sha256=digest,
                            publisher_identity_url=identity_url,
                            issuer_company_number=company_number,
                        )
                    return [
                        {
                            "period": period,
                            "url": url,
                            "source_kind": "other",
                            "banking_notes": {
                                "metrics": metrics,
                                "disclosures": [],
                                "gaps": [],
                                "retry_pages": [],
                            },
                            "sha256": digest,
                            "publisher_page": identity_url,
                        }
                    ], None
                except (httpx.HTTPError, ValueError, OSError):
                    failures.add("document_fetch_or_parse_failed")
            reason = ", ".join(sorted(failures)) or "no_candidate_disclosure"
            return (
                [],
                f"No matching, source-validated Pillar 3 disclosure was retrieved ({reason}). Parent-group, wrong-period and unsupported documents are not substituted.",
            )
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        return (
            [],
            "Supplementary Pillar 3 retrieval failed or exceeded its budget; missing ratios remain unavailable.",
        )

"""Read platform-published business aggregate ratings; never derive them with an LLM."""

import asyncio
import json
import math
import os
import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .review_sentiment import name_matches, profile_identity, review_profile_url

MAX_BYTES = 2 * 1024 * 1024
BUSINESS_TYPES = {
    "Organization",
    "LocalBusiness",
    "Store",
    "DepartmentStore",
    "ShoppingCenter",
    "Corporation",
    "Restaurant",
    "ProfessionalService",
    "FinancialService",
    "HomeAndConstructionBusiness",
}


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.collect = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            self.collect = dict(attrs).get("type", "").lower() == "application/ld+json"
            self.parts = []

    def handle_data(self, text):
        if self.collect:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.collect:
            self.scripts.append("".join(self.parts))
            self.collect = False


def business_nodes(value):
    # Do not traverse individual reviews, products or related-business recommendations.
    if isinstance(value, list):
        for item in value[:100]:
            yield from business_nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity"):
            if key in value:
                yield from business_nodes(value[key])


def exact_count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", value.strip()):
        return int(value.strip().replace(",", ""))
    raise ValueError("Review count is not an exact nonnegative integer")


def rating_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid rating")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Invalid rating")
    return number


def parse_aggregate(html, company_name, provider, profile_url):
    parser = MetadataParser()
    parser.feed(html)
    matches = []
    for script in parser.scripts:
        try:
            data = json.loads(script)
        except (ValueError, RecursionError):
            continue
        for node in business_nodes(data):
            kinds = node.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if not isinstance(kinds, list) or not any(
                isinstance(k, str) and k in BUSINESS_TYPES for k in kinds
            ):
                continue
            name = unescape(str(node.get("name", "")))
            aggregate = node.get("aggregateRating")
            if not name_matches(name, company_name) or not isinstance(aggregate, dict):
                continue
            # A same-provider identifier for a different profile must not be borrowed.
            identifier = node.get("@id") or node.get("url")
            if isinstance(identifier, str) and review_profile_url(identifier, provider):
                if urlsplit(identifier).path.rstrip("/") != urlsplit(profile_url).path.rstrip("/"):
                    continue
            try:
                value = rating_number(aggregate.get("ratingValue"))
                # Require an explicit maximum scale; never assume every site uses five stars.
                best = rating_number(aggregate.get("bestRating"))
                worst = rating_number(aggregate.get("worstRating", 1))
                count = exact_count(aggregate.get("reviewCount"))
                if not 0 <= worst < best or not worst <= value <= best or count == 0:
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            matches.append(
                {
                    "profile_name": name,
                    "rating": value,
                    "scale_min": worst,
                    "scale_max": best,
                    "review_count": count,
                    "profile_website": str(node.get("url", ""))[:500],
                    "scope": "individual_location"
                    if provider == "Yelp"
                    else "brand_or_business_profile",
                    "entity_match": "name_match_only; legal entity not independently verified",
                    "method": "Public profile JSON-LD aggregateRating",
                    "source_excerpt": json.dumps(
                        {
                            "name": name,
                            "aggregateRating": {
                                k: aggregate[k]
                                for k in ("ratingValue", "bestRating", "worstRating", "reviewCount")
                                if k in aggregate
                            },
                        },
                        ensure_ascii=True,
                    ),
                }
            )
    signatures = {
        (m["rating"], m["scale_min"], m["scale_max"], m["review_count"], m["profile_name"])
        for m in matches
    }
    if len(signatures) > 1:
        return {
            "status": "ambiguous_aggregate",
            "reason": "Conflicting business aggregates; no rating displayed.",
        }
    if not matches:
        return {
            "status": "aggregate_unavailable",
            "reason": "No unambiguous business rating, explicit scale and exact review count found in profile metadata.",
        }
    return {"status": "confirmed_on_profile", **matches[0]}


async def fetch_rating(client, url, provider, company_name):
    checked = datetime.now(timezone.utc).isoformat()
    base = {"checked_at": checked, "url": url, "platform_updated_at": "unknown"}
    current = url
    try:
        for _ in range(3):
            parsed = urlsplit(current)
            if (
                not review_profile_url(current, provider)
                or parsed.scheme != "https"
                or parsed.port not in (None, 443)
            ):
                return {
                    **base,
                    "status": "unsafe_profile_url",
                    "reason": "URL is not an allowed HTTPS review profile.",
                }
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    current = urljoin(current, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    return {
                        **base,
                        "status": "page_unavailable",
                        "http_status": response.status_code,
                        "reason": "Public profile could not be fetched; search-snippet ratings are not used.",
                    }
                if "text/html" not in response.headers.get("content-type", "").lower():
                    return {
                        **base,
                        "status": "unsupported_content",
                        "reason": "Profile did not return HTML.",
                    }
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        return {
                            **base,
                            "status": "page_too_large",
                            "reason": "Profile exceeded the bounded download limit.",
                        }
                html = content.decode(response.encoding or "utf-8", errors="replace")
                return {
                    **base,
                    "url": current,
                    **parse_aggregate(html, company_name, provider, current),
                }
        return {**base, "status": "redirect_limit", "reason": "Profile exceeded redirect limit."}
    except (httpx.HTTPError, ValueError, RecursionError):
        return {
            **base,
            "status": "fetch_failed",
            "reason": "Profile retrieval or metadata parsing failed.",
        }


async def enrich_review_ratings(search, company_name):
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:

        async def enrich(provider, item):
            async with semaphore:
                identity = profile_identity(
                    item.get("title", ""), company_name, item["url"], provider["provider"]
                )
                match_name = identity["match_name"] if identity else company_name
                item["aggregate"] = await fetch_rating(
                    client, item["url"], provider["provider"], match_name
                )

        await asyncio.gather(
            *(
                enrich(provider, item)
                for provider in search["providers"]
                for item in provider["results"]
            )
        )
    await tavily_rating_fallback(search, company_name)
    for provider in search["providers"]:
        for item in provider["results"]:
            identity = profile_identity(
                item.get("title", ""), company_name, item["url"], provider["provider"]
            )
            if identity:
                item["aggregate"]["entity_match"] = identity["relation"]
                item["aggregate"]["assessed_legal_name"] = company_name


def parse_extracted_rating(content, company_name, provider, url):
    """Accept only structured metadata or an anchored business-profile header."""
    structured = parse_aggregate(content, company_name, provider, url)
    if structured["status"] == "confirmed_on_profile":
        return {
            **structured,
            "status": "confirmed_via_tavily_extract",
            "method": "Tavily Extract: profile JSON-LD aggregateRating",
        }
    if structured["status"] == "ambiguous_aggregate":
        return structured
    headers = list(re.finditer(r"^# ([^\n]+)\s*$", content, re.M))
    matches = []
    for header in headers:
        name = unescape(header.group(1).strip())
        if not name_matches(name, company_name):
            continue
        tail = content[header.end() :]
        section = re.split(r"^#{1,6}\s", tail, maxsplit=1, flags=re.M)[0][:2000]
        if provider == "Trustpilot":
            # The numerical score following the star graphic is precise; the graphic
            # itself can be rounded (e.g. 4 stars beside a 3.9 TrustScore).
            count_match = re.match(r"\s*(\d{1,3}(?:,\d{3})+|\d+)\s*\n", section)
            scores = list(
                re.finditer(
                    r"!\[[^\]\n]*(?:Rated|TrustScore)\s+\d+(?:\.\d+)?\s+out of\s+(\d+(?:\.\d+)?)[^\]\n]*\]\([^\n]+\)\s*\n(\d+(?:\.\d+)?)\s*(?=\n|$)",
                    section,
                )
            )
            if not count_match or len(scores) != 1:
                continue
            count = exact_count(count_match.group(1))
            scale = rating_number(scores[0].group(1))
            score = rating_number(scores[0].group(2))
            # If the page also gives a precise total, it must agree with the header.
            totals = {
                exact_count(m)
                for m in re.findall(r"See all (\d{1,3}(?:,\d{3})+|\d+) reviews", content, re.I)
            }
            if totals and totals != {count}:
                return {
                    "status": "ambiguous_aggregate",
                    "reason": "Extracted profile review totals disagree; rating withheld.",
                }
        elif provider == "Yelp":
            # Deliberately require an explicit scale and exact count in the header.
            scores = list(
                re.finditer(
                    r"(\d+(?:\.\d+)?)\s+(?:out of|/)\s*(\d+(?:\.\d+)?)\s*(?:stars)?\s*\(?\s*(\d{1,3}(?:,\d{3})+|\d+)\s+reviews?\b",
                    section,
                    re.I,
                )
            )
            if len(scores) != 1:
                continue
            score = rating_number(scores[0].group(1))
            scale = rating_number(scores[0].group(2))
            count = exact_count(scores[0].group(3))
        else:
            continue
        if not 0 <= score <= scale or scale <= 0 or count <= 0:
            continue
        matches.append(
            {
                "status": "confirmed_via_tavily_extract",
                "profile_name": name,
                "rating": score,
                "scale_max": scale,
                "review_count": count,
                "scope": "individual_location"
                if provider == "Yelp"
                else "brand_or_business_profile",
                "entity_match": "name_match_only; legal entity not independently verified",
                "method": f"Tavily Extract: {provider} profile header",
                "source_excerpt": (header.group(0) + "\n" + section)[:2000],
            }
        )
    if len(matches) == 1:
        return matches[0]
    return {
        "status": "aggregate_unavailable",
        "reason": "Tavily returned content without one matching business header, explicit score/scale and exact review count.",
    }


async def tavily_rating_fallback(search, company_name):
    """One bounded batch; no search-snippet scores, retries or alternate URL crawling."""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    targets = {}
    eligible = {"page_unavailable", "fetch_failed", "unsupported_content", "aggregate_unavailable"}
    for provider in search["providers"]:
        if provider["provider"] not in ("Trustpilot", "Yelp"):
            continue
        for item in provider["results"]:
            url = item["url"]
            parsed = urlsplit(url)
            if (
                item["aggregate"]["status"] in eligible
                and review_profile_url(url, provider["provider"])
                and parsed.scheme == "https"
                and parsed.port in (None, 443)
            ):
                targets.setdefault(url, []).append((provider["provider"], item))
    if not targets or not key:
        return
    targets = dict(list(targets.items())[:4])
    usage = {"status": "failed", "requested_profiles": len(targets), "returned_profiles": 0}
    search["rating_fallback"] = usage
    checked = datetime.now(timezone.utc).isoformat()

    def apply(url, result):
        for _, item in targets[url]:
            direct = item["aggregate"]
            item["aggregate"] = {
                "url": url,
                "checked_at": checked,
                "platform_updated_at": "unknown",
                "direct_access_status": direct.get("direct_access_status", direct["status"]),
                "retrieval_method": "Tavily Extract",
                "freshness": "May be cached; publisher update time unknown.",
                **result,
            }

    for url in targets:
        apply(
            url,
            {
                "status": "tavily_extraction_failed",
                "reason": "Direct access was unavailable and Tavily could not retrieve a usable profile.",
            },
        )
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            response = await client.post(
                "https://api.tavily.com/extract",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "urls": list(targets),
                    "extract_depth": "basic",
                    "format": "markdown",
                    "include_usage": True,
                    "timeout": 20,
                },
            )
            if response.status_code != 200:
                usage["http_status"] = response.status_code
                return
            if len(response.content) > MAX_BYTES:
                usage["status"] = "response_too_large"
                return
            data = response.json()
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("results"), list)
                or not isinstance(data.get("failed_results", []), list)
            ):
                return
            credits = (
                data.get("usage", {}).get("credits")
                if isinstance(data.get("usage"), dict)
                else None
            )
            if type(credits) in (int, float) and math.isfinite(credits) and credits >= 0:
                usage["credits"] = credits
            failures = {
                r.get("url")
                for r in data.get("failed_results", [])
                if isinstance(r, dict) and isinstance(r.get("url"), str)
            }
            returned = {}
            for result in data["results"]:
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("url"), str)
                    and result["url"] in targets
                ):
                    returned.setdefault(result["url"], []).append(result)
            for url, results in returned.items():
                if url in failures or len(results) != 1:
                    continue
                content = results[0].get("raw_content")
                if not isinstance(content, str) or not content.strip() or len(content) > 200000:
                    continue
                provider, item = targets[url][0]
                identity = profile_identity(item.get("title", ""), company_name, url, provider)
                result = parse_extracted_rating(
                    content, identity["match_name"] if identity else company_name, provider, url
                )
                apply(url, result)
                usage["returned_profiles"] += 1
            usage["status"] = (
                "completed"
                if usage["returned_profiles"] == len(targets)
                else "partial"
                if usage["returned_profiles"]
                else "failed"
            )
    except (httpx.HTTPError, ValueError, TypeError, RecursionError):
        return

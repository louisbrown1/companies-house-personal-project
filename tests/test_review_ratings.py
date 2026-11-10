import json

import httpx
import pytest

from agent_ui import review_ratings as ratings
from agent_ui.local_report import build_evidence, render_report
from agent_ui.review_sentiment import add_review_evidence

URL = "https://www.reviews.io/company-reviews/store/example"
TP = "https://www.trustpilot.com/review/example.com"
YELP = "https://www.yelp.com/biz/example"
EXTRACT = """Suggested business 4.8 / 5, 18K reviews
# Example Stores Reviews

6,636

![Rated 4 out of 5 stars](https://example.com/stars.svg)
3.9

## Review summary
Based on 1,789 reviews
See all 6,636 reviews
"""


def test_extracted_header_uses_precise_score_and_exact_total():
    result = ratings.parse_extracted_rating(EXTRACT, "Example Retail Limited", "Trustpilot", TP)
    assert result["status"] == "confirmed_via_tavily_extract"
    assert (result["rating"], result["scale_max"], result["review_count"]) == (3.9, 5, 6636)


@pytest.mark.parametrize(
    "content",
    [
        EXTRACT.replace("6,636", "7K"),
        EXTRACT.replace("Example Stores", "Other Stores"),
        EXTRACT.replace("out of 5", "stars"),
        EXTRACT.replace("See all 6,636", "See all 6,637"),
        EXTRACT + EXTRACT,
    ],
)
def test_unreliable_extracted_ratings_are_withheld(content):
    result = ratings.parse_extracted_rating(content, "Example Retail Limited", "Trustpilot", TP)
    assert result["status"] != "confirmed_via_tavily_extract"


def test_yelp_requires_explicit_scale_and_keeps_location_scope():
    result = ratings.parse_extracted_rating(
        "# Example Stores\n4.2 out of 5 stars (123 reviews)\n", "Example Limited", "Yelp", YELP
    )
    assert result["scope"] == "individual_location" and result["review_count"] == 123
    assert (
        ratings.parse_extracted_rating(
            "# Example Stores\n4.2 (123 reviews)\n", "Example Limited", "Yelp", YELP
        )["status"]
        == "aggregate_unavailable"
    )


@pytest.mark.asyncio
async def test_tavily_partial_result_preserves_provenance(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-secret")
    search = {
        "providers": [
            {
                "provider": provider,
                "results": [{"url": url, "aggregate": {"status": "page_unavailable"}}],
            }
            for provider, url in [("Trustpilot", TP), ("Yelp", YELP)]
        ]
    }

    def handler(request):
        assert str(request.url) == "https://api.tavily.com/extract"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert json.loads(request.content)["urls"] == [TP, YELP]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": TP, "raw_content": EXTRACT},
                    {"url": "https://unrelated.example", "raw_content": EXTRACT},
                ],
                "failed_results": [{"url": YELP, "error": "Failed to fetch url"}],
                "usage": {"credits": 0},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ratings.httpx, "AsyncClient", lambda **kwargs: client)
    await ratings.tavily_rating_fallback(search, "Example Limited")
    tp = search["providers"][0]["results"][0]["aggregate"]
    assert tp["status"] == "confirmed_via_tavily_extract"
    assert tp["direct_access_status"] == "page_unavailable" and "cached" in tp["freshness"]
    assert search["providers"][1]["results"][0]["aggregate"]["status"] == "tavily_extraction_failed"
    assert search["rating_fallback"] == {
        "status": "partial",
        "requested_profiles": 2,
        "returned_profiles": 1,
        "credits": 0,
    }
    assert "test-secret" not in json.dumps(search)


@pytest.mark.asyncio
async def test_tavily_does_not_replace_direct_success_or_ambiguous_data(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-secret")

    def unexpected(**kwargs):
        pytest.fail("No extraction should be attempted")

    monkeypatch.setattr(ratings.httpx, "AsyncClient", unexpected)
    search = {
        "providers": [
            {
                "provider": "Trustpilot",
                "results": [
                    {"url": TP, "aggregate": {"status": status}}
                    for status in (
                        "confirmed_on_profile",
                        "ambiguous_aggregate",
                        "unsafe_profile_url",
                    )
                ],
            }
        ]
    }
    await ratings.tavily_rating_fallback(search, "Example Limited")
    assert "rating_fallback" not in search


def node(**aggregate):
    return {
        "@type": "LocalBusiness",
        "@id": URL,
        "name": "Example Stores",
        "aggregateRating": {
            "ratingValue": "2.62",
            "bestRating": "5",
            "worstRating": "1",
            "reviewCount": "356",
            **aggregate,
        },
    }


def html(value):
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


def parse(value):
    return ratings.parse_aggregate(html(value), "Example Retail Limited", "REVIEWS.io", URL)


def test_reads_business_aggregate_and_keeps_exact_count():
    value = node(reviewCount="12,345")
    value["review"] = [{"reviewRating": {"ratingValue": 5}}]
    result = parse(value)
    assert result["status"] == "confirmed_on_profile"
    assert result["rating"] == 2.62 and result["review_count"] == 12345
    assert result["entity_match"].startswith("name_match_only")


@pytest.mark.parametrize(
    "values",
    [
        {"ratingValue": "NaN"},
        {"ratingValue": 6},
        {"reviewCount": "7K"},
        {"reviewCount": True},
        {"reviewCount": -3},
        {"reviewCount": 0},
        {"bestRating": None},
    ],
)
def test_invalid_or_approximate_values_are_not_published(values):
    assert parse(node(**values))["status"] == "aggregate_unavailable"


def test_unrelated_company_product_and_individual_reviews_are_not_used():
    value = node()
    value["name"] = "Other Company"
    assert parse(value)["status"] == "aggregate_unavailable"
    value = node()
    value["@type"] = "Product"
    assert parse(value)["status"] == "aggregate_unavailable"
    assert (
        parse({"@type": "Review", "reviewRating": {"ratingValue": 5}})["status"]
        == "aggregate_unavailable"
    )
    value = node()
    value["@id"] = "https://www.reviews.io/company-reviews/store/other"
    assert parse(value)["status"] == "aggregate_unavailable"


def test_graph_duplicates_and_conflicts():
    assert parse({"@graph": [node(), node()]})["status"] == "confirmed_on_profile"
    assert parse({"@graph": [node(), node(ratingValue=4)]})["status"] == "ambiguous_aggregate"


def test_html_entities_in_business_name():
    value = node()
    value["name"] = "B&amp;M Stores"
    result = ratings.parse_aggregate(html(value), "B & M RETAIL LIMITED", "REVIEWS.io", URL)
    assert result["status"] == "confirmed_on_profile"


@pytest.mark.asyncio
async def test_blocked_profile_is_not_replaced_by_snippet_rating():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(403))
    ) as client:
        result = await ratings.fetch_rating(client, URL, "REVIEWS.io", "Example Retail Limited")
    assert result["status"] == "page_unavailable" and result["http_status"] == 403
    assert "rating" not in result
    assert result["checked_at"]


@pytest.mark.asyncio
async def test_redirect_to_untrusted_host_is_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ratings.fetch_rating(client, URL, "REVIEWS.io", "Example Retail Limited")
    assert len(calls) == 1 and result["status"] == "unsafe_profile_url"


@pytest.mark.asyncio
async def test_profile_download_size_limit(monkeypatch):
    monkeypatch.setattr(ratings, "MAX_BYTES", 16)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"x" * 32
            )
        )
    ) as client:
        result = await ratings.fetch_rating(client, URL, "REVIEWS.io", "Example Retail Limited")
    assert result["status"] == "page_too_large"


def test_aggregate_is_primary_and_individual_sentiment_is_supporting():
    aggregate = {**parse(node()), "url": URL, "checked_at": "2026-11-07T17:00:00+00:00"}
    search = {
        "providers": [
            {
                "provider": "REVIEWS.io",
                "status": "completed",
                "results": [
                    {
                        "url": URL,
                        "title": "Example Stores",
                        "excerpt": "I had great service.",
                        "sentiment": "positive",
                        "supporting_quote": "I had great service.",
                        "analysis_status": "completed",
                        "aggregate": aggregate,
                    }
                ],
            }
        ]
    }
    bundle, groups = build_evidence(
        {"company_number": "12345678", "profile": {"company_name": "Example"}}, None, {}
    )
    add_review_evidence(bundle, groups, search)
    report = render_report(bundle, groups, [])
    assert report.index("2.62 / 5; 356 reviews") < report.index("Supporting excerpts only")
    assert "platform update time unknown" in report
    assert "no combined score" in report

import json

import pytest

from agent_ui import review_ratings
from agent_ui.review_ratings import enrich_review_ratings, parse_aggregate, parse_extracted_rating
from agent_ui.review_sentiment import merge_discovered_profiles, profile_identity

URL = "https://www.trustpilot.com/review/investec.com"


def inputs():
    return {
        "providers": [{"provider": "Trustpilot", "status": "no_usable_results", "results": []}]
    }, {
        "results": [
            {
                "url": URL,
                "title": "Investec is rated Excellent with 4.7 / 5 on Trustpilot",
                "excerpt": "I found the service helpful.",
                "retrieved_at": "2026-11-08T00:00:00Z",
            }
        ]
    }


def test_web_discovery_is_reused_without_publishing_snippet_rating():
    reviews, web = inputs()
    merge_discovered_profiles(reviews, web, "INVESTEC BANK PLC")
    merge_discovered_profiles(reviews, web, "INVESTEC BANK PLC")
    provider = reviews["providers"][0]
    assert len(provider["results"]) == 1
    item = provider["results"][0]
    assert item["profile_identity"]["match_name"] == "INVESTEC"
    assert "legal_entity_unverified" in item["entity_match"]
    assert "aggregate" not in item
    assert provider["provider_search_status"] == "no_usable_results"
    assert provider["status"] == "completed"


@pytest.mark.parametrize(
    "title,url",
    [
        ("Investec Wealth", URL),
        ("Investec", "https://www.trustpilot.com/review/unrelated.com"),
        ("Investec", "https://trustpilot.com.evil.test/review/investec.com"),
        ("Investec", "https://www.trustpilot.com/review/investec-other.com"),
    ],
)
def test_brand_gate_rejects_unrelated_profiles(title, url):
    assert profile_identity(title, "INVESTEC BANK PLC", url, "Trustpilot") is None


def test_title_or_rounded_review_count_is_insufficient():
    assert (
        parse_extracted_rating(
            "Investec is rated Excellent with 4.7 / 5 on Trustpilot", "INVESTEC", "Trustpilot", URL
        )["status"]
        != "confirmed_via_tavily_extract"
    )
    data = {
        "@type": "Organization",
        "name": "Investec",
        "aggregateRating": {"ratingValue": "4.7", "bestRating": 5, "reviewCount": "6K"},
    }
    html = '<script type="application/ld+json">' + json.dumps(data) + "</script>"
    assert parse_aggregate(html, "INVESTEC", "Trustpilot", URL)["status"] != "confirmed_on_profile"
    data["aggregateRating"]["reviewCount"] = "6,321"
    html = '<script type="application/ld+json">' + json.dumps(data) + "</script>"
    rating = parse_aggregate(html, "INVESTEC", "Trustpilot", URL)
    assert (rating["rating"], rating["review_count"]) == (4.7, 6321)


@pytest.mark.asyncio
async def test_brand_rating_verification_keeps_legal_entity_caveat(monkeypatch):
    reviews, web = inputs()
    merge_discovered_profiles(reviews, web, "INVESTEC BANK PLC")

    async def fetch(client, url, provider, name):
        assert name == "INVESTEC"
        return {
            "status": "confirmed_on_profile",
            "rating": 4.7,
            "scale_max": 5,
            "review_count": 6321,
        }

    async def fallback(*args):
        return

    monkeypatch.setattr(review_ratings, "fetch_rating", fetch)
    monkeypatch.setattr(review_ratings, "tavily_rating_fallback", fallback)
    await enrich_review_ratings(reviews, "INVESTEC BANK PLC")
    rating = reviews["providers"][0]["results"][0]["aggregate"]
    assert rating["assessed_legal_name"] == "INVESTEC BANK PLC"
    assert "brand_name_and_profile_domain_match" in rating["entity_match"]
    assert "legal_entity_unverified" in rating["entity_match"]

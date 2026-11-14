import json
from types import SimpleNamespace

import httpx
import pytest

from agent_ui import review_sentiment as review
from agent_ui.local_report import build_evidence, render_report


@pytest.mark.asyncio
async def test_missing_key_lists_all_four_sources(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = await review.company_review_search("Example", "12345678")
    assert [p["provider"] for p in result["providers"]] == list(review.PROVIDERS)
    assert all(p["status"] == "not_configured" for p in result["providers"])


@pytest.mark.asyncio
async def test_targeted_domains_caps_and_false_hosts(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    calls = []

    def handle(request):
        data = json.loads(request.content)
        calls.append(data)
        domain = data["include_domains"][0]
        path = (
            "/company-reviews/store/example"
            if domain == "reviews.io"
            else "/en-GB/reviews/example"
            if domain == "feefo.com"
            else "/biz/example"
            if domain == "yelp.com"
            else "/review/example"
        )
        assert data["include_domains_mode"] == "restrict"
        assert data["max_results"] == 2 and data["include_answer"] is False
        return httpx.Response(
            200,
            json={
                "usage": {"credits": 1},
                "results": [
                    {
                        "url": "https://www." + domain + path,
                        "title": "Example",
                        "content": "Great service! " * 100,
                    },
                    {"url": "https://" + domain + ".evil.test/review", "content": "Bad"},
                ],
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        review.httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handle), **kw),
    )
    result = await review.company_review_search("Example", "12345678")
    assert len(calls) == 4
    assert all(len(p["results"]) == 1 for p in result["providers"])
    assert all(len(p["results"][0]["excerpt"]) == 1000 for p in result["providers"])
    assert result["providers"][-1]["results"][0]["scope"] == "branch_or_business_unverified"


def test_marketing_and_policy_pages_are_not_review_profiles():
    assert not review.review_profile_url("https://business.feefo.com/sector/retail", "Feefo")
    assert not review.review_profile_url("https://terms.yelp.com/privacy/en_us", "Yelp")
    assert review.review_profile_url("https://www.feefo.com/en-GB/reviews/example", "Feefo")


def test_unrelated_brand_names_are_excluded():
    assert review.name_matches("B&M Stores Reviews - Read reviews", "B & M RETAIL LIMITED")
    assert not review.name_matches("B&M Care is rated Average", "B & M RETAIL LIMITED")
    assert not review.name_matches("B&M Flowers Reviews", "B & M RETAIL LIMITED")


@pytest.mark.asyncio
async def test_quota_failure_stops_and_does_not_claim_no_reviews(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, text="secret")

    client = httpx.AsyncClient
    monkeypatch.setattr(
        review.httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handle), **kw),
    )
    result = await review.company_review_search("Example", "12345678")
    assert len(calls) == 1
    assert result["providers"][0]["status"] == "failed"
    assert all(
        p["status"] == "skipped_after_auth_or_quota_failure" for p in result["providers"][1:]
    )
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "decision",
    [
        {"id": "r9", "sentiment": "positive", "quote": "Excellent customer service"},
        {"id": "r1", "sentiment": "positive", "quote": "fabricated customer feedback"},
        {
            "id": "r1",
            "sentiment": "positive",
            "quote": "Excellent customer service",
            "credit": "good",
        },
        {"id": "r1", "sentiment": "insufficient", "quote": "Excellent customer service"},
    ],
)
def test_model_cannot_invent_quote_id_or_credit_claim(decision):
    with pytest.raises(ValueError):
        review.validate_decisions(
            json.dumps({"items": [decision]}), {"r1": {"excerpt": "Excellent customer service"}}
        )


@pytest.mark.asyncio
async def test_model_attribution_and_rendering(monkeypatch):
    class Model:
        async def generate_content_async(self, request, stream):
            assert request.config.response_schema.model_json_schema()["type"] == "object"
            yield SimpleNamespace(
                error_code=None,
                content=SimpleNamespace(
                    parts=[SimpleNamespace(text=json.dumps({"selection": "positive|q1"}))]
                ),
            )

    monkeypatch.setattr(review, "agent_model", lambda: Model())
    search = {
        "providers": [
            {
                "provider": "Trustpilot",
                "status": "completed",
                "results": [
                    {
                        "url": "https://www.trustpilot.com/review/example",
                        "title": "Example ev_999",
                        "excerpt": "I received excellent customer service",
                        "sentiment": "insufficient",
                        "supporting_quote": "",
                        "analysis_status": "not_analysed",
                    }
                ],
            }
        ]
    }
    await review.analyse_review_snippets(search)
    assert search["providers"][0]["results"][0]["sentiment"] == "positive"
    bundle, groups = build_evidence(
        {"company_number": "12345678", "profile": {"company_name": "Example"}}, None, {}
    )
    review.add_review_evidence(bundle, groups, search)
    report = render_report(bundle, groups, [])
    assert "ev_999" not in report
    assert "Customer sentiment" in report and "I received excellent customer service" in report
    assert "No overall rating" in report


def test_platform_summary_is_not_customer_testimony():
    assert not review.customer_passages(
        "Customers frequently praise the helpful staff. 3.9 stars. We attract happy customers."
    )
    assert review.customer_passages("I was very impressed with my local store.")


@pytest.mark.parametrize(
    "status", ["aggregate_unavailable", "page_unavailable", "tavily_extraction_failed"]
)
def test_unavailable_rating_does_not_abort_report_or_publish_score(status):
    search = {
        "providers": [
            {
                "provider": "REVIEWS.io",
                "status": "completed",
                "results": [
                    {
                        "url": "https://www.reviews.io/company-reviews/store/example",
                        "title": "Example reviews",
                        "excerpt": "No verified customer feedback.",
                        "sentiment": "insufficient",
                        "supporting_quote": "",
                        "analysis_status": "not_analysed",
                        "aggregate": {
                            "status": status,
                            "url": "https://www.reviews.io/company-reviews/store/example",
                            "checked_at": "2026-11-13T21:00:00Z",
                            "reason": "Rating could not be verified.",
                            "entity_match": "brand_name_only",
                            "assessed_legal_name": "Example Limited",
                        },
                    }
                ],
            }
        ]
    }
    bundle, groups = build_evidence(
        {"company_number": "12345678", "profile": {"company_name": "Example"}}, None, {}
    )
    review.add_review_evidence(bundle, groups, search)
    report = render_report(bundle, groups, [])
    assert "Overall customer rating: unavailable. Rating could not be verified." in report
    assert len(groups["reviews"][0]["rating_ids"]) == 1
    assert "review_count" not in bundle.evidence[-2].structured_fact["platform_customer_rating"]

import asyncio
import json

import httpx
import pytest

from agent_ui import research_requests as research


@pytest.fixture
def budget(monkeypatch, tmp_path):
    monkeypatch.setenv("SEARCH_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    state = research.ResearchBudget(research.ResearchLimits(searches=2, extractions=1))
    token = research.active_budget.set(state)
    yield state
    research.active_budget.reset(token)


async def request(client, query="public company", operation="search", key="test-secret"):
    return await research.post_json(
        client,
        "https://api.tavily.com/" + operation,
        headers={"Authorization": "Bearer " + key},
        json={"query": query},
    )


@pytest.mark.asyncio
async def test_cache_hit_preserves_original_timestamp_and_does_not_spend_budget(budget):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://example.org", "content": "Public evidence"}],
                "usage": {"credits": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = (await request(client)).json()
        second = (await request(client)).json()
    assert len(calls) == budget.usage.searches == budget.usage.cache_hits == 1
    assert second["_research"]["cache_hit"]
    assert first["usage"]["credits"] == 1 and second["usage"]["credits"] == 0
    assert first["_research"]["retrieved_at"] == second["_research"]["retrieved_at"]
    assert b"test-secret" not in research.cache_path().read_bytes()


@pytest.mark.asyncio
async def test_request_limits_cover_search_and_extraction_separately(budget):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await request(client, "one")).status_code == 200
        assert (await request(client, "two")).status_code == 200
        assert (await request(client, "three")).status_code == 429
        assert (await request(client, operation="extract")).status_code == 200
        assert (await request(client, "another page", "extract")).status_code == 429
        assert (await request(client, "one")).status_code == 200
    assert len(calls) == 3 and budget.usage.requests_prevented == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 432, 433])
async def test_auth_and_quota_failure_stops_all_stages_without_caching_error(budget, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, text="provider response containing test-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await request(client)
        result = await request(client, "other", "extract")
    assert len(calls) == 1 and result.status_code == status
    assert "test-secret" not in json.dumps(research.usage_summary())
    assert "test-secret" not in result.text
    assert b"test-secret" not in research.cache_path().read_bytes()


@pytest.mark.asyncio
async def test_cache_expiry_retries_and_does_not_serve_stale_on_failure(budget, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(research.time, "time", lambda: clock[0])
    budget.limits.cache_seconds = 10
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"results": []}) if len(calls) == 1 else httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await request(client)
        clock[0] += 11
        assert (await request(client)).status_code == 503
    assert len(calls) == 2 and budget.usage.cache_hits == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_calls_share_cache_and_account_key_isolation(budget):
    calls = []

    async def handler(req):
        calls.append(req)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(request(client), request(client))
        await request(client, key="other-account")
    assert len(calls) == 2 and budget.usage.cache_hits == 1


@pytest.mark.asyncio
async def test_report_context_is_isolated_and_reset_on_failure(monkeypatch):
    monkeypatch.setenv("SEARCH_CACHE_TTL_SECONDS", "0")

    @research.research_budget
    async def run(fail=False):
        state = research.active_budget.get()
        state.usage.searches += 1
        await asyncio.sleep(0)
        if fail:
            raise RuntimeError("report failed")
        return research.usage_summary()

    results = await asyncio.gather(run(), run())
    assert all(r["searches"] == 1 for r in results)
    assert research.active_budget.get() is None
    with pytest.raises(RuntimeError):
        await run(True)
    assert research.active_budget.get() is None


@pytest.mark.asyncio
async def test_google_grounding_cannot_enter_reusable_cache(budget):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="not approved"):
            await research.post_json(
                client,
                "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent",
                headers={},
                json={},
            )

"""Report-scoped research budgets and expiring Tavily response caching.

Google grounded answers must not enter this reusable discovery cache.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import httpx
from pydantic import BaseModel, Field


class ResearchLimits(BaseModel):
    browser_runs: int = Field(default=2, ge=0, le=4)
    searches: int = Field(default=40, ge=0, le=100)
    extractions: int = Field(default=12, ge=0, le=40)
    cache_seconds: int = Field(default=3600, ge=0, le=86400)

    @classmethod
    def configured(cls):
        return cls(
            browser_runs=os.getenv("BROWSER_RESEARCH_MAX_RUNS", "2"),
            searches=os.getenv("SEARCH_MAX_REQUESTS_PER_REPORT", "40"),
            extractions=os.getenv("SEARCH_MAX_EXTRACT_REQUESTS_PER_REPORT", "12"),
            cache_seconds=os.getenv("SEARCH_CACHE_TTL_SECONDS", "3600"),
        )


class ResearchUsage(BaseModel):
    browser_runs: int = 0
    browser_steps: int = 0
    browser_outcomes: list[dict] = Field(default_factory=list)
    searches: int = 0
    extractions: int = 0
    cache_hits: int = 0
    requests_prevented: int = 0
    stop_reason: str | None = None


class ResearchBudget:
    def __init__(self, limits=None):
        self.limits = limits or ResearchLimits.configured()
        self.usage = ResearchUsage()
        self.blocked_status = None
        self.lock = asyncio.Lock()


active_budget = ContextVar("research_budget", default=None)


def research_budget(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        if active_budget.get() is not None:
            return await function(*args, **kwargs)
        token = active_budget.set(ResearchBudget())
        try:
            return await function(*args, **kwargs)
        finally:
            active_budget.reset(token)

    return wrapped


def usage_summary():
    budget = active_budget.get()
    return budget.usage.model_dump() if budget else {}


def cache_path():
    return Path(os.getenv("SEARCH_CACHE_PATH", ".data/research-cache.sqlite3"))


def cached_response(key, ttl, value=None):
    """Atomic writes, bounded storage, no credentials, and no stale-on-error fallback."""
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with sqlite3.connect(path, timeout=5) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, created REAL, body TEXT)"
        )
        db.execute("DELETE FROM responses WHERE created < ?", (now - 86400,))
        if value is not None:
            db.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (key, now, json.dumps(value))
            )
            db.execute(
                "DELETE FROM responses WHERE key NOT IN (SELECT key FROM responses ORDER BY created DESC LIMIT 1000)"
            )
            return None
        row = db.execute("SELECT created, body FROM responses WHERE key = ?", (key,)).fetchone()
        if row and 0 <= now - row[0] < ttl:
            try:
                return json.loads(row[1])
            except ValueError:
                db.execute("DELETE FROM responses WHERE key = ?", (key,))
    return None


async def post_json(client, url, *, headers, json):
    """Preserve HTTP semantics for callers; only charge outbound cache misses."""
    import json as codec

    budget = active_budget.get()
    if budget is None:
        # Standalone tools retain their existing bounded behaviour. Full report
        # entry points and integration tests establish a shared budget explicitly.
        return await client.post(url, headers=headers, json=json)
    operations = {
        "https://api.tavily.com/search": "searches",
        "https://api.tavily.com/extract": "extractions",
    }
    if url not in operations:
        raise ValueError("Provider is not approved for reusable research caching")
    operation = operations[url]
    request = httpx.Request("POST", url)
    fingerprint = hashlib.sha256(headers.get("Authorization", "").encode()).hexdigest()
    key = hashlib.sha256(
        codec.dumps(["research-v1", url, json, fingerprint], sort_keys=True).encode()
    ).hexdigest()
    # Review aggregates must not silently inherit the longer general search TTL.
    ttl = min(budget.limits.cache_seconds, 900 if operation == "extractions" else 86400)
    async with budget.lock:
        if ttl:
            try:
                data = await asyncio.to_thread(cached_response, key, ttl)
            except (OSError, sqlite3.Error):
                data = None
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                data.setdefault("_research", {})["cache_hit"] = True
                if isinstance(data.get("usage"), dict):
                    data["usage"]["credits"] = 0
                budget.usage.cache_hits += 1
                return httpx.Response(200, json=data, request=request)
        if budget.blocked_status or getattr(budget.usage, operation) >= getattr(
            budget.limits, operation
        ):
            budget.usage.requests_prevented += 1
            reason = (
                budget.usage.stop_reason
                if budget.blocked_status
                else f"Per-report {operation} limit reached"
            )
            budget.usage.stop_reason = reason
            return httpx.Response(
                budget.blocked_status or 429, json={"error": reason}, request=request
            )
        setattr(budget.usage, operation, getattr(budget.usage, operation) + 1)
        response = await client.post(url, headers=headers, json=json)
        if response.status_code in {401, 403, 429, 432, 433}:
            budget.blocked_status = response.status_code
            budget.usage.stop_reason = f"Tavily authentication/quota failure (HTTP {response.status_code}); further paid requests stopped"
        if response.status_code != 200 or len(response.content) > 5_000_000:
            return response
        try:
            data = response.json()
        except ValueError:
            return response
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            return response
        data["_research"] = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "cache_hit": False,
        }
        if ttl:
            try:
                await asyncio.to_thread(cached_response, key, ttl, data)
            except (OSError, sqlite3.Error):
                pass  # Cache availability never changes source validation.
        return httpx.Response(200, json=data, request=request)

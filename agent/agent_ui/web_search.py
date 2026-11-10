"""Bounded Tavily search tool; external snippets are candidate sources, not facts."""

import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from google.adk.tools import FunctionTool


async def company_web_search(company_name: str, company_number: str) -> dict:
    """Search public company background and reviews/news; at most two basic requests."""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        return {"status": "not_configured", "results": [], "searches": []}
    # Only authoritative public company identity is sent, never the user's report prompt.
    name = " ".join(company_name.replace('"', "").split())[:200]
    queries = [f'"{name}" "{company_number}" company', f'"{name}" UK reviews news']
    results, searches, seen = [], [], set()
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        for query in queries:
            record = {"query": query}
            try:
                response = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 2,
                        "include_answer": False,
                        "include_raw_content": False,
                        "auto_parameters": False,
                        "include_usage": True,
                    },
                )
                if response.status_code != 200:
                    record.update(status="failed", http_status=response.status_code)
                    searches.append(record)
                    # Avoid consuming more requests after auth/quota failures.
                    if response.status_code in {401, 403, 429, 432, 433}:
                        break
                    continue
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("Invalid search response")
                record["status"] = "completed"
                credits = (
                    data.get("usage", {}).get("credits")
                    if isinstance(data.get("usage"), dict)
                    else None
                )
                if type(credits) in (int, float) and credits >= 0:
                    record["credits"] = credits
                for item in data["results"][:2]:
                    if not isinstance(item, dict):
                        continue
                    url, snippet = item.get("url"), item.get("content")
                    if (
                        not isinstance(url, str)
                        or not isinstance(snippet, str)
                        or not snippet.strip()
                    ):
                        continue
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                    ):
                        continue
                    if url in seen:
                        continue
                    seen.add(url)
                    results.append(
                        {
                            "url": url,
                            "title": str(item.get("title", ""))[:160],
                            "excerpt": snippet[:600],
                            "excerpt_truncated": len(snippet) > 600,
                            "query": query,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "entity_match": "unverified",
                            "content_type": "search_snippet",
                        }
                    )
            except (httpx.HTTPError, ValueError):
                record["status"] = "failed"
            searches.append(record)
    completed = sum(s["status"] == "completed" for s in searches)
    return {
        "status": "completed" if completed == 2 else "partial" if completed else "failed",
        "results": results,
        "searches": searches,
    }


company_web_search_tool = FunctionTool(company_web_search)

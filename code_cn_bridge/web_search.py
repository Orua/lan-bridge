"""Configurable web search providers used by the admin UI and tool bridge."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .http_utils import make_async_client


class WebSearchError(Exception):
    """A safe-to-display web search configuration or connection error."""


async def search_web(provider: dict[str, Any], query: str) -> dict[str, Any]:
    """Execute a configured provider search and return normalized outcome details."""
    adapter = str(provider.get("adapter", "")).strip().lower()
    if adapter != "bocha":
        raise WebSearchError(f"Unsupported web search adapter: {adapter or '(empty)'}")
    return await _search_bocha(provider, query)


async def test_web_search_provider(provider: dict[str, Any], query: str) -> dict[str, Any]:
    """Run one small provider request and return a UI-friendly result summary."""
    adapter = str(provider.get("adapter", "")).strip().lower()
    if adapter != "bocha":
        raise WebSearchError(f"Unsupported web search adapter: {adapter or '(empty)'}")
    result = await _search_bocha(provider, query)
    if result["outcome"] != "ok":
        raise WebSearchError(result["message"])
    return {
        "status": "ok",
        "message": "Search connection succeeded",
        "elapsed_ms": result["elapsed_ms"],
        "result_count": result["result_count"],
        "results": result["results"][:3],
    }


# This is an admin API helper, not a pytest test case.  It is imported by the
# web-search settings tests, so explicitly prevent pytest from collecting it.
test_web_search_provider.__test__ = False


async def _search_bocha(provider: dict[str, Any], query: str) -> dict[str, Any]:
    api_key = str(provider.get("api_key", "")).strip()
    if not api_key and provider.get("api_key_env"):
        api_key = os.environ.get(str(provider["api_key_env"]), "").strip()
    if not api_key:
        raise WebSearchError("API Key is not configured")

    url = str(provider.get("base_url", "")).strip()
    if not url:
        raise WebSearchError("API address is not configured")

    body = {
        "query": query.strip() or "OpenAI",
        "freshness": provider.get("freshness", "noLimit"),
        "summary": bool(provider.get("summary", True)),
        "count": max(1, min(int(provider.get("max_results", 5)), 50)),
    }
    timeout = max(1, min(float(provider.get("timeout", 30)), 120))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    start = time.monotonic()
    try:
        async with make_async_client(timeout=httpx.Timeout(timeout)) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise WebSearchError("Connection timed out") from exc
    except httpx.HTTPError as exc:
        raise WebSearchError(f"Connection failed: {exc}") from exc

    elapsed = round((time.monotonic() - start) * 1000, 1)
    if response.status_code != 200:
        raise WebSearchError(f"HTTP {response.status_code}: {response.text[:200]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise WebSearchError("Provider returned invalid JSON") from exc

    provider_code = payload.get("code") if isinstance(payload, dict) else None
    provider_message = str(payload.get("msg") or payload.get("message") or "").strip() if isinstance(payload, dict) else ""
    if provider_code not in (None, 200, "200"):
        return {
            "elapsed_ms": elapsed,
            "result_count": 0,
            "results": [],
            "outcome": "rejected",
            "message": provider_message or f"Search provider rejected the query (code {provider_code})",
            "log_id": str(payload.get("log_id", "")),
        }

    search_data = payload.get("data", payload)
    if not isinstance(search_data, dict):
        raise WebSearchError("Provider returned an unsupported response structure")
    values = search_data.get("webPages", {}).get("value", [])
    if not isinstance(values, list):
        raise WebSearchError("Provider returned an unsupported web result structure")
    results = [
        {
            "title": str(item.get("name", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("snippet", "")),
        }
        for item in values
    ]
    return {
        "elapsed_ms": elapsed,
        "result_count": len(values),
        "results": results,
        "outcome": "ok",
        "message": "",
        "log_id": str(payload.get("log_id", "")) if isinstance(payload, dict) else "",
    }

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

from code_cn_bridge import native_proxy


def request():
    return Request({"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"", "headers": []})


def merge(payload, _config):
    return {"models": payload.get("models", []) + [{"slug": "deepseek-flash"}]}


def test_catalog_caches_success_and_keeps_it_on_transient_timeout():
    cfg = SimpleNamespace(model_mapping={"deepseek-flash": {"enabled": True}})
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol"}]})), aclose=AsyncMock())

    async def run():
        with patch.object(native_proxy, "_native_client", return_value=client), patch.object(native_proxy, "_host_catalog_client_version", return_value="0.161.0"), patch.object(native_proxy, "_native_base_url", return_value="https://example.com"), patch.object(native_proxy, "native_request_headers", return_value={}), patch.object(native_proxy, "merge_model_catalog", side_effect=merge):
            first = await native_proxy.fetch_merged_models(request(), cfg)
            second = await native_proxy.fetch_merged_models(request(), cfg)
            assert first.status_code == second.status_code == 200
            assert client.get.await_count == 1
            cfg._native_catalog_cache = (cfg._native_catalog_cache[0], cfg._native_catalog_cache[1] - 61, cfg._native_catalog_cache[2])
            client.get.side_effect = httpx.ReadTimeout("temporary")
            third = await native_proxy.fetch_merged_models(request(), cfg)
            assert third.status_code == 200
            assert {item["slug"] for item in json.loads(third.body)["models"]} == {"gpt-6.1-sol", "deepseek-flash"}

    asyncio.run(run())


def test_catalog_timeout_still_serves_configured_deepseek():
    cfg = SimpleNamespace(model_mapping={"deepseek-flash": {"enabled": True}})
    client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadTimeout("temporary")), aclose=AsyncMock())

    async def run():
        with patch.object(native_proxy, "_native_client", return_value=client), patch.object(native_proxy, "_host_catalog_client_version", return_value="0.161.0"), patch.object(native_proxy, "_native_base_url", return_value="https://example.com"), patch.object(native_proxy, "native_request_headers", return_value={}), patch.object(native_proxy, "merge_model_catalog", side_effect=merge):
            response = await native_proxy.fetch_merged_models(request(), cfg)
            assert response.status_code == 200
            assert json.loads(response.body)["data"][0]["id"] == "deepseek-flash"

    asyncio.run(run())

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request
from code_cn_bridge import native_proxy, catalog_store


def request(refresh=False):
    return Request({"type":"http", "method":"GET", "path":"/v1/models", "query_string":b"refresh=1" if refresh else b"", "headers":[]})


def merge(payload, _config):
    return {"models":payload.get("models", []) + [{"slug":"deepseek-flash"}]}


def config():
    return SimpleNamespace(model_mapping={"deepseek-flash":{"enabled":True}})


def response(*ids):
    return httpx.Response(200,json={"models":[{"slug":x} for x in ids]})


async def scenario(run, cfg=None):
    cfg=cfg or config()
    client=SimpleNamespace(get=AsyncMock(return_value=response("gpt-6.1-sol")),aclose=AsyncMock())
    with patch.object(native_proxy,"_native_client",return_value=client), patch.object(native_proxy,"_host_catalog_client_version",return_value="0.163.0"), patch.object(native_proxy,"_native_base_url",return_value="https://example.com"), patch.object(native_proxy,"native_request_headers",return_value={}) as headers, patch.object(native_proxy,"merge_model_catalog",side_effect=merge), patch.object(catalog_store,"RETRY_DELAY",0):
        await run(cfg,client,headers)


def ids(result):
    return {x["id"] for x in json.loads(result.body).get("data",[])}


def test_passthrough_accounts_do_not_share_persistent_catalog_scope():
    cfg = config()
    first = catalog_store.account_scope(cfg, "https://example.com/models", [],
                                        {"Authorization": "Bearer test-account-a"})
    second = catalog_store.account_scope(cfg, "https://example.com/models", [],
                                         {"Authorization": "Bearer test-account-b"})
    assert first != second
    assert "test-account" not in first + second


def test_malformed_persistent_snapshot_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LAN_BRIDGE_STATE_DIR", str(tmp_path))
    directory = tmp_path / "catalog"
    directory.mkdir()
    (directory / "scope.json").write_text("[]", encoding="utf-8")
    assert catalog_store.CatalogSnapshot("scope").payload is None


def test_snapshot_normal_reads_do_not_call_live_upstream():
    async def run(cfg,client,headers):
        first=await native_proxy.fetch_merged_models(request(),cfg)
        assert client.get.await_count==2
        second=await native_proxy.fetch_merged_models(request(),cfg)
        assert ids(first)==ids(second)=={"gpt-6.1-sol","deepseek-flash"}
        assert client.get.await_count==2
        assert json.loads(second.body)["native_catalog"]["source"]=="snapshot"
    asyncio.run(scenario(run))


def test_timeout_without_snapshot_is_503_not_partial_success():
    async def run(cfg,client,headers):
        client.get.side_effect=httpx.ReadTimeout("temporary")
        result=await native_proxy.fetch_merged_models(request(),cfg)
        assert result.status_code==503
        assert "data" not in json.loads(result.body)
        assert client.get.await_count==3
    asyncio.run(scenario(run))


import pytest
@pytest.mark.parametrize("failure",["timeout",401,403,429,500,"missing"])
def test_failed_or_missing_gpt_refresh_preserves_good_snapshot(failure):
    async def run(cfg,client,headers):
        original=await native_proxy.fetch_merged_models(request(),cfg)
        if failure=="timeout":client.get.side_effect=httpx.ReadTimeout("temporary")
        else:client.get.return_value=response("deepseek-only") if failure=="missing" else httpx.Response(failure)
        refreshed=await native_proxy.fetch_merged_models(request(True),cfg)
        assert refreshed.status_code==200 and ids(refreshed)==ids(original)
        assert json.loads(refreshed.body)["native_catalog"]["stale"] is True
        assert client.get.await_count==5 and headers.call_count==5
    asyncio.run(scenario(run))


def test_changed_catalog_needs_two_matching_observations():
    async def run(cfg,client,headers):
        await native_proxy.fetch_merged_models(request(),cfg)
        client.get.side_effect=[response("gpt-6.1-sol","gpt-a"),response("gpt-6.1-sol","gpt-b"),response("gpt-6.1-sol","gpt-b")]
        final=await native_proxy.fetch_merged_models(request(True),cfg)
        assert "gpt-b" in ids(final) and "gpt-a" not in ids(final)
    asyncio.run(scenario(run))


def test_snapshot_survives_restart_and_network_outage():
    async def run(cfg,client,headers):
        first=await native_proxy.fetch_merged_models(request(),cfg)
        assert list((catalog_store.state_root()/"catalog").glob("*.json"))
        fresh_config=config()
        client.get.side_effect=httpx.ReadTimeout("offline")
        result=await native_proxy.fetch_merged_models(request(),fresh_config)
        assert result.status_code==200 and ids(result)==ids(first)
        snapshot=next(iter(fresh_config._catalog_snapshots.values()))
        if snapshot.task:await snapshot.task
    asyncio.run(scenario(run))


def test_catalog_confirmation_and_retry_use_fresh_pool_leases():
    from code_cn_bridge import native_pool

    async def run():
        pool = native_pool.NativeClientPool(max_active=1)
        cfg = config()
        cfg.data = {"server": {}}
        cfg._native_http_pool = pool
        calls = []

        def upstream(request):
            calls.append(request)
            # Confirm the initial catalog, then retry a transient failure
            # before confirming the newly released model twice.
            if len(calls) == 3:
                return httpx.Response(503)
            models = ["gpt-6.1-sol"]
            if len(calls) > 3:
                models.append("gpt-6.1-terra")
            return response(*models)

        with patch.object(native_pool, "make_native_async_client", side_effect=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(upstream))), patch.object(native_proxy, "_host_catalog_client_version", return_value="0.164.0"), patch.object(native_proxy, "_native_base_url", return_value="https://example.com"), patch.object(native_proxy, "native_request_headers", return_value={}), patch.object(native_proxy, "merge_model_catalog", side_effect=merge), patch.object(catalog_store, "RETRY_DELAY", 0):
            try:
                first = await native_proxy.fetch_merged_models(request(), cfg)
                assert ids(first) == {"gpt-6.1-sol", "deepseek-flash"}
                updated = await native_proxy.fetch_merged_models(request(True), cfg)
                assert "gpt-6.1-terra" in ids(updated)
                assert json.loads(updated.body)["native_catalog"]["stale"] is False
                assert len(calls) == 5
                assert pool.snapshot()["active"] == 0
                assert pool.snapshot()["reserved"] == 0
            finally:
                await pool.close()

    asyncio.run(run())

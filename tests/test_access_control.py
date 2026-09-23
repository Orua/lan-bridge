import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from code_cn_bridge import admin_api, server
from code_cn_bridge.access_control import (
    ACCESS_KEY_PREFIX,
    AccessKeyStore,
    BridgeAccessError,
    authenticate_bridge_headers,
    get_access_key_store,
    public_access_key_record,
    require_model_access,
)
from code_cn_bridge.stats import RequestLog, StatsCollector


class FakeConfig:
    def __init__(self, store_path, *, enabled=True):
        self._data = {
            "access_control": {
                "enabled": enabled,
                "store_path": str(store_path),
            },
            "server": {},
        }
        self.server_host = "127.0.0.1"
        self.server_port = 8765
        self.native_models = {
            "gpt-5.6-sol": {
                "display_name": "GPT-5.6 Sol",
                "enabled": True,
            },
        }
        self.model_mapping = {
            "deepseek-v4-pro": {
                "display_name": "DeepSeek V4 Pro",
                "target": "deepseek-chat",
                "provider": "deepseek",
                "route_kind": "custom",
                "enabled": True,
            },
            "disabled-model": {
                "display_name": "Disabled",
                "target": "disabled",
                "provider": "deepseek",
                "enabled": False,
            },
        }
        self.model_slots = {}
        self.vision_routing = {}
        self.providers = {}
        self.save = Mock()

    @property
    def data(self):
        return self._data

    def get_provider(self, name):
        return self.providers.get(name)

    def slot_alias(self, slot_id):
        raise KeyError(slot_id)


def _bearer(raw_key):
    return {"authorization": f"Bearer {raw_key}"}


def _created_key(config, models=("gpt-5.6-sol",)):
    return get_access_key_store(config).create("test agent", list(models))


def test_key_store_persists_only_hash_and_public_record_never_exposes_verifier(tmp_path):
    store_path = tmp_path / "access-keys.json"
    store = AccessKeyStore(store_path)

    raw_key, record = store.create("graduation agent", ["gpt-5.6-sol"])
    disk_text = store_path.read_text(encoding="utf-8")
    public = public_access_key_record(record)

    assert raw_key.startswith(ACCESS_KEY_PREFIX)
    assert raw_key not in disk_text
    assert "key_hash" in json.loads(disk_text)["keys"][0]
    assert "key_hash" not in public
    assert "key" not in public


def test_rotate_invalidates_old_key_and_never_persists_either_plaintext(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    store = get_access_key_store(config)
    old_key, record = store.create("agent", ["gpt-5.6-sol"])

    new_key, rotated = store.rotate(record["id"])

    with pytest.raises(BridgeAccessError, match="Invalid") as old_error:
        authenticate_bridge_headers(_bearer(old_key), config)
    assert old_error.value.status_code == 401
    principal = authenticate_bridge_headers(_bearer(new_key), config)
    assert principal.key_id == record["id"]
    assert rotated["prefix"] != record["prefix"]
    disk_text = (tmp_path / "access-keys.json").read_text(encoding="utf-8")
    assert old_key not in disk_text
    assert new_key not in disk_text


def test_authentication_rejects_missing_wrong_disabled_and_empty_store(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    store = get_access_key_store(config)
    disabled_key, disabled = store.create("disabled", ["gpt-5.6-sol"])
    active_key, _ = store.create("active", ["gpt-5.6-sol"])
    store.update(disabled["id"], {"enabled": False})

    for headers in ({}, _bearer("lbk_wrong"), _bearer(disabled_key)):
        with pytest.raises(BridgeAccessError) as caught:
            authenticate_bridge_headers(headers, config)
        assert caught.value.status_code == 401

    assert authenticate_bridge_headers(_bearer(active_key), config).name == "active"

    empty = FakeConfig(tmp_path / "empty.json")
    with pytest.raises(BridgeAccessError, match="no enabled") as caught:
        authenticate_bridge_headers({}, empty)
    assert caught.value.status_code == 503


def test_existing_key_can_use_new_models_without_updating_permissions(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    restricted_key, _ = _created_key(config)
    wildcard_key, _ = get_access_key_store(config).create("all", ["*"])

    restricted = authenticate_bridge_headers(_bearer(restricted_key), config)
    require_model_access(restricted, "gpt-5.6-sol")
    require_model_access(restricted, "gpt-6-sol")
    assert restricted.allowed_models == ("*",)

    wildcard = authenticate_bridge_headers(_bearer(wildcard_key), config)
    require_model_access(wildcard, "deepseek-v4-pro")


def test_all_openai_compatible_http_endpoints_require_a_key(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    _created_key(config)
    endpoints = (
        ("get", "/v1/models", None),
        ("post", "/v1/responses", {"model": "gpt-5.6-sol", "input": []}),
        ("post", "/v1/responses/compact", {"model": "gpt-5.6-sol", "input": []}),
        ("post", "/v1/chat/completions", {"model": "deepseek-v4-pro", "messages": []}),
        ("post", "/v1/images/generations", {"model": "deepseek-v4-pro", "prompt": "x"}),
    )

    with patch.object(server, "get_config", return_value=config), patch(
        "code_cn_bridge.config.get_config", return_value=config
    ), patch.object(
        server, "_setup_logging"
    ):
        with TestClient(server.create_app()) as client:
            for method, path, body in endpoints:
                response = client.request(method, path, json=body)
                assert response.status_code == 401, (path, response.text)
                assert response.headers["www-authenticate"] == "Bearer"


def test_models_catalog_exposes_all_models_to_valid_key(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    raw_key, _ = _created_key(config)
    merged = JSONResponse({
        "object": "list",
        "models": [
            {"slug": "gpt-5.6-sol"},
            {"slug": "deepseek-v4-pro"},
        ],
        "data": [
            {"id": "gpt-5.6-sol", "object": "model"},
            {"id": "deepseek-v4-pro", "object": "model"},
        ],
    })

    with patch.object(server, "get_config", return_value=config), patch(
        "code_cn_bridge.config.get_config", return_value=config
    ), patch.object(
        server, "_setup_logging"
    ), patch.object(
        server, "fetch_merged_models", new=AsyncMock(return_value=merged)
    ):
        with TestClient(server.create_app()) as client:
            response = client.get("/v1/models", headers=_bearer(raw_key))

    assert response.status_code == 200
    assert [item["slug"] for item in response.json()["models"]] == ["gpt-5.6-sol", "deepseek-v4-pro"]
    assert [item["id"] for item in response.json()["data"]] == ["gpt-5.6-sol", "deepseek-v4-pro"]


def test_admin_crud_returns_plaintext_only_on_create_and_rotate(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    stats = StatsCollector(usage_dir=tmp_path / "usage")

    with patch.object(admin_api, "get_config", return_value=config), patch.object(
        admin_api, "get_stats", return_value=stats
    ):
        created = asyncio.run(admin_api.create_access_key({
            "name": "first agent",
            "allowed_models": ["gpt-5.6-sol"],
        }))
        raw_key = created["key"]
        key_id = created["record"]["id"]
        listed = asyncio.run(admin_api.list_access_keys())
        updated = asyncio.run(admin_api.update_access_key(key_id, {
            "name": "renamed",
            "allowed_models": ["deepseek-v4-pro"],
            "enabled": True,
        }))
        rotated = asyncio.run(admin_api.rotate_access_key(key_id))
        deleted = asyncio.run(admin_api.delete_access_key(key_id))
        listed_after_delete = asyncio.run(admin_api.list_access_keys())

    assert raw_key.startswith(ACCESS_KEY_PREFIX)
    assert "key_hash" not in created["record"]
    assert "key" not in listed["keys"][0]
    assert "key_hash" not in listed["keys"][0]
    assert updated["name"] == "renamed"
    assert created["record"]["allowed_models"] == ["*"]
    assert updated["allowed_models"] == ["*"]
    assert rotated["key"].startswith(ACCESS_KEY_PREFIX)
    assert rotated["key"] != raw_key
    assert "key_hash" not in rotated["record"]
    assert deleted == {"status": "ok"}
    assert listed_after_delete["keys"] == []
    assert {item["alias"] for item in listed["available_models"]} == {
        "gpt-5.6-sol",
        "deepseek-v4-pro",
    }


def test_admin_list_joins_per_key_usage_totals(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    raw_key, record = _created_key(config)
    stats = StatsCollector(usage_dir=tmp_path / "usage")
    stats.record(RequestLog(
        1,
        "gpt-5.6-sol",
        "responses",
        200,
        25,
        tokens=42,
        access_key_id=record["id"],
        access_key_prefix=record["prefix"],
    ))

    with patch.object(admin_api, "get_config", return_value=config), patch.object(
        admin_api, "get_stats", return_value=stats
    ):
        listed = asyncio.run(admin_api.list_access_keys())

    assert raw_key not in json.dumps(listed)
    assert listed["keys"][0]["request_count"] == 1
    assert listed["keys"][0]["total_tokens"] == 42
    assert listed["keys"][0]["last_used_at"]


def test_websocket_authenticates_connection_and_authorizes_every_frame(tmp_path):
    config = FakeConfig(tmp_path / "access-keys.json")
    raw_key, _ = _created_key(config)

    with patch.object(server, "get_config", return_value=config), patch(
        "code_cn_bridge.config.get_config", return_value=config
    ), patch.object(
        server, "_setup_logging"
    ):
        with TestClient(server.create_app()) as client:
            with pytest.raises(WebSocketDisconnect) as missing:
                with client.websocket_connect("/v1/responses"):
                    pass
            assert missing.value.code == 4401

            with client.websocket_connect(
                "/v1/responses", headers=_bearer(raw_key)
            ) as websocket:
                websocket.send_json({
                    "type": "response.create",
                    "model": "deepseek-v4-pro",
                    "generate": False,
                })
                assert websocket.receive_json()["type"] == "response.created"
                assert websocket.receive_json()["type"] == "response.completed"

                websocket.send_json({
                    "type": "response.create",
                    "model": "gpt-5.6-sol",
                    "generate": False,
                })
                assert websocket.receive_json()["type"] == "response.created"
                assert websocket.receive_json()["type"] == "response.completed"

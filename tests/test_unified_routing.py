import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from code_cn_bridge.adapters.deepseek import DeepSeekAdapter
from code_cn_bridge.client import UpstreamClient
from code_cn_bridge.http_utils import make_async_client
from code_cn_bridge.native_proxy import (
    _native_context_put,
    _prepare_native_responses_payload,
    NativeUpstreamHTTPError,
    _native_client,
    custom_model_info,
    custom_model_context_settings,
    fetch_merged_models,
    merge_model_catalog,
    native_request_headers,
    normalize_native_payload,
    proxy_native_responses,
    prepare_chat_responses_payload,
    prepare_provider_responses_payload,
)
from code_cn_bridge.provider_proxy import (
    model_uses_responses,
    provider_uses_responses,
    proxy_provider_responses,
)
from code_cn_bridge.routing import resolve_route
from code_cn_bridge.codex_auth import NativeCredentials
from code_cn_bridge.access_control import ANONYMOUS_PRINCIPAL
from code_cn_bridge import server


class FakeConfig:
    def __init__(self):
        self._data = {
            "access_control": {"enabled": False},
            "server": {
                "native_codex_base_url": "https://chatgpt.com/backend-api/codex",
                "native_auth_injection": {"enabled": True},
            },
        }
        self.providers = {
            "deepseek": {
                "adapter": "deepseek",
                "api_key": "THIRD_PARTY_KEY",
                "timeout": 30,
                "wire_api": "chat",
            },
        }
        self.model_mapping = {
            "deepseek-v4-pro": {
                "display_name": "DeepSeek V4 Pro",
                "target": "deepseek-chat",
                "provider": "deepseek",
                "route_kind": "custom",
                "enabled": True,
                "capabilities": {"reasoning": True, "context_window": 128000},
            },
        }
        self.native_models = {
            "gpt-5.6-sol": {
                "display_name": "GPT-5.6 Sol",
                "enabled": True,
                "capabilities": {"vision": True, "image_generation": True},
            },
        }
        self.model_slots = {}
        self.vision_routing = {}

    @property
    def data(self):
        return self._data

    def resolve_model(self, model):
        entry = self.model_mapping.get(model)
        if entry:
            return entry["provider"], entry["target"]
        return "unknown", model

    def get_provider(self, name):
        return self.providers.get(name)


class UnifiedRoutingTests(unittest.TestCase):
    def setUp(self):
        credentials = NativeCredentials(
            access_token="HOST_OPENAI_OAUTH",
            account_id="account-host",
        )
        native_credentials = patch(
            "code_cn_bridge.native_proxy.load_native_credentials",
            return_value=credentials,
        )
        native_credentials.start()
        self.addCleanup(native_credentials.stop)
        bridge_auth = patch(
            "code_cn_bridge.middleware.authenticate_bridge_headers",
            return_value=ANONYMOUS_PRINCIPAL,
        )
        bridge_auth.start()
        self.addCleanup(bridge_auth.stop)

    def test_explicit_custom_mapping_wins_over_gpt_prefix(self):
        cfg = FakeConfig()
        cfg.model_mapping["gpt-custom"] = {
            "target": "deepseek-chat",
            "provider": "deepseek",
            "route_kind": "custom",
            "enabled": True,
        }

        route = resolve_route(cfg, "gpt-custom")

        self.assertEqual(route.kind, "custom")
        self.assertEqual(route.provider, "deepseek")

    def test_native_route_is_selected_by_exact_model_alias(self):
        route = resolve_route(FakeConfig(), "gpt-5.6-sol")

        self.assertEqual(route.kind, "native_codex")
        self.assertEqual(route.auth_mode, "host_login")

    def test_native_image_generation_request_bypasses_image_slot(self):
        cfg = FakeConfig()
        cfg.slot_alias = lambda slot_id: self.fail(
            f"native ChatGPT request consulted capability slot: {slot_id}"
        )
        native_response = JSONResponse({"id": "resp_image", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(
                server,
                "proxy_native_responses",
                new=AsyncMock(return_value=native_response),
            ) as proxy,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-sol",
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "生成一张蓝色小鸟图片"}],
                    }],
                    "tools": [{"type": "image_generation"}],
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        forwarded = proxy.await_args.args[1]
        self.assertEqual(forwarded["tools"], [{"type": "image_generation"}])
        self.assertEqual(forwarded["model"], "gpt-5.6-sol")

    def test_native_chatgpt_without_capability_flags_defaults_to_hosted_image(self):
        cfg = FakeConfig()
        cfg.native_models["gpt-5.6-sol"].pop("capabilities", None)
        cfg.slot_alias = lambda slot_id: self.fail(
            f"native ChatGPT request consulted fallback slot: {slot_id}"
        )
        native_response = JSONResponse({"id": "resp_image", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(
                server,
                "proxy_native_responses",
                new=AsyncMock(return_value=native_response),
            ) as proxy,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-sol",
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "生成一张蓝色小鸟图片"}],
                    }],
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy.await_args.args[1]["tools"], [{"type": "image_generation"}])

    def test_non_chatgpt_without_image_capability_keeps_fallback_route(self):
        cfg = FakeConfig()
        route = resolve_route(cfg, "deepseek-v4-pro")
        body = {
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "生成一张蓝色小鸟图片"}],
            }],
        }

        self.assertFalse(server._ensure_native_image_generation_tool(body, route))
        self.assertNotIn("tools", body)

    def test_native_image_tool_remains_available_after_an_agent_tool_round(self):
        cfg = FakeConfig()
        cfg.native_models["gpt-5.6-sol"].pop("capabilities", None)
        route = resolve_route(cfg, "gpt-5.6-sol")
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "生成一张蓝色小鸟图片"}],
                },
                {"type": "custom_tool_call", "name": "read_file", "call_id": "call_1", "input": "{}"},
                {"type": "custom_tool_call_output", "call_id": "call_1", "output": "instructions"},
            ],
        }

        self.assertTrue(server._ensure_native_image_generation_tool(body, route))
        self.assertEqual(body["tools"], [{"type": "image_generation"}])

    def test_native_image_edit_request_bypasses_vision_and_image_slots(self):
        cfg = FakeConfig()
        cfg.slot_alias = lambda slot_id: self.fail(
            f"native ChatGPT request consulted capability slot: {slot_id}"
        )
        native_response = JSONResponse({"id": "resp_edit", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(
                server,
                "proxy_native_responses",
                new=AsyncMock(return_value=native_response),
            ) as proxy,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-sol",
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "把背景改成白色"},
                            {"type": "input_image", "image_url": "data:image/png;base64,c291cmNl"},
                        ],
                    }],
                    "tools": [{"type": "image_generation"}],
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        forwarded = proxy.await_args.args[1]
        content = forwarded["input"][0]["content"]
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(forwarded["tools"], [{"type": "image_generation"}])

    def test_native_capable_route_replaces_client_image_tool_with_hosted_tool(self):
        cfg = FakeConfig()
        native_response = JSONResponse({"id": "resp_image", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(
                server,
                "proxy_native_responses",
                new=AsyncMock(return_value=native_response),
            ) as proxy,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-sol",
                    "input": [
                        {
                            "type": "additional_tools",
                            "tools": [
                                {
                                    "type": "custom",
                                    "name": "image_gen__imagegen",
                                    "description": "Generate or edit an image",
                                },
                                {"type": "custom", "name": "local_executor"},
                            ],
                        },
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "生成一张蓝色小鸟图片"}],
                        },
                    ],
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        forwarded = proxy.await_args.args[1]
        self.assertEqual(forwarded["tools"], [{"type": "image_generation"}])
        self.assertEqual(
            forwarded["input"][0]["tools"],
            [{"type": "custom", "name": "local_executor"}],
        )

    def test_agent_imagegen_namespace_is_replaced_for_native_model(self):
        cfg = FakeConfig()
        route = resolve_route(cfg, "gpt-5.6-sol")
        body = {
            "input": [{
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "namespace": "mcp__goldenluck_imagegen",
                        "tools": [
                            {"type": "custom", "name": "mcp__goldenluck_imagegen__generate_image"},
                            {"type": "custom", "name": "mcp__goldenluck_imagegen__edit_image"},
                        ],
                    },
                    {"type": "custom", "name": "local_executor"},
                ],
            }],
        }

        replaced = server._replace_client_image_tools_with_hosted(body, route)

        self.assertEqual(replaced, 1)
        self.assertEqual(body["tools"], [{"type": "image_generation"}])
        self.assertEqual(
            body["input"][0]["tools"],
            [{"type": "custom", "name": "local_executor"}],
        )

    def test_client_image_tool_is_preserved_for_model_without_hosted_capability(self):
        cfg = FakeConfig()
        route = resolve_route(cfg, "deepseek-v4-pro")
        body = {
            "input": [{
                "type": "additional_tools",
                "tools": [{
                    "type": "function",
                    "function": {"name": "image_gen__imagegen"},
                }],
            }],
        }

        replaced = server._replace_client_image_tools_with_hosted(body, route)

        self.assertEqual(replaced, 0)
        self.assertNotIn("tools", body)
        self.assertEqual(
            body["input"][0]["tools"][0]["function"]["name"],
            "image_gen__imagegen",
        )

    def test_media_routing_uses_slots_only_for_missing_model_capabilities(self):
        cfg = FakeConfig()
        cfg.model_mapping.update({
            "custom-capable": {
                "target": "custom-capable-target",
                "provider": "deepseek",
                "capabilities": {"vision": True, "image_generation": True},
            },
            "vision-fallback": {
                "target": "vision-target",
                "provider": "vision-provider",
                "is_multimodal": True,
            },
            "image-fallback": {
                "target": "image-target",
                "provider": "image-provider",
                "is_image_gen": True,
            },
        })
        cfg.slot_alias = lambda slot_id: {
            "vision": "vision-fallback",
            "image_gen": "image-fallback",
        }[slot_id]
        capable_route = resolve_route(cfg, "custom-capable")
        image_body = {
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "edit this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,c291cmNl"},
                ],
            }],
            "tools": [{"type": "image_generation"}],
        }

        self.assertEqual(
            server._configured_model_dependency_aliases(
                cfg, "custom-capable", image_body, "responses", route=capable_route
            ),
            [],
        )
        self.assertFalse(server._requires_media_routing(image_body, capable_route))

        cfg.model_mapping["custom-capable"]["capabilities"] = {
            "vision": False,
            "image_generation": False,
        }
        incapable_route = resolve_route(cfg, "custom-capable")
        self.assertEqual(
            server._configured_model_dependency_aliases(
                cfg, "custom-capable", image_body, "responses", route=incapable_route
            ),
            ["image-fallback"],
        )
        self.assertTrue(server._requires_media_routing(image_body, incapable_route))

        cfg.model_mapping["custom-capable"]["capabilities"] = {
            "vision": False,
            "image_generation": True,
        }
        edit_capable_route = resolve_route(cfg, "custom-capable")
        self.assertEqual(
            server._configured_model_dependency_aliases(
                cfg, "custom-capable", image_body, "responses", route=edit_capable_route
            ),
            [],
        )
        self.assertFalse(server._requires_media_routing(image_body, edit_capable_route))

        vision_body = {
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is in this image?"},
                    {"type": "input_image", "image_url": "data:image/png;base64,c291cmNl"},
                ],
            }],
        }
        self.assertEqual(
            server._configured_model_dependency_aliases(
                cfg, "custom-capable", vision_body, "responses", route=edit_capable_route
            ),
            ["vision-fallback"],
        )
        self.assertTrue(server._requires_media_routing(vision_body, edit_capable_route))

    def test_native_header_allowlist_injects_host_credentials(self):
        config = FakeConfig()
        headers = native_request_headers({
            "authorization": "Bearer LAN_BRIDGE_KEY",
            "chatgpt-account-id": "account-client",
            "x-oai-attestation": "attestation",
            "cookie": "must-not-forward",
            "x-random-secret": "must-not-forward",
        }, config)

        self.assertEqual(headers["authorization"], "Bearer HOST_OPENAI_OAUTH")
        self.assertEqual(headers["chatgpt-account-id"], "account-host")
        self.assertNotIn("x-oai-attestation", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("x-random-secret", headers)

    def test_native_client_uses_only_official_proxy_setting(self):
        cfg = FakeConfig()
        cfg._data["server"]["codex_official_proxy_url"] = "http://127.0.0.1:19828"
        with patch("code_cn_bridge.native_proxy.make_native_async_client") as make_client:
            _native_client(cfg, httpx.Timeout(10))

        self.assertEqual(make_client.call_args.kwargs["proxy_url"], "http://127.0.0.1:19828")

    def test_non_openai_native_base_url_cannot_use_official_proxy(self):
        cfg = FakeConfig()
        cfg._data["server"]["codex_official_proxy_url"] = "http://127.0.0.1:19828"
        cfg._data["server"]["native_codex_base_url"] = "https://api.deepseek.com/v1"
        with patch("code_cn_bridge.native_proxy.make_native_async_client") as make_client:
            _native_client(cfg, httpx.Timeout(10))

        self.assertEqual(make_client.call_args.kwargs["proxy_url"], "")

    def test_custom_http_clients_ignore_environment_vpn_proxies(self):
        with (
            patch.dict(os.environ, {
                "HTTP_PROXY": "http://127.0.0.1:19001",
                "HTTPS_PROXY": "http://127.0.0.1:19002",
                "ALL_PROXY": "socks5://127.0.0.1:19003",
            }),
            patch("code_cn_bridge.http_utils.httpx.AsyncClient") as client_class,
        ):
            make_async_client(timeout=30)

        kwargs = client_class.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertNotIn("proxy", kwargs)

    def test_models_proxy_supplies_default_client_version(self):
        observed: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["client_version"] = request.url.params.get("client_version", "")
            return httpx.Response(200, json={"models": []})

        request = Request({
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/models",
            "raw_path": b"/v1/models",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        })
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with patch("code_cn_bridge.native_proxy._native_client", return_value=client):
            response = asyncio.run(fetch_merged_models(request, FakeConfig()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["client_version"], "0.0.0")

    def test_models_proxy_also_returns_openai_compatible_model_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": [{
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6 Sol",
                "visibility": "list",
                "supported_in_api": True,
            }]})

        request = Request({
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/models",
            "raw_path": b"/v1/models",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        })
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with patch("code_cn_bridge.native_proxy._native_client", return_value=client):
            response = asyncio.run(fetch_merged_models(request, FakeConfig()))

        payload = json.loads(response.body)
        self.assertEqual(payload["object"], "list")
        data_by_id = {item["id"]: item for item in payload["data"]}
        self.assertEqual(data_by_id["gpt-5.6-sol"], {
            "id": "gpt-5.6-sol",
            "object": "model",
            "created": 0,
            "owned_by": "lan-bridge",
        })
        self.assertIn("gpt-5.6-sol", [model["slug"] for model in payload["models"]])

    def test_native_payload_drops_non_native_encrypted_content_and_previous_id(self):
        payload = normalize_native_payload({
            "model": "alias",
            "previous_response_id": "third-party-response",
            "input": [{
                "type": "reasoning",
                "encrypted_content": "third-party-plain-reasoning",
                "summary": [{"type": "summary_text", "text": "kept"}],
            }],
        }, "gpt-5.6-sol")

        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertIn("kept", payload["input"][0]["content"][0]["text"])
        self.assertNotIn("rs_", json.dumps(payload))

    def test_native_compaction_payload_preserves_previous_response_id(self):
        payload = normalize_native_payload(
            {
                "model": "alias",
                "previous_response_id": "resp_native_previous",
                "input": [],
            },
            "gpt-5.6-sol",
            preserve_previous_response_id=True,
        )

        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["previous_response_id"], "resp_native_previous")

    def test_native_proxy_drops_continuation_id_and_replays_complete_tool_round_trip(self):
        captured = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_next", "output": []})

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"authorization", b"Bearer OPENAI_OAUTH")],
        })
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.native_proxy._native_client", return_value=client):
            response = asyncio.run(proxy_native_responses(
                request,
                {
                    "model": "gpt-5.6-sol",
                    "previous_response_id": "resp_previous_native",
                    "input": [
                        {
                            "type": "custom_tool_call",
                            "call_id": "call_native",
                            "name": "shell_command",
                            "input": "Get-Date",
                        },
                        {
                            "type": "custom_tool_call_output",
                            "call_id": "call_native",
                            "output": "tool result",
                        },
                    ],
                },
                "gpt-5.6-sol",
                FakeConfig(),
            ))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("previous_response_id", captured[0])
        self.assertEqual(captured[0]["input"][0]["type"], "custom_tool_call")
        self.assertEqual(captured[0]["input"][1]["type"], "custom_tool_call_output")

    def test_native_proxy_repairs_orphan_tool_output_before_first_request(self):
        captured = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_recovered", "output": []})

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"authorization", b"Bearer OPENAI_OAUTH")],
        })
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.native_proxy._native_client", return_value=client):
            response = asyncio.run(proxy_native_responses(
                request,
                {
                    "model": "gpt-5.6-sol",
                    "previous_response_id": "resp_third_party",
                    "input": [{
                        "type": "custom_tool_call_output",
                        "call_id": "call_old",
                        "output": "recoverable output",
                    }],
                },
                "gpt-5.6-sol",
                FakeConfig(),
            ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("previous_response_id", captured[0])
        self.assertEqual(captured[0]["input"][0]["type"], "message")

    def test_native_context_cache_expands_incremental_tool_continuation(self):
        response_id = "resp_cached_tool_round_trip"
        _native_context_put(
            response_id,
            [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "remember BRIDGE-MARKER"}],
            }],
            [{
                "type": "custom_tool_call",
                "call_id": "call_cached",
                "name": "shell_command",
                "input": "Get-Date",
            }],
        )

        payload = _prepare_native_responses_payload({
            "model": "gpt-5.6-sol",
            "previous_response_id": response_id,
            "input": [{
                "type": "custom_tool_call_output",
                "call_id": "call_cached",
                "output": "2026-08-11 12:34:56",
            }],
        }, "gpt-5.6-sol")

        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(
            [item["type"] for item in payload["input"]],
            ["message", "custom_tool_call", "custom_tool_call_output"],
        )
        self.assertIn("BRIDGE-MARKER", json.dumps(payload, ensure_ascii=False))

    def test_native_stream_cache_collects_output_item_done_events(self):
        response_id = "resp_streamed_cache_entry"
        sse = "\n".join([
            f'data: {json.dumps({"type": "response.created", "response": {"id": response_id}})}',
            f'data: {json.dumps({"type": "response.output_item.done", "item": {"type": "custom_tool_call", "call_id": "call_streamed", "name": "shell_command", "input": "Get-Date"}})}',
            f'data: {json.dumps({"type": "response.completed", "response": {"id": response_id, "output": []}})}',
            "data: [DONE]",
            "",
        ])

        class SseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse.encode()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=SseStream(),
                headers={"content-type": "text/event-stream"},
            )

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"authorization", b"Bearer OPENAI_OAUTH")],
        })

        async def run_proxy() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch("code_cn_bridge.native_proxy._native_client", return_value=client):
                response = await proxy_native_responses(
                    request,
                    {
                        "model": "gpt-5.6-sol",
                        "stream": True,
                        "input": [{
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "stream marker"}],
                        }],
                    },
                    "gpt-5.6-sol",
                    FakeConfig(),
                )
                async for _ in response.body_iterator:
                    pass

        asyncio.run(run_proxy())
        payload = _prepare_native_responses_payload({
            "model": "gpt-5.6-sol",
            "previous_response_id": response_id,
            "input": [{
                "type": "custom_tool_call_output",
                "call_id": "call_streamed",
                "output": "streamed result",
            }],
        }, "gpt-5.6-sol")

        self.assertEqual(
            [item["type"] for item in payload["input"]],
            ["message", "custom_tool_call", "custom_tool_call_output"],
        )

    def test_native_payload_preserves_native_ciphertext_and_recovers_agent_text(self):
        native_token = "gAAAAA" + "A" * 96
        payload = normalize_native_payload({
            "input": [
                {"type": "reasoning", "encrypted_content": native_token, "summary": []},
                {"type": "agent_message", "content": [
                    {"type": "encrypted_content", "encrypted_content": "custom agent handoff"},
                ]},
            ],
        }, "gpt-5.6-sol")

        self.assertEqual(payload["input"][0]["encrypted_content"], native_token)
        self.assertEqual(payload["input"][1]["content"], [
            {"type": "input_text", "text": "custom agent handoff"},
        ])

    def test_native_payload_preserves_paired_custom_tool_items(self):
        tool_call = {
            "type": "custom_tool_call",
            "call_id": "call_paired",
            "name": "exec",
            "input": "run test",
        }
        tool_output = {
            "type": "custom_tool_call_output",
            "call_id": "call_paired",
            "output": [{"type": "input_text", "text": "test passed"}],
        }

        payload = normalize_native_payload({
            "input": [tool_call, tool_output],
        }, "gpt-5.6-sol")

        self.assertEqual(payload["input"], [tool_call, tool_output])

    def test_native_payload_normalizes_routed_function_call_item_id(self):
        tool_call = {
            "type": "function_call",
            "id": "call_00_KF9yHnRCYOuYHiwvQlul0861",
            "call_id": "call_00_KF9yHnRCYOuYHiwvQlul0861",
            "name": "shell_command",
            "arguments": "{}",
        }
        tool_output = {
            "type": "function_call_output",
            "id": "fco_routed_output",
            "call_id": "call_00_KF9yHnRCYOuYHiwvQlul0861",
            "output": "test passed",
        }

        payload = normalize_native_payload({
            "input": [tool_call, tool_output],
        }, "gpt-5.6-sol")

        normalized_call = payload["input"][0]
        self.assertTrue(normalized_call["id"].startswith("fc_"))
        self.assertNotEqual(normalized_call["id"], tool_call["id"])
        self.assertEqual(normalized_call["call_id"], tool_call["call_id"])
        self.assertEqual(payload["input"][1], tool_output)

        repeated = normalize_native_payload({
            "input": [tool_call, tool_output],
        }, "gpt-5.6-sol")
        self.assertEqual(repeated["input"][0]["id"], normalized_call["id"])

    def test_native_payload_preserves_official_function_call_item_id(self):
        payload = normalize_native_payload({
            "input": [{
                "type": "function_call",
                "id": "fc_native_item",
                "call_id": "call_native_item",
                "name": "shell_command",
                "arguments": "{}",
            }],
        }, "gpt-5.6-sol")

        self.assertEqual(payload["input"][0]["id"], "fc_native_item")

    def test_native_payload_normalizes_routed_custom_tool_call_item_id(self):
        tool_call = {
            "type": "custom_tool_call",
            "id": "call_00_kfwsaaOg2yUIEeaOtqZd2488",
            "call_id": "call_00_kfwsaaOg2yUIEeaOtqZd2488",
            "name": "exec",
            "input": "Get-Date",
        }
        tool_output = {
            "type": "custom_tool_call_output",
            "call_id": "call_00_kfwsaaOg2yUIEeaOtqZd2488",
            "output": "ok",
        }

        payload = normalize_native_payload({
            "input": [tool_call, tool_output],
        }, "gpt-5.6-luna")

        normalized_call = payload["input"][0]
        self.assertTrue(normalized_call["id"].startswith("ctc_"))
        self.assertEqual(normalized_call["call_id"], tool_call["call_id"])
        self.assertEqual(payload["input"][1], tool_output)

    def test_native_payload_preserves_official_custom_tool_call_item_id(self):
        payload = normalize_native_payload({
            "input": [{
                "type": "custom_tool_call",
                "id": "ctc_native_item",
                "call_id": "call_native_item",
                "name": "exec",
                "input": "Get-Date",
            }],
        }, "gpt-5.6-luna")

        self.assertEqual(payload["input"][0]["id"], "ctc_native_item")

    def test_native_payload_normalizes_legacy_tool_search_call_item_id(self):
        payload = normalize_native_payload({
            "input": [{
                "type": "tool_search_call",
                "id": "ts_4c6fa38d67214cd0b35dd449",
                "call_id": "call_search",
                "arguments": {"query": "model switching"},
            }],
        }, "gpt-5.6-terra")

        item_id = payload["input"][0]["id"]
        self.assertTrue(item_id.startswith("tsc_"))
        self.assertNotEqual(item_id, "ts_4c6fa38d67214cd0b35dd449")

    def test_native_payload_preserves_official_tool_search_call_item_id(self):
        payload = normalize_native_payload({
            "input": [{
                "type": "tool_search_call",
                "id": "tsc_native_item",
                "call_id": "call_search",
                "arguments": {"query": "model switching"},
            }],
        }, "gpt-5.6-terra")

        self.assertEqual(payload["input"][0]["id"], "tsc_native_item")

    def test_native_payload_converts_orphan_tool_output_to_context(self):
        payload = normalize_native_payload({
            "input": [{
                "type": "custom_tool_call_output",
                "call_id": "call_missing",
                "output": [{"type": "input_text", "text": "existing session result"}],
            }],
        }, "gpt-5.6-sol")

        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertEqual(payload["input"][0]["role"], "user")
        self.assertIn("existing session result", payload["input"][0]["content"][0]["text"])
        self.assertNotIn("custom_tool_call_output", json.dumps(payload["input"]))

    def test_native_payload_converts_tool_output_without_an_id(self):
        payload = normalize_native_payload({
            "input": [{"type": "function_call_output", "output": "missing id result"}],
        }, "gpt-5.6-sol")

        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertIn("missing id result", payload["input"][0]["content"][0]["text"])

    def test_catalog_merges_custom_text_models_without_generation_models(self):
        cfg = FakeConfig()
        cfg.model_mapping["seedream"] = {
            "target": "seedream",
            "provider": "doubao",
            "enabled": True,
            "is_image_gen": True,
        }
        native = {"models": [{"slug": "gpt-5.6-sol", "priority": 1}]}

        merged = merge_model_catalog(native, cfg)
        slugs = [model["slug"] for model in merged["models"]]

        self.assertEqual(slugs, ["gpt-5.6-sol", "deepseek-v4-pro"])
        custom = next(model for model in merged["models"] if model["slug"] == "deepseek-v4-pro")
        self.assertEqual(custom["context_window"], 128000)
        self.assertEqual(custom["apply_patch_tool_type"], "freeform")
        self.assertTrue(custom["base_instructions"])
        self.assertIn("availability_nux", custom)
        self.assertIn("model_messages", custom)
        self.assertEqual(custom["tool_mode"], "direct")
        self.assertTrue(custom["supports_reasoning_summaries"])

    def test_catalog_forces_native_models_to_full_http_responses(self):
        cfg = FakeConfig()
        native = {"models": [{
            "slug": "gpt-5.6-sol",
            "priority": 1,
            "context_window": 196000,
            "max_context_window": 196000,
            "auto_compact_token_limit": 175000,
            "prefer_websockets": True,
            "use_responses_lite": True,
        }]}

        merged = merge_model_catalog(native, cfg)

        official = next(model for model in merged["models"] if model["slug"] == "gpt-5.6-sol")
        self.assertFalse(official["prefer_websockets"])
        self.assertFalse(official["use_responses_lite"])
        self.assertIn("supports_reasoning_summaries", official)
        self.assertEqual(official["context_window"], 196000)
        self.assertEqual(official["max_context_window"], 196000)
        self.assertEqual(official["auto_compact_token_limit"], 175000)
        self.assertTrue(native["models"][0]["prefer_websockets"])
        self.assertTrue(native["models"][0]["use_responses_lite"])

    def test_catalog_normalizes_unsupported_max_reasoning_effort(self):
        cfg = FakeConfig()
        native = {"models": [{
            "slug": "gpt-native",
            "default_reasoning_level": "max",
            "supported_reasoning_levels": [
                {"effort": "high", "description": "High"},
                {"effort": "max", "description": "Maximum"},
            ],
        }]}

        merged = merge_model_catalog(native, cfg)
        model = next(item for item in merged["models"] if item["slug"] == "gpt-native")

        self.assertEqual(model["default_reasoning_level"], "xhigh")
        self.assertEqual(
            [item["effort"] for item in model["supported_reasoning_levels"]],
            ["high", "xhigh"],
        )
        self.assertEqual(native["models"][0]["default_reasoning_level"], "max")

    def test_custom_model_context_defaults_are_deterministic_by_family(self):
        deepseek = custom_model_context_settings("deepseek-v4-pro", {
            "provider": "deepseek",
            "target": "deepseek-v4-pro",
        })
        qwen = custom_model_context_settings("qwen3.7-plus", {
            "provider": "aliyun",
            "target": "qwen3.7-plus",
        })
        generic = custom_model_context_settings("local-model", {
            "provider": "custom",
            "target": "local-model",
        })

        self.assertEqual((deepseek["context_window"], deepseek["auto_compact_token_limit"]), (1048576, 900000))
        self.assertEqual((qwen["context_window"], qwen["auto_compact_token_limit"]), (262144, 235000))
        self.assertEqual((generic["context_window"], generic["auto_compact_token_limit"]), (131072, 110000))

    def test_custom_model_context_overrides_and_scales_blank_compact_limit(self):
        overridden = custom_model_context_settings("deepseek-v4-pro", {
            "provider": "deepseek",
            "capabilities": {
                "context_window": 512000,
                "auto_compact_token_limit": 420000,
            },
        })
        scaled = custom_model_context_settings("qwen3.7-plus", {
            "provider": "aliyun",
            "target": "qwen3.7-plus",
            "capabilities": {"context_window": 131072},
        })

        self.assertEqual(overridden["context_window"], 512000)
        self.assertEqual(overridden["auto_compact_token_limit"], 420000)
        self.assertEqual(scaled["context_window"], 131072)
        self.assertEqual(scaled["auto_compact_token_limit"], 117500)

    def test_native_endpoint_bypasses_custom_translation_pipeline(self):
        cfg = FakeConfig()
        native_response = JSONResponse({"id": "resp_native", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "proxy_native_responses", new=AsyncMock(return_value=native_response)) as proxy,
            patch.object(server, "_route_vision") as route_vision,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={"model": "gpt-5.6-sol", "input": "hello", "stream": False},
                headers={"authorization": "Bearer OPENAI_OAUTH"},
            )

        self.assertEqual(response.status_code, 200)
        proxy.assert_awaited_once()
        self.assertEqual(proxy.await_args.args[1]["input"], [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }])
        route_vision.assert_not_called()

    def test_deepseek_responses_endpoint_bypasses_chat_translation(self):
        cfg = FakeConfig()
        cfg.providers["deepseek"]["wire_api"] = "responses"
        provider_response = JSONResponse({"id": "deepseek-response", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "proxy_provider_responses", new=AsyncMock(return_value=provider_response)) as proxy,
            patch.object(server, "translate_request") as translate,
            patch.object(server, "_route_vision") as route_vision,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={"model": "deepseek-v4-pro", "input": "hello", "stream": False},
                headers={"authorization": "Bearer OPENAI_OAUTH"},
            )

        self.assertEqual(response.status_code, 200)
        proxy.assert_awaited_once()
        self.assertEqual(proxy.await_args.args[0]["input"], "hello")
        translate.assert_not_called()
        route_vision.assert_not_called()

    def test_model_wire_override_uses_responses_without_changing_shared_provider(self):
        cfg = FakeConfig()
        cfg.model_mapping["deepseek-v4-pro-responses"] = {
            **cfg.model_mapping["deepseek-v4-pro"],
            "wire_api": "responses",
        }
        provider_response = JSONResponse({"id": "deepseek-response", "output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "proxy_provider_responses", new=AsyncMock(return_value=provider_response)) as proxy,
            patch.object(server, "translate_request") as translate,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={"model": "deepseek-v4-pro-responses", "input": "hello", "stream": False},
                headers={"authorization": "Bearer OPENAI_OAUTH"},
            )

        self.assertEqual(response.status_code, 200)
        proxy.assert_awaited_once()
        translate.assert_not_called()
        self.assertEqual(cfg.providers["deepseek"]["wire_api"], "chat")
        self.assertTrue(model_uses_responses(
            "deepseek",
            cfg.providers["deepseek"],
            cfg.model_mapping["deepseek-v4-pro-responses"],
        ))
        self.assertFalse(model_uses_responses(
            "deepseek",
            cfg.providers["deepseek"],
            cfg.model_mapping["deepseek-v4-pro"],
        ))

    def test_deepseek_defaults_to_responses_but_can_opt_out(self):
        self.assertTrue(provider_uses_responses("deepseek", {"adapter": "openai"}))
        self.assertFalse(provider_uses_responses("deepseek", {"wire_api": "chat"}))
        self.assertTrue(provider_uses_responses("custom", {"wire_api": "responses"}))

    def test_explicit_custom_model_is_not_replaced_by_reasoning_slot(self):
        cfg = FakeConfig()
        cfg.providers["grok"] = {
            "adapter": "openai",
            "api_key": "GROK_KEY",
            "wire_api": "chat",
        }
        cfg.model_mapping["grok-build-0.1"] = {
            "target": "grok-build-0.1",
            "provider": "grok",
            "enabled": True,
            "is_reasoning_text": True,
        }

        with patch.object(server, "get_config", return_value=cfg):
            _, provider, target, _ = server._text_route_for_responses_request(
                "grok-build-0.1",
                {"reasoning": {"effort": "high"}},
                cfg,
            )

        self.assertEqual(provider, "grok")
        self.assertEqual(target, "grok-build-0.1")

    def test_provider_passthrough_changes_only_model_and_auth(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"id": "ds-response", "object": "response", "output": []})

        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid"
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        original = {
            "model": "deepseek-alias",
            "input": "hello",
            "stream": False,
            "tools": [{"type": "function", "name": "ping", "parameters": {"type": "object"}}],
            "metadata": {"keep": "unchanged"},
        }
        with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=mock_client):
            response = asyncio.run(proxy_provider_responses(
                original,
                "deepseek-v4-pro",
                "deepseek",
                {"timeout": 30},
                adapter,
                "DEEPSEEK_KEY",
            ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["body"], {**original, "model": "deepseek-v4-pro"})
        self.assertEqual(captured["headers"]["authorization"], "Bearer DEEPSEEK_KEY")

    def test_deepseek_responses_drops_forced_tool_choice_but_keeps_tools(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "ds-response", "object": "response", "output": []})

        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid"
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body = {
            "model": "alias",
            "input": "call ping",
            "stream": False,
            "tools": [{"type": "function", "name": "ping", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "ping"},
        }
        with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=mock_client):
            response = asyncio.run(proxy_provider_responses(
                body,
                "deepseek-v4-pro",
                "deepseek",
                {"timeout": 30},
                adapter,
                "DEEPSEEK_KEY",
            ))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("tool_choice", captured["body"])
        self.assertEqual(captured["body"]["tools"], body["tools"])

    def test_cross_provider_replays_cached_history_but_same_provider_keeps_state_id(self):
        response_id = "deepseek-owned-response"
        _native_context_put(
            response_id,
            [{"type": "message", "role": "user", "content": "first"}],
            [{"type": "message", "role": "assistant", "content": "answer"}],
            owner="deepseek",
        )
        body = {
            "model": "alias",
            "previous_response_id": response_id,
            "input": [{"type": "message", "role": "user", "content": "next"}],
        }

        same, replayed = prepare_provider_responses_payload(
            body,
            "deepseek-v4-pro",
            "deepseek",
            supports_previous_response_id=True,
        )
        stateless, stateless_replayed = prepare_provider_responses_payload(
            body,
            "deepseek-v4-pro",
            "deepseek",
        )
        crossed, crossed_replayed = prepare_provider_responses_payload(body, "other-model", "other")

        self.assertFalse(replayed)
        self.assertEqual(same["previous_response_id"], response_id)
        self.assertEqual(same["input"], body["input"])
        self.assertTrue(stateless_replayed)
        self.assertNotIn("previous_response_id", stateless)
        self.assertEqual(len(stateless["input"]), 3)
        self.assertTrue(crossed_replayed)
        self.assertNotIn("previous_response_id", crossed)
        self.assertEqual(len(crossed["input"]), 3)

    def test_chat_payload_always_replays_cached_history_and_drops_state_id(self):
        response_id = "chat-owned-response"
        _native_context_put(
            response_id,
            [{"type": "message", "role": "user", "content": "inspect the project"}],
            [{
                "type": "function_call",
                "id": "fc_project_list",
                "call_id": "call_project_list",
                "name": "shell_command",
                "arguments": "{}",
            }],
            owner="grok_cyyc",
        )
        body = {
            "model": "grok-build-0.1",
            "previous_response_id": response_id,
            "input": [{
                "type": "function_call_output",
                "call_id": "call_project_list",
                "output": "README.md\nsrc",
            }],
        }

        payload, replayed = prepare_chat_responses_payload(body, "grok-build-0.1")

        self.assertTrue(replayed)
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(
            [item["type"] for item in payload["input"]],
            ["message", "function_call", "function_call_output"],
        )
        self.assertEqual(payload["model"], "grok-build-0.1")

    def test_chat_stream_completion_is_cached_for_next_turn(self):
        response = {
            "id": "resp_chat_stream",
            "output": [{"type": "message", "role": "assistant", "content": "remembered"}],
        }

        async def events():
            yield server._response_sse_line({"type": "response.completed", "response": response})
            yield "data: [DONE]\n\n"

        input_items = [{"type": "message", "role": "user", "content": "remember this"}]

        async def collect():
            return [
                event
                async for event in server._cache_chat_response_stream(events(), input_items, "grok_cyyc")
            ]

        output = asyncio.run(collect())
        payload, replayed = prepare_chat_responses_payload(
            {
                "model": "grok-build-0.1",
                "previous_response_id": "resp_chat_stream",
                "input": [{"type": "message", "role": "user", "content": "what was it?"}],
            },
            "grok-build-0.1",
        )

        self.assertEqual(len(output), 2)
        self.assertTrue(replayed)
        self.assertEqual(len(payload["input"]), 3)

    def test_native_compaction_endpoint_uses_official_compact_path(self):
        cfg = FakeConfig()
        native_response = JSONResponse({"output": []})
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "proxy_native_responses", new=AsyncMock(return_value=native_response)) as proxy,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses/compact",
                json={
                    "model": "gpt-5.6-sol",
                    "previous_response_id": "resp_previous",
                    "input": [],
                },
                headers={"authorization": "Bearer OPENAI_OAUTH"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxy.await_args.kwargs["upstream_path"], "responses/compact")

    def test_custom_compaction_replacement_keeps_recent_user_intent_and_summary(self):
        output = server._compact_replacement_output(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "first requirement"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "work in progress"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "latest constraint"}],
                },
            ],
            "Continue from the verified implementation.",
        )

        self.assertEqual(len(output), 3)
        self.assertIn("first requirement", output[0]["content"][0]["text"])
        self.assertIn("latest constraint", output[1]["content"][0]["text"])
        self.assertIn("verified implementation", output[2]["content"][0]["text"])

    def test_custom_compaction_endpoint_generates_replacement_history(self):
        cfg = FakeConfig()
        upstream_client = SimpleNamespace(
            _chat_url="https://third-party.invalid/v1/chat/completions",
            chat_completion=AsyncMock(return_value={
                "id": "chat_compact",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Keep the implementation state and run the remaining tests.",
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"total_tokens": 123},
            }),
        )
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "get_upstream_client", return_value=upstream_client),
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses/compact",
                json={
                    "model": "deepseek-v4-pro",
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "finish the bridge fix"}],
                    }],
                },
            )

        self.assertEqual(response.status_code, 200)
        output = response.json()["output"]
        self.assertIn("finish the bridge fix", output[0]["content"][0]["text"])
        self.assertIn("remaining tests", output[-1]["content"][0]["text"])
        compact_request = upstream_client.chat_completion.await_args.args[0]
        self.assertFalse(compact_request["stream"])
        self.assertEqual(compact_request.get("tools", []), [])

    def test_native_endpoint_records_upstream_400_with_response_summary(self):
        cfg = FakeConfig()
        upstream_error = NativeUpstreamHTTPError(400, b'{"error":{"message":"invalid input item"}}', "application/json")
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "proxy_native_responses", new=AsyncMock(side_effect=upstream_error)),
            patch.object(server, "_audit_event") as audit_event,
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={"model": "gpt-5.6-sol", "input": [], "stream": False},
                headers={"authorization": "Bearer OPENAI_OAUTH"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid input item", response.json()["error"]["message"])
        self.assertTrue(any(
            call.args[0] == "responses.native_upstream_error"
            for call in audit_event.call_args_list
        ))

    def test_custom_upstream_replaces_incoming_openai_oauth_with_provider_key(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"choices": []})

        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid/v1"
        client = UpstreamClient(adapter, "THIRD_PARTY_KEY")
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with patch("code_cn_bridge.client.make_async_client", return_value=mock_client):
            asyncio.run(client.chat_completion({
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hello"}],
            }))
            asyncio.run(client.close())

        authorization = captured["headers"].get("authorization", "")
        self.assertEqual(authorization, "Bearer THIRD_PARTY_KEY")
        self.assertNotIn("OPENAI_OAUTH", json.dumps(captured))

    def test_bridge_never_forwards_incoming_openai_identity_to_custom_mock(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={
                "choices": [{
                    "message": {"role": "assistant", "content": "isolated"},
                    "finish_reason": "stop",
                }],
            })

        cfg = FakeConfig()
        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid/v1"
        upstream_client = UpstreamClient(adapter, "THIRD_PARTY_KEY")
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch.object(server, "get_config", return_value=cfg),
            patch.object(server, "get_upstream_client", return_value=upstream_client),
            patch("code_cn_bridge.client.make_async_client", return_value=mock_client),
        ):
            response = TestClient(server.create_app()).post(
                "/v1/responses",
                json={
                    "model": "deepseek-v4-pro",
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }],
                    "stream": False,
                },
                headers={
                    "authorization": "Bearer SECRET_OPENAI_TOKEN",
                    "chatgpt-account-id": "SECRET_ACCOUNT",
                    "x-oai-attestation": "SECRET_ATTESTATION",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["headers"].get("authorization"), "Bearer THIRD_PARTY_KEY")
        serialized = json.dumps(captured)
        self.assertNotIn("SECRET_OPENAI_TOKEN", serialized)
        self.assertNotIn("SECRET_ACCOUNT", serialized)
        self.assertNotIn("SECRET_ATTESTATION", serialized)

    def test_custom_catalog_metadata_respects_disabled_capabilities(self):
        info = custom_model_info("plain", {
            "provider": "custom",
            "capabilities": {
                "reasoning": False,
                "shell": False,
                "apply_patch": False,
                "web_search": False,
            },
        })

        self.assertIsNone(info["apply_patch_tool_type"])
        self.assertFalse(info["supports_search_tool"])
        self.assertEqual(info["supported_reasoning_levels"], [])
        self.assertEqual(info["shell_type"], "disabled")
        self.assertEqual(info["tool_mode"], "direct")

    def test_custom_catalog_uses_direct_tools_for_chat_and_responses(self):
        entry = {
            "provider": "deepseek",
            "target": "deepseek-v4-pro",
            "capabilities": {"shell": True, "apply_patch": True},
        }
        chat_info = custom_model_info("deepseek-v4-pro", entry, {"wire_api": "chat"})
        responses_info = custom_model_info("deepseek-v4-pro", entry, {"wire_api": "responses"})

        self.assertEqual(chat_info["tool_mode"], "direct")
        self.assertEqual(responses_info["tool_mode"], "direct")
        self.assertNotIn("local executor immediately", chat_info["base_instructions"])
        self.assertNotIn("local executor immediately", responses_info["base_instructions"])
        self.assertEqual(chat_info["shell_type"], "shell_command")
        self.assertEqual(chat_info["apply_patch_tool_type"], "freeform")

    def test_custom_catalog_model_wire_override_wins_over_provider_default(self):
        entry = {
            "provider": "deepseek",
            "target": "deepseek-v4-pro",
            "wire_api": "responses",
            "capabilities": {"shell": True},
        }

        info = custom_model_info(
            "deepseek-v4-pro-responses",
            entry,
            {"wire_api": "chat"},
        )

        self.assertEqual(info["tool_mode"], "direct")

    def test_provider_stream_trace_includes_completed_usage(self):
        class EventStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    b'data: {"type":"response.completed","response":{"id":"resp_usage",'
                    b'"output":[],"usage":{"input_tokens":12,"output_tokens":8,"total_tokens":20}}}\n\n'
                )

            async def aclose(self):
                pass

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=EventStream())

        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid"
        traced = []
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def drain_response(response):
            async for _ in response.body_iterator:
                pass

        with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=mock_client):
            response = asyncio.run(proxy_provider_responses(
                {"model": "alias", "input": [], "stream": True},
                "deepseek-v4-pro",
                "deepseek",
                {"timeout": 30},
                adapter,
                "DEEPSEEK_KEY",
                on_trace=lambda event, fields: traced.append((event, fields)),
            ))
            asyncio.run(drain_response(response))

        completed = next(fields for event, fields in traced if event == "completed")
        finished = next(fields for event, fields in traced if event == "stream_finished")
        self.assertEqual(completed["tokens"], 20)
        self.assertEqual(finished["tokens"], 20)
        self.assertTrue(finished["completed"])


if __name__ == "__main__":
    unittest.main()

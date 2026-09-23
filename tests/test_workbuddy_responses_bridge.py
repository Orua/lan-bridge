import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from code_cn_bridge import server
from code_cn_bridge.access_control import ANONYMOUS_PRINCIPAL
from code_cn_bridge.adapters.base import BaseAdapter
from code_cn_bridge.config import Config
from code_cn_bridge.protocol_adapters import (
    ChatToResponsesConversionError,
    ResponsesStreamToChat,
    convert_chat_request_to_responses,
    convert_responses_response_to_chat,
)
from code_cn_bridge.provider_proxy import (
    proxy_chat_to_native_responses,
    proxy_chat_to_responses,
)
from code_cn_bridge.routing import is_chat_to_responses_mapping, resolve_route


class _Adapter(BaseAdapter):
    name = "fake"
    base_url = "https://upstream.invalid/v1"


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


class _Config:
    def __init__(self):
        self._data = {"access_control": {"enabled": False}, "server": {"audit_enabled": False}}
        self.providers = {
            "fake": {
                "adapter": "fake",
                "api_key": "secret-for-test-only",
                "base_url": "https://upstream.invalid/v1",
                "timeout": 10,
            }
        }
        self.model_mapping = {
            "gpt-5.6-terra-wb-responses": {
                "provider": "fake",
                "target": "gpt-5.6-terra",
                "upstream_model": "gpt-5.6-terra",
                "inbound_protocol": "chat_completions",
                "upstream_protocol": "responses",
                "enabled": True,
                "is_multimodal": True,
                "capabilities": {"reasoning": True, "image_input": True},
            },
            "chat-model": {
                "provider": "fake",
                "target": "chat-model-upstream",
                "enabled": True,
            },
        }
        self.native_models = {}
        self.model_slots = {}

    def get_provider(self, name):
        return self.providers.get(name)

    def resolve_model(self, model):
        entry = self.model_mapping.get(model)
        return (entry["provider"], entry.get("target", model)) if entry else ("unknown", model)


class _NativeConfig(_Config):
    def __init__(self):
        super().__init__()
        self.native_models = {
            "gpt-5.6-terra": {
                "display_name": "GPT-5.6 Terra",
                "enabled": True,
            }
        }


class WorkBuddyResponsesBridgeTests(unittest.TestCase):
    def setUp(self):
        self.auth_patch = patch(
            "code_cn_bridge.middleware.authenticate_bridge_headers",
            return_value=ANONYMOUS_PRINCIPAL,
        )
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)

    def test_request_conversion_preserves_roles_images_tools_and_limits(self):
        request = convert_chat_request_to_responses(
            {
                "model": "gpt-5.6-terra-wb-responses",
                "messages": [
                    {"role": "system", "content": "policy"},
                    {"role": "user", "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc", "detail": "high"}},
                    ]},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                    }]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
                ],
                "max_completion_tokens": 128,
                "max_tokens": 64,
                "reasoning_effort": "high",
                "tools": [{"type": "function", "function": {
                    "name": "read_file", "description": "Read", "parameters": {
                        "type": "object", "properties": {"path": {"type": "string"}},
                    },
                }}],
                "tool_choice": {"type": "function", "function": {"name": "read_file"}},
            },
            "gpt-5.6-terra",
            model_entry={"capabilities": {"image_input": True, "reasoning": True}},
        )

        self.assertEqual(request["model"], "gpt-5.6-terra")
        self.assertFalse(request["store"])
        self.assertEqual(request["max_output_tokens"], 128)
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertEqual(request["instructions"], "policy")
        self.assertEqual(request["input"][0]["content"][1], {
            "type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "high",
        })
        self.assertEqual(request["input"][1]["call_id"], "call_1")
        self.assertEqual(request["input"][2], {
            "type": "function_call_output", "call_id": "call_1", "output": "file contents",
        })
        self.assertEqual(request["tools"][0]["name"], "read_file")
        self.assertEqual(request["tool_choice"], {"type": "function", "name": "read_file"})

    def test_system_and_developer_messages_become_top_level_instructions(self):
        request = convert_chat_request_to_responses(
            {
                "messages": [
                    {"role": "system", "content": "system policy"},
                    {"role": "developer", "content": [{"type": "text", "text": "developer policy"}]},
                    {"role": "user", "content": "hello"},
                ]
            },
            "gpt-5.6-terra",
        )

        self.assertEqual(request["instructions"], "system policy\n\ndeveloper policy")
        self.assertEqual([item["role"] for item in request["input"]], ["user"])

    def test_request_conversion_rejects_unrepresentable_tool(self):
        with self.assertRaises(ChatToResponsesConversionError):
            convert_chat_request_to_responses(
                {"messages": [{"role": "user", "content": "x"}], "tools": [{"type": "web_search"}]},
                "model",
            )

    def test_response_conversion_maps_text_tool_calls_usage_and_alias(self):
        result = convert_responses_response_to_chat({
            "id": "resp_1",
            "status": "completed",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
                {"type": "function_call", "call_id": "call_2", "name": "read_file", "arguments": "{}"},
            ],
            "usage": {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
        }, "alias")

        self.assertEqual(result["model"], "alias")
        self.assertEqual(result["choices"][0]["message"]["content"], "done")
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["id"], "call_2")
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["usage"], {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10})

    def test_stream_conversion_is_incremental_and_keeps_tool_indexes(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {"type": "response.output_item.added", "output_index": 1, "item": {
                "type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "",
            }},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "call_id": "call_1", "delta": '{"p'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "call_id": "call_1", "delta": 'ath":"a"}'},
            {"type": "response.output_item.done", "output_index": 1, "item": {
                "type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"a"}',
            }},
            {"type": "response.completed", "response": {
                "id": "resp_1", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }},
        ]

        async def chunks():
            for event in events:
                yield f"data: {json.dumps(event)}\n\n".encode()

        translator = ResponsesStreamToChat("alias", "request-1")
        output = asyncio.run(self._collect(translator.convert(chunks())))
        payloads = [json.loads(line[6:]) for line in output if line.startswith("data: {")]
        content = [item["choices"][0]["delta"].get("content") for item in payloads]
        tool_chunks = [item["choices"][0]["delta"].get("tool_calls") for item in payloads if item["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual([value for value in content if value], ["Hel", "lo"])
        self.assertEqual(tool_chunks[0][0]["index"], 0)
        self.assertEqual("".join(item[0]["function"].get("arguments", "") for item in tool_chunks[1:]), '{"path":"a"}')
        self.assertEqual(payloads[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertIn("data: [DONE]", "".join(output))

    @staticmethod
    async def _collect(iterator):
        return [item async for item in iterator]

    def test_explicit_route_is_selected_without_protocol_guessing(self):
        config = _Config()
        route = resolve_route(config, "gpt-5.6-terra-wb-responses")
        self.assertTrue(is_chat_to_responses_mapping(route.metadata))
        self.assertEqual(route.target_model, "gpt-5.6-terra")
        self.assertFalse(is_chat_to_responses_mapping(config.model_mapping["chat-model"]))

    def test_chat_endpoint_dispatches_explicit_alias_to_responses_proxy(self):
        config = _Config()
        adapter = _Adapter()
        response = JSONResponse({"object": "chat.completion", "choices": [{"message": {"content": "ok"}}]})
        with (
            patch.object(server, "get_config", return_value=config),
            patch.object(server, "_resolve_adapter", return_value=(adapter, "fake", "gpt-5.6-terra", "key")),
            patch.object(server, "proxy_chat_to_responses", new=AsyncMock(return_value=response)) as proxy,
        ):
            result = TestClient(server.create_app()).post(
                "/v1/chat/completions",
                json={"model": "gpt-5.6-terra-wb-responses", "messages": [{"role": "user", "content": "hi"}]},
            )

        self.assertEqual(result.status_code, 200)
        proxy.assert_awaited_once()
        self.assertEqual(proxy.await_args.args[1], "gpt-5.6-terra-wb-responses")
        self.assertEqual(proxy.await_args.args[2], "gpt-5.6-terra")

    def test_chat_endpoint_dispatches_native_terra_without_deepseek_fallback(self):
        config = _NativeConfig()
        response = JSONResponse({
            "object": "chat.completion",
            "model": "gpt-5.6-terra",
            "choices": [{"message": {"content": "ok"}}],
        })
        with (
            patch.object(server, "get_config", return_value=config),
            patch.object(
                server,
                "proxy_chat_to_native_responses",
                new=AsyncMock(return_value=response),
            ) as proxy,
            patch.object(server, "_chat_route_vision") as chat_route,
        ):
            result = TestClient(server.create_app()).post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-5.6-terra",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        self.assertEqual(result.status_code, 200)
        proxy.assert_awaited_once()
        self.assertEqual(proxy.await_args.args[2], "gpt-5.6-terra")
        self.assertEqual(proxy.await_args.args[3], "gpt-5.6-terra")
        chat_route.assert_not_called()

    def test_native_proxy_converts_chat_request_and_responses_json(self):
        observed = {}

        async def fake_native(request, payload, target_model, config, **kwargs):
            observed["payload"] = payload
            observed["target_model"] = target_model
            return Response(
                content=json.dumps({
                    "status": "completed",
                    "output": [{
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "native OK"}],
                    }],
                    "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
                }),
                media_type="application/json",
            )

        with patch(
            "code_cn_bridge.provider_proxy.proxy_native_responses",
            new=AsyncMock(side_effect=fake_native),
        ):
            result = asyncio.run(proxy_chat_to_native_responses(
                SimpleNamespace(headers={}),
                {
                    "model": "gpt-5.6-terra",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_completion_tokens": 64,
                    "stream": False,
                },
                "gpt-5.6-terra",
                "gpt-5.6-terra",
                SimpleNamespace(),
                request_id="request-native",
            ))

        payload = json.loads(result.body)
        self.assertEqual(observed["target_model"], "gpt-5.6-terra")
        self.assertIn("input", observed["payload"])
        self.assertNotIn("messages", observed["payload"])
        self.assertTrue(observed["payload"]["stream"])
        self.assertNotIn("max_output_tokens", observed["payload"])
        self.assertEqual(observed["payload"]["input"][0]["content"], [
            {"type": "input_text", "text": "hello"},
        ])
        self.assertEqual(payload["model"], "gpt-5.6-terra")
        self.assertEqual(payload["choices"][0]["message"]["content"], "native OK")

    def test_native_proxy_aggregates_required_upstream_sse_for_nonstream_chat(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_native"}},
            {"type": "response.output_text.delta", "delta": "native "},
            {"type": "response.output_text.delta", "delta": "OK"},
            {"type": "response.completed", "response": {
                "status": "completed",
                "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
            }},
        ]

        async def raw_events():
            for event in events:
                yield f"data: {json.dumps(event)}\n\n".encode()

        async def fake_native(*args, **kwargs):
            return StreamingResponse(raw_events(), media_type="text/event-stream")

        with patch(
            "code_cn_bridge.provider_proxy.proxy_native_responses",
            new=AsyncMock(side_effect=fake_native),
        ):
            response = asyncio.run(proxy_chat_to_native_responses(
                SimpleNamespace(headers={}),
                {
                    "model": "gpt-5.6-terra",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_completion_tokens": 64,
                    "stream": False,
                },
                "gpt-5.6-terra",
                "gpt-5.6-terra",
                SimpleNamespace(),
                request_id="request-native-nonstream",
            ))

        self.assertNotIsInstance(response, StreamingResponse)
        payload = json.loads(response.body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "native OK")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["usage"]["total_tokens"], 4)

    def test_native_proxy_converts_responses_stream_to_chat_sse(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_native"}},
            {"type": "response.output_text.delta", "delta": "native OK"},
            {"type": "response.completed", "response": {
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }},
        ]

        async def raw_events():
            for event in events:
                yield f"data: {json.dumps(event)}\n\n".encode()

        async def fake_native(*args, **kwargs):
            return StreamingResponse(raw_events(), media_type="text/event-stream")

        with patch(
            "code_cn_bridge.provider_proxy.proxy_native_responses",
            new=AsyncMock(side_effect=fake_native),
        ):
            async def run_proxy():
                response = await proxy_chat_to_native_responses(
                    SimpleNamespace(headers={}),
                    {
                        "model": "gpt-5.6-terra",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    "gpt-5.6-terra",
                    "gpt-5.6-terra",
                    SimpleNamespace(),
                    request_id="request-native-stream",
                )
                return [item async for item in response.body_iterator]

            chunks = asyncio.run(run_proxy())

        text = "".join(chunks)
        self.assertIn('"content":"native OK"', text)
        self.assertIn('"finish_reason":"stop"', text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_incomplete_responses_stream_is_a_normal_terminal_result(self):
        async def chunks():
            yield b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
            yield b'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}\n\n'

        translator = ResponsesStreamToChat("alias", "request-incomplete")
        output = asyncio.run(self._collect(translator.convert(chunks())))

        self.assertFalse(translator.completed)
        self.assertTrue(translator.incomplete)
        self.assertTrue(translator.terminal)
        self.assertIn('"finish_reason":"length"', "".join(output))
        self.assertNotIn("responses_upstream_error", "".join(output))

    def test_proxy_sends_responses_payload_and_converts_json(self):
        observed = {}

        def handler(request):
            observed["path"] = request.url.path
            observed["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "status": "completed",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=client):
            result = asyncio.run(proxy_chat_to_responses(
                {"model": "alias", "messages": [{"role": "user", "content": "hello"}], "stream": False},
                "alias", "upstream", "fake", {"timeout": 10}, _Adapter(), "key", request_id="request-1",
            ))

        payload = json.loads(result.body)
        self.assertEqual(observed["path"], "/v1/responses")
        self.assertIn("input", observed["body"])
        self.assertNotIn("messages", observed["body"])
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")

    def test_proxy_converts_responses_sse_to_chat_sse(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_text.delta", "delta": "OK"},
            {"type": "response.completed", "response": {
                "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }},
        ]

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_BytesStream(b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=client):
            async def run_proxy():
                response = await proxy_chat_to_responses(
                    {"model": "alias", "messages": [{"role": "user", "content": "hello"}], "stream": True},
                    "alias", "upstream", "fake", {"timeout": 10}, _Adapter(), "key", request_id="request-1",
                )
                return [item async for item in response.body_iterator]

            chunks = asyncio.run(run_proxy())

        text = "".join(chunks)
        self.assertIn('"content":"OK"', text)
        self.assertIn('"finish_reason":"stop"', text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_config_persists_explicit_workbuddy_protocol_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.yaml"
            path.write_text(
                """
providers:
  compatible:
    adapter: openai
    api_key_env: TEST_KEY
model_mapping:
  wb:
    alias: wb
    upstream_model: terra
    provider: compatible
    inbound_protocol: chat_completions
    upstream_protocol: responses
    enabled: true
""",
                encoding="utf-8",
            )
            config = Config(path)

        entry = config.model_mapping["wb"]
        self.assertEqual(entry["target"], "terra")
        self.assertEqual(entry["upstream_model"], "terra")
        self.assertEqual(entry["inbound_protocol"], "chat_completions")
        self.assertEqual(entry["upstream_protocol"], "responses")
        self.assertTrue(is_chat_to_responses_mapping(entry))

    def test_unconfigured_chat_model_is_forwarded_to_native_upstream(self):
        config = _Config()
        upstream = JSONResponse({"error": {"type": "model_not_found"}}, status_code=404)
        with patch.object(server, "get_config", return_value=config), patch.object(
            server, "proxy_chat_to_native_responses", new=AsyncMock(return_value=upstream)
        ) as proxy:
            result = TestClient(server.create_app()).post(
                "/v1/chat/completions",
                json={"model": "not-configured", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.json()["error"]["type"], "model_not_found")
        self.assertEqual(proxy.await_args.args[2], "not-configured")


if __name__ == "__main__":
    unittest.main()

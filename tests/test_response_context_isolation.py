import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from code_cn_bridge.adapters.deepseek import DeepSeekAdapter
from code_cn_bridge.native_proxy import (
    ResponseContextAccessError,
    _native_context_put,
    _prepare_native_responses_payload,
    prepare_chat_responses_payload,
    prepare_provider_responses_payload,
)
from code_cn_bridge.provider_proxy import proxy_provider_responses


class ResponseContextIsolationTests(unittest.TestCase):
    def test_access_key_cannot_replay_another_keys_context(self):
        response_id = "resp_context_owned_by_a"
        _native_context_put(
            response_id,
            [{"type": "message", "role": "user", "content": "secret context"}],
            [{"type": "message", "role": "assistant", "content": "answer"}],
            owner="deepseek",
            access_key_id="key-a",
        )

        with self.assertRaises(ResponseContextAccessError) as raised:
            prepare_chat_responses_payload(
                {
                    "previous_response_id": response_id,
                    "input": [{"type": "message", "role": "user", "content": "continue"}],
                },
                "deepseek-chat",
                access_key_id="key-b",
            )

        error_text = str(raised.exception)
        self.assertNotIn(response_id, error_text)
        self.assertNotIn("key-a", error_text)
        self.assertNotIn("key-b", error_text)

    def test_unknown_context_fails_before_provider_upstream_call(self):
        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid"
        with patch("code_cn_bridge.provider_proxy.make_async_client") as make_client:
            with self.assertRaises(ResponseContextAccessError):
                asyncio.run(proxy_provider_responses(
                    {
                        "previous_response_id": "resp_unknown_for_key",
                        "input": [{"type": "message", "role": "user", "content": "continue"}],
                    },
                    "deepseek-chat",
                    "deepseek",
                    {"timeout": 30},
                    adapter,
                    "provider-secret",
                    access_key_id="key-a",
                ))

        make_client.assert_not_called()

    def test_same_key_can_replay_context_across_providers(self):
        response_id = "resp_same_key_cross_provider"
        _native_context_put(
            response_id,
            [{"type": "message", "role": "user", "content": "first"}],
            [{"type": "message", "role": "assistant", "content": "answer"}],
            owner="deepseek",
            access_key_id="key-a",
        )

        payload, replayed = prepare_provider_responses_payload(
            {
                "previous_response_id": response_id,
                "input": [{"type": "message", "role": "user", "content": "next"}],
            },
            "grok-model",
            "grok",
            access_key_id="key-a",
        )

        self.assertTrue(replayed)
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(len(payload["input"]), 3)

    def test_provider_nonstream_cache_is_bound_to_access_key(self):
        response_id = "resp_provider_nonstream_key_bound"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": response_id,
                    "object": "response",
                    "output": [{"type": "message", "role": "assistant", "content": "answer"}],
                },
            )

        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid"
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=client):
            response = asyncio.run(proxy_provider_responses(
                {"input": [{"type": "message", "role": "user", "content": "first"}]},
                "deepseek-chat",
                "deepseek",
                {"timeout": 30},
                adapter,
                "provider-secret",
                access_key_id="key-a",
            ))

        self.assertEqual(response.status_code, 200)
        with self.assertRaises(ResponseContextAccessError):
            _prepare_native_responses_payload(
                {
                    "previous_response_id": response_id,
                    "input": [{"type": "message", "role": "user", "content": "continue"}],
                },
                "gpt-5.6-sol",
                access_key_id="key-b",
            )

    def test_provider_stream_cache_is_bound_to_access_key(self):
        response_id = "resp_provider_stream_key_bound"
        event = {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "output": [{"type": "message", "role": "assistant", "content": "answer"}],
            },
        }

        class EventStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=EventStream(),
            )

        adapter = DeepSeekAdapter()
        adapter.base_url = "https://third-party.invalid"

        async def run_proxy() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=client):
                response = await proxy_provider_responses(
                    {
                        "stream": True,
                        "input": [{"type": "message", "role": "user", "content": "first"}],
                    },
                    "deepseek-chat",
                    "deepseek",
                    {"timeout": 30},
                    adapter,
                    "provider-secret",
                    access_key_id="key-a",
                )
                async for _ in response.body_iterator:
                    pass

        asyncio.run(run_proxy())
        with self.assertRaises(ResponseContextAccessError):
            prepare_chat_responses_payload(
                {
                    "previous_response_id": response_id,
                    "input": [{"type": "message", "role": "user", "content": "continue"}],
                },
                "deepseek-chat",
                access_key_id="key-b",
            )

    def test_default_empty_access_key_remains_backward_compatible(self):
        provider_payload, replayed = prepare_provider_responses_payload(
            {
                "previous_response_id": "resp_legacy_unknown",
                "input": [{"type": "message", "role": "user", "content": "continue"}],
            },
            "deepseek-chat",
            "deepseek",
            supports_previous_response_id=True,
        )
        native_payload = _prepare_native_responses_payload(
            {
                "previous_response_id": "resp_legacy_unknown",
                "input": [{"type": "message", "role": "user", "content": "continue"}],
            },
            "gpt-5.6-sol",
        )

        self.assertFalse(replayed)
        self.assertEqual(provider_payload["previous_response_id"], "resp_legacy_unknown")
        self.assertNotIn("previous_response_id", native_payload)
        self.assertEqual(len(native_payload["input"]), 1)


if __name__ == "__main__":
    unittest.main()

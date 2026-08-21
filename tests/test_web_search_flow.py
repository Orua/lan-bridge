import asyncio
import inspect
import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from code_cn_bridge.adapters.deepseek import DeepSeekAdapter
from code_cn_bridge.server import (
    _buffered_responses_sse,
    _complete_with_web_search,
    _format_search_results,
    _handle_stream,
    _handle_web_search_stream,
    _looks_like_tool_leak,
    _next_stream_chunk,
)
from code_cn_bridge.stats import get_stats


class _FakeConfig:
    web_search = {"enabled": True, "max_rounds": 3}

    @staticmethod
    def get_web_search_provider():
        return {"adapter": "bocha", "api_key": "hidden", "enabled": True}


class _OneRoundSearchConfig(_FakeConfig):
    web_search = {"enabled": True, "max_rounds": 1}


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat_completion(self, request):
        self.requests.append(request)
        return self.responses.pop(0)

    async def close(self):
        return None


class _FakeStreamingClient(_FakeClient):
    async def chat_completion_stream(self, request):
        self.requests.append(request.copy())
        for chunk in self.responses.pop(0):
            yield chunk


def _tool_response(*calls):
    return {
        "choices": [{"message": {"content": None, "tool_calls": list(calls)}}],
        "usage": {"total_tokens": 2},
    }


def _call(name, arguments, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _stream_text(*parts):
    chunks = [
        {"choices": [{"delta": {"content": part}, "finish_reason": None}]}
        for part in parts
    ]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return chunks


def _stream_reasoning(*parts):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": part}, "finish_reason": None}]}
        for part in parts
    ]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return chunks


def _stream_tool(name, arguments, call_id):
    return [
        {
            "choices": [{
                "delta": {"tool_calls": [_call(name, arguments, call_id) | {"index": 0}]},
                "finish_reason": None,
            }]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _json_payloads(lines):
    return [
        json.loads(line.removeprefix("data: ").strip())
        for line in lines
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]


class WebSearchFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = DeepSeekAdapter()
        self.chat_request = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "搜索今天的桥接信息"}],
            "tools": [],
            "stream": False,
        }

    async def test_idle_heartbeat_does_not_cancel_pending_upstream_read(self):
        ready = asyncio.Event()

        async def delayed_stream():
            await ready.wait()
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]}

        stream = delayed_stream()
        chunk, idle, pending = await _next_stream_chunk(stream, timeout=0.01)
        self.assertIsNone(chunk)
        self.assertTrue(idle)
        self.assertIsNotNone(pending)
        self.assertFalse(pending.done())

        ready.set()
        chunk, idle, pending = await _next_stream_chunk(stream, pending, timeout=1.0)
        self.assertFalse(idle)
        self.assertEqual(chunk["choices"][0]["delta"]["content"], "done")
        self.assertIsNone(pending)

    def test_plain_stream_handler_accepts_response_tool_types_argument(self):
        signature = inspect.signature(_handle_stream)
        self.assertIn("response_tool_types", signature.parameters)
        self.assertLessEqual(14, len(signature.parameters))

    async def test_plain_stream_handler_uses_final_usage_chunk(self):
        get_stats().clear_logs()
        request = {
            **self.chat_request,
            "stream": True,
            "tools": [],
        }
        client = _FakeStreamingClient([[
            {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
        ]])

        lines = [
            line async for line in _handle_stream(
                client,
                self.adapter,
                request,
                {"stream": True},
                _FakeConfig(),
                "gpt-5.5",
                False,
                0,
            )
        ]
        payloads = _json_payloads(lines)
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")
        logs = get_stats().get_recent_logs(1)

        self.assertEqual(completed["response"]["usage"]["input_tokens"], 8)
        self.assertEqual(completed["response"]["usage"]["output_tokens"], 4)
        self.assertEqual(completed["response"]["usage"]["total_tokens"], 12)
        self.assertEqual(logs[0]["tokens"], 12)

    async def test_plain_stream_records_captured_client_ip(self):
        get_stats().clear_logs()
        client = _FakeStreamingClient([_stream_text("ok")])

        _ = [
            line async for line in _handle_stream(
                client,
                self.adapter,
                {**self.chat_request, "stream": True},
                {"stream": True},
                _FakeConfig(),
                "gpt-5.5",
                False,
                0,
                client_ip="127.0.0.77",
            )
        ]

        self.assertEqual(get_stats().get_recent_logs(1)[0]["client_ip"], "127.0.0.77")

    async def test_web_search_stream_records_captured_client_ip(self):
        get_stats().clear_logs()
        client = _FakeStreamingClient([_stream_text("ok")])

        _ = [
            line async for line in _handle_web_search_stream(
                client,
                self.adapter,
                {**self.chat_request, "stream": True},
                {"stream": True},
                _FakeConfig(),
                "gpt-5.5",
                False,
                0,
                client_ip="127.0.0.88",
            )
        ]

        self.assertEqual(get_stats().get_recent_logs(1)[0]["client_ip"], "127.0.0.88")

    async def test_executes_search_and_returns_native_item_with_chinese_citation(self):
        self.chat_request["tools"] = [{"type": "function", "function": {"name": "web_search"}}]
        client = _FakeClient([
            _tool_response(_call("web_search", {"query": "今天 Bridge 新闻"}, "call_search")),
            {
                "choices": [{"message": {"content": "根据检索，今天有更新。[1]"}}],
                "usage": {"total_tokens": 5},
            },
        ])
        results = [{"title": "中文来源", "url": "https://example.cn/news", "snippet": "今日更新"}]

        with patch("code_cn_bridge.server.search_web", AsyncMock(return_value={
            "outcome": "ok", "message": "", "results": results,
        })):
            response, tokens = await _complete_with_web_search(
                client, self.adapter, self.chat_request, _FakeConfig(), "gpt-5.5",
            )

        self.assertEqual(tokens, 7)
        self.assertEqual(response["output"][0]["type"], "web_search_call")
        self.assertEqual(response["output"][0]["action"]["queries"], ["今天 Bridge 新闻"])
        text_part = response["output"][1]["content"][0]
        self.assertIn("更新。[1]", text_part["text"])
        self.assertEqual(text_part["annotations"][0]["url"], "https://example.cn/news")
        self.assertIn("中文来源", client.requests[1]["messages"][-1]["content"])
        self.assertEqual(client.requests[1]["messages"][-2]["content"], "")
        self.assertNotIn("tools", client.requests[1])

    def test_empty_search_results_are_reported_as_successful_search(self):
        content = _format_search_results([], 1)

        self.assertIn(date.today().isoformat(), content)
        self.assertIn("executed successfully", content)
        self.assertIn("do not claim that web_search is unavailable", content)
        self.assertIn("do not call web_search again", content)
        self.assertTrue(content.rstrip().endswith("[]"))

    def test_rejected_search_is_described_without_tool_failure_claim(self):
        content = _format_search_results([], 1, {
            "outcome": "rejected",
            "message": "Query not allowed",
            "results": [],
        })

        self.assertIn("provider declined this specific query", content)
        self.assertIn("Query not allowed", content)
        self.assertIn("do not claim that the web_search tool is unavailable", content)

    async def test_empty_search_results_continue_to_normal_model_response(self):
        self.chat_request["tools"] = [{"type": "function", "function": {"name": "web_search"}}]
        client = _FakeClient([
            _tool_response(_call("web_search", {"query": "no direct result"}, "call_search")),
            {
                "choices": [{"message": {"content": "No direct result was found."}}],
                "usage": {"total_tokens": 1},
            },
        ])
        with patch("code_cn_bridge.server.search_web", AsyncMock(return_value={
            "outcome": "ok", "message": "", "results": [],
        })):
            response, _ = await _complete_with_web_search(
                client, self.adapter, self.chat_request, _FakeConfig(), "gpt-5.5",
            )

        self.assertEqual(response["output"][0]["type"], "web_search_call")
        self.assertEqual(response["output"][0]["action"]["sources"], [])
        self.assertIn("No direct result", response["output"][1]["content"][0]["text"])

    async def test_provider_rejection_continues_to_normal_model_response(self):
        self.chat_request["tools"] = [{"type": "function", "function": {"name": "web_search"}}]
        client = _FakeClient([
            _tool_response(_call("web_search", {"query": "blocked"}, "call_search")),
            {
                "choices": [{"message": {"content": "The provider declined this query."}}],
                "usage": {"total_tokens": 1},
            },
        ])
        with patch("code_cn_bridge.server.search_web", AsyncMock(return_value={
            "outcome": "rejected", "message": "Query not allowed", "results": [],
        })):
            response, _ = await _complete_with_web_search(
                client, self.adapter, self.chat_request, _FakeConfig(), "gpt-5.5",
            )

        self.assertEqual(response["output"][0]["type"], "web_search_call")
        self.assertIn("provider declined", client.requests[1]["messages"][-1]["content"])
        self.assertIn("provider declined", response["output"][1]["content"][0]["text"])

    async def test_preserves_client_owned_tool_when_mixed_with_search(self):
        client = _FakeClient([
            _tool_response(
                _call("web_search", {"query": "资料"}, "call_search"),
                _call("exec_command", {"cmd": "pwd"}, "call_exec"),
            ),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.test", "snippet": "x"}],
            }),
        ):
            response, _ = await _complete_with_web_search(
                client, self.adapter, self.chat_request, _FakeConfig(), "gpt-5.5",
            )

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(response["output"][0]["type"], "web_search_call")
        self.assertEqual(response["output"][1]["type"], "function_call")
        self.assertEqual(response["output"][1]["name"], "exec_command")

    async def test_preserves_namespaced_mcp_tool_when_mixed_with_search(self):
        client = _FakeClient([
            _tool_response(
                _call("web_search", {"query": "browser"}, "call_search"),
                _call("mcp__node_repl__js", {"code": "inspect()"}, "call_browser"),
            ),
        ])
        namespace_tools = {
            "mcp__node_repl__js": {"namespace": "mcp__node_repl__", "name": "js"},
        }
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.test", "snippet": "x"}],
            }),
        ):
            response, _ = await _complete_with_web_search(
                client,
                self.adapter,
                self.chat_request,
                _FakeConfig(),
                "gpt-5.5",
                namespace_tools=namespace_tools,
            )

        browser_call = response["output"][1]
        self.assertEqual(browser_call["type"], "function_call")
        self.assertEqual(browser_call["name"], "js")
        self.assertEqual(browser_call["namespace"], "mcp__node_repl__")

    async def test_buffered_stream_emits_search_item_and_completion(self):
        response = {
            "id": "resp_search",
            "object": "response",
            "model": "gpt-5.5",
            "status": "completed",
            "usage": {
                "input_tokens": 3,
                "input_tokens_details": None,
                "output_tokens": 2,
                "output_tokens_details": None,
                "total_tokens": 5,
            },
            "output": [{
                "id": "ws_search",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "queries": ["中文"], "sources": []},
            }],
        }
        lines = [line async for line in _buffered_responses_sse(response)]
        payloads = [
            json.loads(line.removeprefix("data: ").strip())
            for line in lines
            if line.strip() != "data: [DONE]"
        ]

        self.assertEqual(payloads[0]["type"], "response.created")
        self.assertEqual(payloads[1]["item"]["type"], "web_search_call")
        self.assertEqual(payloads[-1]["type"], "response.completed")
        self.assertEqual(payloads[-1]["response"]["usage"]["input_tokens"], 3)
        self.assertEqual(lines[-1], "data: [DONE]\n\n")

    async def test_adds_sources_when_model_omits_citation_markers(self):
        client = _FakeClient([
            _tool_response(_call("web_search", {"query": "最新资料"}, "call_search")),
            {
                "choices": [{"message": {"content": "模型没有自行放入引用。"}}],
                "usage": {"total_tokens": 1},
            },
        ])
        results = [{"title": "可靠来源", "url": "https://example.cn/source", "snippet": "摘要"}]
        with patch("code_cn_bridge.server.search_web", AsyncMock(return_value={
            "outcome": "ok", "message": "", "results": results,
        })):
            response, _ = await _complete_with_web_search(
                client, self.adapter, self.chat_request, _FakeConfig(), "gpt-5.5",
            )

        text_part = response["output"][1]["content"][0]
        self.assertIn("Sources:", text_part["text"])
        self.assertIn("https://example.cn/source", text_part["text"])
        self.assertEqual(text_part["annotations"][0]["title"], "可靠来源")

    async def test_required_search_tool_is_relaxed_after_first_result(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
            "tool_choice": "required",
        }
        client = _FakeClient([
            _tool_response(_call("web_search", {"query": "必要检索"}, "call_search")),
            {"choices": [{"message": {"content": "完成 [1]"}}], "usage": {}},
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "资料", "url": "https://example.cn", "snippet": "摘要"}],
            }),
        ):
            await _complete_with_web_search(client, self.adapter, request, _FakeConfig(), "gpt-5.5")

        self.assertNotIn("tool_choice", client.requests[1])
        self.assertNotIn("tools", client.requests[1])

    async def test_non_stream_round_limit_returns_collected_results(self):
        self.chat_request["tools"] = [{"type": "function", "function": {"name": "web_search"}}]
        client = _FakeClient([
            _tool_response(_call("web_search", {"query": "first"}, "call_search_1")),
            _tool_response(_call("web_search", {"query": "repeat"}, "call_search_2")),
        ])
        with patch("code_cn_bridge.server.search_web", AsyncMock(return_value={
            "outcome": "ok",
            "message": "",
            "results": [{"title": "Source", "url": "https://example.cn", "snippet": "Summary"}],
        })):
            response, _ = await _complete_with_web_search(
                client, self.adapter, self.chat_request, _OneRoundSearchConfig(), "gpt-5.5",
            )

        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["output"][0]["type"], "web_search_call")
        self.assertIn("stopped after 1 search rounds", response["output"][1]["content"][0]["text"])

    async def test_streams_text_immediately_when_search_is_available_but_unused(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        }
        client = _FakeStreamingClient([_stream_text("你", "好")])

        lines = [
            line async for line in _handle_web_search_stream(
                client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
            )
        ]
        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")

        self.assertEqual(deltas, ["你", "好"])
        self.assertIn("input_tokens", completed["response"]["usage"])

    async def test_stream_retries_initial_script_leak_when_search_is_unused(self):
        request = {
            **self.chat_request,
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "exec_command"}},
            ],
        }
        client = _FakeStreamingClient([
            _stream_text(
                "```javascript\nvar doc = app.open(new File('x.pdf'));\n```\n"
                "<|FunctionCallBegin|>{\"name\":\"exec_command\"}<|FunctionCallEnd|>"
            ),
            _stream_text("final after retry"),
        ])

        lines = [
            line async for line in _handle_web_search_stream(
                client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
            )
        ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]

        self.assertEqual(len(client.requests), 2)
        self.assertEqual(deltas, ["final after retry"])
        self.assertEqual(payloads[-1]["type"], "response.completed")

    def test_frontend_css_text_is_treated_as_tool_leak(self):
        request = {
            **self.chat_request,
            "messages": [{"role": "user", "content": "请直接修改前端页面代码并测试"}],
            "tools": [{"type": "function", "function": {"name": "apply_patch"}}],
        }
        text = (
            "可以，把这段样式写进去：\n"
            ".map-card {\n"
            "  position: absolute;\n"
            "  z-index: 20;\n"
            "  min-height: 100px;\n"
            "}\n"
        )

        self.assertTrue(_looks_like_tool_leak(text, request))

    def test_plain_code_answer_is_not_treated_as_tool_leak(self):
        request = {
            **self.chat_request,
            "messages": [{"role": "user", "content": "给我一个 JS 数组 map 的例子"}],
            "tools": [{"type": "function", "function": {"name": "apply_patch"}}],
        }
        text = (
            "可以，例如：\n"
            "```js\n"
            "const items = ['apple', 'banana'];\n"
            "const upper = items.map(item => item.toUpperCase());\n"
            "```\n"
        )

        self.assertFalse(_looks_like_tool_leak(text, request))

    async def test_stream_passes_through_code_text_after_retry_instead_of_failing(self):
        request = {
            **self.chat_request,
            "messages": [{"role": "user", "content": "请直接修改前端页面代码"}],
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "apply_patch"}},
            ],
        }
        client = _FakeStreamingClient([
            _stream_text("```css\n.map-card {\n  position: absolute;\n}\n```"),
            _stream_text("```css\n.map-card {\n  z-index: 20;\n}\n```"),
        ])

        lines = [
            line async for line in _handle_web_search_stream(
                client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
            )
        ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]

        self.assertEqual(len(client.requests), 2)
        self.assertIn("z-index: 20", "".join(deltas))
        self.assertEqual(payloads[-1]["type"], "response.completed")

    async def test_streams_final_answer_after_one_search_and_removes_internal_tool(self):
        request = {
            **self.chat_request,
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "exec_command"}},
            ],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "流式资料"}, "call_search"),
            _stream_text("找", "到"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "来源", "url": "https://example.cn", "snippet": "摘要"}],
            }),
        ):
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")
        second_tools = [tool["function"]["name"] for tool in client.requests[1]["tools"]]

        self.assertEqual(deltas, ["找", "到"])
        self.assertEqual(completed["response"]["output"][0]["type"], "web_search_call")
        self.assertEqual(second_tools, ["exec_command"])

    async def test_stream_retries_when_post_search_answer_is_empty(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "empty followup"}, "call_search"),
            [{"choices": [{"delta": {}, "finish_reason": "stop"}]}],
            _stream_text("final after retry"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.cn", "snippet": "Summary"}],
            }),
        ):
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]

        self.assertEqual(len(client.requests), 3)
        self.assertEqual(deltas, ["final after retry"])

    async def test_stream_retries_when_post_search_only_returns_reasoning(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "reasoning followup"}, "call_search"),
            _stream_reasoning("thinking only"),
            _stream_text("final after reasoning retry"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.cn", "snippet": "Summary"}],
            }),
        ):
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]

        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.requests[2]["thinking"], {"type": "disabled"})
        self.assertEqual(deltas, ["final after reasoning retry"])
        self.assertFalse(any(payload["type"] == "response.failed" for payload in payloads))

    async def test_stream_fails_when_post_search_only_returns_reasoning_after_retry(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "reasoning only"}, "call_search"),
            _stream_reasoning("thinking only"),
            _stream_reasoning("still only thinking"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.cn", "snippet": "Summary"}],
            }),
        ):
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        failed = next(payload for payload in payloads if payload["type"] == "response.failed")

        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.requests[2]["thinking"], {"type": "disabled"})
        self.assertEqual(failed["response"]["error"]["type"], "reasoning_without_action")
        self.assertFalse(any(payload["type"] == "response.completed" for payload in payloads))

    async def test_plain_stream_retries_reasoning_only_with_thinking_disabled(self):
        request = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "Only answer OK."}],
            "stream": True,
            "max_tokens": 20,
        }
        client = _FakeStreamingClient([
            _stream_reasoning("thinking only"),
            _stream_text("OK"),
        ])

        lines = [
            line async for line in _handle_stream(
                client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
            )
        ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]

        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[1]["thinking"], {"type": "disabled"})
        self.assertEqual(client.requests[1]["max_tokens"], 512)
        self.assertEqual(deltas, ["OK"])
        self.assertTrue(any(payload["type"] == "response.completed" for payload in payloads))
        self.assertFalse(any(payload["type"] == "response.failed" for payload in payloads))

    async def test_stream_retries_when_post_search_text_looks_like_script_leak(self):
        request = {
            **self.chat_request,
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "exec_command"}},
            ],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "script leak"}, "call_search"),
            _stream_text("```powershell\nGet-Content .\\server.py\nEND\n```"),
            _stream_tool("exec_command", {"cmd": "Get-Content .\\server.py"}, "call_exec"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.cn", "snippet": "Summary"}],
            }),
        ):
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")

        self.assertEqual(len(client.requests), 3)
        self.assertEqual(completed["response"]["output"][-1]["type"], "function_call")
        self.assertEqual(completed["response"]["output"][-1]["name"], "exec_command")

    async def test_stream_intercepts_repeated_internal_search_before_final_answer(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "首次资料"}, "call_search_1"),
            [
                *_stream_text("我再查一条")[0:-1],
                *_stream_tool("web_search", {"query": "补充资料"}, "call_search_2"),
            ],
            _stream_text("最终", "答案"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(side_effect=[
                {"outcome": "ok", "message": "", "results": [{"title": "来源1", "url": "https://one.example", "snippet": "一"}]},
                {"outcome": "ok", "message": "", "results": [{"title": "来源2", "url": "https://two.example", "snippet": "二"}]},
            ]),
        ) as search:
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _FakeConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        deltas = [payload["delta"] for payload in payloads if payload["type"] == "response.output_text.delta"]
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")

        self.assertEqual(search.await_count, 2)
        self.assertEqual(deltas, ["最终", "答案"])
        self.assertEqual([item["type"] for item in completed["response"]["output"][:2]], [
            "web_search_call", "web_search_call",
        ])
        self.assertFalse(any(item.get("type") == "function_call" for item in completed["response"]["output"]))

    async def test_stream_round_limit_returns_collected_results(self):
        request = {
            **self.chat_request,
            "tools": [{"type": "function", "function": {"name": "web_search"}}],
        }
        client = _FakeStreamingClient([
            _stream_tool("web_search", {"query": "first"}, "call_search_1"),
            _stream_tool("web_search", {"query": "repeat"}, "call_search_2"),
        ])
        with patch(
            "code_cn_bridge.server.search_web",
            AsyncMock(return_value={
                "outcome": "ok",
                "message": "",
                "results": [{"title": "Source", "url": "https://example.cn", "snippet": "Summary"}],
            }),
        ):
            lines = [
                line async for line in _handle_web_search_stream(
                    client, self.adapter, request, {"stream": True}, _OneRoundSearchConfig(), "gpt-5.5", False, 0,
                )
            ]

        payloads = _json_payloads(lines)
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")

        self.assertEqual(completed["response"]["status"], "completed")
        self.assertEqual(completed["response"]["output"][0]["type"], "web_search_call")
        self.assertIn("stopped after 1 search rounds", completed["response"]["output"][1]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()

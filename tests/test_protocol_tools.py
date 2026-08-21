import json
import asyncio
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from code_cn_bridge.adapters.deepseek import DeepSeekAdapter
from code_cn_bridge.adapters.doubao import DoubaoAdapter
from code_cn_bridge.adapters.openai import OpenAICompatibleAdapter
from code_cn_bridge.adapters.qwen import QwenAdapter
from code_cn_bridge.models import build_responses_response
from code_cn_bridge.protocol import StreamTranslator
from code_cn_bridge.protocol import translate_request
from code_cn_bridge.protocol import translate_response
from code_cn_bridge.server import _route_vision
from code_cn_bridge.server import _buffered_responses_sse
from code_cn_bridge.server import _apply_named_provider_chat_compat
from code_cn_bridge.server import _classify_image_generation_intent
from code_cn_bridge.server import _compact_chat_replay_history
from code_cn_bridge.server import _compact_historical_tool_outputs
from code_cn_bridge.server import _extract_text_content
from code_cn_bridge.server import _extract_image_gen_prompt_from_chat_response
from code_cn_bridge.server import _extract_generated_image_reference
from code_cn_bridge.server import _download_generated_image_as_base64
from code_cn_bridge.server import _extract_image_generation_prompt
from code_cn_bridge.server import _extract_workspace_dir_from_responses_body
from code_cn_bridge.server import _finalize_image_generation_output
from code_cn_bridge.server import _image_generation_output_dir
from code_cn_bridge.server import _is_meta_review_request
from code_cn_bridge.server import _is_image_generation_routing_candidate
from code_cn_bridge.server import _looks_like_image_generation_suppression
from code_cn_bridge.server import _resolve_images_generation_entry
from code_cn_bridge.server import _parse_image_intent_decision
from code_cn_bridge.server import _should_handle_image_generation
from code_cn_bridge.server import _strip_too_small_images_from_input


class ProtocolToolTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DeepSeekAdapter()

    def test_codex_hosted_image_model_resolves_to_configured_image_slot(self):
        cfg = SimpleNamespace(
            model_mapping={
                "qwen-image": {
                    "target": "qwen-image-target",
                    "provider": "aliyun",
                    "is_image_gen": True,
                },
            },
            _data={"model_slots": {"image_gen": {"alias": "qwen-image"}}},
        )

        alias, entry = _resolve_images_generation_entry(cfg, "gpt-image-2")

        self.assertEqual(alias, "qwen-image")
        self.assertEqual(entry["target"], "qwen-image-target")

    def test_arbitrary_unknown_image_model_remains_unknown(self):
        cfg = SimpleNamespace(model_mapping={}, _data={"model_slots": {}})

        self.assertEqual(
            _resolve_images_generation_entry(cfg, "not-a-real-model"),
            ("not-a-real-model", None),
        )

    def test_web_search_tool_description_includes_bridge_date(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "today news"}],
                "tools": [{"type": "web_search"}],
            },
            self.adapter,
            "deepseek-chat",
        )

        description = request["tools"][0]["function"]["description"]
        self.assertIn(date.today().isoformat(), description)

    def test_deepseek_reasoning_effort_maps_to_thinking_policy(self):
        cases = [
            ("low", {"type": "disabled"}, None),
            ("medium", None, None),
            ("high", {"type": "enabled"}, "high"),
            ("xhigh", {"type": "enabled"}, "max"),
            ("max", {"type": "enabled"}, "max"),
            ("none", {"type": "disabled"}, None),
        ]

        for effort, expected_thinking, expected_reasoning_effort in cases:
            with self.subTest(effort=effort):
                request = translate_request(
                    {
                        "input": [{"type": "message", "role": "user", "content": "think"}],
                        "reasoning": {"effort": effort},
                    },
                    self.adapter,
                    "deepseek-chat",
                )

                if expected_thinking is None:
                    self.assertNotIn("thinking", request)
                else:
                    self.assertEqual(request["thinking"], expected_thinking)
                if expected_reasoning_effort is None:
                    self.assertNotIn("reasoning_effort", request)
                else:
                    self.assertEqual(request["reasoning_effort"], expected_reasoning_effort)

    def test_deepseek_forced_tool_choice_disables_thinking(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "search"}],
                "reasoning": {"effort": "high"},
                "tools": [{"type": "web_search"}],
                "tool_choice": "required",
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertEqual(request["tool_choice"], "required")
        self.assertEqual(request["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", request)

    def test_named_deepseek_provider_applies_compat_to_generic_adapter(self):
        request = {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            "tool_choice": "required",
        }

        result = _apply_named_provider_chat_compat(
            "deepseek",
            OpenAICompatibleAdapter(),
            request,
        )

        self.assertEqual(result["thinking"], {"type": "disabled"})
        self.assertEqual(result["tools"][0]["function"]["parameters"]["type"], "object")

    def test_image_gen_tool_is_exposed_as_chat_function(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "draw a logo"}],
                "tools": [{"type": "image_gen"}],
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertTrue(request["_has_image_gen"])
        self.assertEqual(request["tools"][0]["function"]["name"], "image_gen")

    def test_build_responses_response_always_has_full_usage_shape(self):
        response = build_responses_response([], "gpt-5", None)

        self.assertEqual(
            response["usage"],
            {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        )

    def test_stream_request_asks_upstream_to_include_usage(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "hello"}],
                "stream": True,
                "stream_options": {"custom": "kept"},
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertEqual(request["stream_options"]["custom"], "kept")
        self.assertTrue(request["stream_options"]["include_usage"])

    def test_stream_translator_defers_completion_until_usage_chunk_arrives(self):
        translator = StreamTranslator(
            model="gpt-5.5",
            defer_completion_until_stream_end=True,
        )

        chunks = [
            {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            },
        ]
        streamed_events = []
        for chunk in chunks:
            streamed_events.extend(translator.translate_chunk(chunk))

        self.assertFalse(any('"type": "response.completed"' in event for event in streamed_events))

        completed_events = translator._finish()
        completed = next(
            json.loads(event.removeprefix("data: ").strip())["response"]
            for event in completed_events
            if '"type": "response.completed"' in event
        )

        self.assertEqual(completed["usage"]["input_tokens"], 7)
        self.assertEqual(completed["usage"]["output_tokens"], 3)
        self.assertEqual(completed["usage"]["total_tokens"], 10)

    def test_stream_translator_splits_think_tags_across_chunks(self):
        translator = StreamTranslator(
            model="gpt-5.5",
            defer_completion_until_stream_end=True,
        )
        events = []
        for chunk in [
            {"choices": [{"delta": {"content": "<thi"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "nk>private notes"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "</thi"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "nk>final answer"}, "finish_reason": "stop"}]},
        ]:
            events.extend(translator.translate_chunk(chunk))
        events.extend(translator._finish())
        payloads = [
            json.loads(event.removeprefix("data: ").strip())
            for event in events
            if event.startswith("data: ")
        ]

        reasoning = "".join(
            payload["delta"]
            for payload in payloads
            if payload.get("type") == "response.reasoning_summary_text.delta"
        )
        text = "".join(
            payload["delta"]
            for payload in payloads
            if payload.get("type") == "response.output_text.delta"
        )
        completed = next(payload["response"] for payload in payloads if payload.get("type") == "response.completed")

        self.assertEqual(reasoning, "private notes")
        self.assertEqual(text, "final answer")
        self.assertNotIn("<think>", json.dumps(completed, ensure_ascii=False))

    def test_hosted_image_generation_tool_is_exposed_as_chat_function(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "generate a poster"}],
                "tools": [{"type": "image_generation", "output_format": "png"}],
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertTrue(request["_has_image_gen"])
        self.assertEqual(request["tools"][0]["function"]["name"], "image_gen")

    def test_image_generation_guardrail_prevents_local_cli_fallback(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "generate an image"}],
                "tools": [
                    {"type": "function", "name": "exec_command", "description": "Run shell"},
                    {"type": "image_generation"},
                ],
            },
            self.adapter,
            "deepseek-chat",
        )

        system_text = "\n".join(
            msg["content"] for msg in request["messages"]
            if msg.get("role") == "system"
        )
        self.assertIn("MUST call", system_text)
        self.assertIn("image_gen", system_text)
        self.assertIn("Do not use shell commands", system_text)

    def test_image_generation_intercepts_only_visual_requests(self):
        self.assertFalse(_should_handle_image_generation({
            "input": [{"type": "message", "role": "user", "content": "help me make a technology poster"}],
        }, True))
        self.assertFalse(_should_handle_image_generation({
            "input": [{"type": "message", "role": "user", "content": "help me check why Python tests failed"}],
        }, True))

    def test_explicit_image_request_without_hosted_tool_uses_bridge_fallback(self):
        self.assertTrue(_should_handle_image_generation({
            "input": [{"type": "message", "role": "user", "content": "生成一个花的图片"}],
        }, False))
        self.assertFalse(_should_handle_image_generation({
            "input": [{"type": "message", "role": "user", "content": "这个图片是什么内容"}],
        }, False))
        self.assertFalse(_should_handle_image_generation({
            "input": [{"type": "message", "role": "user", "content": "把这个文件导出为 PNG"}],
        }, False))

    def test_extracts_codex_input_text_content_for_image_generation(self):
        content = [
            {"type": "input_text", "text": "generate an image"},
            {"type": "input_image", "image_url": "data:image/png;base64,abc"},
        ]

        self.assertEqual(_extract_text_content(content), "generate an image")

    def test_text_image_fallback_finishes_with_download_message(self):
        image_item = {"type": "image_generation_call", "result": "abc"}

        finalized = _finalize_image_generation_output(
            [image_item],
            True,
            "flower.png",
        )

        self.assertEqual(finalized[0], image_item)
        self.assertEqual(finalized[-1]["type"], "message")
        self.assertIn("flower.png", str(finalized[-1]))

    def test_ambiguous_spreadsheet_image_request_is_classification_candidate(self):
        self.assertTrue(_is_image_generation_routing_candidate("找10个无LOGO的拉牌，插入图片，插入编号"))
        self.assertFalse(_is_image_generation_routing_candidate("把这张图片导出为 PNG"))

    def test_parses_json_image_intent_decision(self):
        decision = _parse_image_intent_decision({
            "choices": [{"message": {"content": json.dumps({
                "shouldGenerateImage": False,
                "reason": "spreadsheet file task",
                "prompt": "",
            })}}],
        })

        self.assertEqual(decision["shouldGenerateImage"], False)
        self.assertEqual(decision["prompt"], "")

    def test_image_intent_uses_text_model_then_reasoning_fallback(self):
        cfg = SimpleNamespace(
            model_slots={
                "text": {"alias": "text-fast", "enabled": True},
                "reasoning_text": {"alias": "text-reasoning", "enabled": True},
            },
            model_mapping={
                "text-fast": {"enabled": True},
                "text-reasoning": {"enabled": True, "is_reasoning_text": True},
            },
            slot_alias=lambda slot: {"text": "text-fast", "reasoning_text": "text-reasoning"}[slot],
            get_provider=lambda provider: {"timeout": 30},
        )
        adapter = SimpleNamespace(preprocess_chat_request=lambda request: request)
        client = SimpleNamespace(chat_completion=AsyncMock(side_effect=[
            {"choices": [{"message": {"content": "not-json"}}]},
            {"choices": [{"message": {"content": json.dumps({
                "shouldGenerateImage": False,
                "reason": "spreadsheet file task",
                "prompt": "",
            })}}]},
        ]))

        with patch(
            "code_cn_bridge.server._get_adapter_for_model",
            side_effect=[
                (adapter, "provider-fast", "fast-model", "key"),
                (adapter, "provider-reasoning", "reasoning-model", "key"),
            ],
        ) as get_adapter, patch("code_cn_bridge.server.get_upstream_client", return_value=client):
            decision = asyncio.run(_classify_image_generation_intent(
                cfg,
                "做一个表格，插入2个产品图片跟编号",
                "request-1",
            ))

        self.assertFalse(decision["shouldGenerateImage"])
        self.assertEqual(
            [record.args[0] for record in get_adapter.call_args_list],
            ["text-fast", "text-reasoning"],
        )

    def test_saved_image_keeps_download_message_when_turn_continuation_is_false(self):
        finalized = _finalize_image_generation_output(
            [{"type": "image_generation_call", "result": "abc"}],
            False,
            "flower.png",
        )

        self.assertIn("flower.png", str(finalized[-1]))

    def test_image_prompt_removes_agent_permission_preamble(self):
        prompt = (
            "permission context\n"
            "\u4e0a\u4f20\u6587\u4ef6\u5728 input\uff0c\u4ea4\u4ed8\u6587\u4ef6\u5fc5\u987b\u5199\u5165 output\u3002\n\n"
            "\u751f\u6210\u4e00\u4e2a\u82b1\u7684\u56fe\u7247"
        )

        self.assertEqual(
            _extract_image_generation_prompt(prompt),
            "\u751f\u6210\u4e00\u4e2a\u82b1\u7684\u56fe\u7247",
        )

    def test_image_output_uses_current_task_output_directory(self):
        workspace = Path(r"C:\runtime\workspace\users\u_2\tasks\task-1")

        self.assertEqual(
            _image_generation_output_dir(workspace),
            workspace / "output",
        )

    def test_meta_review_transcript_does_not_trigger_image_generation(self):
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": (
                        "The following is the Codex agent history whose request action you are assessing\n"
                        ">>> TRANSCRIPT START\n"
                        "[1] user: generate an image"
                    ),
                }
            ]
        }

        self.assertTrue(_is_meta_review_request(body["input"][0]["content"]))
        self.assertFalse(_should_handle_image_generation(body, True))

    def test_image_generation_does_not_retrigger_from_assistant_retry_history(self):
        body = {
            "input": [
                {"type": "message", "role": "user", "content": "generate an image of a bridge"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "鍥剧墖宸茬敓鎴愬苟淇濆瓨鍒? C:\\temp\\generated.png",
                },
            ],
            "tools": [{"type": "image_generation"}],
        }

        self.assertFalse(_should_handle_image_generation(body, True))

    def test_editable_fireworks_png_does_not_trigger_image_generation(self):
        text = "make this a Fireworks/FW editable PNG"

        self.assertTrue(_looks_like_image_generation_suppression(text))
        self.assertFalse(_should_handle_image_generation({
            "input": [{"type": "message", "role": "user", "content": text}],
        }, True))

    def test_extracts_image_gen_prompt_from_router_tool_call(self):
        chat_resp = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "image_gen",
                                    "arguments": json.dumps({"prompt": "swimsuit character"}),
                                }
                            }
                        ]
                    }
                }
            ]
        }

        self.assertEqual(
            _extract_image_gen_prompt_from_chat_response(chat_resp),
            "swimsuit character",
        )

    def test_image_generation_history_maps_to_valid_chat_content(self):
        request = translate_request(
            {
                "input": [
                    {"type": "message", "role": "user", "content": "generate an image"},
                    {
                        "type": "image_generation_call",
                        "id": "icall_1",
                        "status": "completed",
                        "result": "abc123",
                    },
                ]
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertIsInstance(request["messages"][1]["content"], str)

    def test_buffered_sse_completes_image_generation_call(self):
        response = {
            "id": "resp_img",
            "object": "response",
            "model": "gpt-5",
            "status": "completed",
            "output": [
                {
                    "id": "icall_1",
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": "abc123",
                }
            ],
            "usage": {},
            "end_turn": False,
        }

        async def collect():
            return [event async for event in _buffered_responses_sse(response)]

        events = asyncio.run(collect())
        payloads = [
            json.loads(event.removeprefix("data: ").strip())
            for event in events
            if event.startswith("data: ") and event.strip() != "data: [DONE]"
        ]
        added = next(payload for payload in payloads if payload["type"] == "response.output_item.added")
        done = next(payload for payload in payloads if payload["type"] == "response.output_item.done")

        self.assertNotIn("result", added["item"])
        self.assertEqual(done["item"]["result"], "abc123")
        self.assertTrue(any('"type": "response.output_item.done"' in event for event in events))
        self.assertTrue(any('"type": "response.completed"' in event for event in events))
        self.assertTrue(any('"end_turn": false' in event for event in events))
        self.assertEqual(events[-1], "data: [DONE]\n\n")

    def test_extracts_workspace_dir_from_recent_tool_workdir(self):
        workspace = _extract_workspace_dir_from_responses_body({
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "# AGENTS.md instructions for C:\\Project\\Example\n",
                },
                {
                    "type": "function_call",
                    "name": "shell_command",
                    "arguments": json.dumps({
                        "command": "Get-ChildItem",
                        "workdir": "D:\\Workspace\\App",
                    }),
                },
            ],
        })

        self.assertEqual(str(workspace), "D:\\Workspace\\App")

    def test_extracts_workspace_dir_from_agents_instructions(self):
        workspace = _extract_workspace_dir_from_responses_body({
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "# AGENTS.md instructions for C:\\Project\\Example\n\n<INSTRUCTIONS>",
                },
            ],
        })

        self.assertEqual(str(workspace), "C:\\Project\\Example")

    def test_required_tool_choice_does_not_force_image_when_tools_are_mixed(self):
        self.assertFalse(_should_handle_image_generation({
            "tool_choice": "required",
            "tools": [{"type": "function", "name": "exec_command"}, {"type": "image_generation"}],
            "input": [{"type": "message", "role": "user", "content": "review this code"}],
        }, True))
        self.assertTrue(_should_handle_image_generation({
            "tool_choice": "required",
            "tools": [{"type": "image_generation"}],
            "input": [{"type": "message", "role": "user", "content": "make something"}],
        }, True))

    def test_compacts_only_old_large_tool_outputs(self):
        items = [
            {"type": "function_call_output", "output": "a" * 9000},
            {"type": "function_call_output", "output": "b" * 9000},
            {"type": "function_call_output", "output": "c" * 9000},
        ]

        result = _compact_historical_tool_outputs(items, keep_recent=1, max_chars=1000)

        self.assertEqual(result["trimmed_items"], 2)
        self.assertLessEqual(len(items[0]["output"]), 1000)
        self.assertLessEqual(len(items[1]["output"]), 1000)
        self.assertEqual(items[2]["output"], "c" * 9000)

    def test_chat_replay_deduplicates_dynamic_context_and_bounds_old_reasoning(self):
        items = [
            {"type": "message", "role": "developer", "content": "<skills_instructions>old</skills_instructions>"},
            {"type": "message", "role": "user", "content": "<environment_context>old</environment_context>"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "r" * 9000}]},
            {"type": "message", "role": "developer", "content": "<skills_instructions>new</skills_instructions>"},
            {"type": "message", "role": "user", "content": "<environment_context>new</environment_context>"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "recent"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "latest"}]},
        ]

        result = _compact_chat_replay_history(items)

        serialized = json.dumps(items)
        self.assertNotIn("old</skills_instructions>", serialized)
        self.assertNotIn("old</environment_context>", serialized)
        self.assertIn("new</skills_instructions>", serialized)
        self.assertIn("new</environment_context>", serialized)
        old_reasoning = next(item for item in items if item.get("type") == "reasoning")
        self.assertLessEqual(len(old_reasoning["summary"][0]["text"]), 2000)
        self.assertEqual(result["deduped_items"], 2)
        self.assertEqual(result["reasoning_items"], 1)

    def test_strips_too_small_data_images_before_vision_routing(self):
        one_by_one_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
            "WjR9awAAAABJRU5ErkJggg=="
        )
        items = [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "inspect"},
                {"type": "input_image", "image_url": one_by_one_png},
            ],
        }]

        result = _strip_too_small_images_from_input(items)

        self.assertEqual(result["removed_small_images"], 1)
        self.assertEqual(items[0]["content"], [{"type": "input_text", "text": "inspect"}])

    def test_doubao_image_generation_omits_size(self):
        body = DoubaoAdapter().preprocess_image_gen_request({
            "model": "doubao-seedream",
            "prompt": "test",
            "size": "1024x1024",
        })

        self.assertNotIn("size", body)

    def test_qwen_image_generation_normalizes_codex_auto_size(self):
        body = QwenAdapter().preprocess_image_gen_request({
            "model": "qwen-image",
            "prompt": "test",
            "size": "auto",
        })

        self.assertEqual(body["parameters"]["size"], "1024*1024")

    def test_qwen_image_generation_uses_dashscope_size_separator(self):
        body = QwenAdapter().preprocess_image_gen_request({
            "model": "qwen-image",
            "prompt": "test",
            "size": "1536x1024",
        })

        self.assertEqual(body["parameters"]["size"], "1536*1024")

    def test_extracts_qwen_generated_image_url(self):
        response = {
            "output": {
                "choices": [{
                    "message": {
                        "content": [{"image": "https://images.example.test/generated.png"}],
                    },
                }],
            },
        }

        image_data, image_url = _extract_generated_image_reference(response)

        self.assertEqual(image_data, "")
        self.assertEqual(image_url, "https://images.example.test/generated.png")

    def test_extracts_data_uri_generated_image(self):
        image_data, image_url = _extract_generated_image_reference({
            "data": [{"url": "data:image/png;base64,aGVsbG8="}],
        })

        self.assertEqual(image_data, "aGVsbG8=")
        self.assertEqual(image_url, "")

    def test_downloads_generated_image_url_as_base64_for_codex(self):
        class FakeResponse:
            status_code = 200
            content = b"generated-image-bytes"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url):
                self.url = url
                return FakeResponse()

        with patch("code_cn_bridge.server.make_async_client", return_value=FakeClient()):
            encoded = asyncio.run(_download_generated_image_as_base64(
                "https://images.example.test/generated.png"
            ))

        self.assertEqual(encoded, "Z2VuZXJhdGVkLWltYWdlLWJ5dGVz")

    def test_openai_compatible_ark_image_generation_omits_size(self):
        adapter = OpenAICompatibleAdapter()
        adapter.base_url = "https://ark.cn-beijing.volces.com/api/v3"

        body = adapter.preprocess_image_gen_request({
            "model": "doubao-seedream",
            "prompt": "test",
            "size": "1024x1024",
        })

        self.assertNotIn("size", body)

    def test_translates_codex_function_tool_without_renaming(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Run pwd"}],
                "tools": [
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "Run a command.",
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {"cmd": {"type": "string"}},
                            "required": ["cmd"],
                            "additionalProperties": False,
                        },
                    }
                ],
                "tool_choice": "auto",
            },
            self.adapter,
            "deepseek-chat",
        )

        tool = request["tools"][0]["function"]
        self.assertEqual(tool["name"], "exec_command")
        self.assertTrue(tool["strict"])
        self.assertIn("cmd", tool["parameters"]["properties"])
        self.assertNotIn("strict", tool["parameters"])
        self.assertEqual(request["tool_choice"], "auto")

    def test_maps_codex_local_shell_tool_to_native_response_item(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Run dir"}],
                "tools": [{"type": "local_shell"}],
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertEqual(request["tools"][0]["function"]["name"], "local_shell")
        self.assertEqual(request["_response_tool_types"]["local_shell"], "local_shell_call")

        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_shell",
                                    "type": "function",
                                    "function": {
                                        "name": "local_shell",
                                        "arguments": json.dumps({"command": ["cmd", "/c", "dir"]}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
            response_tool_types={"local_shell": "local_shell_call"},
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "local_shell_call")
        self.assertEqual(item["call_id"], "call_shell")
        self.assertEqual(item["action"]["type"], "exec")
        self.assertEqual(item["action"]["command"], ["cmd", "/c", "dir"])

    def test_maps_codex_tool_search_tool_to_native_response_item(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Find tools"}],
                "tools": [{"type": "tool_search"}],
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertEqual(request["tools"][0]["function"]["name"], "tool_search")
        self.assertEqual(request["_response_tool_types"]["tool_search"], "tool_search_call")

        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_search",
                                    "type": "function",
                                    "function": {
                                        "name": "tool_search",
                                        "arguments": json.dumps({"query": "browser", "limit": 5}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
            response_tool_types={"tool_search": "tool_search_call"},
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "tool_search_call")
        self.assertTrue(item["id"].startswith("tsc_"))
        self.assertEqual(item["call_id"], "call_search")
        self.assertEqual(item["execution"], "client")
        self.assertEqual(item["arguments"]["query"], "browser")

    def test_maps_codex_computer_use_tool_like_codex_plus_plus_proxy(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Use the computer"}],
                "tools": [{"type": "computer_use"}],
            },
            self.adapter,
            "deepseek-chat",
        )

        function = request["tools"][0]["function"]
        self.assertEqual(function["name"], "computer_use")
        self.assertEqual(function["parameters"]["required"], ["input"])
        self.assertEqual(request["_custom_tool_names"], ["computer_use"])
        self.assertNotIn("_response_tool_types", request)

        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_computer",
                                    "type": "function",
                                    "function": {
                                        "name": "computer_use",
                                        "arguments": json.dumps({"input": "click the OK button"}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
            custom_tool_names={"computer_use"},
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["name"], "computer_use")
        self.assertEqual(item["call_id"], "call_computer")
        self.assertEqual(item["input"], "click the OK button")

    def test_non_stream_legacy_function_call_is_restored(self):
        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "function_call": {
                                "name": "exec_command",
                                "arguments": '{"cmd":"pwd"}',
                            }
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["name"], "exec_command")
        self.assertEqual(item["arguments"], '{"cmd":"pwd"}')

    def test_non_stream_length_finish_marks_response_incomplete(self):
        response = translate_response(
            {
                "choices": [
                    {
                        "message": {"content": "partial"},
                        "finish_reason": "length",
                    }
                ]
            },
            self.adapter,
            "gpt-5",
        )

        self.assertEqual(response["status"], "incomplete")
        self.assertEqual(response["incomplete_details"]["reason"], "max_output_tokens")

    def test_maps_tool_call_tool_result_history_like_anthropic_shape(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "tool_call",
                        "id": "toolu_1",
                        "name": "exec_command",
                        "input": {"cmd": "pwd"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "D:\\workspace",
                    },
                ]
            },
            self.adapter,
            "deepseek-chat",
        )

        assistant_call = request["messages"][0]["tool_calls"][0]
        self.assertEqual(assistant_call["id"], "toolu_1")
        self.assertEqual(assistant_call["function"]["name"], "exec_command")
        self.assertEqual(json.loads(assistant_call["function"]["arguments"])["cmd"], "pwd")
        self.assertEqual(request["messages"][1]["tool_call_id"], "toolu_1")

    def test_expands_namespace_tools_for_chat_provider(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Open a page"}],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl__",
                        "tools": [
                            {
                                "type": "function",
                                "name": "js",
                                "description": "Run JavaScript.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"code": {"type": "string"}},
                                    "required": ["code"],
                                },
                            }
                        ],
                    },
                ],
                "tool_choice": "auto",
            },
            self.adapter,
            "deepseek-chat",
        )

        tool = request["tools"][0]["function"]
        self.assertEqual(tool["name"], "mcp__node_repl__js")
        self.assertEqual(
            request["_namespace_tools"]["mcp__node_repl__js"],
            {"namespace": "mcp__node_repl__", "name": "js"},
        )
        self.assertEqual(request["tool_choice"], "auto")

    def test_restores_namespace_on_non_streaming_mcp_tool_call(self):
        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_browser",
                                    "type": "function",
                                    "function": {
                                        "name": "mcp__node_repl__js",
                                        "arguments": '{"code":"inspect()"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
            {"mcp__node_repl__js": {"namespace": "mcp__node_repl__", "name": "js"}},
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["name"], "js")
        self.assertEqual(item["namespace"], "mcp__node_repl__")

    def test_maps_namespaced_tool_history_back_to_flat_chat_tool_name(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "function_call",
                        "namespace": "mcp__node_repl__",
                        "name": "js",
                        "call_id": "call_browser",
                        "arguments": '{"code":"inspect()"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_browser",
                        "output": "page title",
                    },
                ]
            },
            self.adapter,
            "deepseek-chat",
        )

        assistant_call = request["messages"][0]["tool_calls"][0]
        self.assertEqual(assistant_call["function"]["name"], "mcp__node_repl__js")
        self.assertEqual(request["messages"][1]["tool_call_id"], "call_browser")

    def test_loads_tool_search_output_namespace_tools_for_chat_provider(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "tool_search_call",
                        "call_id": "call_search",
                        "arguments": {"query": "browser"},
                    },
                    {
                        "type": "tool_search_output",
                        "call_id": "call_search",
                        "status": "completed",
                        "execution": "client",
                        "tools": [
                            {
                                "type": "namespace",
                                "name": "mcp__node_repl",
                                "description": "Node REPL tools.",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "js",
                                        "description": "Run JavaScript.",
                                        "parameters": {
                                            "type": "object",
                                            "properties": {"code": {"type": "string"}},
                                            "required": ["code"],
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {"type": "message", "role": "user", "content": "Open a page"},
                ],
                "tools": [{"type": "tool_search"}],
                "tool_choice": "auto",
            },
            self.adapter,
            "deepseek-chat",
        )

        tool_names = [tool["function"]["name"] for tool in request["tools"]]
        self.assertIn("tool_search", tool_names)
        self.assertIn("mcp__node_repl__js", tool_names)
        self.assertEqual(
            request["_namespace_tools"]["mcp__node_repl__js"],
            {"namespace": "mcp__node_repl", "name": "js"},
        )

    def test_maps_stock_codex_apply_patch_custom_tool_through_chat_function(self):
        patch = "*** Begin Patch\n*** Add File: demo.txt\n+ok\n*** End Patch\n"
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Edit demo.txt"}],
                "tools": [
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "description": "Use apply_patch.",
                        "format": {"type": "grammar", "syntax": "lark", "definition": "start: patch"},
                    }
                ],
            },
            self.adapter,
            "deepseek-chat",
        )

        function = request["tools"][0]["function"]
        self.assertEqual(function["name"], "apply_patch")
        self.assertIn("patch", function["parameters"]["properties"])
        self.assertIn("*** Begin Patch", function["description"])
        self.assertIn("not git diff", function["description"])

        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_patch",
                                    "type": "function",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": json.dumps({"patch": patch}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["call_id"], "call_patch")
        self.assertEqual(item["input"], patch)

    def test_maps_custom_tool_history_back_to_chat_tool_messages(self):
        patch = "*** Begin Patch\n*** Add File: demo.txt\n+ok\n*** End Patch\n"
        request = translate_request(
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "call_patch",
                        "input": patch,
                    },
                    {
                        "type": "custom_tool_call_output",
                        "name": "apply_patch",
                        "call_id": "call_patch",
                        "output": "Success. Updated files.",
                    },
                ]
            },
            self.adapter,
            "deepseek-chat",
        )

        assistant_call = request["messages"][0]["tool_calls"][0]
        self.assertEqual(assistant_call["function"]["name"], "apply_patch")
        self.assertEqual(json.loads(assistant_call["function"]["arguments"])["patch"], patch)
        self.assertEqual(request["messages"][1]["tool_call_id"], "call_patch")

    def test_maps_generic_custom_tool_like_codex_plus_plus(self):
        request = translate_request(
            {
                "input": [{"type": "message", "role": "user", "content": "Run custom"}],
                "tools": [
                    {
                        "type": "custom",
                        "name": "shell_freeform",
                        "description": "Execute a freeform shell request.",
                    }
                ],
            },
            self.adapter,
            "deepseek-chat",
        )

        function = request["tools"][0]["function"]
        self.assertEqual(function["name"], "shell_freeform")
        self.assertEqual(function["parameters"]["required"], ["input"])
        self.assertEqual(request["_custom_tool_names"], ["shell_freeform"])

        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_custom",
                                    "type": "function",
                                    "function": {
                                        "name": "shell_freeform",
                                        "arguments": json.dumps({"input": "ls -la"}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
            custom_tool_names={"shell_freeform"},
        )

        item = response["output"][0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["name"], "shell_freeform")
        self.assertEqual(item["input"], "ls -la")

    def test_maps_generic_custom_tool_history_back_to_input_argument(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "name": "shell_freeform",
                        "call_id": "call_custom",
                        "input": "ls -la",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "name": "shell_freeform",
                        "call_id": "call_custom",
                        "output": "total 1",
                    },
                ]
            },
            self.adapter,
            "deepseek-chat",
        )

        assistant_call = request["messages"][0]["tool_calls"][0]
        self.assertEqual(assistant_call["function"]["name"], "shell_freeform")
        self.assertEqual(json.loads(assistant_call["function"]["arguments"])["input"], "ls -la")
        self.assertEqual(request["messages"][1]["tool_call_id"], "call_custom")

    def test_maps_function_call_and_output_with_same_call_id(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "call_123",
                        "arguments": '{"cmd":"pwd"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": "D:\\workspace",
                    },
                ]
            },
            self.adapter,
            "deepseek-chat",
        )

        self.assertEqual(request["messages"][0]["tool_calls"][0]["id"], "call_123")
        self.assertEqual(request["messages"][1]["tool_call_id"], "call_123")

        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_456",
                                    "type": "function",
                                    "function": {
                                        "name": "exec_command",
                                        "arguments": '{"cmd":"pwd"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
        )

        self.assertEqual(response["output"][0]["call_id"], "call_456")

    def test_preserves_view_image_output_as_multimodal_followup_message(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "function_call",
                        "name": "view_image",
                        "call_id": "call_image",
                        "arguments": '{"path":"slide.png"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_image",
                        "output": [
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,abc123",
                            }
                        ],
                    },
                ]
            },
            QwenAdapter(),
            "qwen3.6-plus",
        )

        self.assertEqual(request["messages"][1]["role"], "tool")
        self.assertEqual(request["messages"][1]["tool_call_id"], "call_image")
        self.assertEqual(request["messages"][2]["role"], "user")
        self.assertEqual(
            request["messages"][2]["content"][1],
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc123"},
            },
        )
        self.assertFalse(request["enable_thinking"])

    def test_qwen_text_request_does_not_force_thinking_setting(self):
        request = translate_request(
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "plain text only",
                    }
                ]
            },
            QwenAdapter(),
            "qwen3.6-plus",
        )

        self.assertNotIn("enable_thinking", request)

    def test_non_stream_empty_response_is_reported_as_failure(self):
        with self.assertRaisesRegex(ValueError, "without returning text or tool calls"):
            translate_response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                            }
                        }
                    ]
                },
                self.adapter,
                "gpt-5",
            )

    def test_non_stream_reasoning_without_action_is_reported_as_failure(self):
        with self.assertRaisesRegex(ValueError, "ended after reasoning"):
            translate_response(
                {
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "thinking only",
                                "content": None,
                            }
                        }
                    ]
                },
                self.adapter,
                "gpt-5",
            )

    def test_non_stream_preserves_reasoning_before_tool_call(self):
        response = translate_response(
            {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "Need inspect the code.",
                            "tool_calls": [
                                {
                                    "id": "call_inspect",
                                    "type": "function",
                                    "function": {
                                        "name": "exec_command",
                                        "arguments": '{"cmd":"rg TODO"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            self.adapter,
            "gpt-5",
        )

        self.assertEqual(response["output"][0]["type"], "reasoning")
        self.assertEqual(
            response["output"][0]["summary"][0]["text"],
            "Need inspect the code.",
        )
        self.assertEqual(response["output"][1]["type"], "function_call")

    def test_stream_buffers_function_arguments_until_done_item(self):
        translator = StreamTranslator(response_id="resp_test", model="gpt-5")
        events = []
        events.extend(
            translator.translate_chunk(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_stream",
                                        "function": {
                                            "name": "exec_command",
                                            "arguments": '{"cmd":"',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
        )
        events.extend(
            translator.translate_chunk(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": "pwd\"}"},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        done = next(p for p in payloads if p["type"] == "response.output_item.done")
        completed = next(p for p in payloads if p["type"] == "response.completed")

        self.assertEqual(done["item"]["call_id"], "call_stream")
        self.assertEqual(done["item"]["name"], "exec_command")
        self.assertEqual(done["item"]["arguments"], '{"cmd":"pwd"}')
        self.assertEqual(completed["response"]["status"], "completed")
        self.assertIn("input_tokens", completed["response"]["usage"])
        self.assertIn("output_tokens", completed["response"]["usage"])
        self.assertIn("total_tokens", completed["response"]["usage"])

    def test_stream_restores_apply_patch_as_custom_tool_call(self):
        patch = "*** Begin Patch\n*** Add File: demo.txt\n+ok\n*** End Patch\n"
        arguments = json.dumps({"patch": patch})
        translator = StreamTranslator(response_id="resp_patch", model="gpt-5")
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_patch",
                                    "function": {"name": "apply_patch", "arguments": arguments},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        done = next(p for p in payloads if p["type"] == "response.output_item.done")

        self.assertEqual(done["item"]["type"], "custom_tool_call")
        self.assertEqual(done["item"]["call_id"], "call_patch")
        self.assertEqual(done["item"]["input"], patch)

    def test_stream_restores_generic_custom_tool_call(self):
        translator = StreamTranslator(
            response_id="resp_custom",
            model="gpt-5",
            custom_tool_names={"shell_freeform"},
        )
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_custom",
                                    "function": {
                                        "name": "shell_freeform",
                                        "arguments": json.dumps({"input": "ls -la"}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        added = next(p for p in payloads if p["type"] == "response.output_item.added")
        delta = next(p for p in payloads if p["type"] == "response.custom_tool_call_input.delta")
        done = next(p for p in payloads if p["type"] == "response.output_item.done")

        self.assertEqual(added["item"]["type"], "custom_tool_call")
        self.assertEqual(delta["delta"], "ls -la")
        self.assertEqual(done["item"]["type"], "custom_tool_call")
        self.assertEqual(done["item"]["input"], "ls -la")

    def test_stream_restores_local_shell_call(self):
        translator = StreamTranslator(
            response_id="resp_shell",
            model="gpt-5",
            response_tool_types={"local_shell": "local_shell_call"},
        )
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_shell",
                                    "function": {
                                        "name": "local_shell",
                                        "arguments": json.dumps({"command": ["cmd", "/c", "dir"]}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        done = next(p for p in payloads if p["type"] == "response.output_item.done")

        self.assertEqual(done["item"]["type"], "local_shell_call")
        self.assertEqual(done["item"]["call_id"], "call_shell")
        self.assertEqual(done["item"]["action"]["command"], ["cmd", "/c", "dir"])

    def test_stream_restores_tool_search_call(self):
        translator = StreamTranslator(
            response_id="resp_tool_search",
            model="gpt-5",
            response_tool_types={"tool_search": "tool_search_call"},
        )
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_tool_search",
                                    "function": {
                                        "name": "tool_search",
                                        "arguments": json.dumps({"query": "browser"}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        done = next(p for p in payloads if p["type"] == "response.output_item.done")

        self.assertEqual(done["item"]["type"], "tool_search_call")
        self.assertTrue(done["item"]["id"].startswith("tsc_"))
        self.assertEqual(done["item"]["call_id"], "call_tool_search")
        self.assertEqual(done["item"]["execution"], "client")
        self.assertEqual(done["item"]["arguments"]["query"], "browser")

    def test_stream_restores_computer_use_as_custom_tool_call(self):
        translator = StreamTranslator(
            response_id="resp_computer",
            model="gpt-5",
            custom_tool_names={"computer_use"},
        )
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_computer",
                                    "function": {
                                        "name": "computer_use",
                                        "arguments": json.dumps({"input": "open the menu"}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        done = next(p for p in payloads if p["type"] == "response.output_item.done")

        self.assertEqual(done["item"]["type"], "custom_tool_call")
        self.assertEqual(done["item"]["name"], "computer_use")
        self.assertEqual(done["item"]["call_id"], "call_computer")
        self.assertEqual(done["item"]["input"], "open the menu")

    def test_stream_accepts_reasoning_field_from_chat_provider(self):
        translator = StreamTranslator(response_id="resp_reasoning_field", model="gpt-5")
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {"reasoning": "Need inspect."},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        reasoning_delta = next(
            p for p in payloads if p["type"] == "response.reasoning_summary_text.delta"
        )
        failed = next(p for p in payloads if p["type"] == "response.failed")

        self.assertEqual(reasoning_delta["delta"], "Need inspect.")
        self.assertEqual(failed["response"]["error"]["type"], "reasoning_without_action")

    def test_stream_restores_namespace_on_mcp_tool_call(self):
        translator = StreamTranslator(
            response_id="resp_browser",
            model="gpt-5",
            namespace_tools={"mcp__node_repl__js": {"namespace": "mcp__node_repl__", "name": "js"}},
        )
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_browser",
                                    "function": {
                                        "name": "mcp__node_repl__js",
                                        "arguments": '{"code":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        done = next(p for p in payloads if p["type"] == "response.output_item.done")

        self.assertEqual(done["item"]["name"], "js")
        self.assertEqual(done["item"]["namespace"], "mcp__node_repl__")

    def test_stream_fails_instead_of_claiming_success_for_empty_output(self):
        translator = StreamTranslator(response_id="resp_empty", model="gpt-5")
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]

        failed = next(p for p in payloads if p["type"] == "response.failed")
        self.assertEqual(failed["response"]["status"], "failed")
        self.assertEqual(failed["response"]["error"]["type"], "empty_upstream_response")
        self.assertFalse(any(p["type"] == "response.completed" for p in payloads))

    def test_stream_preserves_reasoning_before_tool_call(self):
        translator = StreamTranslator(response_id="resp_reasoning_tool", model="gpt-5")
        events = []
        events.extend(
            translator.translate_chunk(
                {
                    "choices": [
                        {"delta": {"reasoning_content": "Need inspect the code."}}
                    ]
                }
            )
        )
        events.extend(
            translator.translate_chunk(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_inspect",
                                        "function": {
                                            "name": "exec_command",
                                            "arguments": '{"cmd":"rg TODO"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        reasoning_delta = next(
            p for p in payloads if p["type"] == "response.reasoning_summary_text.delta"
        )
        completed = next(p for p in payloads if p["type"] == "response.completed")

        self.assertEqual(reasoning_delta["delta"], "Need inspect the code.")
        self.assertEqual(completed["response"]["output"][0]["type"], "reasoning")
        self.assertEqual(completed["response"]["output"][1]["type"], "function_call")

    def test_stream_reasoning_without_action_fails_instead_of_completing(self):
        translator = StreamTranslator(response_id="resp_reasoning_only", model="gpt-5")
        events = translator.translate_chunk(
            {
                "choices": [
                    {
                        "delta": {"reasoning_content": "thinking only"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        payloads = [json.loads(line.removeprefix("data: ").strip()) for line in events]
        reasoning_delta = next(
            p for p in payloads if p["type"] == "response.reasoning_summary_text.delta"
        )
        failed = next(p for p in payloads if p["type"] == "response.failed")

        self.assertEqual(reasoning_delta["delta"], "thinking only")
        self.assertEqual(failed["response"]["error"]["type"], "reasoning_without_action")
        self.assertFalse(any(p["type"] == "response.completed" for p in payloads))


class VisionRoutingTests(unittest.TestCase):
    def _config(self, model_mapping, vision_routing=None, model_slots=None):
        return SimpleNamespace(
            model_mapping=model_mapping,
            vision_routing=vision_routing or {},
            model_slots=model_slots or {},
            slot_alias=lambda slot_id: (model_slots or {}).get(slot_id, {}).get("alias", slot_id),
        )

    def test_plain_text_request_stays_on_text_model(self):
        cfg = self._config({
            "gpt-5.5": {"provider": "deepseek", "target": "deepseek-v4-pro", "is_multimodal": False},
            "gpt-5": {"provider": "qwen", "target": "qwen3.6-plus", "is_multimodal": True},
        })
        expected = (object(), "deepseek", "deepseek-v4-pro", "key")

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._get_adapter_for_model", return_value=expected
        ) as get_adapter:
            routed = _route_vision(
                "gpt-5.5",
                {"input": [{"type": "message", "role": "user", "content": "hello"}]},
            )

        self.assertEqual(routed, expected)
        get_adapter.assert_called_once_with("gpt-5.5")

    def test_low_reasoning_request_uses_plain_text_slot(self):
        cfg = self._config(
            {
                "gpt-5.5": {
                    "provider": "deepseek",
                    "target": "deepseek-v4-flash",
                    "is_multimodal": False,
                },
                "gpt-5.5-reasoning": {
                    "provider": "deepseek",
                    "target": "deepseek-v4-pro",
                    "is_multimodal": False,
                    "is_reasoning_text": True,
                },
            },
            model_slots={
                "text": {"alias": "gpt-5.5", "enabled": True},
                "reasoning_text": {"alias": "gpt-5.5-reasoning", "enabled": True},
            },
        )
        expected = (object(), "deepseek", "deepseek-v4-flash", "key")

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._get_adapter_for_model", return_value=expected
        ) as get_adapter:
            routed = _route_vision(
                "gpt-5.5",
                {
                    "reasoning": {"effort": "low"},
                    "input": [{"type": "message", "role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(routed, expected)
        get_adapter.assert_called_once_with("gpt-5.5")

    def test_medium_reasoning_request_uses_reasoning_text_slot(self):
        cfg = self._config(
            {
                "gpt-5.5": {
                    "provider": "deepseek",
                    "target": "deepseek-v4-flash",
                    "is_multimodal": False,
                },
                "gpt-5.5-reasoning": {
                    "provider": "deepseek",
                    "target": "deepseek-v4-pro",
                    "is_multimodal": False,
                    "is_reasoning_text": True,
                },
            },
            model_slots={
                "text": {"alias": "gpt-5.5", "enabled": True},
                "reasoning_text": {"alias": "gpt-5.5-reasoning", "enabled": True},
            },
        )
        expected = (object(), "deepseek", "deepseek-v4-pro", "key")

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._get_adapter_for_model", return_value=expected
        ) as get_adapter:
            routed = _route_vision(
                "gpt-5.5",
                {
                    "reasoning": {"effort": "medium"},
                    "input": [{"type": "message", "role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(routed, expected)
        get_adapter.assert_called_once_with("gpt-5.5-reasoning")

    def test_historical_image_is_removed_before_routing_new_text_turn(self):
        cfg = self._config({
            "text-model": {"provider": "deepseek", "target": "deepseek-v4-pro", "is_multimodal": False},
            "gpt-5": {"provider": "qwen", "target": "qwen3.6-plus", "is_multimodal": True},
        })
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,old"}],
                },
                {"type": "message", "role": "user", "content": "new text turn"},
            ]
        }

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._get_adapter_for_model",
            return_value=(object(), "deepseek", "deepseek-v4-pro", "key"),
        ) as get_adapter:
            _route_vision("gpt-5", body)

        get_adapter.assert_called_once_with("text-model")
        self.assertEqual(body["input"][0]["content"], [])

    def test_view_image_output_routes_current_turn_to_multimodal_alias(self):
        cfg = self._config({
            "gpt-5.5": {
                "provider": "deepseek",
                "target": "deepseek-v4-pro",
                "is_multimodal": False,
                "vision_alias": "gpt-5",
            },
            "gpt-5": {
                "provider": "qwen",
                "target": "qwen3.6-plus",
                "is_multimodal": True,
                "enabled": True,
            },
        })
        expected = (object(), "qwen", "qwen3.6-plus", "key")
        body = {
            "input": [
                {"type": "message", "role": "user", "content": "inspect this image"},
                {
                    "type": "function_call_output",
                    "call_id": "call_image",
                    "output": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                },
            ]
        }

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._resolve_adapter", return_value=expected
        ) as resolve_adapter:
            routed = _route_vision("gpt-5.5", body)

        self.assertEqual(routed, expected)
        resolve_adapter.assert_called_once_with("qwen", "qwen3.6-plus")

    def test_current_view_image_turn_strips_older_images_before_visual_route(self):
        cfg = self._config({
            "gpt-5.5": {
                "provider": "deepseek",
                "target": "deepseek-v4-pro",
                "is_multimodal": False,
                "vision_alias": "gpt-5",
            },
            "gpt-5": {
                "provider": "qwen",
                "target": "qwen3.6-plus",
                "is_multimodal": True,
                "enabled": True,
            },
        })
        body = {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "old_image",
                    "output": [{"type": "input_image", "image_url": "data:image/png;base64,old"}],
                },
                {"type": "message", "role": "assistant", "content": "old image was handled"},
                {
                    "type": "function_call_output",
                    "call_id": "new_image",
                    "output": [{"type": "input_image", "image_url": "data:image/png;base64,new"}],
                },
            ]
        }

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._resolve_adapter",
            return_value=(object(), "qwen", "qwen3.6-plus", "key"),
        ):
            _route_vision("gpt-5.5", body)

        self.assertEqual(body["input"][0]["output"], [])
        self.assertEqual(
            body["input"][2]["output"],
            [{"type": "input_image", "image_url": "data:image/png;base64,new"}],
        )

    def test_tool_output_after_image_response_returns_to_text_model(self):
        cfg = self._config({
            "gpt-5.5": {
                "provider": "deepseek",
                "target": "deepseek-v4-pro",
                "is_multimodal": False,
                "vision_alias": "gpt-5",
            },
            "gpt-5": {
                "provider": "qwen",
                "target": "qwen3.6-plus",
                "is_multimodal": True,
                "enabled": True,
            },
        })
        expected = (object(), "deepseek", "deepseek-v4-pro", "key")
        body = {
            "input": [
                {"type": "message", "role": "user", "content": "inspect the image and logs"},
                {
                    "type": "function_call_output",
                    "call_id": "call_image",
                    "output": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "The image contains a logo. I will check logs.",
                },
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_logs",
                    "arguments": '{"cmd":"read logs"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_logs",
                    "output": "provider=qwen",
                },
            ]
        }

        with patch("code_cn_bridge.server.get_config", return_value=cfg), patch(
            "code_cn_bridge.server._get_adapter_for_model", return_value=expected
        ) as get_adapter:
            routed = _route_vision("gpt-5.5", body)

        self.assertEqual(routed, expected)
        get_adapter.assert_called_once_with("gpt-5.5")
        self.assertEqual(body["input"][1]["output"], [])

    def test_plain_text_request_rejects_multimodal_model_without_text_route(self):
        cfg = self._config({
            "gpt-5": {"provider": "qwen", "target": "qwen3.6-plus", "is_multimodal": True},
        })

        with patch("code_cn_bridge.server.get_config", return_value=cfg):
            with self.assertRaisesRegex(ValueError, ".+"):
                _route_vision(
                    "gpt-5",
                    {"input": [{"type": "message", "role": "user", "content": "hello"}]},
                )


if __name__ == "__main__":
    unittest.main()

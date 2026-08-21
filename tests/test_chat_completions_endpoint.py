import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from code_cn_bridge import server
from code_cn_bridge.adapters.base import BaseAdapter


class _FakeConfig:
    _data = {"server": {"audit_enabled": False}}
    model_mapping = {"workbuddy-model": {"provider": "fake", "target": "upstream-model", "enabled": True}}

    @staticmethod
    def get_provider(name):
        return {"timeout": 120}


class _FakeAdapter(BaseAdapter):
    name = "fake"
    base_url = "https://example.test/v1"

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        routed = dict(chat_req)
        routed["metadata"] = {"preprocessed": True}
        return routed

    def stream_event_transform(self, raw_event: dict) -> dict:
        transformed = dict(raw_event)
        transformed["adapter_transformed"] = True
        return transformed


class _FakeClient:
    def __init__(self):
        self.requests = []

    async def chat_completion(self, body):
        self.requests.append(body)
        return {
            "id": "chatcmpl_fake",
            "object": "chat.completion",
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        }

    async def chat_completion_stream(self, body):
        self.requests.append(body)
        yield {
            "id": "chatcmpl_fake",
            "object": "chat.completion.chunk",
            "model": body["model"],
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
        }


class _BrokenStreamClient(_FakeClient):
    async def chat_completion_stream(self, body):
        async for chunk in super().chat_completion_stream(body):
            yield chunk
        raise ConnectionResetError("upstream reset")


class _InvalidUnicodeStreamClient(_FakeClient):
    async def chat_completion_stream(self, body):
        self.requests.append(body)
        yield {
            "id": "chatcmpl_unicode",
            "object": "chat.completion.chunk",
            "model": body["model"],
            "choices": [{"index": 0, "delta": {"content": "bad\udcadtext"}, "finish_reason": None}],
        }


class ChatCompletionsEndpointTests(unittest.TestCase):
    def test_audit_log_rotates_with_bounded_retention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"

            class AuditConfig(_FakeConfig):
                _data = {
                    "server": {
                        "audit_enabled": True,
                        "audit_log_path": str(audit_path),
                        "audit_log_max_bytes": 1024,
                        "audit_log_backup_count": 2,
                    }
                }

            with patch.object(server, "get_config", return_value=AuditConfig()):
                for index in range(30):
                    server._audit_event(
                        "test.rotation",
                        f"request-{index}",
                        payload="x" * 200,
                    )

            self.assertTrue(audit_path.exists())
            self.assertTrue(Path(f"{audit_path}.1").exists())
            self.assertTrue(Path(f"{audit_path}.2").exists())
            self.assertFalse(Path(f"{audit_path}.3").exists())
            self.assertLessEqual(audit_path.stat().st_size, 1024)

    def test_chat_text_distribution_classifies_prompt_context_tools_and_mcp(self):
        distribution = server._chat_text_distribution({
            "model": "workbuddy-model",
            "messages": [
                {"role": "system", "content": "software prompt"},
                {"role": "user", "content": "old context"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mcp",
                            "type": "function",
                            "function": {"name": "mcp__node_repl__js", "arguments": "{\"code\":\"1+1\"}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_mcp", "content": "tool result"},
                {"role": "user", "content": "current question"},
            ],
            "tools": [
                {"type": "function", "function": {"name": "exec_command", "description": "run shell"}},
                {"type": "namespace", "name": "mcp__node_repl", "tools": [{"name": "js"}]},
            ],
        })

        categories = distribution["categories"]
        self.assertEqual(categories["software"]["items"], 1)
        self.assertEqual(categories["conversation_context"]["items"], 1)
        self.assertEqual(categories["current_user"]["items"], 1)
        self.assertGreaterEqual(categories["tool_outputs"]["chars"], len("tool result"))
        self.assertGreaterEqual(categories["mcp"]["items"], 2)
        self.assertEqual(distribution["tools"]["total"], 2)
        self.assertEqual(distribution["tools"]["mcp"], 1)

    def test_responses_text_distribution_classifies_instructions_and_tool_outputs(self):
        distribution = server._responses_text_distribution({
            "instructions": "follow the app policy",
            "input": [
                {"type": "message", "role": "developer", "content": "software message"},
                {"type": "message", "role": "user", "content": "older user context"},
                {"type": "function_call_output", "output": "tool output text"},
                {"type": "message", "role": "user", "content": "latest user"},
            ],
            "tools": [{"type": "tool_search"}, {"type": "namespace", "name": "mcp__browser"}],
        })

        categories = distribution["categories"]
        self.assertEqual(categories["guide"]["items"], 1)
        self.assertEqual(categories["software"]["items"], 1)
        self.assertEqual(categories["conversation_context"]["items"], 1)
        self.assertEqual(categories["current_user"]["items"], 1)
        self.assertEqual(categories["tool_outputs"]["items"], 1)
        self.assertEqual(distribution["tools"]["mcp"], 1)

    def test_chat_endpoint_forwards_chat_request_without_responses_translation(self):
        adapter = _FakeAdapter()
        upstream = _FakeClient()
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            app = server.create_app()
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "workbuddy-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "chat.completion")
        self.assertEqual(upstream.requests[0]["model"], "upstream-model")
        self.assertEqual(upstream.requests[0]["messages"][0]["content"], "hello")
        self.assertNotIn("input", upstream.requests[0])

    def test_chat_endpoint_accepts_gzip_json_body(self):
        adapter = _FakeAdapter()
        upstream = _FakeClient()
        payload = gzip.compress(json.dumps({
            "model": "workbuddy-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }).encode("utf-8"))

        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            app = server.create_app()
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                content=payload,
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.requests[0]["messages"][0]["content"], "hello")

    def test_chat_endpoint_captures_full_workbuddy_request_text(self):
        adapter = _FakeAdapter()
        upstream = _FakeClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"

            class CaptureConfig(_FakeConfig):
                _data = {"server": {"audit_enabled": True, "audit_log_path": str(audit_path)}}

            with (
                patch.object(server, "get_config", return_value=CaptureConfig()),
                patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
                patch.object(server, "get_upstream_client", return_value=upstream),
            ):
                app = server.create_app()
                client = TestClient(app)
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "workbuddy-model",
                        "messages": [{"role": "user", "content": "hello capture"}],
                        "stream": False,
                    },
                    headers={
                        "user-agent": "WorkBuddy/5.2.3",
                        "x-ide-name": "WorkBuddy",
                        "authorization": "Bearer should-not-be-written",
                    },
                )

            capture_dir = Path(temp_dir) / "lan-bridge-workbuddy-captures"
            captures = list(capture_dir.glob("*.json"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(captures), 1)
            capture = json.loads(captures[0].read_text(encoding="utf-8"))
            self.assertIn("hello capture", capture["body_text_redacted"])
            self.assertEqual(capture["headers_redacted"]["authorization"], "***")
            self.assertNotIn("should-not-be-written", json.dumps(capture, ensure_ascii=False))

    def test_chat_endpoint_audits_invalid_json_body(self):
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_audit_event") as audit_event,
        ):
            app = server.create_app()
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                content=b"{bad-json",
                headers={"content-type": "application/json"},
            )

        self.assertEqual(response.status_code, 400)
        audit_event.assert_called()
        self.assertEqual(audit_event.call_args.args[0], "chat.invalid_json")
        self.assertIn("raw_sha256", audit_event.call_args.kwargs)

    def test_chat_endpoint_extracts_nested_http_inner_json(self):
        """收到嵌套 HTTP 请求（forward-proxy 格式）时，提取内层 JSON 并正常处理。"""
        adapter = _FakeAdapter()
        upstream = _FakeClient()
        inner_json = json.dumps({
            "model": "workbuddy-model",
            "messages": [{"role": "user", "content": "hi from nested"}],
            "stream": False,
        }).encode("utf-8")
        nested_body = (
            b"POST http://192.0.2.10:8765/v1/chat/completions HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(inner_json)).encode() + b"\r\n"
            b"\r\n" + inner_json
        )
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            app = server.create_app()
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                content=nested_body,
                headers={
                    "content-type": "application/json",
                    "user-agent": "WorkBuddy/5.2.3 CLI/2.106.4",
                    "x-ide-name": "WorkBuddy",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.requests[0]["messages"][0]["content"], "hi from nested")

    def test_chat_endpoint_repairs_truncated_nested_http(self):
        """嵌套 HTTP 内层 body 被截断时，尝试修复并提取有效部分。"""
        adapter = _FakeAdapter()
        upstream = _FakeClient()
        # 构造一个截断的 JSON：messages 完整，但 tools 部分被截断
        full_json = json.dumps({
            "model": "workbuddy-model",
            "messages": [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "truncated test"},
            ],
            "stream": False,
            "agent": "cli",
            "tools": [{"type": "function", "function": {"name": "tool1", "description": "a" * 10000}}],
        })
        # 截断后半部分
        truncated_json = full_json[:full_json.find('"tools"')]
        inner_bytes = truncated_json.encode("utf-8")
        # 声明更大的 Content-Length 模拟截断
        nested_body = (
            b"POST http://192.0.2.10:8765/v1/chat/completions HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(full_json)).encode() + b"\r\n"
            b"\r\n" + inner_bytes
        )
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            app = server.create_app()
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                content=nested_body,
                headers={"content-type": "application/json"},
            )

        self.assertEqual(response.status_code, 200)
        # 修复后的 body 应该有 model 和 messages
        self.assertEqual(upstream.requests[0]["model"], "upstream-model")
        msgs = upstream.requests[0]["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["content"], "truncated test")

    def test_chat_endpoint_rejects_completely_invalid_nested_http(self):
        """嵌套 HTTP 内层 body 完全无法解析时，返回配置错误。"""
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_audit_event") as audit_event,
        ):
            app = server.create_app()
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                content=(
                    b"POST http://192.0.2.10:8765/v1/chat/completions HTTP/1.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 999\r\n"
                    b"\r\n"
                    b'NOT JSON AT ALL {{{'
                ),
                headers={
                    "content-type": "application/json",
                    "user-agent": "WorkBuddy/5.2.3 CLI/2.106.4",
                    "x-ide-name": "WorkBuddy",
                },
            )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["type"], "invalid_nested_http_request")
        self.assertIn(
            "chat.invalid_nested_http_request",
            [call.args[0] for call in audit_event.call_args_list],
        )

    def test_chat_stream_uses_adapter_event_transform(self):
        adapter = _FakeAdapter()
        upstream = _FakeClient()
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            app = server.create_app()
            client = TestClient(app)
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "workbuddy-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            ) as response:
                body = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        first_data_line = next(line for line in body.splitlines() if line.startswith("data: {"))
        first_chunk = json.loads(first_data_line.removeprefix("data: "))
        self.assertTrue(first_chunk["adapter_transformed"])
        self.assertIn("data: [DONE]", body)

    def test_chat_stream_returns_valid_sse_error_before_clean_close(self):
        adapter = _FakeAdapter()
        upstream = _BrokenStreamClient()
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            response = TestClient(server.create_app()).post(
                "/v1/chat/completions",
                json={
                    "model": "workbuddy-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        data_lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: {")]
        payloads = [json.loads(line) for line in data_lines]
        self.assertEqual(payloads[-1]["error"]["type"], "upstream_stream_error")
        self.assertNotIn("upstream reset", payloads[-1]["error"]["message"])
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))

    def test_chat_stream_sanitizes_invalid_upstream_unicode(self):
        adapter = _FakeAdapter()
        upstream = _InvalidUnicodeStreamClient()
        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_get_adapter_for_model", return_value=(adapter, "fake", "upstream-model", "key")),
            patch.object(server, "get_upstream_client", return_value=upstream),
        ):
            response = TestClient(server.create_app()).post(
                "/v1/chat/completions",
                json={
                    "model": "workbuddy-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        first_data_line = next(line for line in response.text.splitlines() if line.startswith("data: {"))
        chunk = json.loads(first_data_line.removeprefix("data: "))
        self.assertEqual(chunk["choices"][0]["delta"]["content"], "bad\ufffdtext")


if __name__ == "__main__":
    unittest.main()

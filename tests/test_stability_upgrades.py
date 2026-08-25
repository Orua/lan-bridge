import logging
import logging.handlers
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from code_cn_bridge import server
from code_cn_bridge.config import Config
from code_cn_bridge.protocol import StreamTranslator


def _payloads(events: list[str]) -> list[dict]:
    import json

    return [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event.startswith("data: ")
    ]


class StabilityUpgradeTests(unittest.TestCase):
    def test_config_reload_if_changed_keeps_last_valid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump({"model_mapping": {"first": {"target": "one"}}}),
                encoding="utf-8",
            )
            config = Config(config_path)

            config_path.write_text(
                yaml.safe_dump({"model_mapping": {"second": {"target": "two"}}}),
                encoding="utf-8",
            )
            self.assertTrue(config.reload_if_changed())
            self.assertIn("second", config.model_mapping)

            config_path.write_text("model_mapping: [", encoding="utf-8")
            with self.assertRaises(yaml.YAMLError):
                config.reload_if_changed()
            self.assertIn("second", config.model_mapping)
            self.assertFalse(config.reload_if_changed())

    def test_config_save_updates_signature_without_reloading(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("model_mapping: {}\n", encoding="utf-8")
            config = Config(config_path)
            config._data["model_mapping"]["saved"] = {"target": "model"}

            config.save()

            self.assertFalse(config.reload_if_changed())
            self.assertIn("saved", config.model_mapping)

    def test_config_backup_redacts_provider_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump({
                    "providers": {"deepseek": {"api_key": "LIVE_SECRET", "base_url": "https://example.invalid"}},
                    "access_control": {"enabled": True, "keys": [{"key_hash": "BRIDGE_SECRET"}]},
                    "model_mapping": {},
                }),
                encoding="utf-8",
            )
            config = Config(config_path)
            config._data["model_mapping"]["new-model"] = {"target": "model"}

            config.save()

            backups = list((Path(temp_dir) / ".config-backups").glob("*.bak"))
            self.assertEqual(len(backups), 1)
            backup_text = backups[0].read_text(encoding="utf-8")
            self.assertNotIn("LIVE_SECRET", backup_text)
            self.assertNotIn("api_key:", backup_text)
            self.assertNotIn("BRIDGE_SECRET", backup_text)
            self.assertNotIn("keys:", backup_text)
            self.assertIn("base_url", backup_text)
            self.assertIn("LIVE_SECRET", config_path.read_text(encoding="utf-8"))

    def test_config_reload_waits_for_atomic_editor_replace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump({"model_mapping": {"active": {"target": "model"}}}),
                encoding="utf-8",
            )
            config = Config(config_path)

            config_path.unlink()

            self.assertFalse(config.reload_if_changed())
            self.assertIn("active", config.model_mapping)

    def test_json_unicode_sanitizer_replaces_isolated_surrogates_recursively(self):
        payload = {
            "messages": [
                {"role": "user", "content": "before\udcadafter"},
                {"role": "user", "content": "中文"},
            ],
            "tool": {"arguments": '{"value":"\udcad"}'},
        }

        normalized, affected_strings = server._normalize_json_unicode(payload)

        self.assertEqual(affected_strings, 2)
        self.assertEqual(normalized["messages"][0]["content"], "before\ufffdafter")
        self.assertEqual(normalized["messages"][1]["content"], "中文")
        self.assertEqual(normalized["tool"]["arguments"], '{"value":"\ufffd"}')
        # The sanitized payload must be safe for HTTPX's UTF-8 JSON encoding.
        import json

        json.dumps(normalized, ensure_ascii=False).encode("utf-8")

    def test_json_unicode_sanitizer_preserves_valid_non_bmp_text(self):
        value = {"content": "valid \U0001f600 text"}

        normalized, affected_strings = server._normalize_json_unicode(value)

        self.assertEqual(normalized, value)
        self.assertEqual(affected_strings, 0)

    def test_chat_json_reader_sanitizes_lone_surrogate(self):
        import asyncio

        raw = b'{"model":"test","messages":[{"role":"user","content":"bad\\udcadtext"}]}'
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}

        request = server.Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8765),
                "scheme": "http",
                "query_string": b"",
            },
            receive,
        )
        with patch.object(server, "_write_workbuddy_received_capture", return_value=None):
            body = asyncio.run(server._read_chat_json_body(request, "test-request"))

        self.assertEqual(body["messages"][0]["content"], "bad\ufffdtext")

    def test_project_logger_writes_each_record_once(self):
        project_logger = logging.getLogger("lan-bridge")
        process_logger = logging.getLogger()
        original_project_handlers = list(project_logger.handlers)
        original_process_handlers = list(process_logger.handlers)
        original_project_level = project_logger.level
        original_process_level = process_logger.level
        original_propagate = project_logger.propagate

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_server_file = Path(temp_dir) / "code_cn_bridge" / "server.py"
            marker = "bridge-log-dedup-marker"
            try:
                # Make the root-handler branch deterministic without disturbing
                # non-file handlers installed by the test runner.
                process_logger.handlers = [
                    handler
                    for handler in original_process_handlers
                    if not isinstance(handler, logging.handlers.RotatingFileHandler)
                ]
                with patch.dict(os.environ, {"LAN_BRIDGE_LOG_DIR": temp_dir}):
                    server._setup_logging(verbose=False)

                project_logger.info(marker)
                for handler in set(project_logger.handlers + process_logger.handlers):
                    handler.flush()

                log_text = (Path(temp_dir) / "bridge.log").read_text(encoding="utf-8")
                self.assertEqual(log_text.count(marker), 1)
                self.assertFalse(project_logger.propagate)
            finally:
                new_handlers = set(project_logger.handlers + process_logger.handlers)
                old_handlers = set(original_project_handlers + original_process_handlers)
                for handler in new_handlers - old_handlers:
                    handler.close()
                project_logger.handlers = original_project_handlers
                process_logger.handlers = original_process_handlers
                project_logger.setLevel(original_project_level)
                process_logger.setLevel(original_process_level)
                project_logger.propagate = original_propagate

    def test_logging_reinitialization_closes_previous_handlers(self):
        project_logger = logging.getLogger("lan-bridge")
        process_logger = logging.getLogger()
        original_project_handlers = list(project_logger.handlers)
        original_process_handlers = list(process_logger.handlers)
        original_propagate = project_logger.propagate
        created_handlers: set[logging.Handler] = set()

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                with patch.dict(os.environ, {"LAN_BRIDGE_LOG_DIR": temp_dir}):
                    server._setup_logging(verbose=False)
                    first_handlers = set(project_logger.handlers)
                    created_handlers.update(first_handlers)
                    server._setup_logging(verbose=False)
                    second_handlers = set(project_logger.handlers)
                    created_handlers.update(second_handlers)

                self.assertTrue(first_handlers.isdisjoint(second_handlers))
                for handler in first_handlers:
                    if isinstance(handler, logging.handlers.RotatingFileHandler):
                        self.assertIsNone(getattr(handler, "stream", None))
                file_handlers = [
                    handler for handler in second_handlers
                    if isinstance(handler, logging.handlers.RotatingFileHandler)
                ]
                self.assertEqual(len(file_handlers), 1)
                self.assertIn(file_handlers[0], process_logger.handlers)
            finally:
                for logger in (project_logger, process_logger):
                    for handler in list(logger.handlers):
                        if handler in created_handlers:
                            logger.removeHandler(handler)
                for handler in created_handlers:
                    handler.close()
                project_logger.handlers = original_project_handlers
                process_logger.handlers = original_process_handlers
                project_logger.propagate = original_propagate

    def test_file_logging_failure_does_not_prevent_startup(self):
        project_logger = logging.getLogger("lan-bridge")
        process_logger = logging.getLogger()
        original_project_handlers = list(project_logger.handlers)
        original_process_handlers = list(process_logger.handlers)
        original_propagate = project_logger.propagate
        try:
            with patch.object(
                server.logging.handlers,
                "RotatingFileHandler",
                side_effect=OSError("disk unavailable"),
            ):
                server._setup_logging(verbose=False)
            self.assertTrue(any(isinstance(handler, logging.StreamHandler) for handler in project_logger.handlers))
        finally:
            for handler in set(project_logger.handlers + process_logger.handlers):
                if handler not in set(original_project_handlers + original_process_handlers):
                    handler.close()
            project_logger.handlers = original_project_handlers
            process_logger.handlers = original_process_handlers
            project_logger.propagate = original_propagate

    def test_safe_debug_log_never_emits_content_or_stream_chunks(self):
        payload = {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT"}],
            "tools": [{"function": {"name": "secret_tool", "arguments": "SECRET-ARGS"}}],
            "choices": [{"delta": {"reasoning_content": "SECRET-REASONING"}}],
        }
        with patch.object(server.logger, "debug") as debug:
            server._safe_log("Chat request", payload)
            server._safe_log("Chat chunk", payload)

        debug.assert_called_once()
        rendered = " ".join(str(value) for value in debug.call_args.args)
        self.assertNotIn("TOP-SECRET-PROMPT", rendered)
        self.assertNotIn("SECRET-ARGS", rendered)
        self.assertNotIn("SECRET-REASONING", rendered)
        self.assertIn("deepseek-v4-pro", rendered)

    def test_tool_summaries_are_bounded(self):
        responses_tools = [
            {"type": "function", "name": f"tool_{index}"}
            for index in range(50)
        ]
        chat_tools = [
            {"type": "function", "function": {"name": f"tool_{index}"}}
            for index in range(50)
        ]

        responses_summary = server._responses_tool_summary(responses_tools)
        chat_summary = server._chat_tool_summary(chat_tools)

        self.assertIn("tool_0", responses_summary)
        self.assertNotIn("tool_49", responses_summary)
        self.assertTrue(responses_summary.endswith("...(+30)"))
        self.assertTrue(chat_summary.endswith("...(+30)"))

    def test_config_save_replaces_file_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.yaml"
            config_path.write_text("server:\n  port: 8765\n", encoding="utf-8")
            config = Config(config_path)
            config.data.setdefault("server", {})["port"] = 9000

            config.save()

            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["server"]["port"], 9000)
            self.assertEqual(list(config_path.parent.glob(f".{config_path.name}.*.tmp")), [])
            backups = list((config_path.parent / ".bridge-backups").glob("bridge.yaml.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "server:\n  port: 8765\n")

    def test_config_save_failure_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.yaml"
            original = b"server:\n  port: 8765\n"
            config_path.write_bytes(original)
            config = Config(config_path)
            config.data.setdefault("server", {})["port"] = 9000

            with patch("code_cn_bridge.config.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    config.save()

            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(list(config_path.parent.glob(f".{config_path.name}.*.tmp")), [])

    def test_config_edit_rolls_back_memory_and_disk_when_save_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.yaml"
            original = b"server:\n  port: 8765\n"
            config_path.write_bytes(original)
            config = Config(config_path)

            with patch("code_cn_bridge.config.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    with config.edit() as candidate:
                        candidate.setdefault("server", {})["port"] = 9000

            self.assertEqual(config.server_port, 8765)
            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(list(config_path.parent.glob(f".{config_path.name}.*.tmp")), [])

    def test_runtime_bind_override_survives_reload_without_persisting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.yaml"
            config_path.write_text("server:\n  host: 0.0.0.0\n  port: 9000\n", encoding="utf-8")
            config = Config(config_path)

            config.set_runtime_server_address("127.0.0.1", 8765)
            with config.edit() as candidate:
                candidate.setdefault("server", {})["log_level"] = "debug"
            config.reload()

            persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["server"]["port"], 9000)
            self.assertEqual(config.server_host, "127.0.0.1")
            self.assertEqual(config.server_port, 8765)

    def test_runtime_bind_rejects_invalid_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.yaml"
            config_path.write_text("server:\n  port: 8765\n", encoding="utf-8")
            config = Config(config_path)

            for port in (0, 65536, -1):
                with self.assertRaises(ValueError):
                    config.set_runtime_server_address("127.0.0.1", port)
            with self.assertRaises(ValueError):
                config.set_runtime_server_address("", 8765)

    def test_config_backup_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.yaml"
            config_path.write_text("server:\n  port: 8765\n", encoding="utf-8")
            config = Config(config_path)

            for index in range(13):
                config.data.setdefault("server", {})["port"] = 9000 + index
                config.save()

            backups = list((config_path.parent / ".bridge-backups").glob("bridge.yaml.*.bak"))
            self.assertEqual(len(backups), 10)

    def test_audit_log_enforces_size_and_retention_caps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"

            class FakeConfig:
                _data = {
                    "server": {
                        "audit_enabled": True,
                        "audit_log_path": str(audit_path),
                        "audit_log_max_bytes": 10**9,
                        "audit_log_backup_count": 100,
                    }
                }

            with patch.object(server, "get_config", return_value=FakeConfig()), patch.object(
                server, "_DEFAULT_AUDIT_LOG_MAX_BYTES", 1024
            ), patch.object(server, "_DEFAULT_AUDIT_LOG_BACKUP_COUNT", 2):
                server._audit_event("huge", "request", payload="x" * 5000)
                self.assertIn("audit.record_oversize", audit_path.read_text(encoding="utf-8"))
                for index in range(30):
                    server._audit_event("normal", f"request-{index}", payload="x" * 150)

            files = list(Path(temp_dir).glob("audit.jsonl*"))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(all(path.stat().st_size <= 1024 for path in files))

    def test_text_stream_emits_content_part_done_before_output_item_done(self):
        translator = StreamTranslator(response_id="resp_text_done", model="gpt-5")
        payloads = _payloads(
            translator.translate_chunk(
                {
                    "choices": [
                        {
                            "delta": {"content": "finished"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        )

        event_types = [payload["type"] for payload in payloads]
        part_done = next(
            payload for payload in payloads
            if payload["type"] == "response.content_part.done"
        )
        self.assertEqual(part_done["part"]["text"], "finished")
        self.assertLess(
            event_types.index("response.content_part.done"),
            event_types.index("response.output_item.done"),
        )

    def test_function_stream_emits_arguments_done_before_output_item_done(self):
        translator = StreamTranslator(response_id="resp_args_done", model="gpt-5")
        payloads = _payloads(
            translator.translate_chunk(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_done",
                                        "function": {
                                            "name": "exec_command",
                                            "arguments": '{"cmd":"pwd"}',
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

        event_types = [payload["type"] for payload in payloads]
        arguments_done = next(
            payload for payload in payloads
            if payload["type"] == "response.function_call_arguments.done"
        )
        self.assertEqual(arguments_done["call_id"], "call_done")
        self.assertEqual(arguments_done["arguments"], '{"cmd":"pwd"}')
        self.assertLess(
            event_types.index("response.function_call_arguments.done"),
            event_types.index("response.output_item.done"),
        )

    def test_custom_tool_stream_does_not_emit_function_arguments_done(self):
        translator = StreamTranslator(
            response_id="resp_custom_done",
            model="gpt-5",
            custom_tool_names={"shell_freeform"},
        )
        payloads = _payloads(
            translator.translate_chunk(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_custom_done",
                                        "function": {
                                            "name": "shell_freeform",
                                            "arguments": '{"input":"dir"}',
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

        self.assertNotIn(
            "response.function_call_arguments.done",
            [payload["type"] for payload in payloads],
        )
        done = next(
            payload for payload in payloads
            if payload["type"] == "response.output_item.done"
        )
        self.assertEqual(done["item"]["type"], "custom_tool_call")
        self.assertTrue(done["item"]["id"].startswith("ctc_"))
        self.assertEqual(done["item"]["call_id"], "call_custom_done")


if __name__ == "__main__":
    unittest.main()

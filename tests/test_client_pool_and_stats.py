import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from code_cn_bridge.adapters.deepseek import DeepSeekAdapter
from code_cn_bridge.client import UpstreamClientPool, _decode_sse_json, _iter_sse_data
from code_cn_bridge.stats import RequestLog, StatsCollector, UsageStore


class UpstreamClientPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_multiline_sse_json_with_unescaped_newline(self):
        class FakeResponse:
            async def aiter_lines(self):
                for line in (
                    'data:{"choices":[{"delta":{"content":"first',
                    'second"},"finish_reason":null}]}',
                    '',
                    'data: [DONE]',
                    '',
                ):
                    yield line

        payloads = [payload async for payload in _iter_sse_data(FakeResponse())]
        chunk = _decode_sse_json(payloads[0])

        self.assertEqual(chunk["choices"][0]["delta"]["content"], "first\nsecond")
        self.assertEqual(payloads[1], "[DONE]")

    async def test_malformed_sse_json_is_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "Malformed upstream SSE JSON"):
            _decode_sse_json('{"choices": [invalid]}')

    async def test_reuses_client_for_matching_connection_settings(self):
        adapter = DeepSeekAdapter()
        pool = UpstreamClientPool()

        first = pool.get("deepseek", adapter, "key", 120, 600)
        second = pool.get("deepseek", adapter, "key", 120, 600)

        self.assertIs(first, second)
        first.close = AsyncMock()
        await pool.close()
        first.close.assert_awaited_once()

    async def test_new_base_url_gets_separate_frozen_endpoint(self):
        adapter = DeepSeekAdapter()
        pool = UpstreamClientPool()

        adapter.base_url = "https://first.example/v1"
        first = pool.get("deepseek", adapter, "key", 120, 600)
        adapter.base_url = "https://second.example/v1"
        second = pool.get("deepseek", adapter, "key", 120, 600)

        self.assertIsNot(first, second)
        self.assertEqual(first._chat_url, "https://first.example/v1/chat/completions")
        self.assertEqual(second._chat_url, "https://second.example/v1/chat/completions")
        first.close = AsyncMock()
        second.close = AsyncMock()
        await pool.close()

    async def test_full_chat_endpoint_is_not_appended_twice(self):
        adapter = DeepSeekAdapter()
        adapter.base_url = "https://example.test/v1/chat/completions"
        pool = UpstreamClientPool()

        client = pool.get("custom", adapter, "key", 120, 600)

        self.assertEqual(client._chat_url, "https://example.test/v1/chat/completions")
        client.close = AsyncMock()
        await pool.close()

    async def test_provider_proxy_gets_a_separate_client(self):
        adapter = DeepSeekAdapter()
        pool = UpstreamClientPool()

        direct = pool.get("custom", adapter, "key", 120, 600)
        proxied = pool.get(
            "custom",
            adapter,
            "key",
            120,
            600,
            "http://127.0.0.1:19828",
        )

        self.assertIsNot(direct, proxied)
        self.assertEqual(direct._proxy_url, "")
        self.assertEqual(proxied._proxy_url, "http://127.0.0.1:19828")
        direct.close = AsyncMock()
        proxied.close = AsyncMock()
        await pool.close()


class StatsCollectorTests(unittest.TestCase):
    def test_reports_total_duration_and_stream_first_response_separately(self):
        with TemporaryDirectory() as tmp:
            stats = StatsCollector(usage_dir=tmp)
            stats.record(RequestLog(1, "a", "responses", 200, 1000, tokens=100, first_response_ms=200))
            stats.record(RequestLog(2, "b", "responses", 200, 3000, tokens=300))

            summary = stats.get_summary()

            self.assertEqual(summary["avg_response_ms"], 2000)
            self.assertEqual(summary["avg_first_response_ms"], 200)
            self.assertEqual(summary["avg_latency_ms"], 2000)
            self.assertEqual(summary["total_tokens"], 400)

    def test_exposes_upstream_api_separately_from_inbound_endpoint(self):
        with TemporaryDirectory() as tmp:
            stats = StatsCollector(usage_dir=tmp)
            stats.record(RequestLog(
                1,
                "deepseek-v4-pro",
                "responses",
                200,
                100,
                provider="deepseek",
                target_model="deepseek-v4-pro",
                upstream_api="chat",
            ))

            log = stats.get_recent_logs(1)[0]
            self.assertEqual(log["endpoint"], "responses")
            self.assertEqual(log["upstream_api"], "chat")

    def test_persists_usage_by_client_ip(self):
        with TemporaryDirectory() as tmp:
            stats = StatsCollector(usage_dir=tmp)
            stats.record(RequestLog(1, "a", "responses", 200, 100, tokens=7, client_ip="10.0.0.2"))
            stats.record(RequestLog(2, "a", "responses", 500, 100, tokens=3, client_ip="10.0.0.3"))

            usage = stats.get_usage_summary("1970-01-01")
            total = stats.get_usage_summary()
            access_log = Path(tmp) / "access-1970-01-01.jsonl"

            self.assertEqual(usage["totals"]["requests"], 2)
            self.assertEqual(usage["totals"]["tokens"], 10)
            self.assertEqual(usage["by_ip"]["10.0.0.2"]["success"], 1)
            self.assertEqual(usage["by_ip"]["10.0.0.3"]["errors"], 1)
            self.assertEqual(total["totals"]["tokens"], 10)
            self.assertEqual(len(access_log.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(stats.get_recent_logs(1)[0]["client_ip"], "10.0.0.3")

    def test_persists_usage_by_access_key_without_plaintext_secret(self):
        with TemporaryDirectory() as tmp:
            stats = StatsCollector(usage_dir=tmp)
            stats.record(RequestLog(
                1,
                "gpt-5.6-sol",
                "responses",
                200,
                100,
                tokens=17,
                client_ip="10.0.0.2",
                access_key_id="key-a",
                access_key_prefix="lbk_example",
            ))
            stats.record(RequestLog(
                2,
                "deepseek-v4-pro",
                "responses",
                500,
                100,
                tokens=3,
                client_ip="10.0.0.2",
                access_key_id="key-a",
                access_key_prefix="lbk_example",
            ))

            usage = stats.get_usage_summary()
            key_usage = usage["by_key"]["key-a"]
            access_log = (Path(tmp) / "access-1970-01-01.jsonl").read_text(encoding="utf-8")

            self.assertEqual(key_usage["requests"], 2)
            self.assertEqual(key_usage["success"], 1)
            self.assertEqual(key_usage["errors"], 1)
            self.assertEqual(key_usage["tokens"], 20)
            self.assertEqual(key_usage["prefix"], "lbk_example")
            self.assertIn("last_used_at", key_usage)
            self.assertNotIn("Bearer", access_log)

    def test_access_logs_rotate_at_bounded_size(self):
        with TemporaryDirectory() as tmp, patch(
            "code_cn_bridge.stats._ACCESS_LOG_MAX_BYTES", 350
        ), patch("code_cn_bridge.stats._ACCESS_LOG_BACKUP_COUNT", 2):
            stats = StatsCollector(usage_dir=tmp)
            timestamp = datetime(2026, 8, 15, 12, 0, 0).timestamp()
            for index in range(12):
                stats.record(RequestLog(
                    timestamp,
                    f"model-{index}",
                    "responses",
                    200,
                    100,
                    client_ip="127.0.0.1",
                ))

            access_files = list(Path(tmp).glob("access-2026-08-15.jsonl*"))
            self.assertLessEqual(len(access_files), 3)
            self.assertTrue((Path(tmp) / "access-2026-08-15.jsonl.1").exists())

    def test_usage_retention_removes_only_expired_daily_files(self):
        with TemporaryDirectory() as tmp, patch(
            "code_cn_bridge.stats._USAGE_RETENTION_DAYS", 3
        ):
            usage_dir = Path(tmp)
            for name in (
                "access-2026-08-10.jsonl",
                "usage-2026-08-11.json",
                "access-2026-08-13.jsonl.1",
                "usage-2026-08-14.json",
                "usage-total.json",
            ):
                (usage_dir / name).write_text("{}\n", encoding="utf-8")

            UsageStore(usage_dir)._cleanup_old_files("2026-08-15")

            self.assertFalse((usage_dir / "access-2026-08-10.jsonl").exists())
            self.assertFalse((usage_dir / "usage-2026-08-11.json").exists())
            self.assertTrue((usage_dir / "access-2026-08-13.jsonl.1").exists())
            self.assertTrue((usage_dir / "usage-2026-08-14.json").exists())
            self.assertTrue((usage_dir / "usage-total.json").exists())


if __name__ == "__main__":
    unittest.main()

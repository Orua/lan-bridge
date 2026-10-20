"""Real keep-alive sockets: reuse, isolation and safe retirement."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import httpx
import pytest
from starlette.requests import Request

from code_cn_bridge.http_utils import close_http_client, make_native_async_client
from code_cn_bridge.native_pool import NativeClientPool
from code_cn_bridge.native_proxy import _native_client, proxy_native_responses


class SocketUpstream:
    def __init__(self):
        self.accepts = 0
        self.requests = []
        self.writers = set()
        self.tasks = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *args):
        self.server.close()
        for writer in tuple(self.writers):
            writer.close()
        if self.tasks:
            for task in tuple(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        # close() already stops the listener. Python 3.12's Proactor server
        # wait_closed can wait forever after an intentionally reset peer;
        # each accepted handler and writer is explicitly closed above.
        assert not self.server.is_serving()

    async def handle(self, reader, writer):
        self.accepts += 1
        self.writers.add(writer)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode().split("\r\n")
                headers = httpx.Headers([(key, value.strip()) for line in lines[1:] if ":" in line for key, value in [line.split(":", 1)]])
                await reader.readexactly(int(headers.get("content-length", 0)))
                self.requests.append(headers)
                chunks = [b'data: {"type":"response.created"}\n\n',
                          'data: {"type":"response.output_text.delta","delta":"中文"}\n\n'.encode(),
                          b'data: {"type":"response.completed","response":{"id":"resp_socket","output":[]}}\n\n']
                if "/failed" in lines[0]:
                    chunks[-1] = b'data: {"type":"response.failed","response":{"error":{"message":"fixture"}}}\n\n'
                if "/truncated" in lines[0]:
                    chunks.pop()
                payload = b"".join(chunks)
                writer.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nSet-Cookie: session=forbidden\r\nContent-Length: " + str(len(payload)) + "\r\n\r\n").encode())
                for chunk in chunks:
                    # Split UTF-8 and JSON bytes over the wire.
                    for part in (chunk[:51], chunk[51:]):
                        writer.write(part)
                        await writer.drain()
                    await asyncio.sleep(0.005)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            self.writers.discard(writer)
            self.tasks.discard(task)


def lease(pool, url, identity="fake-a", timeout=10):
    client = pool.lease(("http", "127.0.0.1", httpx.URL(url).port), "", httpx.Timeout(timeout))
    request = client.build_request("POST", url, headers={"Authorization": identity, "chatgpt-account-id": "fake-account"}, json={})
    return client, request


def test_real_socket_connection_count_and_client_creation_cost():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            output = []
            counts = []
            elapsed = []
            try:
                for pooled in (False, True):
                    start_count = upstream.accepts
                    started = time.perf_counter()
                    bodies = []
                    for _ in range(10):
                        if pooled:
                            client, req = lease(pool, upstream.url)
                        else:
                            client = make_native_async_client(timeout=10)
                            req = client.build_request("POST", upstream.url, headers={"Authorization": "fake-a"}, json={})
                        response = await client.send(req, stream=True)
                        try:
                            bodies.append(await response.aread())
                        finally:
                            await close_http_client(client, response)
                    output.append(bodies)
                    counts.append(upstream.accepts - start_count)
                    elapsed.append((time.perf_counter() - started) * 1000)
                assert counts == [10, 1]
                assert output[0] == output[1]
                assert not any("cookie" in headers for headers in upstream.requests)
                print(json.dumps({"connections": counts, "total_ms": elapsed}))
            finally:
                await pool.close()
    asyncio.run(run())


def test_identity_rotation_drains_old_stream_and_releases_cancelled_lease():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            a, req_a = lease(pool, upstream.url)
            response_a = await a.send(req_a, stream=True)
            old = a._entry
            b, req_b = lease(pool, upstream.url, "fake-b")
            response_b = await b.send(req_b, stream=True)
            assert old.retired and not old.client.is_closed
            assert a._entry is not b._entry
            assert await response_a.aread()
            with anyio.CancelScope() as scope:
                scope.cancel()
                await close_http_client(a, response_a)
            assert old.client.is_closed
            assert await response_b.aread()
            await close_http_client(b, response_b)
            assert not b._entry.client.is_closed
            assert [h["authorization"] for h in upstream.requests] == ["fake-a", "fake-b"]
            await pool.close()
            assert b._entry.client.is_closed
    asyncio.run(run())


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_concurrent_leases_do_not_close_shared_client(concurrency):
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            async def turn():
                client, req = lease(pool, upstream.url)
                response = await client.send(req, stream=True)
                result = await response.aread()
                await close_http_client(client, response)
                return result
            try:
                values = await asyncio.gather(*(turn() for _ in range(concurrency)))
                assert len(set(values)) == 1
                assert all(not e.client.is_closed and e.active == 0 for e in pool._entries.values())
            finally:
                await pool.close()
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["", "/failed", "/truncated"])
def test_pooled_native_proxy_preserves_stream_and_terminal_status(ending):
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            cfg = SimpleNamespace(data={"server": {}}, _native_http_pool=pool)
            request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b""})
            traces = []
            try:
                with patch("code_cn_bridge.native_proxy._native_base_url", return_value=upstream.url + ending), patch("code_cn_bridge.native_proxy.native_request_headers", return_value={"authorization": "fake"}):
                    for _ in range(2):
                        response = await proxy_native_responses(request, {"input": [], "stream": True}, "fixture", cfg, on_trace=lambda event, fields: traces.append((event, fields)))
                        chunks = [chunk async for chunk in response.body_iterator]
                        assert "中文".encode() in b"".join(chunks)
                        final = traces[-1][1]
                        assert final["failed"] == bool(ending)
                        assert final["completed"] == (not ending)
                    assert upstream.accepts == 1
            finally:
                await pool.close()
    asyncio.run(run())


def test_proxy_hot_change_and_disable_retire_without_cutting_active_request():
    async def run():
        pool = NativeClientPool()
        cfg = SimpleNamespace(data={"server": {}}, _native_http_pool=pool)
        old = _native_client(cfg, httpx.Timeout(10))
        old.build_request("GET", "https://chatgpt.com/models", headers={"authorization": "fake"})
        cfg.data["server"]["codex_official_proxy_url"] = "http://127.0.0.1:1"
        new = _native_client(cfg, httpx.Timeout(10))
        new.build_request("GET", "https://chatgpt.com/models", headers={"authorization": "fake"})
        assert old._entry.retired and not old._entry.client.is_closed
        assert old._entry is not new._entry
        cfg.data["server"]["native_connection_pool"] = False
        direct = _native_client(cfg, httpx.Timeout(10))
        assert isinstance(direct, httpx.AsyncClient)
        await direct.aclose()
        await old.aclose()
        await new.aclose()
        assert old._entry.client.is_closed and new._entry.client.is_closed
        await pool.close()
    asyncio.run(run())


def test_disconnect_does_not_close_other_stream_or_replay_request():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            a, req_a = lease(pool, upstream.url)
            b, req_b = lease(pool, upstream.url)
            response_a = await a.send(req_a, stream=True)
            response_b = await b.send(req_b, stream=True)
            await anext(response_a.aiter_raw())
            await close_http_client(a, response_a)
            assert not b._entry.client.is_closed
            assert b"response.completed" in await response_b.aread()
            await close_http_client(b, response_b)
            assert len(upstream.requests) == 2
            await pool.close()
    asyncio.run(run())

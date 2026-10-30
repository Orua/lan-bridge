"""Real keep-alive sockets: reuse, isolation and safe retirement."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
from starlette.requests import Request

from code_cn_bridge.http_utils import close_http_client, make_native_async_client, send_with_connect_retries
from code_cn_bridge.native_pool import NativeClientPool, NativePoolBusy
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
            current_entry = b._entry
            assert old.retired and not old.client.is_closed
            assert a._entry is not b._entry
            assert await response_a.aread()
            with anyio.CancelScope() as scope:
                scope.cancel()
                await close_http_client(a, response_a)
            assert old.client.is_closed
            assert await response_b.aread()
            await close_http_client(b, response_b)
            assert not current_entry.client.is_closed
            assert [h["authorization"] for h in upstream.requests] == ["fake-a", "fake-b"]
            await pool.close()
            assert current_entry.client.is_closed
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
        old_entry, new_entry = old._entry, new._entry
        await old.aclose()
        await new.aclose()
        assert old_entry.client.is_closed and new_entry.client.is_closed
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


class BrokenTLSProxy(SocketUpstream):
    async def handle(self, reader, writer):
        self.accepts += 1
        self.writers.add(writer)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            self.requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            while await reader.read(65536):
                pass
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            self.writers.discard(writer)
            self.tasks.discard(task)


def test_proxy_failure_keeps_other_warm_lane_and_sequential_reuse():
    async def run():
        clients = []

        def factory(**kwargs):
            kwargs.pop("proxy_url")
            client = make_native_async_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok")), **kwargs)
            clients.append(client)
            return client

        pool = NativeClientPool()
        timeout = httpx.Timeout(15)
        def build():
            client = pool.lease(("https", "chatgpt.com", 443), "http://127.0.0.1:19828", timeout)
            return client, client.build_request("POST", "https://chatgpt.com/test", headers={"authorization": "fake"}, content=b"unchanged")

        try:
            with patch("code_cn_bridge.native_pool.make_native_async_client", side_effect=factory):
                healthy, good_req = build()
                failing, bad_req = build()
                good_entry, bad_entry = healthy._entry, failing._entry
                assert good_entry is not bad_entry
                with patch.object(bad_entry.client, "send", AsyncMock(side_effect=httpx.ConnectTimeout("fixture"))), pytest.raises(httpx.ConnectTimeout):
                    await failing.send(bad_req)
                await failing.aclose()
                assert bad_entry.client.is_closed
                assert not good_entry.retired and not good_entry.client.is_closed
                assert (await healthy.send(good_req)).text == "ok"
                await healthy.aclose()
                for _ in range(10):
                    client, req = build()
                    assert client._entry is good_entry
                    assert (await client.send(req)).text == "ok"
                    await client.aclose()
                assert len(clients) == 2
                assert pool.snapshot()["reserved"] == pool.snapshot()["active"] == 0
        finally:
            await pool.close()
    asyncio.run(run())


def test_proxy_lane_count_and_total_admission_remain_bounded():
    async def run():
        pool = NativeClientPool(max_active=4, max_waiters=4)
        leases = []
        try:
            for _ in range(8):
                client = pool.lease(("https", "chatgpt.com", 443), "http://127.0.0.1:19828", httpx.Timeout(15))
                leases.append(client)
                client.build_request("POST", "https://chatgpt.com/test")
            assert len(pool._entries) == 4
            extra = pool.lease(("https", "chatgpt.com", 443), "http://127.0.0.1:19828", httpx.Timeout(15))
            with pytest.raises(NativePoolBusy):
                extra.build_request("POST", "https://chatgpt.com/test")
            await extra.aclose()
        finally:
            for client in leases:
                await client.aclose()
            await pool.close()
        assert pool.snapshot()["reserved"] == 0
    asyncio.run(run())


def test_repeated_proxy_tls_failures_retire_slots_and_next_request_recovers():
    async def run():
        async with BrokenTLSProxy() as proxy:
            pool = NativeClientPool()
            recovered = False

            def factory(**kwargs):
                if recovered:
                    kwargs["proxy_url"] = ""
                    kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
                return make_native_async_client(**kwargs)

            try:
                with patch("code_cn_bridge.native_pool.make_native_async_client", side_effect=factory):
                    for _ in range(22):
                        client = pool.lease(("https", "chatgpt.com", 443), proxy.url, httpx.Timeout(0.2, connect=0.03, pool=0.05))
                        req = client.build_request("GET", "https://chatgpt.com/test", headers={"authorization": "fake"})
                        entry = client._entry
                        with pytest.raises(httpx.ConnectTimeout):
                            await client.send(req)
                        await client.aclose()
                        assert entry.client.is_closed
                        assert not pool._entries
                        assert pool.snapshot()["active"] == pool.snapshot()["reserved"] == 0
                    assert proxy.accepts == 22
                    recovered = True
                    for _ in range(2):
                        client = pool.lease(("https", "chatgpt.com", 443), proxy.url, httpx.Timeout(0.2, connect=0.03, pool=0.05))
                        response = await client.get("https://chatgpt.com/test", headers={"authorization": "fake"})
                        assert response.json() == {"ok": True}
                        await client.aclose()
                    assert pool.snapshot()["resets"] == 22
                    assert len(pool._entries) == 1
            finally:
                await pool.close()
    asyncio.run(run())


def test_connect_retry_uses_fresh_pool_and_preserves_request_bytes():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            client, req = lease(pool, upstream.url)
            entry = client._entry
            calls = AsyncMock(side_effect=httpx.ConnectError("fixture"))
            try:
                with patch.object(entry.client, "send", calls):
                    response = await send_with_connect_retries(client, req)
                assert response.status_code == 200
                assert calls.await_count == 1
                assert len(upstream.requests) == 1
                assert entry.retired and entry.client.is_closed
                assert pool.snapshot()["resets"] == 1
            finally:
                await client.aclose()
                await pool.close()
    asyncio.run(run())


@pytest.mark.parametrize("error", [httpx.WriteError, httpx.ReadError, httpx.RemoteProtocolError])
def test_post_write_failures_retire_for_future_requests_without_replay(error):
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            client, req = lease(pool, upstream.url)
            entry = client._entry
            calls = AsyncMock(side_effect=error("fixture"))
            try:
                with patch.object(entry.client, "send", calls), pytest.raises(error):
                    await send_with_connect_retries(client, req)
                assert calls.await_count == 1
                await client.aclose()
                assert entry.client.is_closed
                following, next_req = lease(pool, upstream.url)
                try:
                    response = await following.send(next_req)
                    assert response.status_code == 200
                    assert len(upstream.requests) == 1
                finally:
                    await following.aclose()
            finally:
                await client.aclose()
                await pool.close()
    asyncio.run(run())


def test_pool_timeout_resets_once_before_business_request_is_sent():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            client, req = lease(pool, upstream.url)
            entry = client._entry
            try:
                with patch.object(entry.client, "send", AsyncMock(side_effect=httpx.PoolTimeout("fixture"))):
                    response = await client.send(req)
                assert response.status_code == 200
                assert pool.snapshot()["resets"] == 1
                assert entry.client.is_closed
                assert len(upstream.requests) == 1
            finally:
                await client.aclose()
                await pool.close()
    asyncio.run(run())


def test_failed_pool_does_not_interrupt_other_active_stream():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            healthy, good_req = lease(pool, upstream.url)
            response = await healthy.send(good_req, stream=True)
            entry = healthy._entry
            failing, bad_req = lease(pool, upstream.url)
            try:
                with patch.object(entry.client, "send", AsyncMock(side_effect=httpx.ConnectError("fixture"))), pytest.raises(httpx.ConnectError):
                    await failing.send(bad_req)
                await failing.aclose()
                assert entry.retired and not entry.client.is_closed
                assert b"response.completed" in await response.aread()
                await close_http_client(healthy, response)
                assert entry.client.is_closed
                assert pool.snapshot()["active"] == 0
            finally:
                await failing.aclose()
                await close_http_client(healthy, response)
                await pool.close()
    asyncio.run(run())


def test_capacity_wait_is_bounded_and_does_not_reset_healthy_pool():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool(max_active=1, max_waiters=1, wait_timeout=0.03)
            active, req = lease(pool, upstream.url)
            response = await active.send(req, stream=True)
            waiting, queued_req = lease(pool, upstream.url)
            try:
                with pytest.raises(NativePoolBusy):
                    await waiting.send(queued_req)
                assert pool.snapshot()["resets"] == 0
                assert pool.snapshot()["active"] == 1
                assert pool.snapshot()["waiting"] == 0
                assert not active._entry.client.is_closed
                rejected = pool.lease(("http", "127.0.0.1", 1), "", httpx.Timeout(600))
                with pytest.raises(NativePoolBusy):
                    rejected.build_request("GET", upstream.url)
                await rejected.aclose()
                assert req.extensions["timeout"]["pool"] == 0.03
            finally:
                await waiting.aclose()
                await close_http_client(active, response)
                await pool.close()
            assert pool.snapshot()["reserved"] == pool.snapshot()["active"] == 0
    asyncio.run(run())


def test_cancelling_queued_request_restores_capacity_without_pool_reset():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool(max_active=1, max_waiters=1, wait_timeout=1)
            active, req = lease(pool, upstream.url)
            response = await active.send(req, stream=True)
            queued, next_req = lease(pool, upstream.url)
            task = asyncio.create_task(queued.send(next_req))
            await asyncio.sleep(0)
            assert pool.snapshot()["waiting"] == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await queued.aclose()
            assert pool.snapshot()["waiting"] == 0 and pool.snapshot()["resets"] == 0
            await close_http_client(active, response)
            await pool.close()
            assert pool.snapshot()["reserved"] == 0
    asyncio.run(run())


def test_hundred_simultaneous_requests_have_bounded_active_and_waiting_counts():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool(max_active=4, max_waiters=8, wait_timeout=1)
            peaks = {"active": 0, "waiting": 0, "reserved": 0}
            async def turn():
                client = pool.lease(("http", "127.0.0.1", httpx.URL(upstream.url).port), "", httpx.Timeout(10))
                try:
                    req = client.build_request("POST", upstream.url, headers={"authorization": "fake"}, json={})
                    task = asyncio.create_task(client.send(req, stream=True))
                    await asyncio.sleep(0)
                    for name in peaks:
                        peaks[name] = max(peaks[name], pool.snapshot()[name])
                    response = await task
                    try:
                        assert b"response.completed" in await response.aread()
                    finally:
                        await response.aclose()
                    return "ok"
                except NativePoolBusy:
                    return "busy"
                finally:
                    await client.aclose()
            try:
                results = await asyncio.gather(*(turn() for _ in range(100)))
                assert results.count("ok") == 12 and results.count("busy") == 88
                assert peaks["active"] <= 4 and peaks["waiting"] <= 8 and peaks["reserved"] <= 12
                assert pool.snapshot()["active"] == pool.snapshot()["waiting"] == pool.snapshot()["reserved"] == 0
                assert pool.snapshot()["resets"] == 0
            finally:
                await pool.close()
    asyncio.run(run())


def test_cancelling_tls_handshake_retires_broken_slot_and_releases_capacity():
    async def run():
        async with BrokenTLSProxy() as proxy:
            pool = NativeClientPool(max_active=1, max_waiters=1)
            client = pool.lease(("https", "fixture.invalid", 443), proxy.url, httpx.Timeout(30))
            req = client.build_request("GET", "https://fixture.invalid/responses")
            entry = client._entry
            task = asyncio.create_task(client.send(req))
            try:
                for _ in range(100):
                    if proxy.requests:
                        break
                    await asyncio.sleep(0.01)
                assert proxy.requests, "CONNECT was not reached"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await client.aclose()
                assert entry.retired and entry.client.is_closed
                assert pool.snapshot()["active"] == pool.snapshot()["reserved"] == 0
                assert pool.snapshot()["resets"] == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.aclose()
                await pool.close()
    asyncio.run(run())


def test_downstream_failure_before_stream_iteration_releases_native_lease():
    async def run():
        async with SocketUpstream() as upstream:
            pool = NativeClientPool()
            cfg = SimpleNamespace(data={"server": {}}, _native_http_pool=pool)
            scope = {"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b"", "asgi": {"spec_version": "2.4"}}
            with patch("code_cn_bridge.native_proxy._native_base_url", return_value=upstream.url), patch("code_cn_bridge.native_proxy.native_request_headers", return_value={"authorization": "fake"}):
                response = await proxy_native_responses(Request(scope), {"input": [], "stream": True}, "fixture", cfg)
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                raise RuntimeError("fixture downstream rejected headers")
            try:
                with pytest.raises(RuntimeError):
                    await response(scope, receive, send)
                assert pool.snapshot()["active"] == pool.snapshot()["reserved"] == 0
                assert response._native_upstream.is_closed
            finally:
                await pool.close()
    asyncio.run(run())

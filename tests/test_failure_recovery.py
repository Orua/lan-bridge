"""Regression coverage for failed reloads and interrupted upstream requests."""

import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from starlette.requests import Request

from code_cn_bridge.config import Config
from code_cn_bridge import server
from code_cn_bridge.http_utils import close_http_client
from code_cn_bridge.native_proxy import proxy_native_responses
from code_cn_bridge.provider_proxy import proxy_provider_responses, proxy_chat_to_responses


@pytest.mark.parametrize("invalid", ["providers: []", "providers:\n  broken: null", "server: []"])
def test_failed_reload_preserves_entire_previous_state(tmp_path, invalid):
    path = tmp_path / "config.yaml"
    path.write_text("model_mapping:\n  active:\n    target: working\n", encoding="utf-8")
    cfg = Config(path)
    previous = copy.deepcopy(cfg.data)
    signature = cfg._file_signature
    path.write_text(invalid, encoding="utf-8")
    with pytest.raises((ValueError, AttributeError, TypeError)):
        cfg.reload_if_changed()
    assert cfg.data == previous
    assert cfg._file_signature == signature
    assert not cfg.reload_if_changed()
    path.write_text("model_mapping:\n  repaired:\n    target: fixed\n", encoding="utf-8")
    assert cfg.reload_if_changed()
    assert "repaired" in cfg.model_mapping


async def call_proxy(kind, client, trace=None):
    body = {"model": "sample", "input": [], "stream": True}
    adapter = SimpleNamespace(base_url="https://provider.invalid/v1", get_headers=lambda key: {})
    request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": []})
    if kind == "native":
        with patch("code_cn_bridge.native_proxy._native_client", return_value=client), patch("code_cn_bridge.native_proxy.native_request_headers", return_value={}):
            return await proxy_native_responses(request, body, "sample", SimpleNamespace(data={"server": {}}), on_trace=trace)
    with patch("code_cn_bridge.provider_proxy.make_async_client", return_value=client):
        if kind == "provider":
            return await proxy_provider_responses(body, "sample", "test", {}, adapter, "test-key", on_trace=trace)
        return await proxy_chat_to_responses(
            {"messages": [{"role": "user", "content": "hello"}], "stream": True},
            "alias", "sample", "test", {}, adapter, "test-key", on_trace=trace,
        )


@pytest.mark.parametrize("kind", ["native", "provider", "chat"])
def test_cancellation_during_connect_closes_client(kind):
    async def run():
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(call_proxy(kind, client))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["native", "provider", "chat"])
def test_failed_error_body_read_closes_response_and_client(kind):
    class BrokenStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raise httpx.ReadError("upstream reset while reading error")
            yield b""

        async def aclose(self):
            self.closed = True

    async def run():
        stream = BrokenStream()
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503, stream=stream)))
        with pytest.raises(httpx.ReadError):
            await call_proxy(kind, client)
        assert stream.closed
        assert client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["native", "provider", "chat"])
def test_failed_request_build_closes_client(kind):
    async def run():
        client = httpx.AsyncClient()
        with patch.object(client, "build_request", side_effect=ValueError("invalid request")):
            with pytest.raises(ValueError):
                await call_proxy(kind, client)
        assert client.is_closed

    asyncio.run(run())


def run_provider_stream(chunks, close_early=False):
    trace = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"})
        ))
        response = await call_proxy("provider", client, lambda event, fields: trace.append((event, fields)))
        result = []
        if close_early:
            result.append(await anext(response.body_iterator))
            await response.body_iterator.aclose()
        else:
            try:
                async for chunk in response.body_iterator:
                    result.append(chunk)
            except httpx.ReadError:
                pass
        assert client.is_closed
        return b"".join(result)

    return asyncio.run(run()), trace[-1][1]


def test_provider_observes_multiline_final_event_without_newline():
    raw = b'data: {"type":"response.completed",\ndata: "response":{"id":"resp_recovery","output":[],"usage":{"total_tokens":9}}}'
    result, trace = run_provider_stream([raw[:40], raw[40:]])
    assert result == raw
    assert trace["completed"] and not trace["failed"]
    assert trace["tokens"] == 9


@pytest.mark.parametrize("chunks,close_early,expected", [
    ([b": heartbeat\n\n"], True, "cancelled"),
    ([b": heartbeat\n\n"], False, "failed"),
    ([b": heartbeat\n\n", httpx.ReadError("connection reset")], False, "failed"),
    ([b'data: {"type":"response.failed","response":{"error":{"message":"overloaded"}}}\n\n'], False, "failed"),
])
def test_provider_distinguishes_failure_from_client_cancel(chunks, close_early, expected):
    _, trace = run_provider_stream(chunks, close_early)
    assert trace[expected]
    assert not trace["completed"]
    assert trace["cancelled"] == close_early


def test_http_cleanup_survives_cancelled_anyio_scope_and_response_close_error():
    import anyio

    class Resource:
        closed = False

        async def aclose(self):
            await anyio.sleep(0)
            self.closed = True

    class BrokenResource(Resource):
        async def aclose(self):
            await super().aclose()
            raise OSError("close failed")

    async def run():
        client, response = Resource(), BrokenResource()
        with anyio.CancelScope() as scope:
            scope.cancel()
            with pytest.raises(OSError):
                await close_http_client(client, response)
        assert response.closed and client.closed

    anyio.run(run)


@pytest.mark.parametrize("handler", [server._handle_stream, server._handle_web_search_stream])
@pytest.mark.parametrize("at_heartbeat", [True, False])
def test_chat_disconnect_cancels_pending_read_and_closes_stream(handler, at_heartbeat):
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()

        async def upstream(_):
            try:
                started.set()
                await asyncio.Event().wait()
                yield {}
            finally:
                await asyncio.sleep(0)
                closed.set()

        original_next = server._next_stream_chunk

        async def short_heartbeat(stream, task=None):
            return await original_next(stream, task, timeout=0.001 if at_heartbeat else 60)

        with patch.object(server, "_next_stream_chunk", short_heartbeat), patch.object(server, "_audit_event"):
            stream = handler(SimpleNamespace(chat_completion_stream=upstream), SimpleNamespace(), {}, {}, None, "test", False, time.time())
            if at_heartbeat:
                assert await anext(stream) == ": heartbeat\n\n"
                await stream.aclose()
            else:
                task = asyncio.create_task(anext(stream))
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert closed.is_set()

    asyncio.run(run())

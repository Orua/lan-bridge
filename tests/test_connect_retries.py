import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from code_cn_bridge.client import UpstreamClient
from code_cn_bridge.http_utils import send_with_connect_retries
from tests.test_failure_recovery import call_proxy


def test_connection_failure_records_tls_stage_and_preserves_existing_trace(caplog):
    async def run():
        observed = []
        async def original(name, info):
            observed.append(name)
        async def handler(request):
            await request.extensions["trace"]("proxy.start_tls.started", {})
            raise httpx.ConnectTimeout("fixture")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            req = client.build_request("POST", "https://example.invalid", extensions={"trace": original})
            with patch('code_cn_bridge.http_utils.anyio.sleep', new_callable=AsyncMock):
                with pytest.raises(httpx.ConnectTimeout) as caught:
                    await send_with_connect_retries(client, req)
            assert caught.value.bridge_connect_stage == "proxy.start_tls"
            assert req.extensions["trace"] is original
            assert observed == ["proxy.start_tls.started"] * 3
            assert "stage=proxy.start_tls" in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['native', 'provider', 'chat'])
def test_responses_routes_retry_connect_timeout_without_changing_payload(kind):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.completed","response":{"id":"resp_retry","output":[]}}\n\n'

    async def run():
        requests = []

        def handler(request):
            requests.append(request.content)
            if len(requests) < 3:
                raise httpx.ConnectTimeout('TLS handshake timed out')
            return httpx.Response(200, stream=Stream(), headers={'content-type': 'text/event-stream'})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('code_cn_bridge.http_utils.anyio.sleep', new_callable=AsyncMock) as sleep:
            response = await call_proxy(kind, client)
            # A streaming request owns the client until its iterator closes.
            if hasattr(response, 'body_iterator'):
                async for _ in response.body_iterator:
                    pass
        assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
        assert client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize('stream', [False, True])
def test_classic_chat_retries_connection_setup(stream):
    async def run():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError('connection refused')
            if stream:
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})

        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = SimpleNamespace(build_chat_url=lambda: 'https://example.invalid/v1/chat/completions', get_headers=lambda key: {})
        client = UpstreamClient(adapter, 'test-key')
        client._client = client._stream_client = transport
        with patch('code_cn_bridge.http_utils.anyio.sleep', new_callable=AsyncMock):
            if stream:
                result = [chunk async for chunk in client.chat_completion_stream({})]
                assert result[0]['choices'][0]['delta']['content'] == 'ok'
            else:
                assert (await client.chat_completion({}))['choices'][0]['message']['content'] == 'ok'
        await client.close()
        assert calls == 2

    asyncio.run(run())


@pytest.mark.parametrize('error_type', [httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.PoolTimeout])
def test_retry_budget_and_non_connection_errors(error_type):
    async def run():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            raise error_type('failure')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch('code_cn_bridge.http_utils.anyio.sleep', new_callable=AsyncMock):
                with pytest.raises(error_type):
                    await send_with_connect_retries(client, client.build_request('POST', 'https://example.invalid', json={'tools': []}), stream=True)
        assert calls == (3 if error_type in (httpx.ConnectTimeout, httpx.ConnectError) else 1)

    asyncio.run(run())


@pytest.mark.parametrize('status', [401, 429, 503])
def test_http_error_response_is_never_replayed(status):
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(status))) as client:
            response = await send_with_connect_retries(client, client.build_request('POST', 'https://example.invalid'))
            assert response.status_code == status
        assert len(calls) == 1

    asyncio.run(run())


def test_cancellation_during_backoff_stops_retry_immediately():
    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ConnectTimeout('timeout')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch('code_cn_bridge.http_utils.anyio.sleep', side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await send_with_connect_retries(client, client.build_request('POST', 'https://example.invalid'))
        assert len(calls) == 1

    asyncio.run(run())


def test_failure_after_stream_bytes_never_restarts_generation():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: first\n\n'
            raise httpx.ReadTimeout('stream stalled')

    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, stream=Stream()))) as client:
            response = await send_with_connect_retries(client, client.build_request('POST', 'https://example.invalid'), stream=True)
            iterator = response.aiter_raw()
            assert await anext(iterator) == b'data: first\n\n'
            with pytest.raises(httpx.ReadTimeout):
                await anext(iterator)
            await response.aclose()
        assert len(calls) == 1

    asyncio.run(run())

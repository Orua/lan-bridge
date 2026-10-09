import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.requests import Request

from code_cn_bridge.native_proxy import fetch_merged_models, proxy_native_responses, _prepare_native_responses_payload
from code_cn_bridge.sse_observer import SSEJsonObserver


def config():
    return SimpleNamespace(data={"server": {}}, native_models={}, model_mapping={})


def request(query=b""):
    return Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "query_string": query})


def run_stream(chunks, close_early=False):
    trace = []
    forwarded = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

    def handler(req):
        forwarded.append(json.loads(req.content))
        return httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.native_proxy._native_client", return_value=client), patch("code_cn_bridge.native_proxy.native_request_headers", return_value={}):
            response = await proxy_native_responses(request(), {
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "上下文 marker"}]}],
                "stream": True, "tools": [{"type": "function", "name": "probe", "parameters": {"type": "object"}}],
                "service_tier": "fast", "metadata": {"marker": "keep"},
            }, "gpt-6.1-sol", config(), on_trace=lambda event, fields: trace.append((event, fields)))
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
            return b"".join(result)

    return asyncio.run(run()), trace, forwarded


def test_native_stream_is_byte_preserving_and_caches_final_unterminated_event():
    output = {"type": "function_call", "name": "probe", "call_id": "call_observer", "arguments": '{"value":"中文"}'}
    raw = ('data: ' + json.dumps({"type": "response.completed", "response": {"id": "resp_observer", "output": [output], "usage": {"total_tokens": 7}}}, ensure_ascii=False)).encode()
    chunks = [raw[:53], raw[53:]]
    result, trace, sent = run_stream(chunks)
    assert result == raw
    assert sum(event == "first_chunk" for event, _ in trace) == 1
    finished = trace[-1][1]
    assert finished["completed"] and not finished["failed"]
    assert finished["tokens"] == 7
    assert sent[0]["tools"][0]["name"] == "probe"
    assert sent[0]["metadata"] == {"marker": "keep"}
    assert sent[0]["service_tier"] == "fast"
    continuation = _prepare_native_responses_payload({"previous_response_id": "resp_observer", "input": [{"type": "function_call_output", "call_id": "call_observer", "output": "tool result"}]}, "gpt-6.1-sol")
    assert [item["type"] for item in continuation["input"]] == ["message", "function_call", "function_call_output"]
    assert continuation["input"][1] == output


def test_upstream_failed_event_preserves_error_and_is_not_client_cancellation():
    raw = b'data: {"type":"response.failed","response":{"error":{"message":"at capacity"}}}\n\n'
    result, trace, _ = run_stream([raw])
    assert result == raw
    assert trace[-1][1]["failed"] and not trace[-1][1]["cancelled"]
    assert trace[-1][1]["error"] == "at capacity"


def test_upstream_truncation_is_not_client_cancellation():
    _, trace, _ = run_stream([b'data: {"type":"response.created","response":{"id":"resp_cut"}}\n\n'])
    assert trace[-1][1]["failed"] and not trace[-1][1]["cancelled"]


def test_client_close_is_distinct_from_transport_failure():
    _, trace, _ = run_stream([b': heartbeat\n\n'], close_early=True)
    assert trace[-1][1]["cancelled"] and not trace[-1][1]["failed"]
    _, trace, _ = run_stream([b': heartbeat\n\n', httpx.ReadError("connection reset")])
    assert trace[-1][1]["failed"] and not trace[-1][1]["cancelled"]


def test_sse_observer_accepts_split_utf8_and_multiline_data():
    raw = 'data: {"type":\n' 'data: "中文"}\r\n\r\n'
    observer = SSEJsonObserver()
    events = []
    for byte in raw.encode():
        events.extend(observer.feed(bytes([byte])))
    assert events == [{"type": "中文"}]


def test_old_downstream_cannot_hide_new_account_models():
    observed = []

    def handler(req):
        observed.append(req.url.params)
        return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol"}]})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("code_cn_bridge.native_proxy._native_client", return_value=client), patch("code_cn_bridge.native_proxy.native_request_headers", return_value={}), patch("code_cn_bridge.native_proxy._host_catalog_client_version", return_value="0.162.0"):
            return await fetch_merged_models(request(b'client_version=0.149.1&refresh=1'), config())

    response = asyncio.run(run())
    payload = json.loads(response.body)
    assert observed[0]["client_version"] == "0.162.0"
    assert "refresh" not in observed[0]
    assert payload["data"][0]["id"] == "gpt-6.1-sol"
    assert payload["native_catalog"]["source"] == "account"

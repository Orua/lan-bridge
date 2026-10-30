import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from code_cn_bridge.local_asr import LocalASRService, create_asr_router
from code_cn_bridge import asr_stream
from code_cn_bridge.middleware import current_bridge_principal


def client(enabled=True):
    config = SimpleNamespace(data={"local_asr": {"enabled": enabled},
                                   "access_control": {"enabled": True}})
    service = LocalASRService(lambda: config)
    service.demo_key = "asr_demo_unit_test"
    service.demo_key_id = "demo"
    service.ensure_started = AsyncMock()
    app = FastAPI()
    app.state.records = Mock()
    app.state.audit = Mock()
    app.state.principals = []
    app.state.records.side_effect = lambda *a, **kw: app.state.principals.append(current_bridge_principal().key_id)
    app.include_router(create_asr_router(service, Mock(), app.state.records, app.state.audit))
    return TestClient(app), service


def test_stream_requires_authentication_and_enabled_service():
    app, _ = client()
    with pytest.raises(WebSocketDisconnect):
        with app.websocket_connect("/v1/audio/stream"):
            pass
    app, _ = client(False)
    with pytest.raises(WebSocketDisconnect):
        with app.websocket_connect("/v1/audio/stream", headers={"Authorization": "Bearer asr_demo_unit_test"}):
            pass


def test_stream_forwards_pcm_and_partial_then_final():
    app, service = client()

    class Upstream:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.frames = []

        async def __aenter__(self):
            await self.queue.put(json.dumps({"type": "ready"}))
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, frame):
            self.frames.append(frame)
            if isinstance(frame, bytes):
                await self.queue.put(json.dumps({"type": "partial", "text": "你好"}))
            else:
                await self.queue.put(json.dumps({"type": "final", "text": "你好。"}))
                await self.queue.put(None)

        async def __aiter__(self):
            while (frame := await self.queue.get()) is not None:
                yield frame

    upstream = Upstream()
    with patch("websockets.legacy.client.connect", return_value=upstream) as connect:
        with app.websocket_connect("/v1/audio/stream", headers={"Authorization": "Bearer asr_demo_unit_test"}) as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(b"\0\0" * 9600)
            assert ws.receive_json() == {"type": "partial", "text": "你好"}
            ws.send_json({"type": "stop"})
            assert ws.receive_json() == {"type": "final", "text": "你好。"}
    assert connect.call_args.args[0] == "ws://127.0.0.1:8791/v1/audio/stream"
    assert "extra_headers" not in connect.call_args.kwargs
    assert len(upstream.frames[0]) == 19200
    service.ensure_started.assert_awaited_once()


class FlowUpstream:
    def __init__(self, mode="final"):
        self.queue = asyncio.Queue()
        self.frames = []
        self.mode = mode
        self.delayed = None

    async def __aenter__(self):
        await self.queue.put(json.dumps({"type": "ready", "recognizer": "SeacoParaformer", "hotword_count": 139}))
        return self

    async def __aexit__(self, *args):
        if self.delayed:
            self.delayed.cancel()
            await asyncio.gather(self.delayed, return_exceptions=True)

    async def send(self, frame):
        self.frames.append(frame)
        if isinstance(frame, bytes):
            await self.queue.put(json.dumps({"type": "partial", "text": "private transcript"}))
            return
        if self.mode == "close":
            await self.queue.put(None)
        elif self.mode == "error":
            await self.queue.put(json.dumps({"type": "error", "code": "asr_busy", "message": "busy"}))
        elif self.mode in {"slow", "hang"}:
            async def finish():
                await asyncio.sleep(.12 if self.mode == "slow" else 10)
                await self.queue.put(json.dumps({"type": "final", "text": "private final", "recognizer": "SeacoParaformer"}))
            self.delayed = asyncio.create_task(finish())
        else:
            await self.queue.put(json.dumps({"type": "final", "text": "private final", "recognizer": "SeacoParaformer",
                                            "hotword_count": 139, "processing_ms": 3}))

    async def __aiter__(self):
        while (frame := await self.queue.get()) is not None:
            yield frame


HEADERS = {"Authorization": "Bearer asr_demo_unit_test"}


def test_final_required_and_audit_metadata_without_transcript():
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream()):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(b"\0\0" * 9600)
            assert ws.receive_json()["type"] == "partial"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "final"
    record = app.app.state.records.call_args
    assert record.args[3] == 200
    assert record.kwargs["upstream_api"] == "audio/stream"
    assert record.kwargs["target_model"] == "SeacoParaformer"
    assert record.kwargs["client_ip"] != "unknown"
    assert record.kwargs["first_response_ms"] is not None
    assert record.kwargs["details"]["audio_bytes"] == 19200
    assert record.kwargs["details"]["outcome"] == "completed"
    assert app.app.state.principals == ["demo"]
    assert "private" not in str(app.app.state.audit.call_args_list)
    assert app.app.state.audit.call_args.args[0] == "asr.completed"


@pytest.mark.parametrize("mode,status,code", [("close", 502, "asr_missing_final"), ("error", 503, "asr_busy")])
def test_worker_end_without_final_is_failure(mode, status, code):
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream(mode)):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            ws.receive_json()
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["code"] == code
    assert app.app.state.records.call_args.args[3] == status


@pytest.mark.parametrize("explicit", [True, False])
def test_cancel_and_early_disconnect_are_not_server_errors(explicit):
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream()):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            ws.receive_json()
            if explicit:
                ws.send_json({"type": "cancel"})
                assert ws.receive_json()["type"] == "cancelled"
    record = app.app.state.records.call_args
    assert record.args[3] == 204 and record.args[5] == ""
    assert record.kwargs["details"]["outcome"] == "cancelled"
    assert app.app.state.audit.call_args.args[0] == "asr.cancelled"


@pytest.mark.parametrize("frame,status,code", [(b"x", 400, "invalid_audio_frame"),
                                              (b"\0" * 32002, 413, "audio_too_large"),
                                              ("[]", 400, "invalid_audio_command"),
                                              ('{"type":"other"}', 400, "invalid_audio_command")],
                         ids=["odd-pcm", "oversize-frame", "non-object", "unknown-command"])
def test_invalid_client_frames_are_classified(frame, status, code):
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream()):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            ws.receive_json()
            ws.send_bytes(frame) if isinstance(frame, bytes) else ws.send_text(frame)
            assert ws.receive_json()["code"] == code
    assert app.app.state.records.call_args.args[3] == status


def test_stop_waits_for_final_beyond_audio_idle_timeout(monkeypatch):
    monkeypatch.setattr(asr_stream, "IDLE_TIMEOUT", .03)
    monkeypatch.setattr(asr_stream, "FINAL_TIMEOUT", 1)
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream("slow")):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            ws.receive_json()
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "final"
    assert app.app.state.records.call_args.args[3] == 200


def test_final_timeout_is_504(monkeypatch):
    monkeypatch.setattr(asr_stream, "FINAL_TIMEOUT", .03)
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream("hang")):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            ws.receive_json()
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["code"] == "asr_final_timeout"
    assert app.app.state.records.call_args.args[3] == 504


def test_audio_idle_is_distinct_from_final_timeout(monkeypatch):
    monkeypatch.setattr(asr_stream, "IDLE_TIMEOUT", .03)
    app, _ = client()
    with patch("websockets.legacy.client.connect", return_value=FlowUpstream()):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            ws.receive_json()
            assert ws.receive_json()["code"] == "asr_audio_idle"
    assert app.app.state.records.call_args.args[3] == 408


def test_unavailable_worker_is_503():
    app, _ = client()
    with patch("websockets.legacy.client.connect", side_effect=OSError("unavailable")):
        with app.websocket_connect("/v1/audio/stream", headers=HEADERS) as ws:
            assert ws.receive_json()["code"] == "asr_worker_unavailable"
    assert app.app.state.records.call_args.args[3] == 503

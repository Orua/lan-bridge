import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_cn_bridge import local_asr
from code_cn_bridge.access_control import AccessKeyStore
from code_cn_bridge.admin_api import _require_local_admin
from code_cn_bridge.middleware import BridgeAccessMiddleware


def setup(tmp_path, *, models=(local_asr.MODEL,), enabled=True, remote=False):
    config = SimpleNamespace(data={
        "access_control": {"enabled": True, "store_path": str(tmp_path / "keys.json")},
        "local_asr": {"enabled": enabled, "worker_url": "http://127.0.0.1:8791"},
    })
    key, _ = AccessKeyStore(tmp_path / "keys.json").create("ASR test", list(models))
    service = local_asr.LocalASRService(lambda: config)
    app = FastAPI()
    app.state.local_asr = service
    app.add_middleware(BridgeAccessMiddleware)
    recorder = Mock()
    app.include_router(local_asr.create_asr_router(service, _require_local_admin, recorder))
    client = TestClient(app, client=("192.0.2.1" if remote else "127.0.0.1", 12345))
    return config, service, client, key, recorder


def test_authentication_and_demo_session_scope(tmp_path):
    config, service, client, key, _ = setup(tmp_path)
    with patch("code_cn_bridge.config.get_config", return_value=config):
        assert client.get("/v1/audio/status").status_code == 401
        assert client.post("/v1/audio/transcriptions").status_code == 401
        key = client.post("/admin/api/audio/demo-session").json()["key"]
        assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 401
        assert client.post("/v1/responses", headers={"Authorization": "Bearer " + key}).status_code == 401
        assert client.get("/v1/audio/status", headers={"Authorization": "Bearer " + key}).status_code != 401


def test_exact_multipart_forwarding_without_credentials_and_usage_record(tmp_path):
    config, _, client, key, record = setup(tmp_path)
    seen = []
    def upstream(request):
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json={"text": "不锈钢接头。", "processing_ms": 100})
    original = httpx.AsyncClient
    def factory(**kwargs):
        return original(transport=httpx.MockTransport(upstream), **kwargs)
    headers = {"Authorization": "Bearer " + key}
    with patch("code_cn_bridge.config.get_config", return_value=config), patch.object(local_asr.httpx, "AsyncClient", factory):
        assert client.get("/v1/audio/status", headers=headers).json()["status"] == "ready"
        response = client.post("/v1/audio/transcriptions", headers=headers,
                               data={"model": local_asr.MODEL}, files={"file": ("cn.wav", b"RIFF-test-audio", "audio/wav")})
        assert response.status_code == 200
        assert response.json()["text"] == "不锈钢接头。"
        assert response.headers["x-lan-bridge-asr"] == "local"
    forwarded = [request for request in seen if request.method == "POST"][0]
    assert b"RIFF-test-audio" in forwarded.content
    assert "authorization" not in forwarded.headers
    assert key.encode() not in forwarded.content
    assert record.call_args.args[3] == 200
    assert record.call_args.kwargs["provider"] == "local_asr"


def test_disabled_invalid_format_and_unsupported_model_do_not_forward(tmp_path):
    config, _, client, key, _ = setup(tmp_path, enabled=False)
    headers = {"Authorization": "Bearer " + key}
    with patch("code_cn_bridge.config.get_config", return_value=config):
        assert client.get("/v1/audio/status", headers=headers).status_code == 503
        config.data["local_asr"]["enabled"] = True
        assert client.post("/v1/audio/transcriptions", json={}, headers=headers).status_code == 415
        response = client.post("/v1/audio/transcriptions", headers=headers,
                               data={"model": "unexpected-model"}, files={"file": ("x.wav", b"x")})
        assert response.status_code == 400


def test_loopback_worker_constraint():
    for url in ("https://api.openai.com", "http://192.0.2.1:8791", "http://localhost:8791", "http://user:secret@127.0.0.1:8791"):
        service = local_asr.LocalASRService(lambda: SimpleNamespace(data={"local_asr": {"worker_url": url}}))
        try:
            service.worker_url()
            assert False, url
        except ValueError:
            pass


def test_demo_session_is_scoped_local_only_and_revoked_on_close(tmp_path):
    config, service, client, _, _ = setup(tmp_path)
    with patch("code_cn_bridge.config.get_config", return_value=config):
        response = client.post("/admin/api/audio/demo-session")
        assert response.status_code == 200
        raw_key = response.json()["key"]
        assert raw_key not in (tmp_path / "keys.json").read_text(encoding="utf-8")
        assert client.get("/v1/audio/status", headers={"Authorization": "Bearer " + raw_key}).status_code != 403
    assert len(AccessKeyStore(tmp_path / "keys.json").list_records()) == 1
    assert service.authenticate_demo({"authorization": "Bearer " + raw_key}, "/v1/audio/status").allowed_models == (local_asr.MODEL,)
    asyncio.run(service.close())
    assert not service.demo_key
    _, _, remote, _, _ = setup(tmp_path / "remote", remote=True)
    assert remote.post("/admin/api/audio/demo-session").status_code == 403


def test_request_limit(tmp_path, monkeypatch):
    config, _, client, key, _ = setup(tmp_path)
    monkeypatch.setattr(local_asr, "MAX_REQUEST_BYTES", 32)
    with patch("code_cn_bridge.config.get_config", return_value=config):
        result = client.post("/v1/audio/transcriptions", headers={"Authorization": "Bearer " + key},
                             files={"file": ("x.wav", b"x" * 33)})
    assert result.status_code == 413


def test_plain_text_response_format_remains_supported(tmp_path):
    config, _, client, key, record = setup(tmp_path)
    def upstream(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ready", "recognizer": "SeacoParaformer"})
        return httpx.Response(200, text="狗扣。", headers={"Content-Type": "text/plain; charset=utf-8"})
    original = httpx.AsyncClient
    def factory(**kwargs):
        return original(transport=httpx.MockTransport(upstream), **kwargs)
    with patch("code_cn_bridge.config.get_config", return_value=config), patch.object(local_asr.httpx, "AsyncClient", factory):
        response = client.post("/v1/audio/transcriptions", headers={"Authorization": "Bearer " + key},
                               data={"model": local_asr.MODEL, "response_format": "text"},
                               files={"file": ("x.wav", b"RIFF-test", "audio/wav")})
        assert response.status_code == 200 and response.text == "狗扣。"
    assert record.call_args.kwargs["upstream_api"] == "audio/transcriptions"
    assert record.call_args.kwargs["target_model"] == "SeacoParaformer"


def test_invalid_worker_health_is_reported_as_unavailable(tmp_path):
    config, _, client, key, _ = setup(tmp_path)
    original = httpx.AsyncClient
    def factory(**kwargs):
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])), **kwargs)
    with patch("code_cn_bridge.config.get_config", return_value=config), patch.object(local_asr.httpx, "AsyncClient", factory):
        result = client.get("/v1/audio/status", headers={"Authorization": "Bearer " + key})
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "asr_worker_unavailable"

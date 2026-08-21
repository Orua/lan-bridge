import json
from types import SimpleNamespace
from unittest.mock import patch

import zstandard as zstd
from fastapi.testclient import TestClient

from code_cn_bridge import server


def _config():
    return SimpleNamespace(
        _data={"server": {}},
        server_host="127.0.0.1",
        server_port=8765,
        model_mapping={},
    )


def test_decode_request_body_supports_zstd():
    payload = json.dumps({"model": "test", "input": []}).encode("utf-8")
    compressed = zstd.ZstdCompressor().compress(payload)

    assert server._decode_request_body(compressed, "zstd") == payload


def test_responses_websocket_handles_prewarm_without_upstream():
    with patch.object(server, "get_config", return_value=_config()), patch.object(
        server, "_setup_logging"
    ):
        client = TestClient(server.create_app())
        with client.websocket_connect("/v1/responses") as websocket:
            websocket.send_json({
                "type": "response.create",
                "model": "deepseek-v4-pro",
                "generate": False,
            })

            created = websocket.receive_json()
            completed = websocket.receive_json()

    assert created["type"] == "response.created"
    assert created["response"]["status"] == "in_progress"
    assert completed["type"] == "response.completed"
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["model"] == "deepseek-v4-pro"

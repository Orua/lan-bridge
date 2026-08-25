import base64
import json
import time
from pathlib import Path

import pytest

from code_cn_bridge.codex_auth import (
    NativeAuthError,
    load_native_credentials,
)
from code_cn_bridge.native_proxy import native_request_headers


class FakeConfig:
    def __init__(self, auth_file: Path, *, enabled: bool = True):
        self.data = self._data = {
            "server": {
                "native_codex_base_url": "https://chatgpt.com/backend-api/codex",
                "native_auth_injection": {
                    "enabled": enabled,
                    "auth_file": str(auth_file),
                },
            }
        }


def _jwt(expiry: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_auth(path: Path, *, expiry: int | None = None) -> None:
    token = _jwt(expiry or int(time.time()) + 3600)
    path.write_text(json.dumps({
        "tokens": {
            "access_token": token,
            "refresh_token": "test-refresh-never-logged",
            "account_id": "account-host",
        }
    }), encoding="utf-8")


def test_injection_overrides_client_openai_credentials(tmp_path):
    auth_file = tmp_path / "auth.json"
    _write_auth(auth_file)
    config = FakeConfig(auth_file)

    headers = native_request_headers({
        "authorization": "Bearer attacker-token",
        "chatgpt-account-id": "account-attacker",
        "x-codex-installation-id": "installation-1",
        "cookie": "must-not-forward",
    }, config)

    assert headers["authorization"] != "Bearer attacker-token"
    assert headers["chatgpt-account-id"] == "account-host"
    assert headers["x-codex-installation-id"] == "installation-1"
    assert "cookie" not in headers


def test_disabled_injection_refuses_native_proxying(tmp_path):
    config = FakeConfig(tmp_path / "missing.json", enabled=False)

    with pytest.raises(NativeAuthError, match="disabled"):
        native_request_headers({"authorization": "Bearer original"}, config)


def test_expired_host_token_returns_sanitized_error(tmp_path):
    auth_file = tmp_path / "auth.json"
    _write_auth(auth_file, expiry=int(time.time()) - 60)

    with pytest.raises(NativeAuthError, match="expired") as caught:
        load_native_credentials(FakeConfig(auth_file))

    assert "test-refresh-never-logged" not in str(caught.value)


def test_injection_refuses_non_openai_upstream(tmp_path):
    auth_file = tmp_path / "auth.json"
    _write_auth(auth_file)
    config = FakeConfig(auth_file)
    config.data["server"]["native_codex_base_url"] = "https://example.invalid/codex"

    with pytest.raises(NativeAuthError, match="official OpenAI"):
        native_request_headers({}, config)

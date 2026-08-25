"""Secure host-side Codex credential loading for trusted-LAN injection mode."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_MAX_AUTH_FILE_BYTES = 1024 * 1024


class NativeAuthError(RuntimeError):
    """A sanitized native-auth configuration or credential error."""


@dataclass(frozen=True)
class NativeCredentials:
    access_token: str
    account_id: str


def native_auth_settings(config) -> dict[str, Any]:
    server = getattr(config, "data", {}).get("server", {})
    settings = server.get("native_auth_injection", {})
    return settings if isinstance(settings, dict) else {}


def native_auth_injection_enabled(config) -> bool:
    return bool(native_auth_settings(config).get("enabled", False))


def resolve_auth_file(config) -> Path:
    configured = str(native_auth_settings(config).get("auth_file") or "").strip()
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        return Path(expanded)
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "auth.json"


def _jwt_expiry(access_token: str) -> int | None:
    try:
        import base64

        payload = access_token.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
        expiry = claims.get("exp")
        return int(expiry) if expiry is not None else None
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def load_native_credentials(config) -> NativeCredentials:
    """Read only the access token and account id; never surface credential values."""
    path = resolve_auth_file(config)
    try:
        if path.stat().st_size > _MAX_AUTH_FILE_BYTES:
            raise NativeAuthError("Codex auth cache is unexpectedly large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except NativeAuthError:
        raise
    except FileNotFoundError as exc:
        raise NativeAuthError(
            "Codex file-based login cache was not found; set cli_auth_credentials_store = \"file\" and run codex login"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeAuthError("Codex login cache could not be read") from exc

    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        raise NativeAuthError("Codex login cache does not contain ChatGPT tokens")
    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        raise NativeAuthError("Codex ChatGPT login is incomplete; sign in again on the bridge computer")
    expiry = _jwt_expiry(access_token)
    if expiry is not None and expiry <= int(time.time()) + 30:
        raise NativeAuthError(
            "Codex access token has expired; use Codex on the bridge computer or sign in again to refresh it"
        )
    return NativeCredentials(access_token=access_token, account_id=account_id)

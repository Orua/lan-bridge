"""Managed bridge access keys and per-model authorization.

Only SHA-256 verifiers are persisted.  The plaintext key is returned once when
it is created or rotated and is never written to logs or configuration backups.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping


ACCESS_KEY_PREFIX = "lbk_"
DEFAULT_ACCESS_CONTROL = {"enabled": True, "store_path": ""}
_HASH_DOMAIN = b"LAN-BRIDGE-ACCESS-KEY-V1\0"
_MAX_STORE_BYTES = 2 * 1024 * 1024


class BridgeAccessError(RuntimeError):
    """A sanitized bridge authentication or authorization error."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class BridgePrincipal:
    key_id: str
    name: str
    prefix: str
    allowed_models: tuple[str, ...]

    def can_use_model(self, model: str) -> bool:
        requested = str(model or "").strip()
        return bool(requested) and (
            "*" in self.allowed_models or requested in self.allowed_models
        )


ANONYMOUS_PRINCIPAL = BridgePrincipal(
    key_id="",
    name="unmanaged",
    prefix="",
    allowed_models=("*",),
)


def access_control_settings(config) -> dict[str, Any]:
    settings = getattr(config, "data", {}).get("access_control", {})
    return settings if isinstance(settings, dict) else {}


def access_control_enabled(config) -> bool:
    return bool(access_control_settings(config).get("enabled", False))


def access_key_store_path(config) -> Path:
    configured = str(access_control_settings(config).get("store_path") or "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return Path.home() / ".lan-bridge" / "access-keys.json"


class AccessKeyStore:
    """Small, process-safe-enough single-worker store with atomic replacement."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def list_records(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            records = self._read_unlocked()
            if not include_revoked:
                records = [item for item in records if not item.get("revoked", False)]
            return [dict(item) for item in records]

    def create(self, name: str, allowed_models: Any) -> tuple[str, dict[str, Any]]:
        raw_key, record = create_access_key_record(name, allowed_models)
        with self._lock:
            records = self._read_unlocked()
            records.append(record)
            self._write_unlocked(records)
        return raw_key, dict(record)

    def update(self, key_id: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            records = self._read_unlocked()
            index = self._find_index(records, key_id)
            updated = dict(records[index])
            if "name" in updates:
                name = str(updates.get("name") or "").strip()
                if not name:
                    raise ValueError("Key name is required")
                if len(name) > 100:
                    raise ValueError("Key name is too long")
                updated["name"] = name
            if "allowed_models" in updates:
                updated["allowed_models"] = normalize_allowed_models(updates["allowed_models"])
            if "enabled" in updates:
                updated["enabled"] = bool(updates["enabled"])
            records[index] = updated
            self._write_unlocked(records)
            return dict(updated)

    def rotate(self, key_id: str) -> tuple[str, dict[str, Any]]:
        with self._lock:
            records = self._read_unlocked()
            index = self._find_index(records, key_id)
            raw_key, rotated = rotate_access_key_record(records[index])
            records[index] = rotated
            self._write_unlocked(records)
            return raw_key, dict(rotated)

    def revoke(self, key_id: str) -> None:
        with self._lock:
            records = self._read_unlocked()
            index = self._find_index(records, key_id)
            revoked = dict(records[index])
            revoked["enabled"] = False
            revoked["revoked"] = True
            revoked["revoked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            revoked.pop("key_hash", None)
            records[index] = revoked
            self._write_unlocked(records)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > _MAX_STORE_BYTES:
                raise BridgeAccessError("LAN BRIDGE access-key store is unexpectedly large", 503)
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except BridgeAccessError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BridgeAccessError("LAN BRIDGE access-key store could not be read", 503) from exc
        records = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise BridgeAccessError("LAN BRIDGE access-key store is invalid", 503)
        return [dict(item) for item in records if isinstance(item, dict)]

    def _write_unlocked(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "keys": records}
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            os.replace(temporary, self.path)
            temporary = None
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _find_index(records: list[dict[str, Any]], key_id: str) -> int:
        for index, record in enumerate(records):
            if not record.get("revoked", False) and str(record.get("id") or "") == key_id:
                return index
        raise KeyError(key_id)


_STORE_LOCK = threading.RLock()
_STORES: dict[Path, AccessKeyStore] = {}


def get_access_key_store(config) -> AccessKeyStore:
    path = access_key_store_path(config).resolve()
    with _STORE_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = AccessKeyStore(path)
            _STORES[path] = store
        return store


def configured_access_keys(config) -> list[dict[str, Any]]:
    return get_access_key_store(config).list_records()


def enabled_access_keys_exist(config) -> bool:
    return any(
        bool(item.get("enabled", True)) and _valid_hash(item.get("key_hash"))
        for item in configured_access_keys(config)
    )


def normalize_allowed_models(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("allowed_models must be a list")
    normalized: list[str] = []
    for raw in value:
        model = str(raw or "").strip()
        if not model:
            continue
        if len(model) > 200:
            raise ValueError("model name is too long")
        if model not in normalized:
            normalized.append(model)
    if not normalized:
        raise ValueError("Select at least one allowed model")
    if "*" in normalized:
        return ["*"]
    if len(normalized) > 256:
        raise ValueError("Too many allowed models")
    return normalized


def generate_access_key() -> str:
    return ACCESS_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_access_key(raw_key: str) -> str:
    return hashlib.sha256(_HASH_DOMAIN + str(raw_key).encode("utf-8")).hexdigest()


def create_access_key_record(name: str, allowed_models: Any) -> tuple[str, dict[str, Any]]:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("Key name is required")
    if len(normalized_name) > 100:
        raise ValueError("Key name is too long")
    raw_key = generate_access_key()
    record = {
        "id": uuid.uuid4().hex,
        "name": normalized_name,
        "prefix": raw_key[:12],
        "key_hash": hash_access_key(raw_key),
        "allowed_models": normalize_allowed_models(allowed_models),
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return raw_key, record


def rotate_access_key_record(record: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    raw_key = generate_access_key()
    rotated = dict(record)
    rotated["prefix"] = raw_key[:12]
    rotated["key_hash"] = hash_access_key(raw_key)
    rotated["rotated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return raw_key, rotated


def public_access_key_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "prefix": str(record.get("prefix") or ""),
        "allowed_models": list(record.get("allowed_models") or []),
        "enabled": bool(record.get("enabled", True)),
        "created_at": str(record.get("created_at") or ""),
        "rotated_at": str(record.get("rotated_at") or ""),
    }


def authenticate_bridge_headers(
    headers: Mapping[str, str],
    config,
) -> BridgePrincipal:
    """Authenticate a bridge bearer key, or return unmanaged access when disabled."""
    if not access_control_enabled(config):
        return ANONYMOUS_PRINCIPAL
    if not enabled_access_keys_exist(config):
        raise BridgeAccessError("LAN BRIDGE has no enabled access keys", 503)

    authorization = str(headers.get("authorization") or "")
    scheme, separator, supplied = authorization.partition(" ")
    supplied = supplied.strip()
    if not separator or scheme.lower() != "bearer" or not supplied:
        raise BridgeAccessError("LAN BRIDGE bearer key is required")

    supplied_hash = hash_access_key(supplied)
    matched: dict[str, Any] | None = None
    for record in configured_access_keys(config):
        stored_hash = str(record.get("key_hash") or "")
        is_match = _valid_hash(stored_hash) and hmac.compare_digest(
            supplied_hash.encode("ascii"), stored_hash.encode("ascii")
        )
        if is_match:
            matched = record
    if matched is None or not bool(matched.get("enabled", True)):
        raise BridgeAccessError("Invalid or disabled LAN BRIDGE bearer key")

    try:
        allowed_models = tuple(normalize_allowed_models(matched.get("allowed_models", [])))
    except ValueError as exc:
        raise BridgeAccessError("LAN BRIDGE key has no valid model permissions", 403) from exc
    return BridgePrincipal(
        key_id=str(matched.get("id") or ""),
        name=str(matched.get("name") or ""),
        prefix=str(matched.get("prefix") or ""),
        allowed_models=allowed_models,
    )


def require_model_access(principal: BridgePrincipal, model: str) -> None:
    if not principal.can_use_model(model):
        raise BridgeAccessError(
            f"This LAN BRIDGE key is not allowed to use model '{str(model or 'unknown')[:200]}'",
            403,
        )


def _valid_hash(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)

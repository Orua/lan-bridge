"""Stable account-scoped model snapshots; candidates are confirmed before replacement."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

REFRESH_SECONDS = 300
RETRY_DELAY = .3
_TASKS = set()


def state_root():
    return Path(os.getenv("LAN_BRIDGE_STATE_DIR") or (Path.home() / ".lan-bridge"))


def account_scope(config, url, query, headers=None):
    from .codex_auth import resolve_auth_file, native_auth_injection_enabled
    # Pass-through clients can carry different accounts. Only the final digest
    # reaches disk; never persist the bearer token or account header itself.
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    identity = [headers.get("authorization", ""), headers.get("chatgpt-account-id", "")]
    if native_auth_injection_enabled(config):
        path = resolve_auth_file(config)
        identity = str(path.resolve())
        try:
            if path.stat().st_size <= 1024 * 1024:
                data = json.loads(path.read_text("utf-8"))
                identity += "|" + str(data.get("tokens", {}).get("account_id") or "missing-account")
        except (OSError, ValueError, TypeError, AttributeError):
            identity += "|unreadable"
    canonical = [(k, v) for k, v in query if k != "client_version"]
    return hashlib.sha256(json.dumps([identity, url, canonical], sort_keys=True).encode()).hexdigest()


def model_ids(payload):
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return set()
    return {str(m.get("slug") or "") for m in models if isinstance(m, dict) and m.get("slug")}


def has_gpt(payload):
    return any(x.startswith("gpt-") for x in model_ids(payload))


class CatalogSnapshot:
    def __init__(self, scope):
        self.scope = scope
        self.path = state_root() / "catalog" / (scope + ".json")
        self.payload = None
        self.checked_at = 0.0
        self.next_check = 0.0
        self.error_code = ""
        self.upstream_status = 0
        self.task = None
        self.lock = asyncio.Lock()
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                return
            saved = json.loads(self.path.read_text("utf-8"))
            if isinstance(saved, dict) and saved.get("scope") == scope and has_gpt(saved.get("payload")):
                self.payload = saved["payload"]
                self.checked_at = float(saved.get("checked_at", 0))
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump({"scope": self.scope, "checked_at": self.checked_at, "payload": self.payload}, output, ensure_ascii=False)
            temporary.replace(self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    async def refresh(self, fetch, emit):
        async with self.lock:
            previous = model_ids(self.payload)
            candidate_ids = None
            try:
                for attempt in range(3):
                    try:
                        payload, status, code = await fetch()
                    except asyncio.CancelledError:
                        raise
                    self.upstream_status = status
                    self.error_code = code
                    ids = model_ids(payload)
                    if not code and has_gpt(payload):
                        # An unchanged list is trusted immediately. Every changed/new list
                        # requires two matching successful observations.
                        if ids == previous or ids == candidate_ids:
                            self.payload = payload
                            self.checked_at = time.time()
                            self.error_code = ""
                            try:
                                await asyncio.to_thread(self.save)
                            except OSError:
                                emit("catalog.persistence_failed", reason="cache_write_failed")
                            emit("catalog.confirmed", previous_count=len(previous), current_count=len(ids),
                                 added=sorted(ids - previous), removed=sorted(previous - ids), attempts=attempt + 1)
                            return
                        candidate_ids = ids
                        self.error_code = "catalog_change_unconfirmed"
                        emit("catalog.change_detected", added=sorted(ids - previous), removed=sorted(previous - ids), attempt=attempt + 1)
                    else:
                        self.error_code = code or "chatgpt_models_missing"
                        candidate_ids = None
                        emit("catalog.retry", attempt=attempt + 1, reason=self.error_code, upstream_status=status,
                             retained_count=len(previous))
                    if attempt < 2:
                        await asyncio.sleep(RETRY_DELAY)
                emit("catalog.refresh_failed", reason=self.error_code, upstream_status=self.upstream_status,
                     retained_count=len(previous))
            finally:
                close = getattr(fetch, "close", None)
                if close:
                    await close()
                self.next_check = time.monotonic() + REFRESH_SECONDS

    def start_refresh(self, fetch, emit):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.refresh(fetch, emit))
            _TASKS.add(self.task)
            self.task.add_done_callback(_TASKS.discard)
            self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)


async def close_catalog_tasks():
    tasks = list(_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

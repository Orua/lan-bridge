"""Application-owned native connection pool with request-scoped leases."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from http.cookiejar import CookieJar, DefaultCookiePolicy

import anyio
import httpx

from .http_utils import make_native_async_client


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


class _Entry:
    def __init__(self, client):
        self.client = client
        self.active = 0
        self.retired = False


class NativeClientPool:
    """Keep a bounded set of clients; retired active leases drain naturally.

    Only the official native route uses this pool. No credential material is
    stored in pool keys, cookies, shared headers, diagnostics or logs.
    """

    def __init__(self):
        self._entries = OrderedDict()
        self._generation = None
        self._closing = set()
        self._closed = False

    def lease(self, origin, proxy_url, timeout):
        if self._closed:
            raise RuntimeError("Native pool is closed")
        return NativeClientLease(self, origin, proxy_url, timeout)

    def _schedule_close(self, entry):
        task = asyncio.create_task(entry.client.aclose())
        self._closing.add(task)

        def finished(task):
            self._closing.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logging.getLogger("lan-bridge.http").warning("Retired native client cleanup failed")

        task.add_done_callback(finished)

    def retire(self):
        for entry in self._entries.values():
            entry.retired = True
            if not entry.active:
                self._schedule_close(entry)
        self._entries.clear()

    def _acquire(self, origin, proxy_url, timeout, headers):
        if self._closed:
            raise RuntimeError("Native pool is closed")
        # Header names must be case insensitive. Include token rotation even
        # within one account; never use a raw token as the pool identity.
        headers = httpx.Headers(headers)
        identity = hashlib.sha256((headers.get("authorization", "") + "\0" + headers.get("chatgpt-account-id", "")).encode()).digest()
        generation = (origin, proxy_url, identity)
        if generation != self._generation:
            self.retire()
            self._generation = generation
        key = (generation, tuple(timeout.as_dict().items()))
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(make_native_async_client(
                proxy_url=proxy_url, timeout=timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30),
                cookies=CookieJar(policy=_NoCookies()),
            ))
            self._entries[key] = entry
            while len(self._entries) > 8:
                _, old = self._entries.popitem(last=False)
                old.retired = True
                if not old.active:
                    self._schedule_close(old)
        else:
            self._entries.move_to_end(key)
        entry.active += 1
        return entry

    async def close(self):
        self._closed = True
        self.retire()
        with anyio.CancelScope(shield=True):
            if self._closing:
                await asyncio.gather(*self._closing)
                self._closing.clear()


class NativeClientLease:
    """Closing a request releases its lease, never another request's client."""

    def __init__(self, pool, origin, proxy_url, timeout):
        self._pool = pool
        self._origin = origin
        self._proxy_url = proxy_url
        self._timeout = timeout
        self._entry = None
        self._closed = False

    def build_request(self, method, url, **kwargs):
        if self._closed or self._entry is not None:
            raise RuntimeError("Native lease builds exactly one request")
        self._entry = self._pool._acquire(self._origin, self._proxy_url, self._timeout, kwargs.get("headers", {}))
        return self._entry.client.build_request(method, url, **kwargs)

    async def send(self, request, **kwargs):
        if self._closed or self._entry is None:
            raise RuntimeError("Native lease is not active")
        return await self._entry.client.send(request, **kwargs)

    async def get(self, url, **kwargs):
        return await self.send(self.build_request("GET", url, **kwargs))

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        if self._entry is not None:
            entry = self._entry
            entry.active -= 1
            if entry.retired and not entry.active:
                with anyio.CancelScope(shield=True):
                    await entry.client.aclose()

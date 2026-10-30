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

logger = logging.getLogger("lan-bridge.http")


class NativePoolBusy(httpx.HTTPError):
    """Capacity was not acquired; the business request has not been sent."""

    code = "native_upstream_busy"

    def __init__(self):
        super().__init__("Official upstream is busy; its bounded queue is full or timed out. Retry later.")


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

    def __init__(self, *, max_active=32, max_waiters=128, wait_timeout=10.0):
        if not 1 <= max_active <= 128 or not 0 <= max_waiters <= 512 or not 0 < wait_timeout <= 30:
            raise ValueError("Invalid native pool capacity or queue timeout")
        self._entries = OrderedDict()
        self._generation = None
        self._closing = set()
        self._closed = False
        self.max_active = max_active
        self.max_waiters = max_waiters
        self.wait_timeout = float(wait_timeout)
        self._slots = asyncio.Semaphore(max_active)
        self._reserved = 0
        self._active = 0
        self._waiting = 0
        self._rejected = 0
        self._resets = 0

    def snapshot(self):
        return {"active": self._active, "waiting": self._waiting,
                "reserved": self._reserved, "max_active": self.max_active,
                "max_waiters": self.max_waiters, "wait_timeout_seconds": self.wait_timeout,
                "rejected": self._rejected, "resets": self._resets}

    def _reserve(self):
        if self._closed:
            raise RuntimeError("Native pool is closed")
        if self._reserved >= self.max_active + self.max_waiters:
            self._rejected += 1
            raise NativePoolBusy()
        self._reserved += 1

    async def _admit(self):
        if not self._slots.locked():
            await self._slots.acquire()
            self._active += 1
            return
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), self.wait_timeout)
            except TimeoutError:
                self._rejected += 1
                raise NativePoolBusy() from None
            self._active += 1
        finally:
            self._waiting -= 1

    def invalidate(self, entry, reason):
        """Stop assigning new work, without interrupting other active streams."""
        if entry.retired:
            return
        for key, value in tuple(self._entries.items()):
            if value is entry:
                del self._entries[key]
        entry.retired = True
        self._resets += 1
        logger.warning("Native HTTP pool retired reason=%s active_leases=%d", reason, entry.active)
        if not entry.active:
            self._schedule_close(entry)

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
        base_key = (generation, tuple(timeout.as_dict().items()))
        # VPN handshakes fail independently. Keep up to four lazy lanes so a
        # failed tunnel retires only its lane, preserving other warm clients.
        # Sequential requests still use one client; admission bounds all lanes.
        lane_count = 4 if proxy_url else 1
        keys = [(*base_key, lane) for lane in range(lane_count)]
        idle = [key for key in keys if key in self._entries and not self._entries[key].active]
        unused = [key for key in keys if key not in self._entries]
        key = (idle or unused or sorted(keys, key=lambda key: self._entries[key].active))[0]
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(make_native_async_client(
                proxy_url=proxy_url, timeout=timeout,
                limits=httpx.Limits(max_connections=self.max_active, max_keepalive_connections=min(10, self.max_active), keepalive_expiry=30),
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
        # Pool acquisition must not inherit the 600-second model read budget.
        self._timeout = httpx.Timeout(timeout)
        self._timeout.pool = min(timeout.pool if timeout.pool is not None else pool.wait_timeout, pool.wait_timeout)
        self._entry = None
        self._closed = False
        self._reserved = False
        self._admitted = False

    def build_request(self, method, url, **kwargs):
        if self._closed or self._entry is not None:
            raise RuntimeError("Native lease builds exactly one request")
        self._pool._reserve()
        self._reserved = True
        self._entry = self._pool._acquire(self._origin, self._proxy_url, self._timeout, kwargs.get("headers", {}))
        return self._entry.client.build_request(method, url, **kwargs)

    async def _release_entry(self):
        entry, self._entry = self._entry, None
        if entry is not None:
            entry.active -= 1
            if entry.retired and not entry.active:
                with anyio.CancelScope(shield=True):
                    await entry.client.aclose()

    async def _refresh(self, request):
        if self._entry.retired:
            await self._release_entry()
            self._entry = self._pool._acquire(self._origin, self._proxy_url, self._timeout, request.headers)

    def invalidate(self, reason):
        if self._entry is not None:
            self._pool.invalidate(self._entry, reason)

    async def send(self, request, **kwargs):
        if self._closed or self._entry is None:
            raise RuntimeError("Native lease is not active")
        if not self._admitted:
            await self._pool._admit()
            self._admitted = True
        # A previous connect-only retry or another request can retire this
        # entry. Reuse the same request bytes only before any successful send.
        await self._refresh(request)
        for attempt in range(2):
            try:
                return await self._entry.client.send(request, **kwargs)
            except httpx.PoolTimeout:
                self.invalidate("PoolTimeout")
                if attempt:
                    raise
                # PoolTimeout means no connection was assigned, hence no
                # business bytes were sent. At most one safe pool reset retry.
                await self._refresh(request)
            except (httpx.TransportError, asyncio.CancelledError) as exc:
                self.invalidate(type(exc).__name__)
                raise
        raise AssertionError("unreachable")

    async def get(self, url, **kwargs):
        return await self.send(self.build_request("GET", url, **kwargs))

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            await self._release_entry()
        finally:
            if self._admitted:
                self._admitted = False
                self._pool._active -= 1
                self._pool._slots.release()
            if self._reserved:
                self._reserved = False
                self._pool._reserved -= 1

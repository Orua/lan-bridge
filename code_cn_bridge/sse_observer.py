"""Observe JSON SSE events without changing any forwarded bytes."""

from __future__ import annotations

import json
from typing import Any

_SSE_PENDING_MAX_BYTES = 8 * 1024 * 1024
_SSE_EVENT_MAX_BYTES = 8 * 1024 * 1024
_SSE_EVENT_MAX_LINES = 4096


class SSEJsonObserver:
    def __init__(self) -> None:
        self.pending = b""
        self.data: list[bytes] = []
        self.data_bytes = 0

    def _line(self, line: bytes) -> list[dict[str, Any]]:
        line = line.rstrip(b"\r")
        if len(line) > _SSE_PENDING_MAX_BYTES:
            self.data.clear()
            self.data_bytes = 0
            raise ValueError(f"SSE line exceeds {_SSE_PENDING_MAX_BYTES} byte limit")
        if not line:
            self.data.clear()
            self.data_bytes = 0
            return []
        if not line.startswith(b"data:"):
            return []
        value = line[5:].lstrip(b" ")
        if value == b"[DONE]":
            self.data.clear()
            self.data_bytes = 0
            return []
        if len(self.data) >= _SSE_EVENT_MAX_LINES or self.data_bytes + len(value) > _SSE_EVENT_MAX_BYTES:
            self.data.clear()
            self.data_bytes = 0
            raise ValueError("SSE event exceeds configured byte or line limit")
        self.data.append(value)
        self.data_bytes += len(value)
        try:
            event = json.loads(b"\n".join(self.data))
        except (ValueError, UnicodeError):
            return []
        self.data.clear()
        self.data_bytes = 0
        return [event] if isinstance(event, dict) else []

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if len(self.pending) + len(chunk) > _SSE_PENDING_MAX_BYTES and b"\n" not in chunk:
            self.pending = b""
            self.data.clear()
            self.data_bytes = 0
            raise ValueError(f"SSE line exceeds {_SSE_PENDING_MAX_BYTES} byte limit")
        self.pending += chunk
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        if len(self.pending) > _SSE_PENDING_MAX_BYTES:
            self.pending = b""
            self.data.clear()
            self.data_bytes = 0
            raise ValueError(f"SSE line exceeds {_SSE_PENDING_MAX_BYTES} byte limit")
        return [event for line in lines for event in self._line(line)]

    def finish(self) -> list[dict[str, Any]]:
        events = self._line(self.pending) if self.pending else []
        self.pending = b""
        self.data.clear()
        self.data_bytes = 0
        return events

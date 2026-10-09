"""Observe JSON SSE events without changing any forwarded bytes."""

from __future__ import annotations

import json
from typing import Any


class SSEJsonObserver:
    def __init__(self) -> None:
        self.pending = b""
        self.data: list[bytes] = []

    def _line(self, line: bytes) -> list[dict[str, Any]]:
        line = line.rstrip(b"\r")
        if not line:
            self.data.clear()
            return []
        if not line.startswith(b"data:"):
            return []
        value = line[5:].lstrip(b" ")
        if value == b"[DONE]":
            self.data.clear()
            return []
        self.data.append(value)
        try:
            event = json.loads(b"\n".join(self.data))
        except (ValueError, UnicodeError):
            return []
        self.data.clear()
        return [event] if isinstance(event, dict) else []

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.pending += chunk
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        return [event for line in lines for event in self._line(line)]

    def finish(self) -> list[dict[str, Any]]:
        events = self._line(self.pending) if self.pending else []
        self.pending = b""
        self.data.clear()
        return events

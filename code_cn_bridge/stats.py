"""Request statistics, recent request logs, and lightweight usage files."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

logger = logging.getLogger("lan-bridge.stats")

_ACCESS_LOG_MAX_BYTES = 20 * 1024 * 1024
_ACCESS_LOG_BACKUP_COUNT = 3
_USAGE_RETENTION_DAYS = 31
_DATED_USAGE_RE = re.compile(r"^(?:access|usage)-(\d{4}-\d{2}-\d{2})(?:\.jsonl(?:\.\d+)?|\.json)$")


@dataclass
class RequestLog:
    timestamp: float
    model: str
    endpoint: str
    status_code: int
    elapsed_ms: float
    tokens: int = 0
    error: str = ""
    stream: bool = False
    provider: str = ""
    target_model: str = ""
    upstream_api: str = ""
    client_ip: str = ""
    input_tools: str = ""
    chat_tools: str = ""
    first_response_ms: float | None = None
    access_key_id: str = ""
    access_key_prefix: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "time": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
            "model": self.model,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "tokens": self.tokens,
            "error": self.error,
            "stream": self.stream,
            "provider": self.provider,
            "target_model": self.target_model,
            "upstream_api": self.upstream_api,
            "client_ip": self.client_ip,
            "input_tools": self.input_tools,
            "chat_tools": self.chat_tools,
            "first_response_ms": round(self.first_response_ms, 1) if self.first_response_ms is not None else None,
            "access_key_id": self.access_key_id,
            "access_key_prefix": self.access_key_prefix,
        }


class UsageStore:
    """Persist request access records and token totals without storing prompts."""

    def __init__(self, usage_dir: str | Path):
        self.usage_dir = Path(usage_dir)
        self._last_cleanup_day: str | None = None

    def record(self, log: RequestLog) -> None:
        try:
            self.usage_dir.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.localtime(log.timestamp))
            self._append_access_log(day, log)
            self._increment_usage_file(self.usage_dir / f"usage-{day}.json", log, day)
            self._increment_usage_file(self.usage_dir / "usage-total.json", log, None)
            if self._last_cleanup_day != day:
                self._cleanup_old_files(day)
                self._last_cleanup_day = day
        except Exception as exc:
            logger.debug("write usage stats failed: %s", exc)

    def get_usage(self, day: str | None = None) -> dict:
        if day:
            return self._read_json(self.usage_dir / f"usage-{day}.json", self._empty_usage(day))
        return self._read_json(self.usage_dir / "usage-total.json", self._empty_usage(None))

    def _append_access_log(self, day: str, log: RequestLog) -> None:
        record = {
            "ts": log.timestamp,
            "time": datetime.fromtimestamp(log.timestamp).isoformat(timespec="seconds"),
            "client_ip": log.client_ip or "unknown",
            "access_key_id": log.access_key_id,
            "access_key_prefix": log.access_key_prefix,
            "endpoint": log.endpoint,
            "model": log.model,
            "provider": log.provider,
            "target_model": log.target_model,
            "upstream_api": log.upstream_api,
            "status_code": log.status_code,
            "stream": log.stream,
            "tokens": int(log.tokens or 0),
            "elapsed_ms": round(log.elapsed_ms, 1),
            "first_response_ms": round(log.first_response_ms, 1) if log.first_response_ms is not None else None,
            "error": bool(log.error),
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        path = self.usage_dir / f"access-{day}.jsonl"
        try:
            current_size = path.stat().st_size
        except OSError:
            current_size = 0
        if current_size and current_size + len(line.encode("utf-8")) > _ACCESS_LOG_MAX_BYTES:
            self._rotate_access_log(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    @staticmethod
    def _rotate_access_log(path: Path) -> None:
        oldest = path.with_name(f"{path.name}.{_ACCESS_LOG_BACKUP_COUNT}")
        oldest.unlink(missing_ok=True)
        for index in range(_ACCESS_LOG_BACKUP_COUNT - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        if path.exists():
            path.replace(path.with_name(f"{path.name}.1"))

    def _cleanup_old_files(self, reference_day: str) -> None:
        try:
            reference = datetime.strptime(reference_day, "%Y-%m-%d").date()
        except ValueError:
            return
        cutoff = reference - timedelta(days=max(_USAGE_RETENTION_DAYS - 1, 0))
        for item in self.usage_dir.iterdir():
            match = _DATED_USAGE_RE.match(item.name)
            if not match:
                continue
            try:
                item_day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                if item_day < cutoff:
                    item.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue

    def _increment_usage_file(self, path: Path, log: RequestLog, day: str | None) -> None:
        data = self._read_json(path, self._empty_usage(day))
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if day:
            data["date"] = day
        ip = log.client_ip or "unknown"
        totals = data.setdefault("totals", self._empty_counter())
        by_ip = data.setdefault("by_ip", {})
        ip_totals = by_ip.setdefault(ip, self._empty_counter())
        self._increment_counter(totals, log)
        self._increment_counter(ip_totals, log)
        if log.access_key_id:
            by_key = data.setdefault("by_key", {})
            key_totals = by_key.setdefault(log.access_key_id, self._empty_counter())
            key_totals["prefix"] = log.access_key_prefix
            key_totals["last_used_at"] = datetime.fromtimestamp(
                log.timestamp, timezone.utc
            ).isoformat(timespec="seconds")
            self._increment_counter(key_totals, log)
        self._write_json_atomic(path, data)

    @staticmethod
    def _empty_counter() -> dict:
        return {"requests": 0, "success": 0, "errors": 0, "tokens": 0}

    def _empty_usage(self, day: str | None) -> dict:
        data = {
            "updated_at": "",
            "totals": self._empty_counter(),
            "by_ip": {},
            "by_key": {},
        }
        if day:
            data["date"] = day
        return data

    @staticmethod
    def _increment_counter(counter: dict, log: RequestLog) -> None:
        counter["requests"] = int(counter.get("requests", 0)) + 1
        if log.status_code < 400:
            counter["success"] = int(counter.get("success", 0)) + 1
        else:
            counter["errors"] = int(counter.get("errors", 0)) + 1
        counter["tokens"] = int(counter.get("tokens", 0)) + max(int(log.tokens or 0), 0)

    @staticmethod
    def _read_json(path: Path, default: dict) -> dict:
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else default
        except Exception:
            return default

    @staticmethod
    def _write_json_atomic(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)


class StatsCollector:
    """Thread-safe in-memory stats plus best-effort persisted usage totals."""

    def __init__(self, max_logs: int = 500, usage_dir: str | Path | None = None):
        self._lock = threading.RLock()
        self._start_time = time.time()
        self._request_count = 0
        self._success_count = 0
        self._error_count = 0
        self._total_latency_ms = 0.0
        self._first_response_count = 0
        self._total_first_response_ms = 0.0
        self._total_tokens = 0
        self._logs: deque[RequestLog] = deque(maxlen=max_logs)
        self._log_listeners: list[callable] = []
        self._usage_store = UsageStore(usage_dir or _default_usage_dir())

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def add_listener(self, callback):
        self._log_listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._log_listeners:
            self._log_listeners.remove(callback)

    def record(self, log: RequestLog):
        with self._lock:
            self._request_count += 1
            if log.status_code < 400:
                self._success_count += 1
            else:
                self._error_count += 1
            self._total_latency_ms += log.elapsed_ms
            if log.first_response_ms is not None:
                self._first_response_count += 1
                self._total_first_response_ms += log.first_response_ms
            self._total_tokens += int(log.tokens or 0)
            self._logs.appendleft(log)
            self._usage_store.record(log)

        d = log.to_dict()
        for cb in self._log_listeners:
            try:
                cb(d)
            except Exception:
                pass

    def get_summary(self) -> dict:
        with self._lock:
            avg_latency = (self._total_latency_ms / self._request_count) if self._request_count > 0 else 0
            avg_first_response = (
                self._total_first_response_ms / self._first_response_count
                if self._first_response_count > 0 else 0
            )
            return {
                "uptime_seconds": round(self.uptime_seconds, 0),
                "request_count": self._request_count,
                "success_count": self._success_count,
                "error_count": self._error_count,
                "avg_latency_ms": round(avg_latency, 1),
                "avg_response_ms": round(avg_latency, 1),
                "avg_first_response_ms": round(avg_first_response, 1),
                "total_tokens": self._total_tokens,
            }

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [log.to_dict() for log in list(self._logs)[:limit]]

    def get_usage_summary(self, day: str | None = None) -> dict:
        with self._lock:
            return self._usage_store.get_usage(day)

    def clear_logs(self):
        with self._lock:
            self._logs.clear()
            self._request_count = 0
            self._success_count = 0
            self._error_count = 0
            self._total_latency_ms = 0.0
            self._first_response_count = 0
            self._total_first_response_ms = 0.0
            self._total_tokens = 0


_stats: StatsCollector | None = None


def get_stats() -> StatsCollector:
    global _stats
    if _stats is None:
        _stats = StatsCollector()
    return _stats


def _default_usage_dir() -> Path:
    return Path.home() / ".lan-bridge" / "usage"

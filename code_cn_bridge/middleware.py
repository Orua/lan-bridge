"""FastAPI middleware for errors, access logging, and log redaction."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .models import build_error_response

logger = logging.getLogger("lan-bridge")
_current_client_ip: ContextVar[str] = ContextVar("lan_bridge_client_ip", default="")


def client_ip_from_request(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def current_client_ip() -> str:
    return _current_client_ip.get() or "unknown"


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            logger.exception("Unhandled exception: %s", exc)
            return JSONResponse(
                content=build_error_response(str(exc)),
                status_code=500,
            )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        client_ip = client_ip_from_request(request)
        _current_client_ip.set(client_ip)
        request.state.client_ip = client_ip

        if request.url.path == "/health":
            return await call_next(request)

        logger.info("access start method=%s path=%s ip=%s", request.method, request.url.path, client_ip)

        response = await call_next(request)

        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "access done method=%s path=%s ip=%s status=%d elapsed_ms=%.1f",
            request.method,
            request.url.path,
            client_ip,
            response.status_code,
            elapsed,
        )
        return response


class ApiKeyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, dict):
            record.args = {
                k: ("***" if "key" in k.lower() or "token" in k.lower() else v)
                for k, v in record.args.items()
            }
        return True

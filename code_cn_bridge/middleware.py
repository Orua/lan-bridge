"""FastAPI middleware for errors, access logging, and log redaction."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .models import build_error_response
from .access_control import (
    ANONYMOUS_PRINCIPAL,
    BridgeAccessError,
    BridgePrincipal,
    authenticate_bridge_headers,
)

logger = logging.getLogger("lan-bridge")
_current_client_ip: ContextVar[str] = ContextVar("lan_bridge_client_ip", default="")
_current_bridge_principal: ContextVar[BridgePrincipal] = ContextVar(
    "lan_bridge_principal",
    default=ANONYMOUS_PRINCIPAL,
)


def client_ip_from_request(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def current_client_ip() -> str:
    return _current_client_ip.get() or "unknown"


def current_bridge_principal() -> BridgePrincipal:
    return _current_bridge_principal.get()


@contextmanager
def bridge_principal_context(principal: BridgePrincipal):
    """Bind an explicitly captured principal while a streaming body is consumed."""
    token = _current_bridge_principal.set(principal)
    try:
        yield
    finally:
        try:
            _current_bridge_principal.reset(token)
        except ValueError:
            # Starlette can finalize a disconnected streaming iterator in a
            # different asyncio context. Resetting a token across contexts
            # raises ValueError; contain it so a client disconnect can never
            # become an unhandled task exception.
            _current_bridge_principal.set(ANONYMOUS_PRINCIPAL)
            logger.warning("流式请求已在不同异步上下文中结束，访问主体已安全清理")


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


class BridgeAccessMiddleware(BaseHTTPMiddleware):
    """Authenticate every OpenAI-compatible endpoint with a managed bridge key."""

    async def dispatch(self, request: Request, call_next):
        if request.method != "OPTIONS" and request.url.path.startswith("/v1/"):
            from .config import get_config

            try:
                principal = authenticate_bridge_headers(request.headers, get_config())
            except BridgeAccessError as exc:
                status_code = exc.status_code
                error_type = (
                    "bridge_configuration_error"
                    if status_code == 503
                    else "bridge_auth_error"
                )
                headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
                return JSONResponse(
                    content=build_error_response(str(exc), error_type, status_code),
                    status_code=status_code,
                    headers=headers,
                )
            request.state.bridge_principal = principal
            token = _current_bridge_principal.set(principal)
            try:
                return await call_next(request)
            finally:
                _current_bridge_principal.reset(token)
        return await call_next(request)


class ApiKeyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, dict):
            record.args = {
                k: ("***" if "key" in k.lower() or "token" in k.lower() else v)
                for k, v in record.args.items()
            }
        return True

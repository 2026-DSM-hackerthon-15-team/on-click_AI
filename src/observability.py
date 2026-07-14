"""Structured, secret-safe logging shared by every ON:CLICK service."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)


class JsonLogFormatter(logging.Formatter):
    """Render one JSON object per line for Docker and log collectors."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": getattr(record, "service", self.service_name),
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
            "requestId": getattr(record, "requestId", current_request_id()),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_FIELDS or key.startswith("_") or key in payload:
                continue
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service_name: str) -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonLogFormatter(service_name))
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)


def current_request_id() -> str:
    return _request_id.get()


def _incoming_request_id(request: Request) -> str:
    candidate = request.headers.get("X-Request-ID", "").strip()
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def safe_upstream_target(url: str) -> str:
    """Return scheme/host/path only, excluding credentials and query values."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


def upstream_service_name(url: str) -> str:
    parsed = urlparse(url)
    configured_services = {
        "API_BASE_URL": "BACKEND_API",
        "AI_SERVICE_URL": "AI_SERVICE",
        "MCP_SERVICE_URL": "MCP_SERVICE",
        "STATS_SERVICE_URL": "STATS_SERVICE",
    }
    for env_name, service_name in configured_services.items():
        configured = os.getenv(env_name)
        if not configured:
            continue
        base = urlparse(configured)
        if (parsed.scheme, parsed.hostname, parsed.port) == (
            base.scheme,
            base.hostname,
            base.port,
        ):
            return service_name
    port = parsed.port
    if port == 8000:
        return "BACKEND_API"
    if port == 8001:
        return "AI_SERVICE"
    if port == 8002:
        return "MCP_SERVICE"
    if port == 8003:
        return "STATS_SERVICE"
    return "EXTERNAL_SERVICE"


def request_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Add correlation only; never copy inbound credentials or request bodies."""
    result = dict(headers or {})
    request_id = current_request_id()
    if request_id != "-":
        result.setdefault("X-Request-ID", request_id)
    return result


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    service: str | None = None,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    extra = {"event": event, "requestId": current_request_id()}
    for key, value in fields.items():
        safe_key = f"context_{key}" if key in _STANDARD_LOG_RECORD_FIELDS else key
        extra[safe_key] = value
    if service:
        extra["service"] = service
    logger.log(level, event, extra=extra, exc_info=exc_info)


def install_observability(app: FastAPI, service_name: str) -> None:
    configure_logging(service_name)
    logger = logging.getLogger("on_click.http")

    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        request_id = _incoming_request_id(request)
        token = _request_id.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "request.started",
            service=service_name,
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            level = logging.WARNING if response.status_code >= 400 else logging.INFO
            log_event(
                logger,
                level,
                "request.completed",
                service=service_name,
                method=request.method,
                path=request.url.path,
                statusCode=response.status_code,
                latencyMs=latency_ms,
            )
            return response
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                logger,
                logging.ERROR,
                "request.unhandled_exception",
                service=service_name,
                method=request.method,
                path=request.url.path,
                statusCode=500,
                latencyMs=latency_ms,
                errorCode="INTERNAL_SERVER_ERROR",
                exceptionType=exc.__class__.__name__,
                exc_info=True,
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "errorCode": "INTERNAL_SERVER_ERROR",
                    "message": "서버 내부 오류가 발생했습니다. requestId로 서버 로그를 확인해 주세요.",
                    "requestId": request_id,
                    "retryable": False,
                },
            )
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            _request_id.reset(token)

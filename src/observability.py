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
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse


_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)
_BROWSER_LOG_FIELDS = {
    "timestamp",
    "level",
    "service",
    "logger",
    "event",
    "requestId",
    "method",
    "path",
    "statusCode",
    "latencyMs",
    "errorCode",
    "retryable",
    "upstreamService",
    "upstreamTarget",
    "upstreamStatus",
    "failureStage",
    "exceptionType",
    "aiProvider",
    "aiModel",
    "tool",
    "imageCount",
    "hashtagCount",
    "storeId",
    "transactionCount",
    "serviceModule",
    "port",
    "pid",
    "exitCode",
}


def _browser_buffer_size() -> int:
    try:
        configured = int(os.getenv("BROWSER_LOG_BUFFER_SIZE", "500"))
    except ValueError:
        configured = 500
    return max(50, min(configured, 5000))


_browser_log_buffer: deque[dict[str, Any]] = deque(
    maxlen=_browser_buffer_size()
)


class BrowserLogHandler(logging.Handler):
    """Keep a strictly allow-listed, browser-safe rolling view of application logs."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("on_click."):
            return
        try:
            rendered = JsonLogFormatter(
                str(getattr(record, "service", self.service_name))
            ).format(record)
            payload = json.loads(rendered)
            _browser_log_buffer.append(
                {key: payload[key] for key in _BROWSER_LOG_FIELDS if key in payload}
            )
        except Exception:
            self.handleError(record)


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
    root.addHandler(BrowserLogHandler(service_name))
    for external_logger in ("httpx", "httpcore", "urllib3", "anthropic", "openai"):
        logging.getLogger(external_logger).setLevel(logging.WARNING)


def current_request_id() -> str:
    return _request_id.get()


def browser_logs(
    *,
    limit: int = 200,
    request_id: str | None = None,
    level: str | None = None,
    event: str | None = None,
) -> list[dict[str, Any]]:
    """Return newest browser-safe logs only; tracebacks and secrets are never included."""
    rows = list(_browser_log_buffer)
    if request_id:
        rows = [row for row in rows if row.get("requestId") == request_id]
    if level:
        rows = [row for row in rows if row.get("level") == level.upper()]
    if event:
        rows = [row for row in rows if row.get("event") == event]
    return list(reversed(rows[-max(1, min(limit, 500)) :]))


def install_browser_log_api(
    app: FastAPI,
    service_name: str,
    require_backend_jwt: Any,
) -> None:
    @app.get("/internal/observability/logs", include_in_schema=False)
    def read_browser_logs(
        limit: int = Query(default=200, ge=1, le=500),
        requestId: str | None = Query(default=None, max_length=128),
        level: str | None = Query(default=None, max_length=20),
        event: str | None = Query(default=None, max_length=100),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_backend_jwt(authorization)
        return {
            "service": service_name,
            "logs": browser_logs(
                limit=limit,
                request_id=requestId,
                level=level,
                event=event,
            ),
        }


def render_log_viewer_html() -> str:
    """A dependency-free browser viewer; the key remains in browser memory only."""
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ON:CLICK 연동 로그</title>
<style>body{font:14px system-ui,sans-serif;margin:24px;background:#101828;color:#e5e7eb}input,select,button{padding:8px;margin:4px;background:#1f2937;color:#fff;border:1px solid #475569;border-radius:6px}button{cursor:pointer;background:#2563eb}#status{margin:10px 4px;color:#93c5fd}table{width:100%;border-collapse:collapse;background:#111827}th,td{padding:8px;border-bottom:1px solid #334155;text-align:left;vertical-align:top;word-break:break-word}th{position:sticky;top:0;background:#1e293b}tr.ERROR,tr.CRITICAL{background:#451a1a}tr.WARNING{background:#422006}.mono{font-family:ui-monospace,monospace;font-size:12px}</style>
</head><body><h1>ON:CLICK 연동 로그</h1>
<p>백엔드 JWT는 이 브라우저의 요청 헤더로만 전송되며 저장하지 않습니다.</p>
<input id="key" type="password" placeholder="Backend JWT" autocomplete="off">
<input id="requestId" placeholder="Request ID 필터">
<select id="service"><option value="all">전체 서비스</option><option value="api-gateway">Gateway</option><option value="ai-service">AI</option><option value="mcp-service">MCP</option><option value="stats-service">Stats</option></select>
<select id="level"><option value="">모든 레벨</option><option>ERROR</option><option>WARNING</option><option>INFO</option></select>
<button onclick="loadLogs()">조회</button><label><input id="auto" type="checkbox"> 5초 자동 새로고침</label><div id="status"></div>
<table><thead><tr><th>시간</th><th>서비스</th><th>레벨</th><th>이벤트</th><th>Request ID</th><th>상세</th></tr></thead><tbody id="rows"></tbody></table>
<script>
let timer; const $=id=>document.getElementById(id);
function esc(value){const d=document.createElement('div');d.textContent=String(value??'');return d.innerHTML}
async function loadLogs(){const key=$('key').value;if(!key){$('status').textContent='백엔드 JWT를 입력하세요.';return}const q=new URLSearchParams({limit:'200',service:$('service').value});if($('requestId').value)q.set('requestId',$('requestId').value);if($('level').value)q.set('level',$('level').value);$('status').textContent='조회 중…';try{const r=await fetch('/internal/observability/logs?'+q,{headers:{'Authorization':'Bearer '+key}});const body=await r.json();if(!r.ok)throw new Error(body.errorCode||r.status);const rows=body.logs||[];$('rows').innerHTML=rows.map(x=>{const detail={...x};delete detail.timestamp;delete detail.service;delete detail.level;delete detail.event;delete detail.requestId;return `<tr class="${esc(x.level)}"><td>${esc(x.timestamp)}</td><td>${esc(x.service)}</td><td>${esc(x.level)}</td><td>${esc(x.event)}</td><td class="mono">${esc(x.requestId)}</td><td class="mono">${esc(JSON.stringify(detail))}</td></tr>`}).join('');$('status').textContent=`${rows.length}개 로그`; }catch(e){$('status').textContent='조회 실패: '+e.message}}
$('auto').addEventListener('change',e=>{clearInterval(timer);if(e.target.checked)timer=setInterval(loadLogs,5000)});
</script></body></html>"""


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

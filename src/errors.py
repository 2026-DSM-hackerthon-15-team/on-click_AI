"""Consistent, traceable JSON errors for the FastAPI services."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.observability import current_request_id, log_event


logger = logging.getLogger("on_click.errors")


def api_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {"errorCode": error_code, "message": message}
    if details:
        detail["details"] = details
    if retryable is not None:
        detail["retryable"] = retryable
    return HTTPException(status_code=status_code, detail=detail)


def _body_from_detail(detail: Any, fallback_code: str) -> dict[str, Any]:
    if isinstance(detail, dict) and "errorCode" in detail:
        return dict(detail)
    return {"errorCode": fallback_code, "message": str(detail)}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", current_request_id())


def _validation_error_code(path: str) -> str:
    if path == "/ai/chat":
        return "INVALID_AI_CHAT_REQUEST"
    if path == "/ai/consultings/daily":
        return "INVALID_DAILY_CONSULTING_REQUEST"
    if path == "/ai/forecasts/closing-sales":
        return "INVALID_CLOSING_SALES_FORECAST_REQUEST"
    if path == "/ai/forecasts/tomorrow-visitors":
        return "INVALID_TOMORROW_VISITORS_FORECAST_REQUEST"
    if path == "/ai/marketings/copy":
        return "INVALID_MARKETING_COPY_REQUEST"
    if path.endswith("/publish/instagram"):
        return "INVALID_INSTAGRAM_POST"
    return "INVALID_REQUEST"


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """Do not echo rejected values because they may contain passwords or tokens."""
    errors: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", []))
        errors.append(
            {
                "field": location,
                "reason": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "validation_error")),
            }
        )
    return errors


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        body = _body_from_detail(exc.detail, "REQUEST_FAILED")
        body.setdefault("requestId", _request_id(request))
        body.setdefault("retryable", exc.status_code in {429, 502, 503, 504})
        log_event(
            logger,
            logging.ERROR if exc.status_code >= 500 else logging.WARNING,
            "api.error",
            method=request.method,
            path=request.url.path,
            statusCode=exc.status_code,
            errorCode=body["errorCode"],
            retryable=body["retryable"],
        )
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        error_code = _validation_error_code(request.url.path)
        errors = _safe_validation_errors(exc)
        log_event(
            logger,
            logging.WARNING,
            "api.validation_failed",
            method=request.method,
            path=request.url.path,
            statusCode=400,
            errorCode=error_code,
            invalidFields=[item["field"] for item in errors],
        )
        return JSONResponse(
            status_code=400,
            content={
                "errorCode": error_code,
                "message": "요청 형식이 올바르지 않습니다.",
                "requestId": _request_id(request),
                "retryable": False,
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log_event(
            logger,
            logging.ERROR,
            "api.unhandled_exception",
            method=request.method,
            path=request.url.path,
            statusCode=500,
            errorCode="INTERNAL_SERVER_ERROR",
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "errorCode": "INTERNAL_SERVER_ERROR",
                "message": "서버 내부 오류가 발생했습니다. requestId로 서버 로그를 확인해 주세요.",
                "requestId": _request_id(request),
                "retryable": False,
            },
        )

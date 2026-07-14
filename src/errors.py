"""Consistent JSON errors for the FastAPI services."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def api_error(status_code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"errorCode": error_code, "message": message},
    )


def _body_from_detail(detail: Any, fallback_code: str) -> dict[str, Any]:
    if isinstance(detail, dict) and "errorCode" in detail:
        return detail
    return {"errorCode": fallback_code, "message": str(detail)}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_body_from_detail(exc.detail, "REQUEST_FAILED"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path == "/ai/chat":
            error_code = "INVALID_AI_CHAT_REQUEST"
        elif request.url.path == "/ai/consultings/daily":
            error_code = "INVALID_DAILY_CONSULTING_REQUEST"
        elif request.url.path == "/ai/forecasts/closing-sales":
            error_code = "INVALID_CLOSING_SALES_FORECAST_REQUEST"
        elif request.url.path == "/ai/forecasts/tomorrow-visitors":
            error_code = "INVALID_TOMORROW_VISITORS_FORECAST_REQUEST"
        elif request.url.path == "/ai/marketings/copy":
            error_code = "INVALID_MARKETING_COPY_REQUEST"
        elif request.url.path.endswith("/publish/instagram"):
            error_code = "INVALID_INSTAGRAM_POST"
        else:
            error_code = "INVALID_REQUEST"
        return JSONResponse(
            status_code=400,
            content={
                "errorCode": error_code,
                "message": "요청 형식이 올바르지 않습니다.",
                "errors": jsonable_encoder(exc.errors()),
            },
        )

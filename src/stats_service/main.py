from __future__ import annotations

import hmac
import logging
import time
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Header
from pydantic import BaseModel, Field

from src.errors import api_error, install_error_handlers
from src.observability import (
    install_browser_log_api,
    install_observability,
    log_event,
    request_headers,
    safe_upstream_target,
)
from src.settings import settings
from src.stats_service.forecasting import predict_closing_sales, predict_tomorrow_visitors


app = FastAPI(title="stats-service")
install_observability(app, "stats-service")
install_error_handlers(app)
KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("on_click.stats.upstream")


class ForecastRequest(BaseModel):
    storeId: int
    start: str | None = None
    end: str | None = None
    salesData: list[dict[str, Any]] = Field(default_factory=list)


def _require_internal_key(value: str | None) -> None:
    if not value or not hmac.compare_digest(value, settings.internal_api_key):
        raise api_error(401, "INVALID_INTERNAL_API_KEY", "내부 API Key가 올바르지 않습니다.")


install_browser_log_api(app, "stats-service", _require_internal_key)


def _load_transactions(store_id: int) -> list[dict[str, Any]]:
    url = f"{settings.api_base_url}/stores/{store_id}/sales/transactions"
    target = safe_upstream_target(url)
    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "pos.request.started",
        upstreamService="BACKEND_API",
        upstreamTarget=target,
        storeId=store_id,
    )
    try:
        response = requests.get(
            url,
            params={"page": 0, "size": 100, "sortBy": "soldAt", "sortDirection": "desc"},
            headers=request_headers({"X-Internal-Api-Key": settings.internal_api_key}),
            timeout=settings.request_timeout_seconds,
        )
    except requests.RequestException as exc:
        if isinstance(exc, requests.ConnectTimeout):
            code, stage = "BACKEND_POS_CONNECT_TIMEOUT", "connect"
        elif isinstance(exc, (requests.ReadTimeout, requests.Timeout)):
            code, stage = "BACKEND_POS_READ_TIMEOUT", "read"
        elif isinstance(exc, requests.ConnectionError):
            code, stage = "BACKEND_POS_CONNECTION_FAILED", "connect"
        else:
            code, stage = "BACKEND_POS_REQUEST_FAILED", "request"
        log_event(
            logger,
            logging.ERROR,
            "pos.request.failed",
            upstreamService="BACKEND_API",
            upstreamTarget=target,
            storeId=store_id,
            errorCode=code,
            failureStage=stage,
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        raise api_error(
            502 if "TIMEOUT" not in code else 504,
            code,
            "백엔드 POS API와 통신하지 못했습니다.",
            details={
                "upstreamService": "BACKEND_API",
                "target": target,
                "stage": stage,
                "storeId": store_id,
            },
            retryable=True,
        )
    else:
        if not response.ok:
            log_event(
                logger,
                logging.ERROR,
                "pos.response.error",
                upstreamService="BACKEND_API",
                upstreamTarget=target,
                upstreamStatus=response.status_code,
                storeId=store_id,
            )
            raise api_error(
                502,
                "BACKEND_POS_HTTP_ERROR",
                "백엔드 POS API가 오류 응답을 반환했습니다.",
                details={
                    "upstreamService": "BACKEND_API",
                    "target": target,
                    "upstreamStatus": response.status_code,
                    "storeId": store_id,
                },
                retryable=response.status_code >= 500,
            )
        try:
            body = response.json()
            transactions = list(body.get("content", []))
        except (ValueError, TypeError, AttributeError):
            raise api_error(
                502,
                "BACKEND_POS_INVALID_RESPONSE",
                "백엔드 POS API 응답 형식이 올바르지 않습니다.",
                details={"upstreamService": "BACKEND_API", "target": target, "storeId": store_id},
                retryable=False,
            )
        log_event(
            logger,
            logging.INFO,
            "pos.request.completed",
            upstreamService="BACKEND_API",
            upstreamTarget=target,
            upstreamStatus=response.status_code,
            storeId=store_id,
            transactionCount=len(transactions),
            latencyMs=round((time.perf_counter() - started) * 1000, 2),
        )
        return transactions


@app.post("/forecast")
def forecast(
    req: ForecastRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_key(x_internal_api_key)
    transactions = req.salesData or _load_transactions(req.storeId)
    now = datetime.now(KST).replace(tzinfo=None)
    closing = predict_closing_sales(transactions, now)
    visitors = predict_tomorrow_visitors(transactions, now.date())
    return {
        "storeId": req.storeId,
        "businessDate": now.date().isoformat(),
        "predictedSales": closing["forecastClosingSalesAmount"],
        "predictedVisitors": visitors["expectedVisitors"],
        "trend": "up" if closing["forecastClosingSalesAmount"] > closing["observedSalesAmount"] else "flat",
        "model": closing["model"],
    }


@app.get("/stores/{storeId}/dashboard/closing-sales-forecast")
def closing_sales_forecast(
    storeId: int,
    x_internal_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_key(x_internal_api_key)
    transactions = _load_transactions(storeId)
    now = datetime.now(KST).replace(tzinfo=None)
    result = predict_closing_sales(transactions, now)
    return {
        "storeId": storeId,
        "businessDate": now.date().isoformat(),
        "currency": "KRW",
        "observedSalesAmount": result["observedSalesAmount"],
        "forecastClosingSalesAmount": result["forecastClosingSalesAmount"],
        "generatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


@app.get("/stores/{storeId}/dashboard/tomorrow-visitors-forecast")
def tomorrow_visitors_forecast(
    storeId: int,
    x_internal_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_key(x_internal_api_key)
    transactions = _load_transactions(storeId)
    today = datetime.now(KST).date()
    result = predict_tomorrow_visitors(transactions, today)
    return {
        "storeId": storeId,
        "targetDate": result["targetDate"],
        "expectedVisitors": result["expectedVisitors"],
        "generatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

from __future__ import annotations

import hmac
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Header
from pydantic import BaseModel, Field

from src.errors import api_error, install_error_handlers
from src.settings import settings
from src.stats_service.forecasting import predict_closing_sales, predict_tomorrow_visitors


app = FastAPI(title="stats-service")
install_error_handlers(app)
KST = ZoneInfo("Asia/Seoul")


class ForecastRequest(BaseModel):
    storeId: int
    start: str | None = None
    end: str | None = None
    salesData: list[dict[str, Any]] = Field(default_factory=list)


def _require_internal_key(value: str | None) -> None:
    if not value or not hmac.compare_digest(value, settings.internal_api_key):
        raise api_error(401, "INVALID_INTERNAL_API_KEY", "내부 API Key가 올바르지 않습니다.")


def _load_transactions(store_id: int) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            f"{settings.api_base_url}/stores/{store_id}/sales/transactions",
            params={"page": 0, "size": 100, "sortBy": "soldAt", "sortDirection": "desc"},
            headers={"X-Internal-Api-Key": settings.internal_api_key},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        return list(body.get("content", []))
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise api_error(502, "POS_DATA_UNAVAILABLE", f"POS 판매 데이터를 불러오지 못했습니다: {exc}")


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

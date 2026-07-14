"""HTTP-backed tools exposed to the ON:CLICK AI agent."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging
import time
from typing import Any, Callable

import requests
from pydantic import BaseModel, ConfigDict

from src.observability import (
    log_event,
    request_headers,
    safe_upstream_target,
    upstream_service_name,
)
from src.settings import settings


ToolResult = dict[str, Any]
ToolFunction = Callable[..., ToolResult]
logger = logging.getLogger("on_click.tools")


class BoundToolInput(BaseModel):
    """A bound store tool takes no model-supplied arguments."""

    model_config = ConfigDict(extra="forbid")


TOOL_DESCRIPTIONS = {
    "weather_search": "매장 위치의 검증된 날씨 데이터를 조회합니다.",
    "event_search": "매장 주변의 공공 행사 데이터를 조회합니다.",
    "closing_sales_forecast": "오늘 누적 POS 매출로 마감 예상 매출을 조회합니다.",
    "tomorrow_visitors_forecast": "POS 거래 이력으로 내일 방문자 수를 예측합니다.",
    "products": "선택한 매장의 상품 목록과 현재 가격을 조회합니다.",
    "pos_lookup": "선택한 매장의 POS 판매 거래 원장을 조회합니다.",
    "sales_analysis": "POS 거래에서 최근 매출, 주문 수, 시간대 변화를 계산합니다.",
}


def _ok(data: Any, **metadata: Any) -> ToolResult:
    return {"ok": True, "data": data, **metadata}


def _failed(
    message: str,
    error_code: str = "TOOL_REQUEST_FAILED",
    **metadata: Any,
) -> ToolResult:
    return {"ok": False, "error": message, "errorCode": error_code, **metadata}


def _json_get(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> ToolResult:
    service = upstream_service_name(url)
    target = safe_upstream_target(url)
    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "tool.upstream.started",
        upstreamService=service,
        upstreamTarget=target,
        method="GET",
    )
    try:
        response = requests.get(
            url,
            headers=request_headers(headers),
            params=params,
            timeout=settings.request_timeout_seconds,
        )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = None
            error_code = (
                body.get("errorCode")
                if isinstance(body, dict) and body.get("errorCode")
                else f"{service}_HTTP_ERROR"
            )
            log_event(
                logger,
                logging.WARNING if response.status_code < 500 else logging.ERROR,
                "tool.upstream.error",
                upstreamService=service,
                upstreamTarget=target,
                upstreamStatus=response.status_code,
                errorCode=error_code,
                latencyMs=round((time.perf_counter() - started) * 1000, 2),
            )
            return _failed(
                "연동 서비스가 오류 응답을 반환했습니다.",
                error_code,
                retryable=response.status_code >= 500,
                details={
                    "upstreamService": service,
                    "target": target,
                    "upstreamStatus": response.status_code,
                    "upstreamRequestId": body.get("requestId") if isinstance(body, dict) else None,
                },
            )
        try:
            body = response.json()
        except ValueError:
            return _failed(
                "연동 서비스 응답이 JSON 형식이 아닙니다.",
                f"{service}_INVALID_JSON_RESPONSE",
                retryable=False,
                details={"upstreamService": service, "target": target},
            )
        log_event(
            logger,
            logging.INFO,
            "tool.upstream.completed",
            upstreamService=service,
            upstreamTarget=target,
            upstreamStatus=response.status_code,
            latencyMs=round((time.perf_counter() - started) * 1000, 2),
        )
        return _ok(body)
    except requests.RequestException as exc:
        if isinstance(exc, requests.ConnectTimeout):
            code, stage = f"{service}_CONNECT_TIMEOUT", "connect"
        elif isinstance(exc, (requests.ReadTimeout, requests.Timeout)):
            code, stage = f"{service}_TIMEOUT", "read"
        elif isinstance(exc, requests.ConnectionError):
            code, stage = f"{service}_CONNECTION_FAILED", "connect"
        else:
            code, stage = f"{service}_REQUEST_FAILED", "request"
        log_event(
            logger,
            logging.ERROR,
            "tool.upstream.failed",
            upstreamService=service,
            upstreamTarget=target,
            errorCode=code,
            failureStage=stage,
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        return _failed(
            "연동 서비스와 통신하지 못했습니다.",
            code,
            retryable=True,
            details={"upstreamService": service, "target": target, "stage": stage},
        )


def _auth_headers(user_id: int) -> dict[str, str]:
    """Forward the JWT received for this AI request to backend tools."""
    from src.auth import backend_authorization_header

    return backend_authorization_header()


def _store_context_from_list(store_id: int, user_id: int) -> ToolResult:
    stores_result = _json_get(
        f"{settings.api_base_url}/stores",
        headers=_auth_headers(user_id),
    )
    if not stores_result.get("ok"):
        return stores_result
    stores = stores_result.get("data")
    if not isinstance(stores, list):
        return _failed("매장 목록 응답 형식이 올바르지 않습니다.", "STORE_LIST_INVALID")
    store = next(
        (
            item
            for item in stores
            if isinstance(item, dict) and str(item.get("id")) == str(store_id)
        ),
        None,
    )
    if store is None:
        return _failed("JWT 사용자가 소유한 매장 목록에서 대상 매장을 찾을 수 없습니다.", "STORE_NOT_FOUND")
    region = store.get("region")
    industry = store.get("industry")
    if not region or not industry:
        return _failed("매장에 지역 또는 업종 정보가 없습니다.", "STORE_CONTEXT_MISSING")
    return _ok(
        {
            "storeId": store_id,
            "name": str(store.get("name") or "매장"),
            "region": str(region),
            "industry": str(industry),
        }
    )


def weather_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    del store_context
    context_result = _store_context_from_list(store_id, user_id)
    if not context_result.get("ok"):
        return context_result
    context = context_result["data"]
    params = {"region": context["region"], "industry": context["industry"]}
    return _json_get(f"{settings.mcp_service_url}/weather", params=params)


def events_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    del store_context
    context_result = _store_context_from_list(store_id, user_id)
    if not context_result.get("ok"):
        return context_result
    context = context_result["data"]
    return _json_get(
        f"{settings.mcp_service_url}/events",
        params={"days": 7, "region": context["region"], "industry": context["industry"]},
    )


def closing_sales_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    del store_context
    return _json_get(
        f"{settings.api_base_url}/stores/{store_id}/dashboard/closing-sales-forecast",
        headers=_auth_headers(user_id),
    )


def tomorrow_visitors_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    del store_context
    return _json_get(
        f"{settings.api_base_url}/stores/{store_id}/dashboard/tomorrow-visitors-forecast",
        headers=_auth_headers(user_id),
    )


def products_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    del store_context
    return _json_get(
        f"{settings.api_base_url}/stores/{store_id}/products",
        headers=_auth_headers(user_id),
    )


def pos_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    del store_context
    return _json_get(
        f"{settings.api_base_url}/stores/{store_id}/sales/transactions",
        headers=_auth_headers(user_id),
        params={"page": 0, "size": 100, "sortBy": "soldAt", "sortDirection": "desc"},
    )


def sales_analysis_tool(
    store_id: int,
    user_id: int = 1,
    store_context: dict[str, Any] | None = None,
) -> ToolResult:
    raw = pos_tool(store_id, user_id, store_context)
    if not raw.get("ok"):
        return raw
    body = raw.get("data") or {}
    transactions = [
        tx for tx in body.get("content", []) if str(tx.get("status", "")).upper() == "COMPLETED"
    ]
    if not transactions:
        return _ok(
            {
                "storeId": store_id,
                "totalSales": 0,
                "orderCount": 0,
                "message": "분석할 완료 거래가 없습니다.",
            }
        )

    def sold_at(tx: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(str(tx["soldAt"]).replace("Z", "+00:00")).replace(tzinfo=None)

    latest_day = max(sold_at(tx).date() for tx in transactions)
    current_start = latest_day - timedelta(days=6)
    previous_start = current_start - timedelta(days=7)
    current = [tx for tx in transactions if current_start <= sold_at(tx).date() <= latest_day]
    previous = [tx for tx in transactions if previous_start <= sold_at(tx).date() < current_start]
    current_sales = sum(int(tx.get("totalPaidAmount", 0) or 0) for tx in current)
    previous_sales = sum(int(tx.get("totalPaidAmount", 0) or 0) for tx in previous)
    change_rate = (
        round((current_sales - previous_sales) / previous_sales * 100, 1)
        if previous_sales
        else None
    )
    hourly: dict[int, int] = defaultdict(int)
    for tx in current:
        hourly[sold_at(tx).hour] += int(tx.get("totalPaidAmount", 0) or 0)
    peak_hour = max(hourly, key=hourly.get) if hourly else None
    return _ok(
        {
            "storeId": store_id,
            "periodStart": current_start.isoformat(),
            "periodEnd": latest_day.isoformat(),
            "totalSales": current_sales,
            "previousSales": previous_sales if previous else None,
            "salesChangeRate": change_rate,
            "orderCount": len(current),
            "previousOrderCount": len(previous) if previous else None,
            "peakHour": peak_hour,
            "peakHourSales": hourly.get(peak_hour, 0) if peak_hour is not None else None,
        }
    )


def get_tool_map(
    *,
    user_id: int | None = None,
    store_id: int | None = None,
    store_context: dict[str, Any] | None = None,
) -> dict[str, ToolFunction]:
    raw: dict[str, ToolFunction] = {
        "weather_search": weather_tool,
        "event_search": events_tool,
        "closing_sales_forecast": closing_sales_tool,
        "tomorrow_visitors_forecast": tomorrow_visitors_tool,
        "products": products_tool,
        "pos_lookup": pos_tool,
        "sales_analysis": sales_analysis_tool,
    }
    if store_id is None:
        return raw
    resolved_user_id = user_id or 1
    return {
        name: (
            lambda fn=fn: fn(
                store_id,
                resolved_user_id,
                store_context,
            )
        )
        for name, fn in raw.items()
    }


def get_langchain_tools(
    *,
    user_id: int = 1,
    store_id: int | None = None,
    store_context: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
) -> list[Any]:
    """Build LangChain tools; when context is bound the LLM only selects the tool name."""
    try:
        from langchain_core.tools import StructuredTool
    except ImportError:
        return []

    tool_map = get_tool_map(
        user_id=user_id,
        store_id=store_id,
        store_context=store_context,
    )
    allowed = set(allowed_tools) if allowed_tools is not None else set(tool_map)
    tools = []
    for name, function in tool_map.items():
        if name not in allowed:
            continue
        tools.append(
            StructuredTool.from_function(
                func=function,
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                args_schema=BoundToolInput,
            )
        )
    return tools

from __future__ import annotations

import hmac
import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.consulting import analyze_consulting
from src.errors import api_error, install_error_handlers
from src.feature_contracts import (
    ClosingSalesForecastRequest,
    ClosingSalesForecastResponse,
    GenerateMarketingCopyRequest,
    GenerateMarketingCopyResponse,
    GenerateDailyConsultingRequest,
    GenerateDailyConsultingResponse,
    PublishInstagramRequest,
    PublishInstagramResponse,
    TomorrowVisitorsForecastRequest,
    TomorrowVisitorsForecastResponse,
)
from src.observability import (
    browser_logs,
    install_observability,
    log_event,
    request_headers,
    render_log_viewer_html,
    safe_upstream_target,
    upstream_service_name,
)
from src.settings import settings
from src.stats_service.forecasting import predict_closing_sales, predict_tomorrow_visitors


app = FastAPI(title="on-click-api")
install_observability(app, "api-gateway")
install_error_handlers(app)
KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("on_click.upstream")


class AiChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    userId: int = Field(gt=0)
    storeId: int = Field(gt=0)
    chatRoomId: int = Field(gt=0)
    message: str = Field(min_length=1)
    availableTools: list[str]
    attachmentKeys: list[str] = Field(default_factory=list)


class AiToolExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    toolName: str
    status: Literal["SUCCESS", "FAILED", "SKIPPED"]
    arguments: dict[str, Any] | None = None
    resultSummary: str | None = None
    latencyMs: int | None = None


class AiCitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    organization: str | None = None


class AiChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    usedTools: list[AiToolExecutionResponse]
    citations: list[AiCitationResponse] = Field(default_factory=list)
    model: str = Field(min_length=1)
    finishReason: Literal["STOP", "TOOL_ERROR", "MAX_TOKENS", "SAFETY"]


class SaveConsultingRequest(BaseModel):
    userId: int = Field(gt=0)
    storeId: int = Field(gt=0)
    title: str
    periodType: str
    periodStart: date
    periodEnd: date
    summary: str
    estimatedCauses: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    keyMetrics: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SaveConsultingResponse(BaseModel):
    consultingId: int
    createdAt: datetime


STORES = [
    {
        "id": 10,
        "name": "온클릭 강남점",
        "category": "CAFE",
        "industry": "CAFE",
        "region": "서울특별시 강남구",
        "roadAddress": "서울특별시 강남구 테헤란로 1",
        "latitude": 37.498,
        "longitude": 127.027,
        "owner_user_id": 1,
        "closingTime": "22:00",
        "createdAt": "2026-07-01T09:30:00",
        "updatedAt": "2026-07-01T09:30:00",
    },
]

PRODUCTS = {
    10: [
        {
            "id": 1,
            "storeId": 10,
            "name": "김밥",
            "price": 3000,
            "active": True,
            "createdAt": "2026-07-01T00:00:00",
            "updatedAt": "2026-07-01T00:00:00",
        },
        {
            "id": 2,
            "storeId": 10,
            "name": "아메리카노",
            "price": 4500,
            "active": True,
            "createdAt": "2026-07-01T00:00:00",
            "updatedAt": "2026-07-01T00:00:00",
        },
    ]
}

CONSULTINGS: list[dict[str, Any]] = []
MESSAGES = {
    (10, 1): [
        {
            "id": 1,
            "chatRoomId": 1,
            "clientMessageId": "c1",
            "role": "USER",
            "status": "COMPLETED",
            "content": "오늘 매출이 왜 줄었어?",
            "retryCount": 0,
            "createdAt": "2026-07-14T00:10:00",
            "updatedAt": "2026-07-14T00:10:30",
        }
    ]
}


def _now_local() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _build_demo_sales() -> list[dict[str, Any]]:
    """Generate a repeatable rolling ledger so local forecasts use real rows, not constants."""
    today = _now_local().date()
    now_hour = _now_local().hour
    hours = (8, 10, 12, 14, 17, 20)
    rows: list[dict[str, Any]] = []
    sale_id = 1
    for days_ago in range(42, -1, -1):
        business_day = today - timedelta(days=days_ago)
        weekday_factor = 1.25 if business_day.weekday() >= 5 else 1.0
        trend_factor = 1 + (42 - days_ago) * 0.004
        for line_index, hour in enumerate(hours, start=1):
            if business_day == today and hour > now_hour:
                continue
            product = PRODUCTS[10][line_index % len(PRODUCTS[10])]
            quantity = max(1, round((1 + line_index % 3) * weekday_factor * trend_factor))
            paid_amount = int(product["price"] * quantity)
            sold_at = datetime.combine(business_day, datetime.min.time()).replace(hour=hour, minute=15)
            cancelled = days_ago > 0 and days_ago % 13 == 0 and line_index == 1
            rows.append(
                {
                    "saleId": sale_id,
                    "storeId": 10,
                    "clientTransactionId": f"DEMO-{business_day:%Y%m%d}-{line_index:02d}",
                    "soldAt": sold_at.isoformat(timespec="seconds"),
                    "totalQuantity": quantity,
                    "totalPaidAmount": paid_amount,
                    "status": "CANCELLED" if cancelled else "COMPLETED",
                    "createdAt": (sold_at + timedelta(seconds=1)).isoformat(timespec="seconds"),
                    "cancelledAt": (sold_at + timedelta(hours=1)).isoformat(timespec="seconds") if cancelled else None,
                    "items": [
                        {
                            "id": sale_id,
                            "lineNo": 1,
                            "productId": product["id"],
                            "productName": product["name"],
                            "productPrice": product["price"],
                            "quantity": quantity,
                            "paidAmount": paid_amount,
                        }
                    ],
                }
            )
            sale_id += 1
    return rows


SALES_TRANSACTIONS = _build_demo_sales()


def _valid_internal_key(value: str | None) -> bool:
    return bool(value and hmac.compare_digest(value, settings.internal_api_key))


def _get_user_id_from_auth(auth_header: str | None) -> int | None:
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    if not token.startswith("user-"):
        return None
    try:
        return int(token.split("-", 1)[1])
    except ValueError:
        return None


def _find_store(store_id: int) -> dict[str, Any] | None:
    return next((store for store in STORES if store["id"] == store_id), None)


def _require_store_access(
    store_id: int,
    authorization: str | None,
    internal_api_key: str | None = None,
) -> dict[str, Any]:
    store = _find_store(store_id)
    if _valid_internal_key(internal_api_key):
        if store is None:
            raise api_error(404, "STORE_NOT_FOUND", "매장을 찾을 수 없습니다.")
        return store
    user_id = _get_user_id_from_auth(authorization)
    if user_id is None:
        raise api_error(401, "UNAUTHORIZED", "인증이 필요합니다.")
    if store is None or store.get("owner_user_id") != user_id:
        raise api_error(403, "STORE_ACCESS_DENIED", "해당 매장에 접근할 수 없습니다.")
    return store


def _require_internal_key(value: str | None) -> None:
    if not _valid_internal_key(value):
        raise api_error(401, "INVALID_INTERNAL_API_KEY", "내부 API Key가 올바르지 않습니다.")


def _proxy_json(
    method: str,
    url: str,
    *,
    timeout_seconds: float | None = None,
    timeout_error_code: str = "UPSTREAM_SERVICE_TIMEOUT",
    timeout_message: str = "내부 서비스 응답 시간이 초과되었습니다.",
    upstream_service: str | None = None,
    **kwargs: Any,
) -> Any:
    service = upstream_service or upstream_service_name(url)
    target = safe_upstream_target(url)
    timeout = timeout_seconds or settings.request_timeout_seconds
    kwargs["headers"] = request_headers(kwargs.get("headers"))
    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "upstream.request.started",
        upstreamService=service,
        upstreamTarget=target,
        method=method,
        timeoutSeconds=timeout,
    )
    try:
        response = requests.request(
            method,
            url,
            timeout=timeout,
            **kwargs,
        )
    except requests.ConnectTimeout:
        log_event(
            logger,
            logging.ERROR,
            "upstream.request.failed",
            upstreamService=service,
            upstreamTarget=target,
            failureStage="connect",
            errorType="ConnectTimeout",
            exc_info=True,
        )
        raise api_error(
            504,
            f"{service}_CONNECT_TIMEOUT",
            f"{service} 연결 시간이 초과되었습니다.",
            details={"upstreamService": service, "target": target, "stage": "connect"},
            retryable=True,
        )
    except (requests.ReadTimeout, requests.Timeout):
        log_event(
            logger,
            logging.ERROR,
            "upstream.request.failed",
            upstreamService=service,
            upstreamTarget=target,
            failureStage="read",
            errorType="ReadTimeout",
            exc_info=True,
        )
        raise api_error(
            504,
            timeout_error_code,
            timeout_message,
            details={"upstreamService": service, "target": target, "stage": "read"},
            retryable=True,
        )
    except requests.ConnectionError:
        log_event(
            logger,
            logging.ERROR,
            "upstream.request.failed",
            upstreamService=service,
            upstreamTarget=target,
            failureStage="connect",
            errorType="ConnectionError",
            exc_info=True,
        )
        raise api_error(
            502,
            f"{service}_CONNECTION_FAILED",
            f"{service}에 연결할 수 없습니다.",
            details={"upstreamService": service, "target": target, "stage": "connect"},
            retryable=True,
        )
    except requests.RequestException as exc:
        log_event(
            logger,
            logging.ERROR,
            "upstream.request.failed",
            upstreamService=service,
            upstreamTarget=target,
            failureStage="request",
            errorType=exc.__class__.__name__,
            exc_info=True,
        )
        raise api_error(
            502,
            f"{service}_REQUEST_FAILED",
            f"{service} 요청 처리 중 통신 오류가 발생했습니다.",
            details={"upstreamService": service, "target": target, "stage": "request"},
            retryable=True,
        )
    status_code = getattr(response, "status_code", 200 if response.ok else 502)
    if not isinstance(status_code, int):
        status_code = 200 if response.ok else 502
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.ok:
        if isinstance(body, dict) and body.get("errorCode"):
            details = dict(body.get("details") or {})
            details.update(
                {
                    "upstreamService": service,
                    "target": target,
                    "upstreamStatus": status_code,
                    "upstreamRequestId": body.get("requestId"),
                }
            )
            log_event(
                logger,
                logging.WARNING if status_code < 500 else logging.ERROR,
                "upstream.response.error",
                upstreamService=service,
                upstreamTarget=target,
                upstreamStatus=status_code,
                errorCode=body["errorCode"],
                latencyMs=round((time.perf_counter() - started) * 1000, 2),
            )
            raise api_error(
                status_code,
                body["errorCode"],
                body.get("message", "내부 서비스 요청에 실패했습니다."),
                details=details,
                retryable=body.get("retryable", status_code in {429, 502, 503, 504}),
            )
        raise api_error(
            502,
            f"{service}_HTTP_ERROR",
            f"{service}가 오류 응답을 반환했습니다.",
            details={
                "upstreamService": service,
                "target": target,
                "upstreamStatus": status_code,
            },
            retryable=status_code >= 500,
        )
    if body is None:
        raise api_error(
            502,
            f"{service}_INVALID_JSON_RESPONSE",
            f"{service} 응답이 JSON 형식이 아닙니다.",
            details={
                "upstreamService": service,
                "target": target,
                "upstreamStatus": status_code,
            },
            retryable=False,
        )
    log_event(
        logger,
        logging.INFO,
        "upstream.request.completed",
        upstreamService=service,
        upstreamTarget=target,
        upstreamStatus=status_code,
        latencyMs=round((time.perf_counter() - started) * 1000, 2),
    )
    return body


@app.get("/observability", include_in_schema=False, response_class=HTMLResponse)
def observability_viewer() -> str:
    return render_log_viewer_html()


@app.get("/internal/observability/logs", include_in_schema=False)
def browser_observability_logs(
    limit: int = Query(default=200, ge=1, le=500),
    requestId: str | None = Query(default=None, max_length=128),
    level: str | None = Query(default=None, max_length=20),
    event: str | None = Query(default=None, max_length=100),
    service: str = Query(default="all", max_length=30),
    x_internal_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_key(x_internal_api_key)
    services: dict[str, tuple[str, str | None]] = {
        "api-gateway": ("api-gateway", None),
        "ai-service": ("ai-service", settings.ai_service_url),
        "mcp-service": ("mcp-service", settings.mcp_service_url),
        "stats-service": ("stats-service", settings.stats_service_url),
    }
    selected = list(services) if service == "all" else [service]
    if any(name not in services for name in selected):
        raise api_error(
            400,
            "INVALID_LOG_SERVICE_FILTER",
            "service는 all, api-gateway, ai-service, mcp-service, stats-service 중 하나여야 합니다.",
        )

    filters = {"limit": limit, "requestId": requestId, "level": level, "event": event}
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for name in selected:
        label, base_url = services[name]
        if base_url is None:
            rows.extend(
                browser_logs(
                    limit=limit,
                    request_id=requestId,
                    level=level,
                    event=event,
                )
            )
            sources.append({"service": label, "available": True})
            continue
        target = f"{base_url}/internal/observability/logs"
        try:
            response = requests.get(
                target,
                params={key: value for key, value in filters.items() if value is not None},
                headers=request_headers({"X-Internal-Api-Key": settings.internal_api_key}),
                timeout=settings.request_timeout_seconds,
            )
            body = response.json()
            if not response.ok or not isinstance(body, dict) or not isinstance(body.get("logs"), list):
                raise ValueError("invalid log source response")
            rows.extend(body["logs"])
            sources.append({"service": label, "available": True})
        except (requests.RequestException, ValueError):
            log_event(
                logger,
                logging.WARNING,
                "observability.source_unavailable",
                sourceService=label,
            )
            sources.append(
                {"service": label, "available": False, "errorCode": "LOG_SOURCE_UNAVAILABLE"}
            )
    rows.sort(key=lambda row: str(row.get("timestamp", "")), reverse=True)
    return {"logs": rows[:limit], "sources": sources}


@app.post("/ai/chat", response_model=AiChatResponse)
def ai_chat(
    payload: AiChatRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> AiChatResponse:
    _require_internal_key(x_internal_api_key)
    body = _proxy_json(
        "POST",
        f"{settings.ai_service_url}/ai/chat",
        json=payload.model_dump(mode="json"),
        headers={"X-Internal-Api-Key": settings.internal_api_key},
        timeout_seconds=settings.ai_request_timeout_seconds,
        timeout_error_code="AI_TIMEOUT",
        timeout_message="AI 처리 시간이 초과되었습니다.",
    )
    try:
        return AiChatResponse.model_validate(body)
    except ValidationError:
        raise api_error(422, "AI_RESPONSE_INVALID", "AI가 유효한 응답을 생성하지 못했습니다.")


@app.post("/ai/consultings")
def generate_consulting(
    payload: dict[str, Any],
    x_internal_api_key: str | None = Header(default=None),
) -> Any:
    _require_internal_key(x_internal_api_key)
    try:
        return _proxy_json(
            "POST",
            f"{settings.ai_service_url}/ai/consultings",
            json=payload,
            headers={"X-Internal-Api-Key": settings.internal_api_key},
        )
    except Exception as exc:
        # The single-container demo remains usable if the AI worker is restarting.
        if getattr(exc, "status_code", None) != 502:
            raise
        try:
            return analyze_consulting(payload)
        except (ValueError, KeyError, TypeError):
            raise exc


@app.post("/ai/consultings/daily", response_model=GenerateDailyConsultingResponse)
def generate_daily_consulting(
    payload: GenerateDailyConsultingRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> GenerateDailyConsultingResponse:
    _require_internal_key(x_internal_api_key)
    body = _proxy_json(
        "POST",
        f"{settings.ai_service_url}/ai/consultings/daily",
        json=payload.model_dump(mode="json"),
        headers={"X-Internal-Api-Key": settings.internal_api_key},
        timeout_seconds=settings.ai_request_timeout_seconds,
        timeout_error_code="AI_TIMEOUT",
        timeout_message="일일 보고서 생성 시간이 초과되었습니다.",
    )
    try:
        return GenerateDailyConsultingResponse.model_validate(body)
    except ValidationError:
        raise api_error(422, "DAILY_CONSULTING_GENERATION_FAILED", "일일 보고서 응답이 올바르지 않습니다.")


@app.post("/ai/forecasts/closing-sales", response_model=ClosingSalesForecastResponse)
def forecast_closing_sales_ai(
    payload: ClosingSalesForecastRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> ClosingSalesForecastResponse:
    _require_internal_key(x_internal_api_key)
    body = _proxy_json(
        "POST",
        f"{settings.ai_service_url}/ai/forecasts/closing-sales",
        json=payload.model_dump(mode="json"),
        headers={"X-Internal-Api-Key": settings.internal_api_key},
        timeout_seconds=settings.ai_request_timeout_seconds,
    )
    try:
        return ClosingSalesForecastResponse.model_validate(body)
    except ValidationError:
        raise api_error(502, "FORECAST_EXECUTION_FAILED", "마감 매출 예측 응답이 올바르지 않습니다.")


@app.post("/ai/forecasts/tomorrow-visitors", response_model=TomorrowVisitorsForecastResponse)
def forecast_tomorrow_visitors_ai(
    payload: TomorrowVisitorsForecastRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> TomorrowVisitorsForecastResponse:
    _require_internal_key(x_internal_api_key)
    body = _proxy_json(
        "POST",
        f"{settings.ai_service_url}/ai/forecasts/tomorrow-visitors",
        json=payload.model_dump(mode="json"),
        headers={"X-Internal-Api-Key": settings.internal_api_key},
        timeout_seconds=settings.ai_request_timeout_seconds,
    )
    try:
        return TomorrowVisitorsForecastResponse.model_validate(body)
    except ValidationError:
        raise api_error(502, "FORECAST_EXECUTION_FAILED", "방문자 예측 응답이 올바르지 않습니다.")


@app.post("/ai/marketings/copy", response_model=GenerateMarketingCopyResponse)
def generate_marketing_copy(
    payload: GenerateMarketingCopyRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> GenerateMarketingCopyResponse:
    _require_internal_key(x_internal_api_key)
    body = _proxy_json(
        "POST",
        f"{settings.ai_service_url}/ai/marketings/copy",
        json=payload.model_dump(mode="json"),
        headers={"X-Internal-Api-Key": settings.internal_api_key},
        timeout_seconds=settings.ai_request_timeout_seconds,
        timeout_error_code="AI_TIMEOUT",
        timeout_message="마케팅 문구 생성 시간이 초과되었습니다.",
    )
    try:
        return GenerateMarketingCopyResponse.model_validate(body)
    except ValidationError:
        raise api_error(502, "MARKETING_COPY_GENERATION_FAILED", "마케팅 문구 응답이 올바르지 않습니다.")


@app.post(
    "/ai/marketings/{marketingId}/publish/instagram",
    response_model=PublishInstagramResponse,
)
def publish_instagram(
    marketingId: int,
    payload: PublishInstagramRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> PublishInstagramResponse:
    _require_internal_key(x_internal_api_key)
    headers = {"X-Internal-Api-Key": settings.internal_api_key}
    publish_payload = payload.model_dump(mode="json")
    publish_payload["instagramPassword"] = payload.instagramPassword.get_secret_value()
    body = _proxy_json(
        "POST",
        f"{settings.ai_service_url}/ai/marketings/{marketingId}/publish/instagram",
        json=publish_payload,
        headers=headers,
        timeout_seconds=settings.instagram_publish_timeout_seconds,
        timeout_error_code="INSTAGRAM_PUBLISH_TIMEOUT",
        timeout_message="Instagram 게시 시간이 초과되었습니다.",
    )
    try:
        return PublishInstagramResponse.model_validate(body)
    except ValidationError:
        raise api_error(502, "BROWSER_MCP_UNAVAILABLE", "Instagram 게시 응답이 올바르지 않습니다.")


@app.get("/stores")
def list_stores(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    user_id = _get_user_id_from_auth(authorization)
    if user_id is None:
        raise api_error(401, "UNAUTHORIZED", "인증이 필요합니다.")
    fields = {"id", "name", "region", "industry", "timeZone", "closingTime", "createdAt", "updatedAt"}
    return [
        {key: value for key, value in store.items() if key in fields}
        for store in STORES
        if store.get("owner_user_id") == user_id
    ]


@app.get("/stores/{storeId}/products")
def list_products(
    storeId: int,
    authorization: str | None = Header(default=None),
    x_internal_api_key: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_store_access(storeId, authorization, x_internal_api_key)
    return PRODUCTS.get(storeId, [])


@app.get("/stores/{storeId}/sales/transactions")
def list_sales_transactions(
    storeId: int,
    page: int = Query(default=0, ge=0),
    size: int = Query(default=20, ge=1, le=100),
    sortBy: str = "soldAt",
    sortDirection: str = "desc",
    authorization: str | None = Header(default=None),
    x_internal_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_store_access(storeId, authorization, x_internal_api_key)
    allowed_sort_fields = {"soldAt", "createdAt", "saleId", "status"}
    if sortBy not in allowed_sort_fields:
        raise api_error(400, "INVALID_REQUEST", "지원하지 않는 정렬 필드입니다.")
    sortDirection = sortDirection.lower()
    if sortDirection not in {"asc", "desc"}:
        raise api_error(400, "INVALID_REQUEST", "sortDirection은 asc 또는 desc여야 합니다.")
    rows = [tx for tx in SALES_TRANSACTIONS if tx["storeId"] == storeId]
    reverse = sortDirection == "desc"
    if sortBy == "saleId":
        rows.sort(key=lambda tx: tx["saleId"], reverse=reverse)
    else:
        rows.sort(key=lambda tx: (tx[sortBy], tx["saleId"]), reverse=reverse)
    total = len(rows)
    start = page * size
    content = rows[start : start + size]
    total_pages = math.ceil(total / size) if total else 0
    return {
        "content": content,
        "page": page,
        "size": size,
        "totalElements": total,
        "totalPages": total_pages,
        "hasNext": page + 1 < total_pages,
        "sortBy": sortBy,
        "sortDirection": sortDirection,
    }


def _today_completed(store_id: int) -> list[dict[str, Any]]:
    today = _now_local().date()
    return [
        tx
        for tx in SALES_TRANSACTIONS
        if tx["storeId"] == store_id
        and tx["status"] == "COMPLETED"
        and datetime.fromisoformat(tx["soldAt"]).date() == today
    ]


@app.get("/stores/{storeId}/dashboard/hourly-visitors")
def hourly_visitors(
    storeId: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_store_access(storeId, authorization)
    counts = {hour: 0 for hour in range(24)}
    for tx in _today_completed(storeId):
        counts[datetime.fromisoformat(tx["soldAt"]).hour] += 1
    return {
        "storeId": storeId,
        "businessDate": _now_local().date().isoformat(),
        "timeZone": "Asia/Seoul",
        "totalVisitors": sum(counts.values()),
        "hourly": [{"hour": hour, "visitorCount": counts[hour]} for hour in range(24)],
    }


@app.get("/stores/{storeId}/dashboard/hourly-sales")
def hourly_sales(
    storeId: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_store_access(storeId, authorization)
    amounts = {hour: 0 for hour in range(24)}
    for tx in _today_completed(storeId):
        amounts[datetime.fromisoformat(tx["soldAt"]).hour] += tx["totalPaidAmount"]
    return {
        "storeId": storeId,
        "businessDate": _now_local().date().isoformat(),
        "currency": "KRW",
        "totalSalesAmount": sum(amounts.values()),
        "hourly": [{"hour": hour, "salesAmount": amounts[hour]} for hour in range(24)],
    }


@app.get("/stores/{storeId}/dashboard/closing-sales-forecast")
def closing_sales_forecast(
    storeId: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_store_access(storeId, authorization)
    try:
        return _proxy_json(
            "GET",
            f"{settings.stats_service_url}/stores/{storeId}/dashboard/closing-sales-forecast",
            headers={"X-Internal-Api-Key": settings.internal_api_key},
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) != 502:
            raise
        result = predict_closing_sales(SALES_TRANSACTIONS, _now_local())
        return {
            "storeId": storeId,
            "businessDate": _now_local().date().isoformat(),
            "currency": "KRW",
            "observedSalesAmount": result["observedSalesAmount"],
            "forecastClosingSalesAmount": result["forecastClosingSalesAmount"],
            "generatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }


@app.get("/stores/{storeId}/dashboard/tomorrow-visitors-forecast")
def tomorrow_visitors_forecast(
    storeId: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_store_access(storeId, authorization)
    try:
        return _proxy_json(
            "GET",
            f"{settings.stats_service_url}/stores/{storeId}/dashboard/tomorrow-visitors-forecast",
            headers={"X-Internal-Api-Key": settings.internal_api_key},
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) != 502:
            raise
        result = predict_tomorrow_visitors(SALES_TRANSACTIONS, _now_local().date())
        return {
            "storeId": storeId,
            "targetDate": result["targetDate"],
            "expectedVisitors": result["expectedVisitors"],
            "generatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }


@app.post("/consultings", response_model=SaveConsultingResponse, status_code=201)
def save_consulting(
    payload: SaveConsultingRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_key(x_internal_api_key)
    store = _find_store(payload.storeId)
    if store is None or store.get("owner_user_id") != payload.userId:
        raise api_error(403, "STORE_ACCESS_DENIED", "해당 매장에 접근할 수 없습니다.")
    consulting_id = max((item["consultingId"] for item in CONSULTINGS), default=0) + 1
    now = _now_local()
    item = {
        "consultingId": consulting_id,
        **payload.model_dump(mode="json"),
        "targetDate": payload.periodEnd.isoformat(),
        "content": payload.summary,
        "status": "COMPLETED",
        "attemptCount": 1,
        "generatedAt": now.isoformat(timespec="seconds"),
        "createdAt": now.isoformat(timespec="seconds"),
        "updatedAt": now.isoformat(timespec="seconds"),
    }
    CONSULTINGS.append(item)
    return {"consultingId": consulting_id, "createdAt": now}


@app.get("/stores/{storeId}/consultings")
def list_consultings(
    storeId: int,
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_store_access(storeId, authorization)
    fields = {
        "consultingId",
        "storeId",
        "targetDate",
        "title",
        "status",
        "attemptCount",
        "generatedAt",
        "createdAt",
        "updatedAt",
    }
    items = [{key: value for key, value in item.items() if key in fields} for item in CONSULTINGS if item["storeId"] == storeId]
    items.sort(key=lambda item: item.get("targetDate", ""), reverse=True)
    return items


@app.get("/stores/{storeId}/consultings/{consultingId}")
def get_consulting_detail(
    storeId: int,
    consultingId: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_store_access(storeId, authorization)
    item = next(
        (
            consulting
            for consulting in CONSULTINGS
            if consulting["consultingId"] == consultingId and consulting["storeId"] == storeId
        ),
        None,
    )
    if item is None:
        raise api_error(404, "CONSULTING_NOT_FOUND", "컨설팅을 찾을 수 없습니다.")
    return item


@app.get("/stores/{storeId}/chat-rooms")
def list_chat_rooms(
    storeId: int,
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_store_access(storeId, authorization)
    rooms: list[dict[str, Any]] = []
    for (message_store_id, chat_room_id), messages in MESSAGES.items():
        if message_store_id != storeId:
            continue
        created_at = min((message["createdAt"] for message in messages), default=None)
        updated_at = max((message["updatedAt"] for message in messages), default=None)
        rooms.append(
            {
                "id": chat_room_id,
                "storeId": storeId,
                "title": "매출 분석 상담",
                "createdAt": created_at,
                "updatedAt": updated_at,
            }
        )
    rooms.sort(key=lambda room: str(room["updatedAt"] or ""), reverse=True)
    return rooms


@app.get("/stores/{storeId}/chat-rooms/{chatRoomId}/messages")
def get_messages(
    storeId: int,
    chatRoomId: int,
    afterId: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_store_access(storeId, authorization)
    messages = [message for message in MESSAGES.get((storeId, chatRoomId), []) if message["id"] > afterId]
    messages.sort(key=lambda message: message["id"])
    return messages

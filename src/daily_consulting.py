"""Daily report generation from backend context, MCP data, and statistical tools."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any, Callable

from src.langchain_tools import _auth_headers, _json_get, _store_context_from_list, get_tool_map
from src.settings import settings


def _summary(value: Any, limit: int = 500) -> str:
    rendered = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _execution(name: str, fn: Callable[[], dict[str, Any]], arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "errorCode": "TOOL_REQUEST_FAILED"}
    elapsed = int((time.perf_counter() - started) * 1000)
    execution = {
        "toolName": name,
        "status": "SUCCESS" if result.get("ok") else "FAILED",
        "arguments": arguments,
        "resultSummary": _summary(result),
        "latencyMs": elapsed,
    }
    return result, execution


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _today_chat_history(store_id: int, user_id: int, target_date: date) -> dict[str, Any]:
    headers = _auth_headers(user_id)
    rooms_result = _json_get(f"{settings.api_base_url}/stores/{store_id}/chat-rooms", headers=headers)
    if not rooms_result.get("ok"):
        return rooms_result
    rooms_body = rooms_result.get("data") or []
    rooms = rooms_body.get("content", []) if isinstance(rooms_body, dict) else rooms_body
    if not isinstance(rooms, list):
        return {"ok": False, "error": "채팅방 목록 형식 오류", "errorCode": "CHAT_HISTORY_INVALID"}
    messages: list[dict[str, Any]] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        room_id = room.get("chatRoomId") or room.get("id")
        if not room_id:
            continue
        message_result = _json_get(
            f"{settings.api_base_url}/stores/{store_id}/chat-rooms/{room_id}/messages",
            headers=headers,
            params={"afterId": 0},
        )
        if not message_result.get("ok"):
            continue
        body = message_result.get("data") or []
        rows = body.get("content", []) if isinstance(body, dict) else body
        for message in rows if isinstance(rows, list) else []:
            if not isinstance(message, dict):
                continue
            if _parse_date(message.get("createdAt")) == target_date:
                messages.append(
                    {
                        "chatRoomId": room_id,
                        "role": message.get("role"),
                        "content": str(message.get("content") or "").strip(),
                        "createdAt": message.get("createdAt"),
                    }
                )
    messages.sort(key=lambda item: str(item.get("createdAt") or ""))
    return {"ok": True, "data": messages}


def _metric(name: str, value: float, unit: str, **extra: Any) -> dict[str, Any]:
    return {"metricName": name, "currentValue": value, "unit": unit, **extra}


def _render_list(values: list[str], empty: str) -> str:
    return "\n".join(f"- {value}" for value in values) if values else f"- {empty}"


def render_daily_content(report: dict[str, Any]) -> str:
    metric_lines = [
        f"{item['metricName']}: {item['currentValue']:,.0f} {item['unit']}"
        for item in report.get("keyMetrics", [])
    ]
    cause_lines = [
        f"{item['title']}: {item['description']}"
        for item in report.get("estimatedCauses", [])
    ]
    recommendation_lines = [
        f"[{item['priority']}] {item['title']}: {item['description']}"
        for item in report.get("recommendations", [])
    ]
    return "\n\n".join(
        [
            f"## 오늘의 요약\n{report['summary']}",
            "## 고객 대화 인사이트\n"
            + _render_list(report.get("chatInsights", []), "수집된 사용자 질문이 없습니다."),
            "## 핵심 지표\n" + _render_list(metric_lines, "사용 가능한 지표가 없습니다."),
            "## 외부 환경\n"
            + _render_list(report.get("externalFactors", []), "사용 가능한 외부 정보가 없습니다."),
            "## 원인 분석\n" + _render_list(cause_lines, "판단 가능한 원인이 없습니다."),
            "## 우선 실행 제안\n" + _render_list(recommendation_lines, "제안이 없습니다."),
            "## 데이터 주의사항\n"
            + _render_list(report.get("warnings", []), "특이사항이 없습니다."),
        ]
    )


def generate_daily_consulting(payload: dict[str, Any]) -> dict[str, Any]:
    user_id = int(payload["userId"])
    store_id = int(payload["storeId"])
    target_date = date.fromisoformat(str(payload["targetDate"]))
    executions: list[dict[str, Any]] = []
    warnings: list[str] = []

    store_result, execution = _execution(
        "store_context",
        lambda: _store_context_from_list(store_id, user_id),
        {"storeId": store_id},
    )
    executions.append(execution)
    if not store_result.get("ok"):
        raise ValueError("STORE_OR_CHAT_CONTEXT_NOT_FOUND")
    store = store_result["data"]

    chat_result, execution = _execution(
        "today_chat_history",
        lambda: _today_chat_history(store_id, user_id, target_date),
        {"storeId": store_id, "targetDate": target_date.isoformat()},
    )
    executions.append(execution)
    messages = chat_result.get("data", []) if chat_result.get("ok") else []
    if not chat_result.get("ok"):
        warnings.append("당일 채팅 내역을 불러오지 못했습니다.")

    tool_map = get_tool_map(user_id=user_id, store_id=store_id)
    results: dict[str, dict[str, Any]] = {}
    for name in [
        "sales_analysis",
        "weather_search",
        "event_search",
        "closing_sales_forecast",
        "tomorrow_visitors_forecast",
    ]:
        result, execution = _execution(name, tool_map[name], {"storeId": store_id})
        results[name] = result
        executions.append(execution)
        if not result.get("ok"):
            warnings.append(f"{name} 데이터를 사용할 수 없습니다.")

    if not any(result.get("ok") for result in results.values()):
        raise RuntimeError("TOOL_EXECUTION_ERROR")

    sales = results.get("sales_analysis", {}).get("data") or {}
    closing = results.get("closing_sales_forecast", {}).get("data") or {}
    visitors = results.get("tomorrow_visitors_forecast", {}).get("data") or {}
    weather = results.get("weather_search", {}).get("data") or {}
    events = results.get("event_search", {}).get("data") or {}

    key_metrics: list[dict[str, Any]] = []
    if sales:
        key_metrics.extend(
            [
                _metric(
                    "RECENT_7D_TOTAL_SALES",
                    float(sales.get("totalSales", 0) or 0),
                    "KRW",
                    previousValue=sales.get("previousSales"),
                    changeRate=sales.get("salesChangeRate"),
                ),
                _metric(
                    "RECENT_7D_ORDER_COUNT",
                    float(sales.get("orderCount", 0) or 0),
                    "COUNT",
                    previousValue=sales.get("previousOrderCount"),
                    changeRate=(
                        round(
                            (float(sales.get("orderCount", 0)) - float(sales["previousOrderCount"]))
                            / float(sales["previousOrderCount"])
                            * 100,
                            1,
                        )
                        if sales.get("previousOrderCount")
                        else None
                    ),
                ),
            ]
        )
        order_count = float(sales.get("orderCount", 0) or 0)
        previous_order_count = float(sales.get("previousOrderCount", 0) or 0)
        if order_count and previous_order_count:
            current_aov = float(sales.get("totalSales", 0) or 0) / order_count
            previous_aov = float(sales.get("previousSales", 0) or 0) / previous_order_count
            key_metrics.append(
                _metric(
                    "RECENT_7D_AVERAGE_ORDER_VALUE",
                    round(current_aov),
                    "KRW",
                    previousValue=round(previous_aov),
                    changeRate=round((current_aov - previous_aov) / previous_aov * 100, 1),
                )
            )
    if closing:
        key_metrics.append(
            _metric(
                "FORECAST_CLOSING_TOTAL_SALES",
                float(closing.get("forecastClosingSalesAmount", 0) or 0),
                "KRW",
            )
        )
    if visitors:
        key_metrics.append(
            _metric("TOMORROW_EXPECTED_VISITORS", float(visitors.get("expectedVisitors", 0) or 0), "COUNT")
        )

    chat_insights = []
    for message in messages:
        if str(message.get("role", "")).upper() == "USER" and message.get("content"):
            content = str(message["content"])
            if content not in chat_insights:
                chat_insights.append(content)
        if len(chat_insights) == 5:
            break
    if not chat_insights:
        warnings.append("당일 사용자 채팅에서 추출할 인사이트가 없습니다.")

    external_factors: list[str] = []
    if weather:
        external_factors.append(
            f"날씨: {weather.get('summary', '정보 없음')}, 기온 {weather.get('temperature', '?')}℃, 강수확률 {weather.get('pop', '?')}%"
        )
    event_names = [
        f"{item.get('name')} ({item.get('date')})" if item.get("date") else str(item.get("name"))
        for item in events.get("events", [])
        if item.get("name")
    ]
    if event_names:
        external_factors.append("지역 행사: " + ", ".join(event_names))

    change = sales.get("salesChangeRate") if isinstance(sales, dict) else None
    causes: list[dict[str, Any]] = []
    if change is not None and float(change) < 0:
        causes.append(
            {
                "title": "최근 매출 감소",
                "description": "최근 7일 매출이 직전 비교 기간보다 감소했습니다.",
                "confidence": 0.85,
                "evidence": [f"매출 증감률 {float(change):+.1f}%"],
            }
        )
    elif sales:
        causes.append(
            {
                "title": "매출 흐름 유지",
                "description": "최근 매출 흐름에서 급격한 하락 신호가 확인되지 않았습니다.",
                "confidence": 0.7,
                "evidence": [f"최근 매출 {int(sales.get('totalSales', 0) or 0):,}원"],
            }
        )

    recommendations = [
        {
            "priority": "HIGH",
            "title": "고객 질문 기반 운영 점검",
            "description": "오늘 채팅에서 반복된 질문을 내일 운영 체크리스트에 반영하세요.",
            "expectedEffect": "고객 요구 대응 속도 개선",
        },
        {
            "priority": "MEDIUM",
            "title": "예측 지표 기반 인력·재고 조정",
            "description": "마감 매출과 예상 방문자 수에 맞춰 인력과 핵심 상품 재고를 조정하세요.",
            "expectedEffect": "품절과 유휴 시간 감소",
        },
    ]

    sales_value = int(sales.get("totalSales", 0) or 0) if sales else 0
    store_label = ", ".join(
        value
        for value in [
            str(store.get("region") or "").strip(),
            str(store.get("industry") or "").strip(),
        ]
        if value
    )
    store_name = str(store.get("name") or "매장")
    summary = (
        f"{target_date.isoformat()} {store_name}"
        f"{f'({store_label})' if store_label else ''}의 최근 분석 매출은 {sales_value:,}원입니다. "
        f"고객 대화 {len(chat_insights)}건과 외부 요인 {len(external_factors)}건을 함께 검토했습니다."
    )
    report = {
        "title": f"{target_date.isoformat()} 일일 컨설팅 보고서",
        "targetDate": target_date,
        "summary": summary,
        "content": "",
        "chatInsights": chat_insights,
        "keyMetrics": key_metrics,
        "externalFactors": external_factors,
        "estimatedCauses": causes,
        "recommendations": recommendations,
        "warnings": warnings,
        "usedTools": executions,
        "citations": [],
        "model": "daily-consulting-v1",
    }
    report["content"] = render_daily_content(report)
    return report

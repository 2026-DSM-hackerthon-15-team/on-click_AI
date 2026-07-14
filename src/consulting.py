"""Deterministic statistical analysis used by the consulting endpoint."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Iterable


VALID_PERIOD_TYPES = {"MONTHLY", "QUARTERLY", "YEARLY", "CUSTOM"}


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _rate(current: float, previous: float | None) -> float | None:
    if previous in (None, 0):
        return None
    return round((current - previous) / previous * 100, 1)


def _metric(
    name: str,
    current: float,
    previous: float | None,
    unit: str,
) -> dict[str, Any]:
    return {
        "metricName": name,
        "currentValue": round(float(current), 2),
        "previousValue": round(float(previous), 2) if previous is not None else None,
        "changeRate": _rate(float(current), float(previous) if previous is not None else None),
        "unit": unit,
    }


def _between(point: dict[str, Any], start: date, end: date) -> bool:
    try:
        point_date = _as_date(point["date"])
    except (KeyError, TypeError, ValueError):
        return False
    return start <= point_date <= end


def _total(points: Iterable[dict[str, Any]], key: str) -> float:
    return sum(float(point.get(key, 0) or 0) for point in points)


def analyze_consulting(payload: dict[str, Any]) -> dict[str, Any]:
    period_type = str(payload.get("periodType", "")).upper()
    if period_type not in VALID_PERIOD_TYPES:
        raise ValueError("INVALID_ANALYSIS_PERIOD")

    start = _as_date(payload["periodStart"])
    end = _as_date(payload["periodEnd"])
    if start > end:
        raise ValueError("INVALID_ANALYSIS_PERIOD")

    comparison_start_raw = payload.get("comparisonPeriodStart")
    comparison_end_raw = payload.get("comparisonPeriodEnd")
    if bool(comparison_start_raw) != bool(comparison_end_raw):
        raise ValueError("INVALID_ANALYSIS_PERIOD")
    comparison_start = _as_date(comparison_start_raw) if comparison_start_raw else None
    comparison_end = _as_date(comparison_end_raw) if comparison_end_raw else None
    if comparison_start and comparison_end and comparison_start > comparison_end:
        raise ValueError("INVALID_ANALYSIS_PERIOD")

    sales_data = [dict(point) for point in payload.get("salesData") or []]
    current = [point for point in sales_data if _between(point, start, end)]
    if not current:
        raise ValueError("INSUFFICIENT_SALES_DATA")
    previous = (
        [point for point in sales_data if _between(point, comparison_start, comparison_end)]
        if comparison_start and comparison_end
        else []
    )

    current_sales = _total(current, "salesAmount")
    current_orders = _total(current, "orderCount")
    previous_sales = _total(previous, "salesAmount") if previous else None
    previous_orders = _total(previous, "orderCount") if previous else None
    current_aov = current_sales / current_orders if current_orders else 0
    previous_aov = (
        previous_sales / previous_orders
        if previous_sales is not None and previous_orders
        else None
    )

    costs = [
        dict(point)
        for point in payload.get("costData") or []
        if _between(point, start, end)
    ]
    current_cost = _total(costs, "amount") if costs else None

    current_hourly: dict[int, float] = defaultdict(float)
    previous_hourly: dict[int, float] = defaultdict(float)
    for point in current:
        if point.get("hour") is not None:
            current_hourly[int(point["hour"])] += float(point.get("salesAmount", 0) or 0)
    for point in previous:
        if point.get("hour") is not None:
            previous_hourly[int(point["hour"])] += float(point.get("salesAmount", 0) or 0)

    sales_change = _rate(current_sales, previous_sales)
    causes: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    declining_hours = []
    for hour, old_amount in previous_hourly.items():
        if old_amount <= 0:
            continue
        hour_change = _rate(current_hourly.get(hour, 0), old_amount)
        if hour_change is not None:
            declining_hours.append((hour_change, hour))
    declining_hours.sort()

    if sales_change is not None and sales_change <= -3:
        evidence = [f"전체 매출 {sales_change:+.1f}%"]
        title = "비교 기간 대비 매출 감소"
        description = "주문 수와 객단가 변화를 함께 점검해야 합니다."
        if declining_hours and declining_hours[0][0] < 0:
            hour_change, hour = declining_hours[0]
            evidence.append(f"{hour}시 매출 {hour_change:+.1f}%")
            title = f"{hour}시 시간대 매출 감소"
            description = f"{hour}시 전후 매출 하락이 전체 감소에 크게 기여했습니다."
            recommendations.append(
                {
                    "priority": "HIGH",
                    "title": f"{hour}시 시간대 프로모션 실험",
                    "description": "2주간 세트 메뉴 또는 한정 프로모션을 운영하고 주문 수 변화를 비교하세요.",
                    "expectedEffect": "취약 시간대 주문 수 회복 가능",
                }
            )
        causes.append(
            {
                "title": title,
                "description": description,
                "confidence": 0.86 if previous else 0.6,
                "evidence": evidence,
            }
        )
    elif sales_change is not None and sales_change >= 3:
        causes.append(
            {
                "title": "비교 기간 대비 성장",
                "description": "현재의 성장 요인을 유지할 수 있도록 인기 상품과 시간대를 추적하세요.",
                "confidence": 0.82,
                "evidence": [f"전체 매출 {sales_change:+.1f}%"],
            }
        )

    if previous_orders and _rate(current_orders, previous_orders) is not None:
        order_change = _rate(current_orders, previous_orders)
        if order_change is not None and order_change <= -5:
            causes.append(
                {
                    "title": "주문 수 감소",
                    "description": "객단가보다 방문·주문 횟수 감소의 영향이 큽니다.",
                    "confidence": 0.9,
                    "evidence": [f"주문 수 {order_change:+.1f}%"],
                }
            )

    if current_cost is not None and current_sales > 0:
        cost_ratio = current_cost / current_sales * 100
        if cost_ratio >= 40:
            recommendations.append(
                {
                    "priority": "HIGH",
                    "title": "원가율 점검",
                    "description": "고원가 상품의 판매가와 발주량을 우선 검토하세요.",
                    "expectedEffect": "매출총이익 개선 가능",
                }
            )

    external = payload.get("externalContext") or {}
    weather_summary = external.get("weatherSummary") if isinstance(external, dict) else None
    local_events = external.get("localEvents", []) if isinstance(external, dict) else []
    if weather_summary or local_events:
        evidence = []
        if weather_summary:
            evidence.append(str(weather_summary))
        evidence.extend(str(event) for event in local_events[:3])
        causes.append(
            {
                "title": "외부 환경 영향 가능성",
                "description": "날씨와 주변 행사는 상관관계 후보이며 직접 인과로 단정하지 않습니다.",
                "confidence": 0.55,
                "evidence": evidence,
            }
        )

    if not recommendations:
        recommendations.append(
            {
                "priority": "MEDIUM",
                "title": "주간 핵심 지표 추적",
                "description": "매출, 주문 수, 객단가를 동일 요일 기준으로 매주 비교하세요.",
                "expectedEffect": "변화 원인의 조기 발견",
            }
        )

    metrics = [
        _metric("TOTAL_SALES", current_sales, previous_sales, "KRW"),
        _metric("ORDER_COUNT", current_orders, previous_orders, "COUNT"),
        _metric("AVERAGE_ORDER_VALUE", current_aov, previous_aov, "KRW"),
    ]
    if current_cost is not None:
        metrics.append(_metric("TOTAL_COST", current_cost, None, "KRW"))

    warnings: list[str] = []
    unique_dates = {str(point.get("date")) for point in current}
    if len(unique_dates) < 7:
        warnings.append("분석 기간의 유효 데이터가 7일 미만이어서 추세 신뢰도가 낮습니다.")
    if comparison_start and not previous:
        warnings.append("비교 기간 데이터가 없어 증감률을 계산하지 못했습니다.")
    elif not comparison_start:
        warnings.append("비교 기간이 제공되지 않아 현재 기간 중심으로 분석했습니다.")

    if sales_change is None:
        summary = f"분석 기간 총매출은 {int(current_sales):,}원, 주문 수는 {int(current_orders):,}건입니다."
    else:
        summary = f"전체 매출은 비교 기간 대비 {abs(sales_change):.1f}% {'증가' if sales_change >= 0 else '감소'}했습니다."

    return {
        "title": f"{start.isoformat()}~{end.isoformat()} 매출 컨설팅",
        "periodType": period_type,
        "periodStart": start.isoformat(),
        "periodEnd": end.isoformat(),
        "summary": summary,
        "estimatedCauses": causes,
        "recommendations": recommendations,
        "keyMetrics": metrics,
        "warnings": warnings,
        "model": "statistical-rules-v1",
    }

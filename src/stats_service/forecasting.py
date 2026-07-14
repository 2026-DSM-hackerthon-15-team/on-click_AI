"""Small, explainable forecasting models for the hackathon PoC."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any, Iterable


def parse_local_datetime(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def completed_transactions(transactions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(tx)
        for tx in transactions
        if str(tx.get("status", "COMPLETED")).upper() == "COMPLETED"
        and tx.get("soldAt")
    ]


def _daily(transactions: Iterable[dict[str, Any]]) -> dict[date, dict[str, float]]:
    daily: dict[date, dict[str, float]] = defaultdict(lambda: {"sales": 0.0, "visitors": 0.0})
    for tx in completed_transactions(transactions):
        sold_at = parse_local_datetime(tx["soldAt"])
        bucket = daily[sold_at.date()]
        bucket["sales"] += float(tx.get("totalPaidAmount", 0) or 0)
        bucket["visitors"] += 1
    return dict(daily)


def _weighted_recent(values: list[float]) -> float:
    if not values:
        return 0.0
    recent = values[-4:]
    weights = list(range(1, len(recent) + 1))
    return sum(value * weight for value, weight in zip(recent, weights)) / sum(weights)


def predict_closing_sales(
    transactions: Iterable[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    txs = completed_transactions(transactions)
    today = now.date()
    observed = sum(
        float(tx.get("totalPaidAmount", 0) or 0)
        for tx in txs
        if parse_local_datetime(tx["soldAt"]).date() == today
        and parse_local_datetime(tx["soldAt"]) <= now
    )

    daily = _daily(txs)
    same_weekday = [
        (day, metrics["sales"])
        for day, metrics in daily.items()
        if day < today and day.weekday() == today.weekday()
    ]
    same_weekday.sort(key=lambda item: item[0])
    baselines = [amount for _, amount in same_weekday]
    if not baselines:
        baselines = [metrics["sales"] for day, metrics in sorted(daily.items()) if day < today]
    baseline = _weighted_recent(baselines)

    historical_ratios = []
    for day, total in same_weekday[-4:]:
        if total <= 0:
            continue
        elapsed = sum(
            float(tx.get("totalPaidAmount", 0) or 0)
            for tx in txs
            if parse_local_datetime(tx["soldAt"]).date() == day
            and parse_local_datetime(tx["soldAt"]).time() <= now.time()
        )
        historical_ratios.append(elapsed / total)

    projection = 0.0
    if historical_ratios:
        elapsed_ratio = min(max(fmean(historical_ratios), 0.05), 1.0)
        projection = observed / elapsed_ratio
    candidates = [observed]
    if baseline:
        candidates.append((baseline + projection) / 2 if projection else baseline)
    elif projection:
        candidates.append(projection)
    forecast = int(round(max(candidates)))

    return {
        "observedSalesAmount": int(round(observed)),
        "forecastClosingSalesAmount": forecast,
        "model": "weekday-weighted-average-v1",
        "sampleDays": len(baselines[-4:]),
    }


def predict_tomorrow_visitors(
    transactions: Iterable[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    daily = _daily(transactions)
    target = today + timedelta(days=1)
    matching = [
        (day, metrics["visitors"])
        for day, metrics in daily.items()
        if day < target and day.weekday() == target.weekday()
    ]
    matching.sort(key=lambda item: item[0])
    values = [count for _, count in matching]
    if not values:
        values = [metrics["visitors"] for day, metrics in sorted(daily.items()) if day <= today]

    baseline = _weighted_recent(values)
    all_recent = [metrics["visitors"] for day, metrics in sorted(daily.items()) if day <= today]
    trend_factor = 1.0
    if len(all_recent) >= 14:
        previous_week = fmean(all_recent[-14:-7])
        current_week = fmean(all_recent[-7:])
        if previous_week > 0:
            trend_factor = min(max(current_week / previous_week, 0.8), 1.2)
    expected = max(0, int(round(baseline * trend_factor)))
    return {
        "targetDate": target.isoformat(),
        "expectedVisitors": expected,
        "model": "weekday-weighted-average-v1",
        "sampleDays": len(values[-4:]),
    }

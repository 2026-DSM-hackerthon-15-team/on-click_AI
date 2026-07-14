from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from src.stats_service.forecasting import predict_closing_sales, predict_tomorrow_visitors


def transaction(sale_id: int, sold_at: datetime, amount: int, status: str = "COMPLETED") -> dict:
    return {
        "saleId": sale_id,
        "soldAt": sold_at.isoformat(timespec="seconds"),
        "totalPaidAmount": amount,
        "status": status,
    }


class ForecastingTests(unittest.TestCase):
    def test_cancelled_sales_are_excluded_from_closing_forecast(self) -> None:
        now = datetime(2026, 7, 14, 15, 0)
        rows = []
        sale_id = 1
        for weeks_ago in range(4, 0, -1):
            day = now.date() - timedelta(days=7 * weeks_ago)
            rows.extend(
                [
                    transaction(sale_id, datetime.combine(day, datetime.min.time()).replace(hour=10), 100000),
                    transaction(sale_id + 1, datetime.combine(day, datetime.min.time()).replace(hour=18), 100000),
                ]
            )
            sale_id += 2
        rows.append(transaction(sale_id, now.replace(hour=10), 90000))
        rows.append(transaction(sale_id + 1, now.replace(hour=11), 999999, "CANCELLED"))

        result = predict_closing_sales(rows, now)
        self.assertEqual(90000, result["observedSalesAmount"])
        self.assertGreaterEqual(result["forecastClosingSalesAmount"], result["observedSalesAmount"])
        self.assertNotEqual(1089999, result["forecastClosingSalesAmount"])

    def test_tomorrow_visitor_forecast_uses_matching_weekday(self) -> None:
        today = date(2026, 7, 14)
        target_weekday = (today + timedelta(days=1)).weekday()
        rows = []
        sale_id = 1
        for days_ago in range(35, 0, -1):
            day = today - timedelta(days=days_ago)
            if day.weekday() != target_weekday:
                continue
            for hour in range(10, 15):
                rows.append(transaction(sale_id, datetime.combine(day, datetime.min.time()).replace(hour=hour), 5000))
                sale_id += 1
        result = predict_tomorrow_visitors(rows, today)
        self.assertEqual(5, result["expectedVisitors"])
        self.assertEqual((today + timedelta(days=1)).isoformat(), result["targetDate"])


if __name__ == "__main__":
    unittest.main()

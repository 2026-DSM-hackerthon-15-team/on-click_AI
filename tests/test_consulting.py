from __future__ import annotations

import unittest

from src.consulting import analyze_consulting


class ConsultingAnalysisTests(unittest.TestCase):
    def test_comparison_uses_actual_comparison_rows(self) -> None:
        result = analyze_consulting(
            {
                "periodType": "MONTHLY",
                "periodStart": "2026-07-01",
                "periodEnd": "2026-07-31",
                "comparisonPeriodStart": "2026-06-01",
                "comparisonPeriodEnd": "2026-06-30",
                "salesData": [
                    {"date": "2026-07-01", "salesAmount": 80, "orderCount": 8, "hour": 18},
                    {"date": "2026-06-01", "salesAmount": 100, "orderCount": 10, "hour": 18},
                ],
            }
        )
        metric = next(item for item in result["keyMetrics"] if item["metricName"] == "TOTAL_SALES")
        self.assertEqual(-20.0, metric["changeRate"])
        self.assertIn("20.0% 감소", result["summary"])

    def test_missing_current_period_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_SALES_DATA"):
            analyze_consulting(
                {
                    "periodType": "CUSTOM",
                    "periodStart": "2026-07-01",
                    "periodEnd": "2026-07-02",
                    "salesData": [{"date": "2026-06-01", "salesAmount": 100, "orderCount": 1}],
                }
            )


if __name__ == "__main__":
    unittest.main()

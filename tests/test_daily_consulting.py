from __future__ import annotations

import unittest
from unittest.mock import patch

from src.daily_consulting import generate_daily_consulting


def _tool_results() -> dict:
    return {
        "sales_analysis": lambda: {
            "ok": True,
            "data": {
                "totalSales": 900000,
                "previousSales": 1000000,
                "salesChangeRate": -10.0,
                "orderCount": 110,
            },
        },
        "weather_search": lambda: {
            "ok": True,
            "data": {"summary": "맑음", "temperature": 27, "pop": 10},
        },
        "event_search": lambda: {
            "ok": True,
            "data": {"events": [{"name": "지역 축제"}]},
        },
        "closing_sales_forecast": lambda: {
            "ok": True,
            "data": {"forecastClosingSalesAmount": 1200000},
        },
        "tomorrow_visitors_forecast": lambda: {
            "ok": True,
            "data": {"expectedVisitors": 130},
        },
    }


class DailyConsultingTests(unittest.TestCase):
    @patch("src.daily_consulting.get_tool_map")
    @patch("src.daily_consulting._today_chat_history")
    @patch("src.daily_consulting._store_context_from_list")
    def test_daily_report_uses_fixed_sections_and_all_context(
        self,
        store_context,
        chat_history,
        tool_map,
    ) -> None:
        store_context.return_value = {
            "ok": True,
            "data": {
                "storeId": 5,
                "name": "온클릭 카페",
                "region": "서울특별시 강남구",
                "industry": "CAFE",
            },
        }
        chat_history.return_value = {
            "ok": True,
            "data": [
                {
                    "role": "USER",
                    "content": "저녁 매출이 왜 줄었어?",
                    "createdAt": "2026-07-14T10:00:00Z",
                }
            ],
        }
        tool_map.return_value = _tool_results()

        result = generate_daily_consulting(
            {"userId": 4, "storeId": 5, "targetDate": "2026-07-14"}
        )

        headings = [
            "## 오늘의 요약",
            "## 고객 대화 인사이트",
            "## 핵심 지표",
            "## 외부 환경",
            "## 원인 분석",
            "## 우선 실행 제안",
            "## 데이터 주의사항",
        ]
        self.assertEqual(sorted(result["content"].index(item) for item in headings), [result["content"].index(item) for item in headings])
        self.assertEqual(["저녁 매출이 왜 줄었어?"], result["chatInsights"])
        self.assertEqual(7, len(result["usedTools"]))
        self.assertEqual("RECENT_7D_TOTAL_SALES", result["keyMetrics"][0]["metricName"])
        self.assertEqual("daily-consulting-v1", result["model"])

    @patch("src.daily_consulting.get_tool_map")
    @patch("src.daily_consulting._today_chat_history", return_value={"ok": True, "data": []})
    @patch(
        "src.daily_consulting._store_context_from_list",
        return_value={
            "ok": True,
            "data": {"storeId": 5, "name": "매장", "region": "서울", "industry": "CAFE"},
        },
    )
    def test_daily_report_degrades_when_every_data_tool_fails(self, _store, _chat, tool_map) -> None:
        tool_map.return_value = {
            name: (lambda: {"ok": False, "error": "unavailable"})
            for name in _tool_results()
        }
        result = generate_daily_consulting(
            {"userId": 4, "storeId": 5, "targetDate": "2026-07-14"}
        )

        self.assertIn("연동 데이터 조회가 모두 실패", result["content"])
        self.assertEqual("daily-consulting-v1", result["model"])


if __name__ == "__main__":
    unittest.main()

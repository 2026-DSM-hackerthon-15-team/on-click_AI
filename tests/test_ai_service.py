from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.ai_service.main import app


class AiServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def _chat_payload(self) -> dict:
        return {
            "userId": 1,
            "storeId": 10,
            "chatRoomId": 1,
            "message": "이번 주 매출이 왜 줄었어?",
            "availableTools": ["sales_analysis"],
            "attachmentKeys": [],
        }

    def test_internal_api_key_is_compared_not_just_present(self) -> None:
        response = self.client.post(
            "/ai/chat",
            json=self._chat_payload(),
            headers={"X-Internal-Api-Key": "wrong"},
        )
        self.assertEqual(401, response.status_code)
        self.assertEqual("INVALID_INTERNAL_API_KEY", response.json()["errorCode"])

    @patch("src.ai_service.main._run_langchain_agent", return_value=None)
    @patch("src.ai_service.main.get_tool_map")
    def test_rule_agent_selects_and_reports_sales_tool(self, get_tool_map, _llm) -> None:
        get_tool_map.return_value = {
            "sales_analysis": lambda: {
                "ok": True,
                "data": {
                    "totalSales": 900000,
                    "salesChangeRate": -8.4,
                    "orderCount": 110,
                },
            }
        }
        response = self.client.post(
            "/ai/chat",
            json=self._chat_payload(),
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("sales_analysis", body["usedTools"][0]["toolName"])
        self.assertEqual("SUCCESS", body["usedTools"][0]["status"])
        self.assertIn("-8.4%", body["answer"])
        self.assertEqual("rule-agent-v1", body["model"])
        self.assertEqual("STOP", body["finishReason"])
        self.assertEqual({"storeId": 10}, body["usedTools"][0]["arguments"])
        self.assertIsInstance(body["usedTools"][0]["latencyMs"], int)

    def test_available_tools_is_required_by_updated_contract(self) -> None:
        payload = self._chat_payload()
        payload.pop("availableTools")
        response = self.client.post(
            "/ai/chat",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("INVALID_AI_CHAT_REQUEST", response.json()["errorCode"])

    def test_removed_conversation_history_is_rejected(self) -> None:
        payload = self._chat_payload()
        payload["conversationHistory"] = []
        response = self.client.post(
            "/ai/chat",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("INVALID_AI_CHAT_REQUEST", response.json()["errorCode"])

    @patch("src.ai_service.main._run_langchain_agent", return_value=None)
    @patch("src.ai_service.main.get_tool_map")
    def test_required_tool_failure_uses_documented_502_error(self, get_tool_map, _llm) -> None:
        get_tool_map.return_value = {
            "sales_analysis": lambda: {"ok": False, "error": "backend unavailable"}
        }
        response = self.client.post(
            "/ai/chat",
            json=self._chat_payload(),
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(502, response.status_code)
        self.assertEqual("TOOL_EXECUTION_ERROR", response.json()["errorCode"])

    @patch("src.ai_service.main._run_langchain_agent", return_value=None)
    @patch("src.ai_service.main.get_tool_map", return_value={})
    def test_chat_never_executes_consulting_or_publish_side_effects(self, get_tool_map, _llm) -> None:
        payload = self._chat_payload()
        payload["message"] = "컨설팅을 저장하고 인스타에 업로드해줘"
        payload["availableTools"] = ["consulting_save", "instagram_publish"]
        response = self.client.post(
            "/ai/chat",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json()["usedTools"])
        self.assertIn("실행하지 않습니다", response.json()["answer"])
        get_tool_map.assert_called_once()

    @patch("src.ai_service.main._write_daily_report_with_llm", side_effect=lambda report: report)
    @patch("src.ai_service.main.build_daily_consulting")
    def test_daily_consulting_endpoint_returns_fixed_contract(self, build_report, _writer) -> None:
        build_report.return_value = {
            "title": "2026-07-14 일일 컨설팅 보고서",
            "targetDate": "2026-07-14",
            "summary": "오늘 요약",
            "content": "## 오늘의 요약\n오늘 요약",
            "chatInsights": [],
            "keyMetrics": [],
            "externalFactors": [],
            "estimatedCauses": [],
            "recommendations": [],
            "warnings": [],
            "usedTools": [],
            "citations": [],
            "model": "daily-consulting-v1",
        }
        response = self.client.post(
            "/ai/consultings/daily",
            json={"userId": 4, "storeId": 5, "targetDate": "2026-07-14"},
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("daily-consulting-v1", response.json()["model"])
        self.assertEqual("DAILY_V1", build_report.call_args.args[0]["reportFormat"])

    def test_structured_consulting_contract(self) -> None:
        payload = {
            "userId": 1,
            "storeId": 10,
            "periodType": "MONTHLY",
            "periodStart": "2026-07-01",
            "periodEnd": "2026-07-07",
            "comparisonPeriodStart": "2026-06-01",
            "comparisonPeriodEnd": "2026-06-07",
            "salesData": [
                {"date": "2026-07-01", "hour": 18, "salesAmount": 90000, "orderCount": 10},
                {"date": "2026-07-02", "hour": 18, "salesAmount": 100000, "orderCount": 11},
                {"date": "2026-06-01", "hour": 18, "salesAmount": 150000, "orderCount": 15},
                {"date": "2026-06-02", "hour": 18, "salesAmount": 160000, "orderCount": 16},
            ],
            "costData": [],
        }
        response = self.client.post(
            "/ai/consultings",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertTrue(body["estimatedCauses"])
        self.assertTrue(body["recommendations"])
        self.assertEqual("TOTAL_SALES", body["keyMetrics"][0]["metricName"])


if __name__ == "__main__":
    unittest.main()

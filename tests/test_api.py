from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from fastapi.testclient import TestClient

from src.api.main import app


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.auth = {"Authorization": "Bearer user-1"}

    def test_error_body_uses_common_contract(self) -> None:
        response = self.client.get("/stores")
        self.assertEqual(401, response.status_code)
        self.assertEqual("UNAUTHORIZED", response.json()["errorCode"])

    def test_store_list_exposes_region_and_industry_without_coordinates(self) -> None:
        response = self.client.get("/stores", headers=self.auth)
        self.assertEqual(200, response.status_code)
        store = response.json()[0]
        self.assertEqual("서울특별시 강남구", store["region"])
        self.assertEqual("CAFE", store["industry"])
        self.assertNotIn("latitude", store)
        self.assertNotIn("longitude", store)
        self.assertNotIn("roadAddress", store)

    def test_chat_room_list_supports_daily_report_history_lookup(self) -> None:
        response = self.client.get(
            "/stores/10/chat-rooms",
            headers={"Authorization": "Bearer user-1"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()[0]["id"])
        self.assertEqual(10, response.json()[0]["storeId"])

    def test_removed_store_detail_and_today_dashboard_are_not_exposed(self) -> None:
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertNotIn("/stores/{storeId}", paths)
        self.assertNotIn("/stores/{storeId}/dashboard/today", paths)

    def test_sales_transactions_are_paged_and_stably_sorted(self) -> None:
        response = self.client.get(
            "/stores/10/sales/transactions?page=0&size=5&sortBy=soldAt&sortDirection=DESC",
            headers=self.auth,
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(5, len(body["content"]))
        self.assertGreater(body["totalElements"], 5)
        self.assertTrue(body["hasNext"])
        sold_at = [item["soldAt"] for item in body["content"]]
        self.assertEqual(sorted(sold_at, reverse=True), sold_at)

    def test_other_owner_cannot_read_store_data(self) -> None:
        response = self.client.get(
            "/stores/10/sales/transactions",
            headers={"Authorization": "Bearer user-2"},
        )
        self.assertEqual(403, response.status_code)
        self.assertEqual("STORE_ACCESS_DENIED", response.json()["errorCode"])

    def test_hourly_visitors_always_returns_24_buckets(self) -> None:
        response = self.client.get("/stores/10/dashboard/hourly-visitors", headers=self.auth)
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(list(range(24)), [bucket["hour"] for bucket in body["hourly"]])
        self.assertEqual(body["totalVisitors"], sum(bucket["visitorCount"] for bucket in body["hourly"]))

    def _chat_payload(self) -> dict:
        return {
            "userId": 1,
            "storeId": 10,
            "chatRoomId": 12,
            "message": "이번 주 매출이 왜 줄었어?",
            "availableTools": ["sales_analysis"],
        }

    @patch("src.api.main.requests.request")
    def test_chat_proxies_only_updated_request_fields(self, request: Mock) -> None:
        request.return_value = Mock(
            ok=True,
            json=lambda: {
                "answer": "평일 저녁 매출이 감소했습니다.",
                "usedTools": [
                    {
                        "toolName": "sales_analysis",
                        "status": "SUCCESS",
                        "arguments": {"storeId": 10},
                        "resultSummary": "저녁 매출 -18.2%",
                        "latencyMs": 824,
                    }
                ],
                "citations": [],
                "model": "claude-sonnet-4-6",
                "finishReason": "STOP",
            },
        )
        response = self.client.post(
            "/ai/chat",
            json=self._chat_payload(),
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(200, response.status_code)
        proxied = request.call_args.kwargs["json"]
        self.assertEqual(45, request.call_args.kwargs["timeout"])
        self.assertEqual([], proxied["attachmentKeys"])
        self.assertNotIn("conversationHistory", proxied)
        self.assertNotIn("storeContext", proxied)
        self.assertEqual("SUCCESS", response.json()["usedTools"][0]["status"])

    def test_chat_missing_available_tools_uses_chat_error_code(self) -> None:
        payload = self._chat_payload()
        payload.pop("availableTools")
        response = self.client.post(
            "/ai/chat",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("INVALID_AI_CHAT_REQUEST", response.json()["errorCode"])

    @patch("src.api.main.requests.request")
    def test_invalid_ai_response_uses_documented_422_error(self, request: Mock) -> None:
        request.return_value = Mock(
            ok=True,
            json=lambda: {
                "answer": "usedTools만 누락된 응답",
                "citations": [],
                "model": "claude-sonnet-4-6",
                "finishReason": "STOP",
            },
        )
        response = self.client.post(
            "/ai/chat",
            json=self._chat_payload(),
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("AI_RESPONSE_INVALID", response.json()["errorCode"])

    @patch("src.api.main.requests.request", side_effect=requests.Timeout("slow AI"))
    def test_ai_timeout_uses_documented_504_error(self, _request: Mock) -> None:
        response = self.client.post(
            "/ai/chat",
            json=self._chat_payload(),
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(504, response.status_code)
        self.assertEqual("AI_TIMEOUT", response.json()["errorCode"])

    def test_chat_openapi_matches_updated_dto(self) -> None:
        schema = self.client.get("/openapi.json").json()
        request_schema = schema["components"]["schemas"]["AiChatRequest"]
        response_schema = schema["components"]["schemas"]["AiChatResponse"]
        self.assertEqual(
            {"userId", "storeId", "chatRoomId", "message", "availableTools"},
            set(request_schema["required"]),
        )
        self.assertEqual(
            {"userId", "storeId", "chatRoomId", "message", "availableTools", "attachmentKeys"},
            set(request_schema["properties"]),
        )
        self.assertFalse(request_schema["additionalProperties"])
        self.assertEqual(
            {"answer", "usedTools", "model", "finishReason"},
            set(response_schema["required"]),
        )

    @patch("src.api.main.requests.request")
    def test_daily_consulting_is_proxied_and_validated(self, request: Mock) -> None:
        request.return_value = Mock(
            ok=True,
            json=lambda: {
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
            },
        )
        response = self.client.post(
            "/ai/consultings/daily",
            json={"userId": 4, "storeId": 5, "targetDate": "2026-07-14"},
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("DAILY_V1", request.call_args.kwargs["json"]["reportFormat"])
        self.assertEqual(45, request.call_args.kwargs["timeout"])

    @patch("src.api.main.requests.request")
    def test_instagram_publish_forwards_provider_credential(self, request: Mock) -> None:
        request.return_value = Mock(
            ok=True,
            json=lambda: {
                "marketingId": 21,
                "platform": "INSTAGRAM",
                "status": "PUBLISHED",
                "externalPostId": "post-1",
                "publishedUrl": "https://instagram.com/p/post-1/",
                "publishedAt": "2026-07-14T12:00:00Z",
                "failureReason": None,
            },
        )
        payload = {
            "userId": 4,
            "instagramAccountId": "account-1",
            "content": "승인된 본문",
            "hashtags": ["#온클릭"],
            "imageUrls": ["https://cdn.example.com/image.jpg"],
            "publishType": "FEED",
            "idempotencyKey": "marketing-21-v1",
        }
        response = self.client.post(
            "/ai/marketings/21/publish/instagram",
            json=payload,
            headers={
                "X-Internal-Api-Key": "secret",
                "X-Instagram-Access-Token": "provider-token",
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "provider-token",
            request.call_args.kwargs["headers"]["X-Instagram-Access-Token"],
        )

    def test_new_ai_endpoints_are_in_openapi(self) -> None:
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/ai/consultings/daily", schema["paths"])
        self.assertIn(
            "/ai/marketings/{marketingId}/publish/instagram",
            schema["paths"],
        )
        daily_request = schema["components"]["schemas"]["GenerateDailyConsultingRequest"]
        self.assertEqual({"userId", "storeId", "targetDate"}, set(daily_request["required"]))
        instagram_request = schema["components"]["schemas"]["PublishInstagramRequest"]
        self.assertIn("idempotencyKey", instagram_request["required"])


if __name__ == "__main__":
    unittest.main()

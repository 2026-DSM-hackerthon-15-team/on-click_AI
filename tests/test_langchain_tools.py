from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.auth import require_backend_jwt
from src.langchain_tools import _auth_headers, events_tool, get_langchain_tools, weather_tool


class BackendAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        require_backend_jwt("Bearer backend-request-jwt")

    def test_bound_langchain_tools_expose_no_model_arguments(self) -> None:
        tools = get_langchain_tools(store_id=10, user_id=1, allowed_tools=["sales_analysis"])

        self.assertEqual(1, len(tools))
        self.assertEqual({}, tools[0].args_schema.model_json_schema().get("properties"))

    def test_request_jwt_is_forwarded(self) -> None:
        self.assertEqual({"Authorization": "Bearer backend-request-jwt"}, _auth_headers(user_id=4))

    @patch("src.langchain_tools._json_get")
    def test_weather_uses_region_and_industry_from_store_list(self, json_get) -> None:
        json_get.side_effect = [
            {
                "ok": True,
                "data": [
                    {"id": 3, "region": "부산광역시", "industry": "RETAIL"},
                    {"id": 5, "region": "서울특별시 강남구", "industry": "CAFE"},
                ],
            },
            {"ok": True, "data": {"summary": "맑음"}},
        ]
        with patch(
            "src.langchain_tools.settings",
            SimpleNamespace(
                api_base_url="https://backend.example.com",
                mcp_service_url="http://mcp",
            ),
        ):
            result = weather_tool(store_id=5, user_id=4)

        self.assertTrue(result["ok"])
        self.assertEqual("https://backend.example.com/stores", json_get.call_args_list[0].args[0])
        self.assertEqual(
            {"region": "서울특별시 강남구", "industry": "CAFE"},
            json_get.call_args_list[1].kwargs["params"],
        )

    @patch("src.langchain_tools._json_get")
    def test_weather_rejects_store_without_region_or_industry(self, json_get) -> None:
        json_get.return_value = {"ok": True, "data": [{"id": 5, "name": "매장"}]}
        with patch(
            "src.langchain_tools.settings",
            SimpleNamespace(
                api_base_url="https://backend.example.com",
                mcp_service_url="http://mcp",
            ),
        ):
            result = weather_tool(store_id=5, user_id=4)

        self.assertFalse(result["ok"])
        self.assertEqual("STORE_CONTEXT_MISSING", result["errorCode"])
        self.assertEqual(1, json_get.call_count)

    @patch("src.langchain_tools._json_get")
    def test_weather_rejects_store_not_owned_by_jwt_user(self, json_get) -> None:
        json_get.return_value = {
            "ok": True,
            "data": [{"id": 3, "region": "부산광역시", "industry": "RETAIL"}],
        }
        with patch(
            "src.langchain_tools.settings",
            SimpleNamespace(
                api_base_url="https://backend.example.com",
                mcp_service_url="http://mcp",
            ),
        ):
            result = weather_tool(store_id=5, user_id=4)

        self.assertFalse(result["ok"])
        self.assertEqual("STORE_NOT_FOUND", result["errorCode"])
        self.assertEqual(1, json_get.call_count)

    @patch("src.langchain_tools._json_get")
    def test_events_use_the_same_store_context_contract(self, json_get) -> None:
        json_get.side_effect = [
            {"ok": True, "data": [{"id": 5, "region": "대전광역시", "industry": "SERVICE"}]},
            {"ok": True, "data": {"events": []}},
        ]
        with patch(
            "src.langchain_tools.settings",
            SimpleNamespace(
                api_base_url="https://backend.example.com",
                mcp_service_url="http://mcp",
            ),
        ):
            result = events_tool(store_id=5, user_id=4)

        self.assertTrue(result["ok"])
        self.assertEqual(
            {"days": 7, "region": "대전광역시", "industry": "SERVICE"},
            json_get.call_args_list[1].kwargs["params"],
        )


if __name__ == "__main__":
    unittest.main()

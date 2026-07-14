from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from src.mcp_service.main import app


class McpServiceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_weather_requires_and_returns_region_and_industry(self) -> None:
        response = self.client.get(
            "/weather",
            params={"region": "서울특별시 강남구", "industry": "CAFE"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"region": "서울특별시 강남구", "industry": "CAFE"},
            response.json()["location"],
        )

    def test_weather_no_longer_accepts_coordinates_as_context(self) -> None:
        response = self.client.get("/weather", params={"lat": 37.5, "lon": 127.0})
        self.assertEqual(422, response.status_code)

    def test_events_are_scoped_by_store_region_and_industry(self) -> None:
        response = self.client.get(
            "/events",
            params={"days": 7, "region": "대전광역시", "industry": "SERVICE"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"region": "대전광역시", "industry": "SERVICE"},
            response.json()["location"],
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.ai_service.main import app
from src.instagram import BrowserMCPUnavailable, clear_publish_registry, publish_instagram


def _payload(key: str = "marketing-21-instagram-v2") -> dict:
    return {
        "userId": 4,
        "instagramUsername": "store_owner",
        "instagramPassword": "safe-password-123",
        "content": "승인된 게시물 본문",
        "hashtags": ["#온클릭", "카페"],
        "imageUrls": ["https://cdn.example.com/image.jpg"],
        "idempotencyKey": key,
    }


class InstagramPublishingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        clear_publish_registry()

    def test_mock_provider_publishes_approved_snapshot(self) -> None:
        response = self.client.post(
            "/ai/marketings/21/publish/instagram",
            json=_payload(),
            headers={"X-Internal-Api-Key": "secret"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("PUBLISHED", response.json()["status"])
        self.assertTrue(response.json()["externalPostId"].startswith("browser-mock-"))
        self.assertNotIn("instagramPassword", response.text)
        self.assertNotIn("safe-password-123", response.text)

    def test_duplicate_idempotency_key_is_rejected(self) -> None:
        headers = {"X-Internal-Api-Key": "secret"}
        self.assertEqual(
            200,
            self.client.post(
                "/ai/marketings/21/publish/instagram", json=_payload(), headers=headers
            ).status_code,
        )
        duplicate = self.client.post(
            "/ai/marketings/21/publish/instagram", json=_payload(), headers=headers
        )

        self.assertEqual(409, duplicate.status_code)
        self.assertEqual("DUPLICATE_PUBLISH_REQUEST", duplicate.json()["errorCode"])

    def test_publish_rejects_non_https_image(self) -> None:
        payload = _payload()
        payload["imageUrls"] = ["http://cdn.example.com/image.jpg"]
        response = self.client.post(
            "/ai/marketings/21/publish/instagram",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual("INVALID_INSTAGRAM_POST", response.json()["errorCode"])

    @patch("src.instagram._call_browser_mcp", new_callable=AsyncMock)
    def test_browser_mcp_provider_receives_approved_snapshot(self, call_mcp: AsyncMock) -> None:
        call_mcp.return_value = {
            "status": "PUBLISHED",
            "externalPostId": "post-1",
            "publishedUrl": "https://instagram.com/p/post-1/",
        }
        fake_settings = SimpleNamespace(
            instagram_provider="browser_mcp",
            instagram_publish_timeout_seconds=30,
        )
        with patch("src.instagram.settings", fake_settings):
            result = publish_instagram(21, _payload("browser-mcp-key"))

        self.assertEqual("post-1", result["externalPostId"])
        sent = call_mcp.await_args.args[0]
        self.assertEqual("store_owner", sent["instagramUsername"])
        self.assertEqual("safe-password-123", sent["instagramPassword"])
        self.assertEqual(["https://cdn.example.com/image.jpg"], sent["imageUrls"])

    @patch("src.instagram._call_browser_mcp", new_callable=AsyncMock)
    def test_browser_mcp_empty_result_is_not_treated_as_published(
        self, call_mcp: AsyncMock
    ) -> None:
        call_mcp.return_value = {}
        fake_settings = SimpleNamespace(
            instagram_provider="browser_mcp",
            instagram_publish_timeout_seconds=30,
        )

        with patch("src.instagram.settings", fake_settings):
            with self.assertRaises(BrowserMCPUnavailable):
                publish_instagram(21, _payload("empty-browser-result"))

    @patch("src.instagram._call_browser_mcp", new_callable=AsyncMock)
    def test_browser_mcp_failure_response_includes_reason_code(
        self, call_mcp: AsyncMock
    ) -> None:
        call_mcp.return_value = {}
        fake_settings = SimpleNamespace(
            instagram_provider="browser_mcp",
            instagram_publish_timeout_seconds=30,
        )
        with patch("src.instagram.settings", fake_settings):
            response = self.client.post(
                "/ai/marketings/21/publish/instagram",
                json=_payload("empty-browser-api-result"),
                headers={
                    "X-Internal-Api-Key": "secret",
                    "X-Request-ID": "instagram-browser-failure",
                },
            )

        self.assertEqual(502, response.status_code)
        self.assertEqual("BROWSER_MCP_UNAVAILABLE", response.json()["errorCode"])
        self.assertEqual(
            "BROWSER_MCP_INVALID_RESPONSE",
            response.json()["details"]["reasonCode"],
        )
        self.assertEqual("instagram-browser-failure", response.json()["requestId"])


if __name__ == "__main__":
    unittest.main()

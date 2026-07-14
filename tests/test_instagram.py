from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.ai_service.main import app
from src.instagram import clear_publish_registry, publish_instagram


def _payload(key: str = "marketing-21-instagram-v1") -> dict:
    return {
        "userId": 4,
        "instagramAccountId": "17841400000000000",
        "content": "승인된 게시물 본문",
        "hashtags": ["#온클릭", "카페"],
        "imageUrls": ["https://cdn.example.com/image.jpg"],
        "publishType": "FEED",
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
        body = response.json()
        self.assertEqual("PUBLISHED", body["status"])
        self.assertEqual("INSTAGRAM", body["platform"])
        self.assertTrue(body["externalPostId"].startswith("mock-"))

    def test_duplicate_idempotency_key_is_rejected(self) -> None:
        headers = {"X-Internal-Api-Key": "secret"}
        self.assertEqual(200, self.client.post("/ai/marketings/21/publish/instagram", json=_payload(), headers=headers).status_code)
        duplicate = self.client.post("/ai/marketings/21/publish/instagram", json=_payload(), headers=headers)
        self.assertEqual(409, duplicate.status_code)
        self.assertEqual("DUPLICATE_PUBLISH_REQUEST", duplicate.json()["errorCode"])

    def test_feed_rejects_multiple_images(self) -> None:
        payload = _payload()
        payload["imageUrls"].append("https://cdn.example.com/second.jpg")
        response = self.client.post(
            "/ai/marketings/21/publish/instagram",
            json=payload,
            headers={"X-Internal-Api-Key": "secret"},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("INVALID_INSTAGRAM_POST", response.json()["errorCode"])

    @patch("src.instagram.requests.request")
    def test_meta_provider_creates_then_publishes_media(self, request: Mock) -> None:
        request.side_effect = [
            Mock(ok=True, json=lambda: {"id": "container-1"}),
            Mock(ok=True, json=lambda: {"id": "post-1"}),
            Mock(ok=True, json=lambda: {"id": "post-1", "permalink": "https://instagram.com/p/post-1/"}),
        ]
        fake_settings = SimpleNamespace(
            instagram_provider="meta",
            instagram_graph_base_url="https://graph.facebook.com",
            instagram_graph_api_version="v23.0",
            instagram_publish_timeout_seconds=30,
        )
        with patch("src.instagram.settings", fake_settings):
            result = publish_instagram(21, _payload("meta-key"), access_token="access-token")

        self.assertEqual("post-1", result["externalPostId"])
        self.assertEqual(3, request.call_count)
        self.assertTrue(request.call_args_list[0].args[1].endswith("/17841400000000000/media"))
        self.assertTrue(request.call_args_list[1].args[1].endswith("/17841400000000000/media_publish"))
        self.assertEqual("permalink", request.call_args_list[2].kwargs["params"]["fields"])


if __name__ == "__main__":
    unittest.main()

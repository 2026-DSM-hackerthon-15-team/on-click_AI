from __future__ import annotations

import json
import logging
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.main import app as gateway_app
from src.errors import install_error_handlers
from src.observability import BrowserLogHandler, JsonLogFormatter, browser_logs, install_observability


class ObservabilityTests(unittest.TestCase):
    def test_unhandled_exception_returns_traceable_generic_error(self) -> None:
        app = FastAPI()
        install_observability(app, "test-service")
        install_error_handlers(app)

        @app.get("/boom")
        def boom() -> None:
            raise RuntimeError("database password must never be returned")

        response = TestClient(app, raise_server_exceptions=False).get(
            "/boom", headers={"X-Request-ID": "backend-boom-001"}
        )

        self.assertEqual(500, response.status_code)
        self.assertEqual("INTERNAL_SERVER_ERROR", response.json()["errorCode"])
        self.assertEqual("backend-boom-001", response.json()["requestId"])
        self.assertEqual("backend-boom-001", response.headers["X-Request-ID"])
        self.assertNotIn("database password", response.text)

    def test_json_formatter_emits_searchable_fields(self) -> None:
        formatter = JsonLogFormatter("ai-service")
        record = logging.LogRecord(
            name="on_click.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="upstream.request.failed",
            args=(),
            exc_info=None,
        )
        record.event = "upstream.request.failed"
        record.requestId = "backend-log-002"
        record.upstreamService = "BACKEND_API"
        record.errorCode = "BACKEND_API_CONNECTION_FAILED"

        body = json.loads(formatter.format(record))

        self.assertEqual("ai-service", body["service"])
        self.assertEqual("backend-log-002", body["requestId"])
        self.assertEqual("BACKEND_API", body["upstreamService"])
        self.assertEqual("BACKEND_API_CONNECTION_FAILED", body["errorCode"])

    def test_browser_buffer_allow_lists_fields_and_hides_password(self) -> None:
        record = logging.LogRecord(
            name="on_click.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="browser.test",
            args=(),
            exc_info=None,
        )
        record.event = "browser.test"
        record.requestId = "browser-safe-001"
        record.service = "ai-service"
        record.errorCode = "TEST_ERROR"
        record.instagramPassword = "must-not-appear"
        BrowserLogHandler("ai-service").emit(record)

        rows = browser_logs(request_id="browser-safe-001")

        self.assertEqual("TEST_ERROR", rows[0]["errorCode"])
        self.assertNotIn("instagramPassword", rows[0])
        self.assertNotIn("must-not-appear", str(rows[0]))

    def test_gateway_exposes_key_protected_browser_log_view(self) -> None:
        client = TestClient(gateway_app)
        root = logging.getLogger()
        previous_level = root.level
        previous_handlers = root.handlers[:]
        root.setLevel(logging.INFO)
        for handler in previous_handlers:
            root.removeHandler(handler)
        root.addHandler(BrowserLogHandler("api-gateway"))
        try:
            client.get(
                "/stores",
                headers={
                    "Authorization": "Bearer user-1",
                    "X-Request-ID": "browser-gateway-001",
                },
            )

            denied = client.get("/internal/observability/logs?service=api-gateway")
            response = client.get(
                "/internal/observability/logs?service=api-gateway&requestId=browser-gateway-001",
                headers={"X-Internal-Api-Key": "secret"},
            )
            viewer = client.get("/observability")
        finally:
            for handler in root.handlers[:]:
                root.removeHandler(handler)
            for handler in previous_handlers:
                root.addHandler(handler)
            root.setLevel(previous_level)

        self.assertEqual(401, denied.status_code)
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["sources"][0]["available"])
        self.assertTrue(
            any(row.get("requestId") == "browser-gateway-001" for row in response.json()["logs"])
        )
        self.assertEqual(200, viewer.status_code)
        self.assertIn("X-Internal-Api-Key", viewer.text)
        self.assertIn("/internal/observability/logs", viewer.text)


if __name__ == "__main__":
    unittest.main()

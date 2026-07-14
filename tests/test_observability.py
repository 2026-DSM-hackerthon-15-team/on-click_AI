from __future__ import annotations

import json
import logging
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.errors import install_error_handlers
from src.observability import JsonLogFormatter, install_observability


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


if __name__ == "__main__":
    unittest.main()

"""Runtime configuration shared by the ON:CLICK PoC services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env() -> None:
    """Load the repository .env without overriding process-level variables."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env()


def _ai_api_key() -> str | None:
    return (
        os.getenv("AI_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )


def _ai_provider() -> str:
    configured = os.getenv("AI_PROVIDER")
    if configured:
        return configured.lower()
    key = _ai_api_key() or ""
    return "anthropic" if key.startswith("sk-ant-") else "openai"


def _ai_model() -> str:
    configured = os.getenv("AI_MODEL")
    if configured:
        return configured
    return "claude-sonnet-4-6" if _ai_provider() == "anthropic" else "gpt-4.1-mini"


@dataclass(frozen=True)
class Settings:
    internal_api_key: str = os.getenv("INTERNAL_API_KEY", "secret")
    backend_auth_token: str | None = os.getenv("BACKEND_AUTH_TOKEN")
    api_base_url: str = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    ai_service_url: str = os.getenv("AI_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
    mcp_service_url: str = os.getenv("MCP_SERVICE_URL", "http://127.0.0.1:8002").rstrip("/")
    stats_service_url: str = os.getenv("STATS_SERVICE_URL", "http://127.0.0.1:8003").rstrip("/")
    ai_provider: str = _ai_provider()
    ai_model: str = _ai_model()
    ai_api_key: str | None = _ai_api_key()
    ai_base_url: str | None = os.getenv("AI_BASE_URL")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    ai_request_timeout_seconds: float = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "45"))
    instagram_provider: str = os.getenv("INSTAGRAM_PROVIDER", "mock").lower()
    instagram_graph_base_url: str = os.getenv(
        "INSTAGRAM_GRAPH_BASE_URL", "https://graph.facebook.com"
    ).rstrip("/")
    instagram_graph_api_version: str = os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v23.0")
    instagram_publish_timeout_seconds: float = float(
        os.getenv("INSTAGRAM_PUBLISH_TIMEOUT_SECONDS", "30")
    )


settings = Settings()

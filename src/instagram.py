"""Instagram browser publishing through a Streamable HTTP MCP provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

from src.settings import settings


class InstagramCredentialsInvalid(Exception):
    pass


class InstagramLoginChallengeRequired(Exception):
    pass


class BrowserMCPUnavailable(Exception):
    pass


class InstagramProviderTimeout(Exception):
    pass


_published_keys: set[str] = set()
_registry_lock = threading.Lock()


def clear_publish_registry() -> None:
    """Test helper; production idempotency belongs in a shared persistent store."""
    with _registry_lock:
        _published_keys.clear()


def _mock_publish(marketing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(
        f"{marketing_id}:{payload['idempotencyKey']}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "marketingId": marketing_id,
        "platform": "INSTAGRAM",
        "status": "PUBLISHED",
        "externalPostId": f"browser-mock-{digest}",
        "publishedUrl": f"https://www.instagram.com/p/browser-mock-{digest}/",
        "publishedAt": datetime.now(timezone.utc),
        "failureReason": None,
    }


def _tool_result_body(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _raise_browser_error(body: dict[str, Any]) -> None:
    error_code = str(body.get("errorCode") or body.get("code") or "")
    if error_code == "INSTAGRAM_CREDENTIALS_INVALID":
        raise InstagramCredentialsInvalid
    if error_code == "INSTAGRAM_LOGIN_CHALLENGE_REQUIRED":
        raise InstagramLoginChallengeRequired
    raise BrowserMCPUnavailable


async def _call_browser_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.browser_mcp_url:
        raise BrowserMCPUnavailable
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise BrowserMCPUnavailable from exc

    arguments = {
        "username": payload["instagramUsername"],
        "password": payload["instagramPassword"],
        "content": payload["content"],
        "hashtags": payload.get("hashtags", []),
        "imageUrls": payload["imageUrls"],
    }
    try:
        async with streamable_http_client(settings.browser_mcp_url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    settings.browser_mcp_tool,
                    arguments=arguments,
                )
    except (InstagramCredentialsInvalid, InstagramLoginChallengeRequired):
        raise
    except Exception as exc:
        raise BrowserMCPUnavailable from exc

    body = _tool_result_body(result)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        _raise_browser_error(body)
    if body.get("status") != "PUBLISHED":
        _raise_browser_error(body)
    return body


def _browser_publish(marketing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        body = asyncio.run(
            asyncio.wait_for(
                _call_browser_mcp(payload),
                timeout=settings.instagram_publish_timeout_seconds,
            )
        )
    except asyncio.TimeoutError as exc:
        raise InstagramProviderTimeout from exc
    if body.get("status") != "PUBLISHED":
        _raise_browser_error(body)
    return {
        "marketingId": marketing_id,
        "platform": "INSTAGRAM",
        "status": "PUBLISHED",
        "externalPostId": body.get("externalPostId"),
        "publishedUrl": body.get("publishedUrl"),
        "publishedAt": body.get("publishedAt") or datetime.now(timezone.utc),
        "failureReason": None,
    }


def publish_instagram(marketing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Publish an approved snapshot without logging or persisting credentials."""
    key = str(payload["idempotencyKey"])
    with _registry_lock:
        if key in _published_keys:
            raise KeyError("DUPLICATE_PUBLISH_REQUEST")
        _published_keys.add(key)
    try:
        if settings.instagram_provider == "mock":
            return _mock_publish(marketing_id, payload)
        if settings.instagram_provider == "browser_mcp":
            return _browser_publish(marketing_id, payload)
        raise BrowserMCPUnavailable
    except Exception:
        with _registry_lock:
            _published_keys.discard(key)
        raise

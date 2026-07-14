"""Instagram browser publishing through a Streamable HTTP MCP provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from src.observability import log_event, safe_upstream_target
from src.settings import settings


logger = logging.getLogger("on_click.instagram")


class InstagramCredentialsInvalid(Exception):
    pass


class InstagramLoginChallengeRequired(Exception):
    pass


class BrowserMCPUnavailable(Exception):
    def __init__(
        self,
        reason_code: str = "BROWSER_MCP_UNAVAILABLE",
        *,
        upstream_error_code: str | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.upstream_error_code = upstream_error_code


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
    if not error_code:
        raise BrowserMCPUnavailable("BROWSER_MCP_INVALID_RESPONSE")
    raise BrowserMCPUnavailable(
        "BROWSER_MCP_TOOL_FAILED", upstream_error_code=error_code
    )


async def _call_browser_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    if not settings.browser_mcp_url:
        raise BrowserMCPUnavailable("BROWSER_MCP_NOT_CONFIGURED")
    target = safe_upstream_target(settings.browser_mcp_url)
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise BrowserMCPUnavailable("BROWSER_MCP_CLIENT_NOT_INSTALLED") from exc

    arguments = {
        "username": payload["instagramUsername"],
        "password": payload["instagramPassword"],
        "content": payload["content"],
        "hashtags": payload.get("hashtags", []),
        "images": payload.get("images", []),
    }
    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "browser_mcp.request.started",
        upstreamService="BROWSER_MCP",
        upstreamTarget=target,
        tool=settings.browser_mcp_tool,
        imageCount=len(payload["imageUrls"]),
        hashtagCount=len(payload.get("hashtags", [])),
    )
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
        exception_name = exc.__class__.__name__.lower()
        reason_code = (
            "BROWSER_MCP_CONNECTION_FAILED"
            if any(word in exception_name for word in ("connect", "network", "socket"))
            else "BROWSER_MCP_TOOL_CALL_FAILED"
        )
        log_event(
            logger,
            logging.ERROR,
            "browser_mcp.request.failed",
            upstreamService="BROWSER_MCP",
            upstreamTarget=target,
            tool=settings.browser_mcp_tool,
            errorCode=reason_code,
            exceptionType=exc.__class__.__name__,
            exc_info=True,
        )
        raise BrowserMCPUnavailable(reason_code) from exc

    body = _tool_result_body(result)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        _raise_browser_error(body)
    if body.get("status") != "PUBLISHED":
        _raise_browser_error(body)
    log_event(
        logger,
        logging.INFO,
        "browser_mcp.request.completed",
        upstreamService="BROWSER_MCP",
        upstreamTarget=target,
        tool=settings.browser_mcp_tool,
        latencyMs=round((time.perf_counter() - started) * 1000, 2),
        status="PUBLISHED",
    )
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


def _write_images_to_temp(images: list[dict[str, Any]]) -> list[str]:
    import base64
    import os
    import tempfile

    paths: list[str] = []
    for image in images:
        content_type = str(image.get("contentType", "image/jpeg")).split(";", 1)[0].lower()
        filename = str(image.get("filename", "image.jpg"))
        suffix = ".jpg"
        if content_type == "image/png":
            suffix = ".png"
        elif content_type == "image/webp":
            suffix = ".webp"
        payload = image.get("content")
        if not isinstance(payload, str) or not payload:
            raise ValueError("image content is required")
        raw_bytes = base64.b64decode(payload)
        f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            f.write(raw_bytes)
            f.flush()
        finally:
            f.close()
        paths.append(f.name)
    return paths


def _instagrapi_publish(marketing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Publish using instagrapi library. Downloads images, logs in, uploads, and returns publish info."""
    try:
        from instagrapi import Client
        from instagrapi.exceptions import ClientError, ClientLoginError, TwoFactorRequired, ChallengeRequired
    except ImportError as exc:
        raise BrowserMCPUnavailable("INSTAGRP_CLIENT_NOT_INSTALLED") from exc

    username = payload["instagramUsername"]
    password = payload["instagramPassword"]
    images = payload.get("images", [])
    caption = payload.get("content", "") + " " + " ".join(payload.get("hashtags", []))

    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "instagrapi.publish.started",
        upstreamService="INSTAGRAPI",
        imageCount=len(images),
    )

    temp_paths: list[str] = []
    client = None
    try:
        temp_paths = _write_images_to_temp(images)
        client = Client()
        client.login(username, password)

        result: dict[str, Any]
        if len(temp_paths) == 1:
            result = client.photo_upload(temp_paths[0], caption=caption)
        else:
            result = client.album_upload(temp_paths, caption=caption)

        # instagrapi returns various shapes; try to extract code or pk
        external = result.get("code") or result.get("media_code") or str(result.get("pk") or "")
        published_url = f"https://www.instagram.com/p/{external}/" if external else None
        log_event(
            logger,
            logging.INFO,
            "instagrapi.publish.completed",
            upstreamService="INSTAGRAPI",
            latencyMs=round((time.perf_counter() - started) * 1000, 2),
        )
        return {
            "marketingId": marketing_id,
            "platform": "INSTAGRAM",
            "status": "PUBLISHED",
            "externalPostId": external,
            "publishedUrl": published_url,
            "publishedAt": datetime.now(timezone.utc),
            "failureReason": None,
        }
    except (TwoFactorRequired, ChallengeRequired) as exc:
        raise InstagramLoginChallengeRequired from exc
    except ClientLoginError as exc:
        raise InstagramCredentialsInvalid from exc
    except ClientError as exc:
        # treat client errors as provider unavailability
        raise BrowserMCPUnavailable("INSTAGRP_CLIENT_ERROR") from exc
    except Exception as exc:
        # network/timeout or unexpected
        exception_name = exc.__class__.__name__.lower()
        if "timeout" in exception_name or isinstance(exc, TimeoutError):
            raise InstagramProviderTimeout from exc
        raise BrowserMCPUnavailable("INSTAGRP_PUBLISH_FAILED") from exc
    finally:
        # cleanup temp files
        for p in temp_paths:
            try:
                import os

                os.unlink(p)
            except Exception:
                pass
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


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
        if settings.instagram_provider == "instagrapi":
            return _instagrapi_publish(marketing_id, payload)
        raise BrowserMCPUnavailable("INSTAGRAM_PROVIDER_NOT_SUPPORTED")
    except Exception:
        with _registry_lock:
            _published_keys.discard(key)
        raise

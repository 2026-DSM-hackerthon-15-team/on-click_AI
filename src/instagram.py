"""Instagram publishing providers with deterministic idempotency handling."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from typing import Any

import requests

from src.settings import settings


class InstagramAccountNotConnected(Exception):
    pass


class InstagramProviderError(Exception):
    pass


class InstagramProviderTimeout(Exception):
    pass


_published_keys: set[str] = set()
_registry_lock = threading.Lock()


def clear_publish_registry() -> None:
    """Test helper; production idempotency belongs in a shared persistent store."""
    with _registry_lock:
        _published_keys.clear()


def _caption(payload: dict[str, Any]) -> str:
    tags = []
    for raw in payload.get("hashtags", []):
        value = str(raw).strip()
        if value:
            tags.append(value if value.startswith("#") else f"#{value}")
    return "\n\n".join(part for part in [str(payload["content"]).strip(), " ".join(tags)] if part)


def _mock_publish(marketing_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(
        f"{marketing_id}:{payload['idempotencyKey']}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "marketingId": marketing_id,
        "platform": "INSTAGRAM",
        "status": "PUBLISHED",
        "externalPostId": f"mock-{digest}",
        "publishedUrl": f"https://www.instagram.com/p/mock-{digest}/",
        "publishedAt": datetime.now(timezone.utc),
        "failureReason": None,
    }


def _meta_request(
    method: str,
    path: str,
    *,
    access_token: str,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{settings.instagram_graph_base_url}/{settings.instagram_graph_api_version}/{path.lstrip('/')}"
    request_data = dict(data or {})
    request_params = dict(params or {})
    if method == "POST":
        request_data["access_token"] = access_token
    else:
        request_params["access_token"] = access_token
    try:
        response = requests.request(
            method,
            url,
            data=request_data or None,
            params=request_params or None,
            timeout=settings.instagram_publish_timeout_seconds,
        )
    except requests.Timeout as exc:
        raise InstagramProviderTimeout(str(exc)) from exc
    except requests.RequestException as exc:
        raise InstagramProviderError(str(exc)) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise InstagramProviderError("Instagram provider returned non-JSON data") from exc
    if not response.ok or not isinstance(body, dict):
        raise InstagramProviderError(f"HTTP {response.status_code}: {body}")
    return body


def _meta_publish(
    marketing_id: int,
    payload: dict[str, Any],
    access_token: str | None,
) -> dict[str, Any]:
    if not access_token:
        raise InstagramAccountNotConnected("Instagram access token is missing")
    account_id = payload["instagramAccountId"]
    caption = _caption(payload)
    if payload["publishType"] == "FEED":
        container = _meta_request(
            "POST",
            f"{account_id}/media",
            access_token=access_token,
            data={"image_url": payload["imageUrls"][0], "caption": caption},
        )
        creation_id = container.get("id")
    else:
        child_ids = []
        for image_url in payload["imageUrls"]:
            child = _meta_request(
                "POST",
                f"{account_id}/media",
                access_token=access_token,
                data={"image_url": image_url, "is_carousel_item": "true"},
            )
            if not child.get("id"):
                raise InstagramProviderError("Instagram child container id is missing")
            child_ids.append(child["id"])
        container = _meta_request(
            "POST",
            f"{account_id}/media",
            access_token=access_token,
            data={"media_type": "CAROUSEL", "children": ",".join(child_ids), "caption": caption},
        )
        creation_id = container.get("id")
    if not creation_id:
        raise InstagramProviderError("Instagram media container id is missing")
    published = _meta_request(
        "POST",
        f"{account_id}/media_publish",
        access_token=access_token,
        data={"creation_id": creation_id},
    )
    post_id = published.get("id")
    if not post_id:
        raise InstagramProviderError("Instagram post id is missing")
    detail = _meta_request(
        "GET",
        str(post_id),
        access_token=access_token,
        params={"fields": "permalink"},
    )
    return {
        "marketingId": marketing_id,
        "platform": "INSTAGRAM",
        "status": "PUBLISHED",
        "externalPostId": str(post_id),
        "publishedUrl": detail.get("permalink"),
        "publishedAt": datetime.now(timezone.utc),
        "failureReason": None,
    }


def publish_instagram(
    marketing_id: int,
    payload: dict[str, Any],
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    key = str(payload["idempotencyKey"])
    with _registry_lock:
        if key in _published_keys:
            raise KeyError("DUPLICATE_PUBLISH_REQUEST")
        _published_keys.add(key)
    try:
        if settings.instagram_provider == "mock":
            return _mock_publish(marketing_id, payload)
        if settings.instagram_provider == "meta":
            return _meta_publish(marketing_id, payload, access_token)
        raise InstagramProviderError(f"Unsupported Instagram provider: {settings.instagram_provider}")
    except Exception:
        with _registry_lock:
            _published_keys.discard(key)
        raise

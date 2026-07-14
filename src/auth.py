"""Forward backend-issued Bearer JWTs without storing them in the AI server."""

from __future__ import annotations

from contextvars import ContextVar

from src.errors import api_error


_current_authorization: ContextVar[str | None] = ContextVar("backend_authorization", default=None)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def require_backend_jwt(authorization: str | None) -> str:
    """Require a Bearer token supplied by the backend and retain it for downstream calls.

    JWT issuance and signature validation remain the backend's responsibility.  This
    service never stores or compares a JWT from environment variables.
    """
    token = _bearer_token(authorization)
    if token is None:
        raise api_error(
            401,
            "BACKEND_JWT_REQUIRED",
            "Authorization Bearer JWT가 필요합니다.",
            retryable=False,
        )
    _current_authorization.set(f"Bearer {token}")
    return token


def backend_authorization_header(authorization: str | None = None) -> dict[str, str]:
    """Return the incoming backend JWT for a downstream backend request."""
    token = _bearer_token(authorization) if authorization is not None else _bearer_token(_current_authorization.get())
    if token is None:
        raise api_error(
            401,
            "BACKEND_JWT_REQUIRED",
            "백엔드 JWT 없이 내부 요청을 수행할 수 없습니다.",
            retryable=False,
        )
    return {"Authorization": f"Bearer {token}"}


def is_backend_jwt(authorization: str | None) -> bool:
    """Whether an incoming request carries a backend Bearer JWT."""
    token = _bearer_token(authorization)
    # `user-<id>` is the local mock's user-session convention, not a JWT.
    return bool(token and not token.startswith("user-"))

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from talap.core.config import Settings, get_settings

_API_KEY_HEADER = APIKeyHeader(
    name="X-Internal-Service-Token",
    auto_error=False,
)

_INVALID_TOKEN_DETAIL = "Invalid internal service token."
_UNCONFIGURED_DETAIL = "Internal service authentication is not configured."


async def require_internal_service_token(
    api_key: str | None = Security(_API_KEY_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate internal service calls via ``X-Internal-Service-Token``."""
    configured_token = settings.internal_service_token
    if configured_token is None or not configured_token.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_UNCONFIGURED_DETAIL,
        )
    if api_key is None or not secrets.compare_digest(
        api_key,
        configured_token.get_secret_value(),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_TOKEN_DETAIL,
        )

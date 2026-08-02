"""Telegram webhook endpoint.

Pipeline: verify secret → read exact raw body once → parse JSON → normalize
(T-019) → persist + enqueue job (T-021) → HTTP 200. No outbound calls, no AI,
no worker execution inside the request.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies.database import get_api_session_factory
from talap.channels.telegram import (
    TelegramNormalizationError,
    TelegramWebhookAuthenticationError,
    TelegramWebhookServiceUnavailableError,
    normalize_telegram_update,
    verify_telegram_webhook_request,
)
from talap.ingestion import (
    ChannelConnectionInactiveError,
    ChannelConnectionMismatchError,
    ChannelConnectionNotFoundError,
    InboundIngestionExecutionError,
    InboundIngestionValidationError,
    ingest_normalized_webhook,
)

router = APIRouter(tags=["telegram-webhook"])

_TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_INVALID_CREDENTIALS_DETAIL = "Invalid Telegram webhook credentials."
_INVALID_PAYLOAD_DETAIL = "Invalid Telegram webhook payload."
_INVALID_UPDATE_DETAIL = "Invalid Telegram webhook update."
_UNAVAILABLE_DETAIL = "Telegram webhook is temporarily unavailable."


@router.post("/webhooks/telegram/{connection_id}")
async def telegram_webhook(
    connection_id: UUID,
    request: Request,
    telegram_secret: str | None = Header(default=None, alias=_TELEGRAM_SECRET_HEADER),
    _session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_api_session_factory
    ),
) -> Response:
    try:
        await verify_telegram_webhook_request(
            connection_id=connection_id,
            provided_secret=telegram_secret,
            session_factory=_session_factory,
        )
    except TelegramWebhookAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS_DETAIL,
        ) from exc
    except TelegramWebhookServiceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_UNAVAILABLE_DETAIL,
        ) from exc

    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_PAYLOAD_DETAIL,
        )
    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_PAYLOAD_DETAIL,
        ) from exc
    if not isinstance(payload, Mapping):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_PAYLOAD_DETAIL,
        )

    received_at = datetime.now(UTC)

    try:
        normalized_message = normalize_telegram_update(
            payload,
            received_at=received_at,
        )
    except TelegramNormalizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_UPDATE_DETAIL,
        ) from exc

    try:
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="telegram",
            raw_body=raw_body,
            payload=payload,
            received_at=received_at,
            messages=(normalized_message,),
            whatsapp_statuses=(),
            session_factory=_session_factory,
        )
    except (
        ChannelConnectionNotFoundError,
        ChannelConnectionInactiveError,
        ChannelConnectionMismatchError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS_DETAIL,
        ) from exc
    except InboundIngestionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_UPDATE_DETAIL,
        ) from exc
    except InboundIngestionExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_UNAVAILABLE_DETAIL,
        ) from exc

    return Response(status_code=status.HTTP_200_OK)

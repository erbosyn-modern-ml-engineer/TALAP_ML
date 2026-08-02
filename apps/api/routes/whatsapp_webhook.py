"""WhatsApp Cloud API webhook endpoints (MVP, single-account).

GET  /webhooks/whatsapp  → Meta challenge verification
POST /webhooks/whatsapp  → verify x-hub-signature-256 → normalize (T-020) →
                           persist via T-021 → HTTP 200.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies.database import get_api_session_factory
from talap.channels.whatsapp import (
    WhatsAppNormalizationError,
    WhatsAppWebhookSecurityError,
    normalize_whatsapp_webhook,
    verify_whatsapp_challenge_token,
    verify_whatsapp_signature,
)
from talap.core.config import Settings, get_settings
from talap.ingestion import (
    ChannelConnectionInactiveError,
    ChannelConnectionMismatchError,
    ChannelConnectionNotFoundError,
    InboundIngestionExecutionError,
    InboundIngestionValidationError,
    ingest_normalized_webhook,
)

router = APIRouter(tags=["whatsapp-webhook"])

_SIGNATURE_HEADER = "x-hub-signature-256"
_INVALID_SIGNATURE_DETAIL = "Invalid WhatsApp webhook signature."
_INVALID_PAYLOAD_DETAIL = "Invalid WhatsApp webhook payload."
_INVALID_UPDATE_DETAIL = "Invalid WhatsApp webhook update."
_UNAVAILABLE_DETAIL = "WhatsApp webhook is temporarily unavailable."
_INVALID_VERIFICATION_DETAIL = "Invalid WhatsApp webhook verification request."


@router.get("/webhooks/whatsapp")
async def whatsapp_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> Response:
    expected_token = (
        settings.whatsapp_verify_token.get_secret_value()
        if settings.whatsapp_verify_token is not None
        else None
    )
    if (
        hub_mode != "subscribe"
        or expected_token is None
        or hub_verify_token is None
        or hub_challenge is None
        or not verify_whatsapp_challenge_token(
            provided_token=hub_verify_token,
            expected_token=expected_token,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_INVALID_VERIFICATION_DETAIL,
        )
    return PlainTextResponse(hub_challenge, status_code=status.HTTP_200_OK)


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    signature_header: str | None = Header(default=None, alias=_SIGNATURE_HEADER),
    _session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_api_session_factory
    ),
    settings: Settings = Depends(get_settings),
) -> Response:
    app_secret = (
        settings.whatsapp_app_secret.get_secret_value()
        if settings.whatsapp_app_secret is not None
        else None
    )
    connection_id = settings.whatsapp_connection_id
    if app_secret is None or connection_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_UNAVAILABLE_DETAIL,
        )

    raw_body = await request.body()

    if signature_header is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_SIGNATURE_DETAIL,
        )
    try:
        signature_valid = verify_whatsapp_signature(
            raw_body=raw_body,
            signature_header=signature_header,
            app_secret=app_secret,
        )
    except WhatsAppWebhookSecurityError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_SIGNATURE_DETAIL,
        ) from exc
    if not signature_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_SIGNATURE_DETAIL,
        )

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
        normalized = normalize_whatsapp_webhook(
            payload,
            received_at=received_at,
        )
    except WhatsAppNormalizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_UPDATE_DETAIL,
        ) from exc

    try:
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="whatsapp",
            raw_body=raw_body,
            payload=payload,
            received_at=received_at,
            messages=normalized.messages,
            whatsapp_statuses=normalized.statuses,
            session_factory=_session_factory,
        )
    except (
        ChannelConnectionNotFoundError,
        ChannelConnectionInactiveError,
        ChannelConnectionMismatchError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_UNAVAILABLE_DETAIL,
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

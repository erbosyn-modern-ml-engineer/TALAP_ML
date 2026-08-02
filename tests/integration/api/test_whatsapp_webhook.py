"""Real-PostgreSQL integration tests for the WhatsApp webhook endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import talap.ingestion.service as ingestion_service
from apps.api.dependencies.database import get_api_session_factory
from apps.api.main import app
from talap.core.config import Settings, get_settings
from talap.db.models import (
    ChannelConnection,
    InboundEvent,
    InboundMessage,
    MessageProcessingJob,
    WhatsAppDeliveryStatus,
)

_VERIFY_TOKEN = "wa-verify-token"
_APP_SECRET = "wa-app-secret-123"
_SIGNATURE_HEADER = "x-hub-signature-256"

_INVALID_SIGNATURE = "Invalid WhatsApp webhook signature."
_INVALID_PAYLOAD = "Invalid WhatsApp webhook payload."
_INVALID_UPDATE = "Invalid WhatsApp webhook update."
_UNAVAILABLE = "WhatsApp webhook is temporarily unavailable."
_INVALID_VERIFICATION = "Invalid WhatsApp webhook verification request."


def _signature(raw_body: bytes, secret: str = _APP_SECRET) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def _text_webhook_raw(*, message_id: str, sender: str, body: str) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "999999999999999",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "100000000000001",
                            },
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1783022400",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _status_webhook_raw(*, status_id: str, recipient_id: str) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "999999999999999",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "100000000000001",
                            },
                            "statuses": [
                                {
                                    "id": status_id,
                                    "recipient_id": recipient_id,
                                    "status": "delivered",
                                    "timestamp": "1783022403",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


@pytest_asyncio.fixture
async def api_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[httpx.AsyncClient, UUID]]:
    connection = ChannelConnection(channel="whatsapp", name="wa-mvp", active=True)
    async with session_factory() as session:
        session.add(connection)
        await session.commit()
        connection_id = connection.id
    test_settings = Settings(
        whatsapp_connection_id=connection_id,
        whatsapp_verify_token=_VERIFY_TOKEN,
        whatsapp_app_secret=_APP_SECRET,
    )
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_api_session_factory] = lambda: session_factory
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, connection_id
    finally:
        app.dependency_overrides.clear()


# ── GET verification ────────────────────────────────────────────────────


async def test_get_correct_verification_returns_exact_challenge(
    api_client: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = api_client
    challenge = "challenge-123456"
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": _VERIFY_TOKEN,
            "hub.challenge": challenge,
        },
    )
    assert response.status_code == 200
    assert response.text == challenge
    assert "text/plain" in response.headers["content-type"]


async def test_get_wrong_token_returns_403(
    api_client: tuple[httpx.AsyncClient, UUID],
) -> None:
    client, _ = api_client
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "challenge-123456",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == _INVALID_VERIFICATION


# ── POST webhook ────────────────────────────────────────────────────────


async def test_valid_signed_text_webhook_returns_200(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_TEXT_0001",
        sender="77000000001",
        body="Show me blue sneakers",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


async def test_event_message_and_pending_job_persisted(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, connection_id = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_TEXT_0002",
        sender="77000000002",
        body="Persist me",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 200
    async with session_factory() as session:
        event = (
            await session.execute(
                select(InboundEvent).where(
                    InboundEvent.connection_id == connection_id
                )
            )
        ).scalar_one()
        assert event.channel == "whatsapp"
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id
                    == "wamid.SYNTHETIC_TEXT_0002"
                )
            )
        ).scalar_one()
        assert message.message_type == "text"
        assert message.text == "Persist me"
        assert message.external_chat_id == "77000000002"
        assert message.external_user_id == "77000000002"
        job = (
            await session.execute(
                select(MessageProcessingJob).where(
                    MessageProcessingJob.message_id == message.id
                )
            )
        ).scalar_one()
        assert job.status.value == "pending"


async def test_status_only_webhook_persists_status_without_message(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = _status_webhook_raw(
        status_id="wamid.SYNTHETIC_STATUS_0001",
        recipient_id="77000000003",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 1


async def test_missing_signature_returns_401_zero_writes(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_401_1",
        sender="77000000004",
        body="No signature",
    )
    response = await client.post("/webhooks/whatsapp", content=raw)
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_SIGNATURE
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_wrong_signature_returns_401_zero_writes(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_401_2",
        sender="77000000005",
        body="Wrong signature",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw, secret="other-secret")},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_SIGNATURE
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_invalid_json_with_valid_signature_returns_400(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = b"this is not json"
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_PAYLOAD
    assert await _count(session_factory, InboundEvent) == 0


async def test_malformed_whatsapp_update_returns_400(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "999999999999999",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messages": [
                                    {"from": "77000000006", "type": "text"}
                                ]
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_UPDATE
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_exact_webhook_twice_is_idempotent(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_DUP_1",
        sender="77000000007",
        body="Twice",
    )
    headers = {_SIGNATURE_HEADER: _signature(raw)}
    first = await client.post("/webhooks/whatsapp", content=raw, headers=headers)
    second = await client.post("/webhooks/whatsapp", content=raw, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


async def test_same_external_message_different_raw_json(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = api_client
    raw1 = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_SAME_1",
        sender="77000000008",
        body="First body",
    )
    raw2 = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_SAME_1",
        sender="77000000008",
        body="Second body",
    )
    assert raw1 != raw2
    first = await client.post(
        "/webhooks/whatsapp",
        content=raw1,
        headers={_SIGNATURE_HEADER: _signature(raw1)},
    )
    second = await client.post(
        "/webhooks/whatsapp",
        content=raw2,
        headers={_SIGNATURE_HEADER: _signature(raw2)},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert await _count(session_factory, InboundEvent) == 2
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


async def test_injected_ingestion_failure_returns_503_no_partial_writes(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(
        session: AsyncSession, *, message_ids: list[UUID]
    ) -> int:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(ingestion_service, "_insert_processing_jobs", _boom)
    client, _ = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_503_1",
        sender="77000000009",
        body="Fail",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == _UNAVAILABLE
    assert "RuntimeError" not in response.text
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0


async def test_stored_payload_sha256_matches_exact_request_bytes(
    api_client: tuple[httpx.AsyncClient, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, connection_id = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_HASH_1",
        sender="77000000010",
        body="Hash me",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 200
    async with session_factory() as session:
        event = (
            await session.execute(
                select(InboundEvent).where(
                    InboundEvent.connection_id == connection_id
                )
            )
        ).scalar_one()
        assert event.payload_sha256 == hashlib.sha256(raw).hexdigest()


def test_openapi_contains_whatsapp_webhook_paths() -> None:
    openapi = app.openapi()
    paths = openapi["paths"]
    assert "/webhooks/whatsapp" in paths
    assert "get" in paths["/webhooks/whatsapp"]
    assert "post" in paths["/webhooks/whatsapp"]
    # No /api/v1 webhook paths.
    assert "/api/v1/webhooks/whatsapp" not in paths


async def test_no_outbound_http_request_occurs(
    api_client: tuple[httpx.AsyncClient, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbid_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("outbound HTTP client must not be created")

    monkeypatch.setattr(httpx, "AsyncClient", _forbid_client)
    client, _ = api_client
    raw = _text_webhook_raw(
        message_id="wamid.SYNTHETIC_NET_1",
        sender="77000000011",
        body="No network",
    )
    response = await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )
    assert response.status_code == 200
    assert response.content == b""

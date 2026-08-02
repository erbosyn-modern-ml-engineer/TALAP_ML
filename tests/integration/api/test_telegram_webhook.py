"""Real-PostgreSQL integration tests for the Telegram webhook endpoint."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import talap.ingestion.service as ingestion_service
from apps.api.dependencies.database import get_api_session_factory
from apps.api.main import app
from talap.channels.telegram import telegram_webhook_secret_sha256
from talap.core.config import Settings, get_settings
from talap.db.models import (
    ChannelConnection,
    InboundEvent,
    InboundMessage,
    MessageProcessingJob,
    TelegramWebhookConfig,
)

_SECRET = "tg-test-secret-123"
_AUTH_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
_INVALID_CREDENTIALS = "Invalid Telegram webhook credentials."
_INVALID_PAYLOAD = "Invalid Telegram webhook payload."
_INVALID_UPDATE = "Invalid Telegram webhook update."
_UNAVAILABLE = "Telegram webhook is temporarily unavailable."

_URL_PREFIX = "/webhooks/telegram"


# ── Helpers ─────────────────────────────────────────────────────────────


async def _create_connection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channel: str = "telegram",
    name: str = "tg-bot",
    active: bool = True,
) -> UUID:
    connection = ChannelConnection(channel=channel, name=name, active=active)
    async with session_factory() as session:
        session.add(connection)
        await session.commit()
        return connection.id


async def _create_config(
    session_factory: async_sessionmaker[AsyncSession],
    connection_id: UUID,
    *,
    secret: str = _SECRET,
) -> None:
    config = TelegramWebhookConfig(
        connection_id=connection_id,
        webhook_secret_sha256=telegram_webhook_secret_sha256(secret),
    )
    async with session_factory() as session:
        session.add(config)
        await session.commit()


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


def _text_message(
    *,
    message_id: int = 41001,
    chat_id: int = 771100001,
    user_id: int = 771100001,
    text: str = "Show me blue sneakers",
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": chat_id},
        "text": text,
    }


def _voice_message(
    *,
    message_id: int = 41002,
    chat_id: int = 771100002,
    user_id: int = 771100002,
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": chat_id},
        "voice": {
            "file_id": "VoiceFileIdDemo001",
            "mime_type": "audio/ogg",
            "duration": 3,
            "file_size": 25121,
        },
    }


def _callback_query(
    *,
    callback_id: str = "722200001",
    user_id: int = 771100003,
    chat_id: int = 771100003,
    data: str = "catalog-shoe-blue-sneakers",
) -> dict[str, object]:
    return {
        "id": callback_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}},
        "chat_instance": "3829450123456789",
        "data": data,
    }


def _sticker_message(
    *,
    message_id: int = 41004,
    chat_id: int = 771100004,
    user_id: int = 771100004,
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": chat_id},
        "sticker": {"file_id": "StickerFileIdDemo001", "type": "regular"},
    }


def _raw_update(
    *,
    update_id: int = 1000001,
    message: dict[str, object] | None = None,
    callback_query: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> bytes:
    payload: dict[str, object] = {"update_id": update_id}
    if message is not None:
        payload["message"] = message
    if callback_query is not None:
        payload["callback_query"] = callback_query
    if extra is not None:
        payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@pytest_asyncio.fixture
async def api_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    test_settings = Settings(internal_service_token="api-test-token")
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_api_session_factory] = lambda: session_factory
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# ── A. Correct secret + text fixture ────────────────────────────────────


async def test_correct_secret_text_message(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(message=_text_message())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=raw,
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1

    async with session_factory() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id
                    == "message:771100001:41001"
                )
            )
        ).scalar_one()
        assert message.message_type == "text"
        assert message.text == "Show me blue sneakers"
        assert message.channel == "telegram"
        assert message.external_chat_id == "771100001"
        assert message.external_user_id == "771100001"
        job = (
            await session.execute(
                select(MessageProcessingJob).where(
                    MessageProcessingJob.message_id == message.id
                )
            )
        ).scalar_one()
        assert job.status.value == "pending"


# ── B–G. Authentication failures ────────────────────────────────────────


async def test_missing_secret_header_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=_raw_update(message=_text_message()),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0


async def test_wrong_secret_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=_raw_update(message=_text_message()),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_unknown_connection_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await api_client.post(
        f"{_URL_PREFIX}/{uuid4()}",
        content=_raw_update(message=_text_message()),
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_connection_without_config_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=_raw_update(message=_text_message()),
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_inactive_connection_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory, active=False)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=_raw_update(message=_text_message()),
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_whatsapp_connection_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number"
    )
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=_raw_update(message=_text_message()),
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


# ── H–J. Malformed bodies ───────────────────────────────────────────────


async def test_invalid_json_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=b"this is not json",
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_PAYLOAD
    assert await _count(session_factory, InboundEvent) == 0


async def test_json_top_level_array_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=b'["not", "a", "mapping"]',
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_PAYLOAD
    assert await _count(session_factory, InboundEvent) == 0


async def test_malformed_telegram_update_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(update_id=1, extra={"message": {"message_id": 1}})
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=raw,
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_UPDATE
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_empty_body_rejected(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        content=b"",
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_PAYLOAD
    assert await _count(session_factory, InboundEvent) == 0


# ── K. Exact duplicate delivery ─────────────────────────────────────────


async def test_exact_webhook_sent_twice(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(message=_text_message())
    first = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    second = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


# ── L. Same external message in different raw bytes ─────────────────────


async def test_same_external_message_different_raw_bytes(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw1 = _raw_update(
        update_id=1000001,
        message=_text_message(text="First body"),
    )
    raw2 = _raw_update(
        update_id=1000002,
        message=_text_message(text="Second body"),
    )
    assert raw1 != raw2
    first = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw1, headers=_AUTH_HEADERS
    )
    second = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw2, headers=_AUTH_HEADERS
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert await _count(session_factory, InboundEvent) == 2
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


# ── M–O. Voice / callback / sticker fixtures ────────────────────────────


async def test_voice_fixture_persists_media(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(update_id=1000002, message=_voice_message())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    assert response.status_code == 200
    async with session_factory() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id
                    == "message:771100002:41002"
                )
            )
        ).scalar_one()
        assert message.message_type == "voice"
        assert message.text is None
        assert message.media_external_id == "VoiceFileIdDemo001"
        assert message.media_mime_type == "audio/ogg"
        assert message.media_duration_seconds == 3
        assert message.media_size_bytes == 25121


async def test_callback_fixture_persisted(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(update_id=1000003, callback_query=_callback_query())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    assert response.status_code == 200
    async with session_factory() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id == "callback:722200001"
                )
            )
        ).scalar_one()
        assert message.message_type == "text"
        assert message.text == "catalog-shoe-blue-sneakers"
        assert message.external_chat_id == "771100003"
        assert message.external_user_id == "771100003"


async def test_sticker_fixture_is_unsupported(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(update_id=1000004, message=_sticker_message())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    assert response.status_code == 200
    async with session_factory() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id
                    == "message:771100004:41004"
                )
            )
        ).scalar_one()
        assert message.message_type == "unsupported"
        assert message.text is None
        assert message.media_external_id is None
    assert await _count(session_factory, MessageProcessingJob) == 1


# ── P. Exact raw-body hash ──────────────────────────────────────────────


async def test_exact_raw_body_hash_stored(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(message=_text_message())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
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


# ── Q. Injected DB failure → 503 ────────────────────────────────────────


async def test_injected_db_failure_returns_503(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(session: AsyncSession, *, message_ids: list[UUID]) -> int:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(ingestion_service, "_insert_processing_jobs", _boom)
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(message=_text_message())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    assert response.status_code == 503
    assert response.json()["detail"] == _UNAVAILABLE
    assert "RuntimeError" not in response.text
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0


# ── R. Query parameter does not authenticate ────────────────────────────


async def test_query_secret_does_not_authenticate(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}",
        params={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        content=_raw_update(message=_text_message()),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == _INVALID_CREDENTIALS
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


# ── S. Route registration / OpenAPI ─────────────────────────────────────


def test_route_registration_openapi() -> None:
    openapi = app.openapi()
    paths = set(openapi["paths"].keys())
    assert "/webhooks/telegram/{connection_id}" in paths
    assert "/api/v1/webhooks/telegram/{connection_id}" not in paths

    operation = openapi["paths"]["/webhooks/telegram/{connection_id}"]["post"]
    path_params = [p for p in operation["parameters"] if p["in"] == "path"]
    assert [p["name"] for p in path_params] == ["connection_id"]
    header_params = [p for p in operation["parameters"] if p["in"] == "header"]
    assert any(
        p["name"] == "X-Telegram-Bot-Api-Secret-Token" for p in header_params
    )
    # No internal-service-token security requirement.
    assert "security" not in operation or not operation["security"]


# ── T. Response contract ────────────────────────────────────────────────


async def test_response_contract_is_empty_200(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    await _create_config(session_factory, connection_id)
    raw = _raw_update(message=_text_message())
    response = await api_client.post(
        f"{_URL_PREFIX}/{connection_id}", content=raw, headers=_AUTH_HEADERS
    )
    assert response.status_code == 200
    assert response.content == b""
    # No internal ids or ingestion summary leaked in headers.
    assert "Location" not in response.headers
    assert "x-inbound-event-id" not in response.headers
    body_lower = response.text.lower()
    assert "inbound" not in body_lower
    assert "summary" not in body_lower

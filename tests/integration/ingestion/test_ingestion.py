"""Real-PostgreSQL integration tests for T-021 inbound ingestion."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import talap.ingestion.service as service_module
from talap.channels import NormalizedInboundMessage
from talap.channels.telegram import normalize_telegram_update
from talap.channels.whatsapp import (
    WhatsAppDeliveryStatusEvent,
    normalize_whatsapp_webhook,
)
from talap.db.models import (
    ChannelConnection,
    InboundEvent,
    InboundMessage,
    MessageProcessingJob,
    WhatsAppDeliveryStatus,
)
from talap.ingestion import (
    ChannelConnectionInactiveError,
    ChannelConnectionMismatchError,
    ChannelConnectionNotFoundError,
    InboundIngestionExecutionError,
    InboundIngestionValidationError,
    ingest_normalized_webhook,
)
from talap.ingestion.fingerprints import payload_sha256

_RECEIVED = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


# ── Helpers ─────────────────────────────────────────────────────────────


async def _create_connection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channel: str = "telegram",
    name: str = "demo-connection",
    active: bool = True,
) -> UUID:
    connection = ChannelConnection(channel=channel, name=name, active=active)
    async with session_factory() as session:
        session.add(connection)
        await session.commit()
        return connection.id


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


def _telegram_text_update(
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
    text: str,
) -> tuple[bytes, dict[str, object], NormalizedInboundMessage]:
    payload = {
        "update_id": 1000001,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    parsed = json.loads(raw)
    message = normalize_telegram_update(parsed, received_at=_RECEIVED)
    return raw, parsed, message


def _telegram_voice_update(
    *,
    message_id: int,
    chat_id: int,
    user_id: int,
) -> tuple[bytes, dict[str, object], NormalizedInboundMessage]:
    payload = {
        "update_id": 1000002,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "voice": {
                "file_id": "VoiceFileIdDemo001",
                "mime_type": "audio/ogg",
                "duration": 3,
                "file_size": 25121,
            },
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    parsed = json.loads(raw)
    message = normalize_telegram_update(parsed, received_at=_RECEIVED)
    return raw, parsed, message


def _whatsapp_status_webhook(
    *,
    status_id: str,
    recipient_id: str,
    status: str,
    timestamp: str,
    errors: list[dict[str, object]] | None = None,
    extra_value: dict[str, object] | None = None,
) -> tuple[bytes, dict[str, object], list[WhatsAppDeliveryStatusEvent]]:
    status_object: dict[str, object] = {
        "id": status_id,
        "recipient_id": recipient_id,
        "status": status,
        "timestamp": timestamp,
    }
    if errors is not None:
        status_object["errors"] = errors
    value: dict[str, object] = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "15550000000",
            "phone_number_id": "100000000000001",
        },
        "statuses": [status_object],
    }
    if extra_value is not None:
        value.update(extra_value)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "999999999999999",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    parsed = json.loads(raw)
    result = normalize_whatsapp_webhook(parsed, received_at=_RECEIVED)
    return raw, parsed, list(result.statuses)


def _whatsapp_mixed_webhook(
    *,
    message_id: str,
    sender: str,
    text: str,
    status_id: str,
    status: str,
) -> tuple[
    bytes,
    dict[str, object],
    list[NormalizedInboundMessage],
    list[WhatsAppDeliveryStatusEvent],
]:
    value: dict[str, object] = {
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
                "text": {"body": text},
            }
        ],
        "statuses": [
            {
                "id": status_id,
                "recipient_id": sender,
                "status": status,
                "timestamp": "1783022401",
            }
        ],
    }
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "999999999999999",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    parsed = json.loads(raw)
    result = normalize_whatsapp_webhook(parsed, received_at=_RECEIVED)
    return raw, parsed, list(result.messages), list(result.statuses)


# ── A. Unknown connection ───────────────────────────────────────────────


async def test_unknown_connection_raises_typed_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ChannelConnectionNotFoundError):
        await ingest_normalized_webhook(
            connection_id=uuid4(),
            channel="telegram",
            raw_body=b"{}",
            payload={},
            received_at=_RECEIVED,
            session_factory=session_factory,
        )
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 0


# ── B. Inactive connection ──────────────────────────────────────────────


async def test_inactive_connection_raises_typed_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="telegram", active=False
    )
    with pytest.raises(ChannelConnectionInactiveError):
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="telegram",
            raw_body=b"{}",
            payload={},
            received_at=_RECEIVED,
            session_factory=session_factory,
        )
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0


# ── C. Channel mismatch ─────────────────────────────────────────────────


async def test_channel_mismatch_raises_typed_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="telegram", name="telegram-bot"
    )
    with pytest.raises(ChannelConnectionMismatchError):
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="whatsapp",
            raw_body=b"{}",
            payload={},
            received_at=_RECEIVED,
            session_factory=session_factory,
        )
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0


# ── D. Empty valid webhook ──────────────────────────────────────────────


async def test_empty_valid_webhook_creates_only_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=b'{"object":"empty"}',
        payload={"object": "empty"},
        received_at=_RECEIVED,
        session_factory=session_factory,
    )
    assert summary.event_created is True
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 0


# ── E. First Telegram message ───────────────────────────────────────────


async def test_first_telegram_message_persists_all_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw, payload, message = _telegram_text_update(
        message_id=41001, chat_id=771100001, user_id=771100001,
        text="Show me blue sneakers",
    )
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    assert summary.event_created is True
    assert summary.messages_created == 1
    assert summary.processing_jobs_created == 1

    async with session_factory() as session:
        row = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id == "message:771100001:41001"
                )
            )
        ).scalar_one()
        assert row.connection_id == connection_id
        assert row.channel == "telegram"
        assert row.business_scope == "talap_global"
        assert row.external_chat_id == "771100001"
        assert row.external_user_id == "771100001"
        assert row.message_type == "text"
        assert row.text == "Show me blue sneakers"
        assert row.media_external_id is None
        assert row.media_mime_type is None
        assert row.media_size_bytes is None
        assert row.media_duration_seconds is None
        assert row.media_checksum_sha256 is None

        job = (
            await session.execute(
                select(MessageProcessingJob).where(
                    MessageProcessingJob.message_id == row.id
                )
            )
        ).scalar_one()
        assert job.status.value == "pending"
        assert job.attempts == 0


# ── F. Exact Telegram webhook sent twice ────────────────────────────────


async def test_exact_webhook_sent_twice_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw, payload, message = _telegram_text_update(
        message_id=41002, chat_id=771100002, user_id=771100002,
        text="Hello twice",
    )
    first = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    second = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    assert first.event_created is True
    assert second.event_created is False
    assert second.messages_created == 0
    assert second.messages_duplicate == 1
    assert second.processing_jobs_created == 0
    assert second.statuses_created == 0
    assert first.inbound_event_id == second.inbound_event_id

    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


# ── G. Same external message in a different raw webhook ─────────────────


async def test_same_external_message_in_different_webhook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw1, payload1, message1 = _telegram_text_update(
        message_id=41003, chat_id=771100003, user_id=771100003,
        text="First body",
    )
    raw2, payload2, message2 = _telegram_text_update(
        message_id=41003, chat_id=771100003, user_id=771100003,
        text="Second body",
    )
    assert raw1 != raw2
    assert message1.external_message_id == message2.external_message_id

    await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw1,
        payload=payload1,
        received_at=_RECEIVED,
        messages=[message1],
        session_factory=session_factory,
    )
    second = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw2,
        payload=payload2,
        received_at=_RECEIVED,
        messages=[message2],
        session_factory=session_factory,
    )
    assert second.event_created is True
    assert second.messages_created == 0
    assert second.messages_duplicate == 1
    assert second.processing_jobs_created == 0

    assert await _count(session_factory, InboundEvent) == 2
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


# ── H. Two Telegram messages in one batch ───────────────────────────────


async def test_two_messages_in_one_batch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw, payload, message1 = _telegram_text_update(
        message_id=41004, chat_id=771100004, user_id=771100004,
        text="One",
    )
    _, _, message2 = _telegram_text_update(
        message_id=41005, chat_id=771100004, user_id=771100004,
        text="Two",
    )
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message1, message2],
        session_factory=session_factory,
    )
    assert summary.messages_created == 2
    assert summary.messages_duplicate == 0
    assert summary.processing_jobs_created == 2
    assert await _count(session_factory, InboundMessage) == 2
    assert await _count(session_factory, MessageProcessingJob) == 2


# ── I. Duplicate message repeated inside one batch ──────────────────────


async def test_duplicate_message_inside_one_batch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw, payload, message1 = _telegram_text_update(
        message_id=41006, chat_id=771100006, user_id=771100006,
        text="Dup",
    )
    _, _, message2 = _telegram_text_update(
        message_id=41006, chat_id=771100006, user_id=771100006,
        text="Dup",
    )
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message1, message2],
        session_factory=session_factory,
    )
    assert summary.messages_created == 1
    assert summary.messages_duplicate == 1
    assert summary.processing_jobs_created == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1


# ── J. Same external message ID on two different connections ────────────


async def test_same_external_message_on_two_connections(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_a = await _create_connection(
        session_factory, channel="telegram", name="bot-a"
    )
    connection_b = await _create_connection(
        session_factory, channel="telegram", name="bot-b"
    )
    raw, payload, message = _telegram_text_update(
        message_id=41007, chat_id=771100007, user_id=771100007,
        text="Scoped identity",
    )
    summary_a = await ingest_normalized_webhook(
        connection_id=connection_a,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    summary_b = await ingest_normalized_webhook(
        connection_id=connection_b,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    assert summary_a.messages_created == 1
    assert summary_b.messages_created == 1
    assert summary_b.messages_duplicate == 0
    assert await _count(session_factory, InboundMessage) == 2
    assert await _count(session_factory, MessageProcessingJob) == 2
    async with session_factory() as session:
        connection_ids = set(
            (
                await session.execute(
                    select(InboundMessage.connection_id).where(
                        InboundMessage.external_message_id
                        == "message:771100007:41007"
                    )
                )
            ).scalars()
        )
    assert connection_ids == {connection_a, connection_b}


# ── K. WhatsApp status-only webhook ─────────────────────────────────────


async def test_whatsapp_status_only_webhook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number-1"
    )
    raw, payload, statuses = _whatsapp_status_webhook(
        status_id="wamid.SYNTHETIC_STATUS_0001",
        recipient_id="77000000001",
        status="delivered",
        timestamp="1783022403",
    )
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        whatsapp_statuses=statuses,
        session_factory=session_factory,
    )
    assert summary.event_created is True
    assert summary.statuses_created == 1
    assert summary.messages_created == 0
    assert summary.processing_jobs_created == 0
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 1


# ── L. Same status delivered twice in different webhook bodies ──────────


async def test_same_status_in_different_bodies_is_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number-2"
    )
    raw1, payload1, statuses1 = _whatsapp_status_webhook(
        status_id="wamid.SYNTHETIC_STATUS_0002",
        recipient_id="77000000002",
        status="failed",
        timestamp="1783022404",
        errors=[{"code": 131047}],
    )
    raw2, payload2, statuses2 = _whatsapp_status_webhook(
        status_id="wamid.SYNTHETIC_STATUS_0002",
        recipient_id="77000000002",
        status="failed",
        timestamp="1783022404",
        errors=[{"code": 131047}],
        extra_value={"nonce": 2},
    )
    assert raw1 != raw2
    await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw1,
        payload=payload1,
        received_at=_RECEIVED,
        whatsapp_statuses=statuses1,
        session_factory=session_factory,
    )
    second = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw2,
        payload=payload2,
        received_at=_RECEIVED,
        whatsapp_statuses=statuses2,
        session_factory=session_factory,
    )
    assert second.event_created is True
    assert second.statuses_created == 0
    assert second.statuses_duplicate == 1
    assert await _count(session_factory, InboundEvent) == 2
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 1


# ── M. Mixed WhatsApp webhook ───────────────────────────────────────────


async def test_mixed_whatsapp_webhook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number-3"
    )
    raw, payload, messages, statuses = _whatsapp_mixed_webhook(
        message_id="wamid.SYNTHETIC_MIXED_1",
        sender="77000000003",
        text="Hi there",
        status_id="wamid.SYNTHETIC_MIXED_S1",
        status="read",
    )
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=messages,
        whatsapp_statuses=statuses,
        session_factory=session_factory,
    )
    assert summary.messages_created == 1
    assert summary.processing_jobs_created == 1
    assert summary.statuses_created == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 1
    # The delivery status must never become a customer message or a job.
    async with session_factory() as session:
        message_row = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id
                    == "wamid.SYNTHETIC_MIXED_1"
                )
            )
        ).scalar_one()
        assert message_row.message_type == "text"
        assert message_row.text == "Hi there"
        status_row = (
            await session.execute(
                select(WhatsAppDeliveryStatus).where(
                    WhatsAppDeliveryStatus.external_message_id
                    == "wamid.SYNTHETIC_MIXED_S1"
                )
            )
        ).scalar_one()
        assert status_row.status == "read"
        assert status_row.recipient_id == "77000000003"


# ── N. Multiple messages and statuses ───────────────────────────────────


async def test_multiple_messages_and_statuses_all_preserved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number-4"
    )
    value: dict[str, object] = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "15550000000",
            "phone_number_id": "100000000000001",
        },
        "messages": [
            {
                "from": "77000000004",
                "id": "wamid.SYNTHETIC_MULTI_M1",
                "timestamp": "1783022400",
                "type": "text",
                "text": {"body": "First"},
            },
            {
                "from": "77000000004",
                "id": "wamid.SYNTHETIC_MULTI_M2",
                "timestamp": "1783022401",
                "type": "text",
                "text": {"body": "Second"},
            },
        ],
        "statuses": [
            {
                "id": "wamid.SYNTHETIC_MULTI_S1",
                "recipient_id": "77000000004",
                "status": "sent",
                "timestamp": "1783022402",
            },
            {
                "id": "wamid.SYNTHETIC_MULTI_S2",
                "recipient_id": "77000000004",
                "status": "delivered",
                "timestamp": "1783022403",
            },
        ],
    }
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "999999999999999",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    parsed = json.loads(raw)
    result = normalize_whatsapp_webhook(parsed, received_at=_RECEIVED)
    summary = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw,
        payload=parsed,
        received_at=_RECEIVED,
        messages=list(result.messages),
        whatsapp_statuses=list(result.statuses),
        session_factory=session_factory,
    )
    assert summary.messages_created == 2
    assert summary.processing_jobs_created == 2
    assert summary.statuses_created == 2
    assert await _count(session_factory, InboundMessage) == 2
    assert await _count(session_factory, MessageProcessingJob) == 2
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 2


# ── O. Transaction rollback ─────────────────────────────────────────────


async def test_transaction_rollback_on_injected_failure(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(session: AsyncSession, *, message_ids: list[UUID]) -> int:
        raise RuntimeError("injected failure after message insertion")

    monkeypatch.setattr(service_module, "_insert_processing_jobs", _boom)
    connection_id = await _create_connection(session_factory)
    raw, payload, message = _telegram_text_update(
        message_id=41008, chat_id=771100008, user_id=771100008,
        text="Rollback me",
    )
    with pytest.raises(InboundIngestionExecutionError) as excinfo:
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="telegram",
            raw_body=raw,
            payload=payload,
            received_at=_RECEIVED,
            messages=[message],
            session_factory=session_factory,
        )
    assert "Inbound webhook ingestion failed." in str(excinfo.value)
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0
    assert await _count(session_factory, MessageProcessingJob) == 0
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 0


# ── P. Payload preservation ─────────────────────────────────────────────


async def test_payload_json_and_sha256_preserved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw, payload, message = _telegram_text_update(
        message_id=41009, chat_id=771100009, user_id=771100009,
        text="Preserve me",
    )
    await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    async with session_factory() as session:
        event = (
            await session.execute(
                select(InboundEvent).where(
                    InboundEvent.connection_id == connection_id
                )
            )
        ).scalar_one()
        assert event.payload_json == payload
        assert event.payload_sha256 == payload_sha256(raw)


# ── Q. Media persistence ────────────────────────────────────────────────


async def test_voice_media_columns_persist_exactly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    raw, payload, message = _telegram_voice_update(
        message_id=41010, chat_id=771100010, user_id=771100010
    )
    await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="telegram",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=[message],
        session_factory=session_factory,
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id
                    == "message:771100010:41010"
                )
            )
        ).scalar_one()
        assert row.message_type == "voice"
        assert row.text is None
        assert row.media_external_id == "VoiceFileIdDemo001"
        assert row.media_mime_type == "audio/ogg"
        assert row.media_size_bytes == 25121
        assert row.media_duration_seconds == 3
        assert row.media_file_name is None
        assert row.media_checksum_sha256 is None


# ── R. Exact webhook duplicate policy ───────────────────────────────────


async def test_exact_duplicate_summary_policy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number-5"
    )
    raw, payload, messages, statuses = _whatsapp_mixed_webhook(
        message_id="wamid.SYNTHETIC_POLICY_M1",
        sender="77000000005",
        text="Policy",
        status_id="wamid.SYNTHETIC_POLICY_S1",
        status="delivered",
    )
    await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=messages,
        whatsapp_statuses=statuses,
        session_factory=session_factory,
    )
    duplicate = await ingest_normalized_webhook(
        connection_id=connection_id,
        channel="whatsapp",
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=messages,
        whatsapp_statuses=statuses,
        session_factory=session_factory,
    )
    # Pinned policy: for an exact duplicate raw webhook no writes occur and
    # the unique supplied identities are reported as duplicates.
    assert duplicate.event_created is False
    assert duplicate.messages_created == 0
    assert duplicate.messages_duplicate == 1
    assert duplicate.processing_jobs_created == 0
    assert duplicate.statuses_created == 0
    assert duplicate.statuses_duplicate == 1
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1
    assert await _count(session_factory, WhatsAppDeliveryStatus) == 1


# ── Extra input-validation coverage ─────────────────────────────────────


async def test_message_channel_mismatch_rejected_before_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(
        session_factory, channel="whatsapp", name="wa-number-6"
    )
    raw, payload, message = _telegram_text_update(
        message_id=41011, chat_id=771100011, user_id=771100011,
        text="wrong channel",
    )
    with pytest.raises(InboundIngestionValidationError):
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="whatsapp",
            raw_body=raw,
            payload=payload,
            received_at=_RECEIVED,
            messages=[message],
            session_factory=session_factory,
        )
    assert await _count(session_factory, InboundEvent) == 0
    assert await _count(session_factory, InboundMessage) == 0


async def test_statuses_rejected_for_telegram_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    status = WhatsAppDeliveryStatusEvent(
        external_message_id="wamid.SYNTHETIC_1",
        recipient_id="77000000001",
        status="delivered",
        occurred_at=_RECEIVED,
    )
    with pytest.raises(InboundIngestionValidationError):
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="telegram",
            raw_body=b"{}",
            payload={},
            received_at=_RECEIVED,
            whatsapp_statuses=[status],
            session_factory=session_factory,
        )
    assert await _count(session_factory, InboundEvent) == 0


async def test_naive_received_at_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection_id = await _create_connection(session_factory)
    with pytest.raises(InboundIngestionValidationError):
        await ingest_normalized_webhook(
            connection_id=connection_id,
            channel="telegram",
            raw_body=b"{}",
            payload={},
            received_at=datetime(2026, 8, 1, 12, 0, 0),
            session_factory=session_factory,
        )
    assert await _count(session_factory, InboundEvent) == 0

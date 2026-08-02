"""Transactional, idempotent inbound ingestion service for TALAP.

Pipeline for one verified webhook:

    validated inputs
    → validate connection
    → insert inbound event (ON CONFLICT DO NOTHING)
    → insert new customer messages (ON CONFLICT DO NOTHING)
    → enqueue one PENDING job per inserted message
    → insert WhatsApp delivery-status events separately
    → commit once

All writes for one webhook happen in ONE PostgreSQL transaction. If the
inbound event already exists (exact raw-body duplicate), no rows are written
and the summary reports the supplied unique identities as duplicates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.channels import Channel, NormalizedInboundMessage
from talap.channels.whatsapp import WhatsAppDeliveryStatusEvent
from talap.db import async_session_factory
from talap.db.models import (
    ChannelConnection,
    InboundEvent,
    InboundMessage,
    MessageProcessingJob,
    MessageProcessingJobStatus,
    WhatsAppDeliveryStatus,
)
from talap.ingestion.fingerprints import payload_sha256, whatsapp_status_fingerprint
from talap.ingestion.types import (
    ChannelConnectionInactiveError,
    ChannelConnectionMismatchError,
    ChannelConnectionNotFoundError,
    InboundIngestionError,
    InboundIngestionExecutionError,
    InboundIngestionValidationError,
    IngestionSummary,
)

_BUSINESS_SCOPE_GLOBAL = "talap_global"


def _validate_inputs(
    *,
    connection_id: object,
    channel: object,
    raw_body: object,
    payload: object,
    received_at: datetime,
    messages: Sequence[object],
    whatsapp_statuses: Sequence[object],
) -> dict[str, object]:
    if not isinstance(connection_id, UUID):
        raise InboundIngestionValidationError("connection_id must be a UUID.")
    if channel not in ("telegram", "whatsapp"):
        raise InboundIngestionValidationError(
            "channel must be 'telegram' or 'whatsapp'."
        )
    if not isinstance(raw_body, bytes) or not raw_body:
        raise InboundIngestionValidationError("raw_body must be non-empty bytes.")
    if not isinstance(payload, Mapping):
        raise InboundIngestionValidationError("payload must be a mapping.")
    payload_dict = dict(payload)
    try:
        json.dumps(payload_dict)
    except (TypeError, ValueError):
        raise InboundIngestionValidationError(
            "payload must be JSON serializable."
        ) from None
    if received_at.tzinfo is None or received_at.tzinfo.utcoffset(received_at) is None:
        raise InboundIngestionValidationError("received_at must be timezone-aware.")
    for message in messages:
        if not isinstance(message, NormalizedInboundMessage):
            raise InboundIngestionValidationError(
                "messages must contain NormalizedInboundMessage instances."
            )
        if message.channel != channel:
            raise InboundIngestionValidationError(
                "message.channel does not match the requested channel."
            )
        if message.business_scope != _BUSINESS_SCOPE_GLOBAL:
            raise InboundIngestionValidationError(
                "message.business_scope must be 'talap_global'."
            )
    if channel != "whatsapp" and whatsapp_statuses:
        raise InboundIngestionValidationError(
            "whatsapp_statuses are only allowed for channel 'whatsapp'."
        )
    for status in whatsapp_statuses:
        if not isinstance(status, WhatsAppDeliveryStatusEvent):
            raise InboundIngestionValidationError(
                "whatsapp_statuses must contain WhatsAppDeliveryStatusEvent instances."
            )
    return payload_dict


def _deduplicate_messages(
    messages: Sequence[NormalizedInboundMessage],
) -> list[NormalizedInboundMessage]:
    seen: set[str] = set()
    unique: list[NormalizedInboundMessage] = []
    for message in messages:
        if message.external_message_id in seen:
            continue
        seen.add(message.external_message_id)
        unique.append(message)
    return unique


def _deduplicate_statuses(
    statuses: Sequence[WhatsAppDeliveryStatusEvent],
) -> list[WhatsAppDeliveryStatusEvent]:
    seen: set[str] = set()
    unique: list[WhatsAppDeliveryStatusEvent] = []
    for status in statuses:
        fingerprint = whatsapp_status_fingerprint(
            external_message_id=status.external_message_id,
            recipient_id=status.recipient_id,
            status=status.status,
            occurred_at=status.occurred_at,
            error_codes=status.error_codes,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(status)
    return unique


async def _insert_messages(
    session: AsyncSession,
    *,
    event_id: UUID,
    connection_id: UUID,
    channel: Channel,
    unique_messages: Sequence[NormalizedInboundMessage],
) -> tuple[list[UUID], int]:
    """Insert messages; return (inserted ids, duplicates from earlier events)."""
    inserted_ids: list[UUID] = []
    duplicates = 0
    for message in unique_messages:
        media = message.media
        result = await session.execute(
            pg_insert(InboundMessage)
            .values(
                id=uuid4(),
                inbound_event_id=event_id,
                connection_id=connection_id,
                channel=channel,
                business_scope=message.business_scope,
                external_chat_id=message.external_chat_id,
                external_user_id=message.external_user_id,
                external_message_id=message.external_message_id,
                message_type=message.message_type,
                text=message.text,
                media_external_id=media.external_media_id if media else None,
                media_mime_type=media.mime_type if media else None,
                media_file_name=media.file_name if media else None,
                media_size_bytes=media.size_bytes if media else None,
                media_duration_seconds=media.duration_seconds if media else None,
                media_checksum_sha256=media.checksum_sha256 if media else None,
                received_at=message.received_at,
            )
            .on_conflict_do_nothing(
                index_elements=["connection_id", "channel", "external_message_id"]
            )
            .returning(InboundMessage.id)
        )
        row = result.first()
        if row is not None:
            inserted_ids.append(row[0])
        else:
            duplicates += 1
    return inserted_ids, duplicates


async def _insert_processing_jobs(
    session: AsyncSession,
    *,
    message_ids: Sequence[UUID],
) -> int:
    """Create one PENDING job per inserted message; return the created count."""
    created = 0
    for message_id in message_ids:
        result = await session.execute(
            pg_insert(MessageProcessingJob)
            .values(
                id=uuid4(),
                message_id=message_id,
                status=MessageProcessingJobStatus.PENDING,
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(MessageProcessingJob.id)
        )
        if result.first() is not None:
            created += 1
    return created


async def _insert_statuses(
    session: AsyncSession,
    *,
    event_id: UUID,
    connection_id: UUID,
    unique_statuses: Sequence[WhatsAppDeliveryStatusEvent],
) -> tuple[int, int]:
    """Insert statuses; return (created count, duplicates from earlier events)."""
    created = 0
    duplicates = 0
    for status in unique_statuses:
        fingerprint = whatsapp_status_fingerprint(
            external_message_id=status.external_message_id,
            recipient_id=status.recipient_id,
            status=status.status,
            occurred_at=status.occurred_at,
            error_codes=status.error_codes,
        )
        result = await session.execute(
            pg_insert(WhatsAppDeliveryStatus)
            .values(
                id=uuid4(),
                inbound_event_id=event_id,
                connection_id=connection_id,
                external_message_id=status.external_message_id,
                recipient_id=status.recipient_id,
                status=status.status,
                occurred_at=status.occurred_at,
                error_codes=list(status.error_codes),
                fingerprint_sha256=fingerprint,
            )
            .on_conflict_do_nothing(
                index_elements=["connection_id", "fingerprint_sha256"]
            )
            .returning(WhatsAppDeliveryStatus.id)
        )
        row = result.first()
        if row is not None:
            created += 1
        else:
            duplicates += 1
    return created, duplicates


async def _ingest_in_transaction(
    session: AsyncSession,
    *,
    connection_id: UUID,
    channel: Channel,
    raw_body: bytes,
    payload_dict: Mapping[str, object],
    received_at_utc: datetime,
    messages: Sequence[NormalizedInboundMessage],
    whatsapp_statuses: Sequence[WhatsAppDeliveryStatusEvent],
) -> IngestionSummary:
    connection = await session.get(ChannelConnection, connection_id)
    if connection is None:
        raise ChannelConnectionNotFoundError(
            f"Channel connection {connection_id} was not found."
        )
    if not connection.active:
        raise ChannelConnectionInactiveError(
            f"Channel connection {connection_id} is inactive."
        )
    if connection.channel != channel:
        raise ChannelConnectionMismatchError(
            "Channel connection channel does not match the requested channel."
        )

    body_hash = payload_sha256(raw_body)

    event_result = await session.execute(
        pg_insert(InboundEvent)
        .values(
            id=uuid4(),
            connection_id=connection_id,
            channel=channel,
            payload_sha256=body_hash,
            payload_json=payload_dict,
            received_at=received_at_utc,
        )
        .on_conflict_do_nothing(
            index_elements=["connection_id", "channel", "payload_sha256"]
        )
        .returning(InboundEvent.id)
    )
    event_row = event_result.first()

    if event_row is None:
        # Exact duplicate raw webhook: the original event/messages/jobs/statuses
        # committed atomically, so nothing is recreated. Duplicate counters
        # report the unique supplied identities (deterministic policy).
        existing_event_id = (
            await session.execute(
                select(InboundEvent.id).where(
                    InboundEvent.connection_id == connection_id,
                    InboundEvent.channel == channel,
                    InboundEvent.payload_sha256 == body_hash,
                )
            )
        ).scalar_one()
        return IngestionSummary(
            inbound_event_id=existing_event_id,
            event_created=False,
            messages_created=0,
            messages_duplicate=len(_deduplicate_messages(messages)),
            processing_jobs_created=0,
            statuses_created=0,
            statuses_duplicate=len(_deduplicate_statuses(whatsapp_statuses)),
        )

    event_id = event_row[0]
    unique_messages = _deduplicate_messages(messages)
    messages_duplicate = len(messages) - len(unique_messages)
    inserted_message_ids, cross_event_duplicates = await _insert_messages(
        session,
        event_id=event_id,
        connection_id=connection_id,
        channel=channel,
        unique_messages=unique_messages,
    )
    messages_duplicate += cross_event_duplicates

    jobs_created = await _insert_processing_jobs(
        session,
        message_ids=inserted_message_ids,
    )

    unique_statuses = _deduplicate_statuses(whatsapp_statuses)
    statuses_duplicate = len(whatsapp_statuses) - len(unique_statuses)
    statuses_created, cross_event_status_duplicates = await _insert_statuses(
        session,
        event_id=event_id,
        connection_id=connection_id,
        unique_statuses=unique_statuses,
    )
    statuses_duplicate += cross_event_status_duplicates

    return IngestionSummary(
        inbound_event_id=event_id,
        event_created=True,
        messages_created=len(inserted_message_ids),
        messages_duplicate=messages_duplicate,
        processing_jobs_created=jobs_created,
        statuses_created=statuses_created,
        statuses_duplicate=statuses_duplicate,
    )


async def ingest_normalized_webhook(
    *,
    connection_id: UUID,
    channel: Channel,
    raw_body: bytes,
    payload: Mapping[str, object],
    received_at: datetime,
    messages: Sequence[NormalizedInboundMessage] = (),
    whatsapp_statuses: Sequence[WhatsAppDeliveryStatusEvent] = (),
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> IngestionSummary:
    """Ingest one verified webhook atomically and idempotently.

    This is the only public write entrypoint for T-021. All writes commit in
    one transaction or roll back together.
    """
    payload_dict = _validate_inputs(
        connection_id=connection_id,
        channel=channel,
        raw_body=raw_body,
        payload=payload,
        received_at=received_at,
        messages=messages,
        whatsapp_statuses=whatsapp_statuses,
    )
    factory = session_factory or async_session_factory
    received_at_utc = received_at.astimezone(UTC)
    async with factory() as session:
        try:
            summary = await _ingest_in_transaction(
                session,
                connection_id=connection_id,
                channel=channel,
                raw_body=raw_body,
                payload_dict=payload_dict,
                received_at_utc=received_at_utc,
                messages=messages,
                whatsapp_statuses=whatsapp_statuses,
            )
            await session.commit()
        except InboundIngestionError:
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            raise InboundIngestionExecutionError(
                "Inbound webhook ingestion failed."
            ) from exc
        return summary

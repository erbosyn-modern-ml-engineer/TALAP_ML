"""Durable inbound ingestion models for TALAP.

Channel connections, inbound webhook events, normalized customer messages,
per-message processing jobs, and WhatsApp delivery status events. Identity
idempotency is scoped by ``connection_id`` so multiple bots/WhatsApp numbers
never collide.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal
from uuid import UUID as PythonUUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from talap.db.models.telegram import TelegramWebhookConfig

ChannelName = Literal["telegram", "whatsapp"]


class ChannelConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One configured bot/WhatsApp-number connection that scopes idempotency.

    Credentials, tokens, and webhook secrets are intentionally not stored
    here; they belong to the channel-configuration/security tasks.
    """

    __tablename__ = "channel_connections"

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    __table_args__ = (
        CheckConstraint(
            "channel IN ('telegram', 'whatsapp')",
            name="ck_channel_connections_channel_valid",
        ),
        CheckConstraint(
            "length(name) > 0",
            name="ck_channel_connections_name_not_empty",
        ),
        UniqueConstraint(
            "id",
            "channel",
            name="uq_channel_connections_id_channel",
        ),
    )

    inbound_events: Mapped[list[InboundEvent]] = relationship(
        "InboundEvent",
        back_populates="connection",
    )
    inbound_messages: Mapped[list[InboundMessage]] = relationship(
        "InboundMessage",
        back_populates="connection",
    )
    whatsapp_delivery_statuses: Mapped[list[WhatsAppDeliveryStatus]] = relationship(
        "WhatsAppDeliveryStatus",
        back_populates="connection",
    )
    telegram_webhook_config: Mapped[TelegramWebhookConfig | None] = relationship(
        "TelegramWebhookConfig",
        back_populates="connection",
        uselist=False,
        passive_deletes=True,
    )


class InboundEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One verified raw webhook request, identity-hashed by exact bytes."""

    __tablename__ = "inbound_events"

    connection_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_inbound_events_payload_sha256_length",
        ),
        UniqueConstraint(
            "connection_id",
            "channel",
            "payload_sha256",
            name="uq_inbound_events_connection_channel_payload_sha256",
        ),
        # Tenant-safe composite FK reusing UNIQUE(channel_connections.id, channel).
        ForeignKeyConstraint(
            ["connection_id", "channel"],
            ["channel_connections.id", "channel_connections.channel"],
            ondelete="RESTRICT",
            name="fk_inbound_events_connection_channel",
        ),
        Index("ix_inbound_events_connection_id", "connection_id"),
    )

    connection: Mapped[ChannelConnection] = relationship(
        "ChannelConnection",
        back_populates="inbound_events",
    )
    messages: Mapped[list[InboundMessage]] = relationship(
        "InboundMessage",
        back_populates="inbound_event",
    )
    whatsapp_delivery_statuses: Mapped[list[WhatsAppDeliveryStatus]] = relationship(
        "WhatsAppDeliveryStatus",
        back_populates="inbound_event",
    )


class InboundMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One normalized customer message derived from a validated T-018 model."""

    __tablename__ = "inbound_messages"

    inbound_event_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    connection_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    business_scope: Mapped[str] = mapped_column(String(50), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)

    media_external_id: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    media_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    media_duration_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    media_checksum_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "channel",
            "external_message_id",
            name="uq_inbound_messages_connection_channel_external_message_id",
        ),
        ForeignKeyConstraint(
            ["connection_id", "channel"],
            ["channel_connections.id", "channel_connections.channel"],
            ondelete="RESTRICT",
            name="fk_inbound_messages_connection_channel",
        ),
        ForeignKeyConstraint(
            ["inbound_event_id"],
            ["inbound_events.id"],
            ondelete="RESTRICT",
            name="fk_inbound_messages_inbound_event",
        ),
        CheckConstraint(
            "business_scope = 'talap_global'",
            name="ck_inbound_messages_business_scope",
        ),
        CheckConstraint(
            "channel IN ('telegram', 'whatsapp')",
            name="ck_inbound_messages_channel_valid",
        ),
        CheckConstraint(
            "message_type IN ('text', 'voice', 'image', 'unsupported')",
            name="ck_inbound_messages_message_type_valid",
        ),
        CheckConstraint(
            "media_size_bytes IS NULL OR media_size_bytes >= 0",
            name="ck_inbound_messages_media_size_non_negative",
        ),
        CheckConstraint(
            "media_duration_seconds IS NULL OR media_duration_seconds >= 0",
            name="ck_inbound_messages_media_duration_non_negative",
        ),
        CheckConstraint(
            "media_checksum_sha256 IS NULL OR length(media_checksum_sha256) = 64",
            name="ck_inbound_messages_media_checksum_length",
        ),
        CheckConstraint(
            "media_external_id IS NOT NULL OR (media_mime_type IS NULL "
            "AND media_file_name IS NULL AND media_size_bytes IS NULL "
            "AND media_duration_seconds IS NULL AND media_checksum_sha256 IS NULL)",
            name="ck_inbound_messages_media_columns_consistent",
        ),
        CheckConstraint(
            "(message_type = 'text' AND text IS NOT NULL AND media_external_id IS NULL) "
            "OR (message_type IN ('voice', 'image') AND media_external_id IS NOT NULL) "
            "OR (message_type = 'unsupported' AND text IS NULL AND media_external_id IS NULL)",
            name="ck_inbound_messages_type_invariant",
        ),
        Index("ix_inbound_messages_connection_id", "connection_id"),
        Index("ix_inbound_messages_external_message_id", "external_message_id"),
    )

    connection: Mapped[ChannelConnection] = relationship(
        "ChannelConnection",
        back_populates="inbound_messages",
    )
    inbound_event: Mapped[InboundEvent] = relationship(
        "InboundEvent",
        back_populates="messages",
    )
    processing_job: Mapped[MessageProcessingJob | None] = relationship(
        "MessageProcessingJob",
        back_populates="message",
        uselist=False,
        passive_deletes=True,
    )


class MessageProcessingJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageProcessingJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One processing job for one normalized inbound message.

    T-021 only ever creates PENDING jobs; claiming/retries/response sending
    belong to later tasks. Ownership is enforced by the PostgreSQL ON DELETE
    CASCADE on ``message_id``.
    """

    __tablename__ = "message_processing_jobs"

    message_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    status: Mapped[MessageProcessingJobStatus] = mapped_column(
        ENUM(
            MessageProcessingJobStatus,
            name="message_processing_job_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            create_type=False,
        ),
        nullable=False,
        default=MessageProcessingJobStatus.PENDING,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)

    __table_args__ = (
        UniqueConstraint(
            "message_id",
            name="uq_message_processing_jobs_message_id",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_message_processing_jobs_attempts_non_negative",
        ),
        ForeignKeyConstraint(
            ["message_id"],
            ["inbound_messages.id"],
            ondelete="CASCADE",
            name="fk_message_processing_jobs_message",
        ),
    )

    message: Mapped[InboundMessage] = relationship(
        "InboundMessage",
        back_populates="processing_job",
        passive_deletes=True,
    )


class WhatsAppDeliveryStatus(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One normalized WhatsApp delivery-status event (never a customer message)."""

    __tablename__ = "whatsapp_delivery_statuses"

    inbound_event_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    connection_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    external_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    error_codes: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "fingerprint_sha256",
            name="uq_whatsapp_delivery_statuses_connection_fingerprint_sha256",
        ),
        CheckConstraint(
            "length(fingerprint_sha256) = 64",
            name="ck_whatsapp_delivery_statuses_fingerprint_length",
        ),
        CheckConstraint(
            "status IN ('sent', 'delivered', 'read', 'failed', 'deleted', 'unknown')",
            name="ck_whatsapp_delivery_statuses_status_valid",
        ),
        ForeignKeyConstraint(
            ["inbound_event_id"],
            ["inbound_events.id"],
            ondelete="RESTRICT",
            name="fk_whatsapp_delivery_statuses_inbound_event",
        ),
        ForeignKeyConstraint(
            ["connection_id"],
            ["channel_connections.id"],
            ondelete="RESTRICT",
            name="fk_whatsapp_delivery_statuses_connection",
        ),
        Index("ix_whatsapp_delivery_statuses_connection_id", "connection_id"),
    )

    connection: Mapped[ChannelConnection] = relationship(
        "ChannelConnection",
        back_populates="whatsapp_delivery_statuses",
    )
    inbound_event: Mapped[InboundEvent] = relationship(
        "InboundEvent",
        back_populates="whatsapp_delivery_statuses",
    )

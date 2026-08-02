"""add inbound ingestion tables

Revision ID: f0e1d2c3b4a5
Revises: c4e9b2d7a1f8
Create Date: 2026-08-02 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f0e1d2c3b4a5"
down_revision: str | None = "c4e9b2d7a1f8"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    job_status = postgresql.ENUM(
        "pending",
        "processing",
        "completed",
        "failed",
        name="message_processing_job_status",
        create_type=False,
    )
    job_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "channel_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('telegram', 'whatsapp')",
            name="ck_channel_connections_channel_valid",
        ),
        sa.CheckConstraint(
            "length(name) > 0",
            name="ck_channel_connections_name_not_empty",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_connections")),
        sa.UniqueConstraint(
            "id",
            "channel",
            name="uq_channel_connections_id_channel",
        ),
    )

    op.create_table(
        "inbound_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_inbound_events_payload_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "channel"],
            ["channel_connections.id", "channel_connections.channel"],
            name="fk_inbound_events_connection_channel",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_events")),
        sa.UniqueConstraint(
            "connection_id",
            "channel",
            "payload_sha256",
            name="uq_inbound_events_connection_channel_payload_sha256",
        ),
    )
    op.create_index(
        "ix_inbound_events_connection_id",
        "inbound_events",
        ["connection_id"],
    )

    op.create_table(
        "inbound_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbound_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("business_scope", sa.String(length=50), nullable=False),
        sa.Column("external_chat_id", sa.String(length=512), nullable=False),
        sa.Column("external_user_id", sa.String(length=512), nullable=False),
        sa.Column("external_message_id", sa.String(length=512), nullable=False),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("media_external_id", sa.String(length=512), nullable=True),
        sa.Column("media_mime_type", sa.String(length=255), nullable=True),
        sa.Column("media_file_name", sa.String(length=512), nullable=True),
        sa.Column("media_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("media_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("media_checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "business_scope = 'talap_global'",
            name="ck_inbound_messages_business_scope",
        ),
        sa.CheckConstraint(
            "channel IN ('telegram', 'whatsapp')",
            name="ck_inbound_messages_channel_valid",
        ),
        sa.CheckConstraint(
            "message_type IN ('text', 'voice', 'image', 'unsupported')",
            name="ck_inbound_messages_message_type_valid",
        ),
        sa.CheckConstraint(
            "media_size_bytes IS NULL OR media_size_bytes >= 0",
            name="ck_inbound_messages_media_size_non_negative",
        ),
        sa.CheckConstraint(
            "media_duration_seconds IS NULL OR media_duration_seconds >= 0",
            name="ck_inbound_messages_media_duration_non_negative",
        ),
        sa.CheckConstraint(
            "media_checksum_sha256 IS NULL OR length(media_checksum_sha256) = 64",
            name="ck_inbound_messages_media_checksum_length",
        ),
        sa.CheckConstraint(
            "media_external_id IS NOT NULL OR (media_mime_type IS NULL "
            "AND media_file_name IS NULL AND media_size_bytes IS NULL "
            "AND media_duration_seconds IS NULL AND media_checksum_sha256 IS NULL)",
            name="ck_inbound_messages_media_columns_consistent",
        ),
        sa.CheckConstraint(
            "(message_type = 'text' AND text IS NOT NULL AND media_external_id IS NULL) "
            "OR (message_type IN ('voice', 'image') AND media_external_id IS NOT NULL) "
            "OR (message_type = 'unsupported' AND text IS NULL AND media_external_id IS NULL)",
            name="ck_inbound_messages_type_invariant",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "channel"],
            ["channel_connections.id", "channel_connections.channel"],
            name="fk_inbound_messages_connection_channel",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inbound_event_id"],
            ["inbound_events.id"],
            name="fk_inbound_messages_inbound_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_messages")),
        sa.UniqueConstraint(
            "connection_id",
            "channel",
            "external_message_id",
            name="uq_inbound_messages_connection_channel_external_message_id",
        ),
    )
    op.create_index(
        "ix_inbound_messages_connection_id",
        "inbound_messages",
        ["connection_id"],
    )
    op.create_index(
        "ix_inbound_messages_external_message_id",
        "inbound_messages",
        ["external_message_id"],
    )

    op.create_table(
        "message_processing_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            job_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_message_processing_jobs_attempts_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["inbound_messages.id"],
            name="fk_message_processing_jobs_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_processing_jobs")),
        sa.UniqueConstraint(
            "message_id",
            name="uq_message_processing_jobs_message_id",
        ),
    )

    op.create_table(
        "whatsapp_delivery_statuses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbound_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_message_id", sa.String(length=512), nullable=False),
        sa.Column("recipient_id", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "error_codes",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(fingerprint_sha256) = 64",
            name="ck_whatsapp_delivery_statuses_fingerprint_length",
        ),
        sa.CheckConstraint(
            "status IN ('sent', 'delivered', 'read', 'failed', 'deleted', 'unknown')",
            name="ck_whatsapp_delivery_statuses_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["inbound_event_id"],
            ["inbound_events.id"],
            name="fk_whatsapp_delivery_statuses_inbound_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["channel_connections.id"],
            name="fk_whatsapp_delivery_statuses_connection",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_delivery_statuses")),
        sa.UniqueConstraint(
            "connection_id",
            "fingerprint_sha256",
            name="uq_whatsapp_delivery_statuses_connection_fingerprint_sha256",
        ),
    )
    op.create_index(
        "ix_whatsapp_delivery_statuses_connection_id",
        "whatsapp_delivery_statuses",
        ["connection_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_delivery_statuses_connection_id",
        table_name="whatsapp_delivery_statuses",
    )
    op.drop_table("whatsapp_delivery_statuses")
    op.drop_table("message_processing_jobs")
    op.drop_index(
        "ix_inbound_messages_external_message_id",
        table_name="inbound_messages",
    )
    op.drop_index(
        "ix_inbound_messages_connection_id",
        table_name="inbound_messages",
    )
    op.drop_table("inbound_messages")
    op.drop_index(
        "ix_inbound_events_connection_id",
        table_name="inbound_events",
    )
    op.drop_table("inbound_events")
    op.drop_table("channel_connections")
    postgresql.ENUM(
        name="message_processing_job_status",
    ).drop(op.get_bind(), checkfirst=True)

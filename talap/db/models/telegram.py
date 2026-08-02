"""Telegram webhook configuration storage (hashed secret only)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from talap.db.base import Base
from talap.db.models.common import TimestampMixin

if TYPE_CHECKING:
    from talap.db.models.inbound import ChannelConnection


class TelegramWebhookConfig(TimestampMixin, Base):
    """One-to-one per-connection Telegram webhook secret (SHA-256 only).

    The plaintext webhook secret is never stored; only its SHA-256 digest.
    Bot tokens, Meta tokens, and encryption material belong elsewhere.
    """

    __tablename__ = "telegram_webhook_configs"

    connection_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    webhook_secret_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "length(webhook_secret_sha256) = 64",
            name="ck_telegram_webhook_configs_secret_sha256_length",
        ),
        ForeignKeyConstraint(
            ["connection_id"],
            ["channel_connections.id"],
            ondelete="CASCADE",
            name="fk_telegram_webhook_configs_connection",
        ),
    )

    connection: Mapped[ChannelConnection] = relationship(
        "ChannelConnection",
        back_populates="telegram_webhook_config",
        passive_deletes=True,
    )

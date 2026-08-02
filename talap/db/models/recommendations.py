"""WhatsApp recommendation-state and unmet-demand storage (MVP-6)."""

from __future__ import annotations

from uuid import UUID as PythonUUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

RECOMMENDATION_STATUS_ACTIVE = "active"
RECOMMENDATION_STATUS_SELECTED = "selected"
RECOMMENDATION_STATUS_SUPERSEDED = "superseded"


class WhatsAppRecommendationState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One displayed recommendation set per WhatsApp customer (MVP-6).

    At most one ``active`` row per ``(channel, external_user_id)`` is enforced
    by a partial unique index. A newer set supersedes the previous active set.
    """

    __tablename__ = "whatsapp_recommendation_states"

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    displayed_products: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=RECOMMENDATION_STATUS_ACTIVE,
        server_default=text("'active'"),
    )
    selected_product_id: Mapped[PythonUUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        default=None,
    )
    selected_index: Mapped[int | None] = mapped_column(Integer, default=None)
    source_message_id: Mapped[PythonUUID | None] = mapped_column(
        ForeignKey("inbound_messages.id", ondelete="SET NULL"),
        default=None,
    )

    __table_args__ = (
        CheckConstraint(
            "channel = 'whatsapp'",
            name="ck_whatsapp_recommendation_states_channel_whatsapp",
        ),
        CheckConstraint(
            "status IN ('active', 'selected', 'superseded')",
            name="ck_whatsapp_recommendation_states_status_valid",
        ),
        Index(
            "uq_whatsapp_recommendation_states_active_customer",
            "channel",
            "external_user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_whatsapp_recommendation_states_customer",
            "channel",
            "external_user_id",
        ),
    )


class UnmetDemand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One customer request with zero matching products (MVP-6).

    Idempotent per source message via ``uq_unmet_demand_source_message_id``.
    """

    __tablename__ = "unmet_demand"

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_message_id: Mapped[PythonUUID] = mapped_column(
        ForeignKey("inbound_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(200), default=None)
    attributes: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    budget_max_kzt: Mapped[int | None] = mapped_column(Integer, default=None)
    quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    language: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "channel IN ('telegram', 'whatsapp')",
            name="ck_unmet_demand_channel_valid",
        ),
        CheckConstraint(
            "language IN ('kk', 'ru', 'mixed', 'unknown')",
            name="ck_unmet_demand_language_valid",
        ),
        UniqueConstraint(
            "source_message_id",
            name="uq_unmet_demand_source_message_id",
        ),
    )

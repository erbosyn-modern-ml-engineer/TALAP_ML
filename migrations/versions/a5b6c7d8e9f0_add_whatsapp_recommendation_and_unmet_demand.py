"""add whatsapp recommendation states and unmet demand

Revision ID: a5b6c7d8e9f0
Revises: 9f8e7d6c5b4a
Create Date: 2026-08-02 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a5b6c7d8e9f0"
down_revision: str | None = "9f8e7d6c5b4a"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_recommendation_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("external_user_id", sa.String(length=512), nullable=False),
        sa.Column("displayed_products", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "selected_product_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("selected_index", sa.Integer(), nullable=True),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
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
            "channel = 'whatsapp'",
            name="ck_whatsapp_recommendation_states_channel_whatsapp",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'selected', 'superseded')",
            name="ck_whatsapp_recommendation_states_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["inbound_messages.id"],
            name="fk_whatsapp_recommendation_states_source_message",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_recommendation_states")),
    )
    op.create_index(
        "uq_whatsapp_recommendation_states_active_customer",
        "whatsapp_recommendation_states",
        ["channel", "external_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_whatsapp_recommendation_states_customer",
        "whatsapp_recommendation_states",
        ["channel", "external_user_id"],
    )

    op.create_table(
        "unmet_demand",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("external_user_id", sa.String(length=512), nullable=False),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=200), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("budget_max_kzt", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=20), nullable=False),
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
            name="ck_unmet_demand_channel_valid",
        ),
        sa.CheckConstraint(
            "language IN ('kk', 'ru', 'mixed', 'unknown')",
            name="ck_unmet_demand_language_valid",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["inbound_messages.id"],
            name="fk_unmet_demand_source_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unmet_demand")),
        sa.UniqueConstraint(
            "source_message_id",
            name="uq_unmet_demand_source_message_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("unmet_demand")
    op.drop_index(
        "ix_whatsapp_recommendation_states_customer",
        table_name="whatsapp_recommendation_states",
    )
    op.drop_index(
        "uq_whatsapp_recommendation_states_active_customer",
        table_name="whatsapp_recommendation_states",
    )
    op.drop_table("whatsapp_recommendation_states")

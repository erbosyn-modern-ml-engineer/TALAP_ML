"""add telegram webhook configs

Revision ID: 9f8e7d6c5b4a
Revises: f0e1d2c3b4a5
Create Date: 2026-08-02 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9f8e7d6c5b4a"
down_revision: str | None = "f0e1d2c3b4a5"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_webhook_configs",
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("webhook_secret_sha256", sa.String(length=64), nullable=False),
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
            "length(webhook_secret_sha256) = 64",
            name="ck_telegram_webhook_configs_secret_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["channel_connections.id"],
            name="fk_telegram_webhook_configs_connection",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "connection_id",
            name=op.f("pk_telegram_webhook_configs"),
        ),
    )


def downgrade() -> None:
    op.drop_table("telegram_webhook_configs")

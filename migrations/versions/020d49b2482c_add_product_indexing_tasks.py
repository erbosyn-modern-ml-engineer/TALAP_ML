"""add product indexing tasks

Revision ID: 020d49b2482c
Revises: 8bd9d7cfe334
Create Date: 2026-07-31 21:19:04.508612

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "020d49b2482c"
down_revision: str | None = "8bd9d7cfe334"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None

# The ORM status column uses create_type=False, so the migration owns the
# product_indexing_task_status PostgreSQL enum lifecycle.
product_indexing_task_status_enum = postgresql.ENUM(
    "pending",
    "processing",
    "completed",
    "failed",
    name="product_indexing_task_status",
    create_type=False,
)


def upgrade() -> None:
    product_indexing_task_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "product_indexing_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            product_indexing_task_status_enum,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "changed_fields",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
            name="ck_product_indexing_tasks_attempts_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["products.id", "products.merchant_id"],
            name="fk_product_indexing_tasks_product_merchant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_indexing_tasks")),
    )
    op.create_index(
        "ix_product_indexing_tasks_status",
        "product_indexing_tasks",
        ["status"],
    )
    op.create_index(
        "ix_product_indexing_tasks_available_at",
        "product_indexing_tasks",
        ["available_at"],
    )
    op.create_index(
        "ix_product_indexing_tasks_product_id",
        "product_indexing_tasks",
        ["product_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_indexing_tasks_product_id",
        table_name="product_indexing_tasks",
    )
    op.drop_index(
        "ix_product_indexing_tasks_available_at",
        table_name="product_indexing_tasks",
    )
    op.drop_index(
        "ix_product_indexing_tasks_status",
        table_name="product_indexing_tasks",
    )
    op.drop_table("product_indexing_tasks")
    product_indexing_task_status_enum.drop(op.get_bind(), checkfirst=True)

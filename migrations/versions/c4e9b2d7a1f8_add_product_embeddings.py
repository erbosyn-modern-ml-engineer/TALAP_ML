"""add product embeddings

Revision ID: c4e9b2d7a1f8
Revises: 020d49b2482c
Create Date: 2026-08-01 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4e9b2d7a1f8"
down_revision: str | None = "020d49b2482c"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    # This migration owns enabling the shared pgvector extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "product_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("document_text", sa.Text(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column(
            "embedded_at",
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
            "dimensions = 1024",
            name="ck_product_embeddings_dimensions_1024",
        ),
        sa.CheckConstraint(
            "length(document_sha256) = 64",
            name="ck_product_embeddings_document_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["products.id", "products.merchant_id"],
            name="fk_product_embeddings_product_merchant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_embeddings")),
        sa.UniqueConstraint(
            "product_id",
            name="uq_product_embeddings_product_id",
        ),
    )
    op.create_index(
        "ix_product_embeddings_merchant_id",
        "product_embeddings",
        ["merchant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_embeddings_merchant_id",
        table_name="product_embeddings",
    )
    op.drop_table("product_embeddings")
    # The shared vector extension is intentionally NOT dropped: it is not
    # proven to be owned exclusively by this migration.

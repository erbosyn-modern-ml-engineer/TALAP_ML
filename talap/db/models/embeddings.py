"""Durable product embedding storage backed by pgvector."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from talap.db.models.catalog import Product

EMBEDDING_DIMENSIONS = 1024
EMBEDDING_PROVIDER_JINA = "jina"


class ProductEmbedding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One current pgvector embedding per Product (upserted, never duplicated).

    Holds only the canonical document text, its SHA-256, and the provider
    vector. The Jina API response body, request headers, and token usage are
    intentionally not stored.
    """

    __tablename__ = "product_embeddings"

    merchant_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    product_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    document_text: Mapped[str] = mapped_column(Text, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=False,
    )
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "product_id",
            name="uq_product_embeddings_product_id",
        ),
        CheckConstraint(
            "dimensions = 1024",
            name="ck_product_embeddings_dimensions_1024",
        ),
        CheckConstraint(
            "length(document_sha256) = 64",
            name="ck_product_embeddings_document_sha256_length",
        ),
        # Tenant-safe composite FK proves the embedding belongs to the same
        # Merchant as its Product, reusing UNIQUE(products.id, products.merchant_id).
        ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["products.id", "products.merchant_id"],
            ondelete="CASCADE",
            name="fk_product_embeddings_product_merchant",
        ),
        Index("ix_product_embeddings_merchant_id", "merchant_id"),
    )

    product: Mapped[Product] = relationship(
        "Product",
        back_populates="embedding",
    )

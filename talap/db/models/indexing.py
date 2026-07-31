from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from talap.db.models.catalog import Product


class ProductIndexingTaskStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProductIndexingTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_indexing_tasks"

    merchant_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    product_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    status: Mapped[ProductIndexingTaskStatus] = mapped_column(
        ENUM(
            ProductIndexingTaskStatus,
            name="product_indexing_task_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            create_type=False,
        ),
        nullable=False,
        default=ProductIndexingTaskStatus.PENDING,
        server_default=text("'pending'"),
    )
    changed_fields: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)

    __table_args__ = (
        CheckConstraint(
            "attempts >= 0",
            name="ck_product_indexing_tasks_attempts_non_negative",
        ),
        # Composite FK proves the task belongs to the same Merchant as its
        # Product, reusing UNIQUE(products.id, products.merchant_id).
        ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["products.id", "products.merchant_id"],
            ondelete="CASCADE",
            name="fk_product_indexing_tasks_product_merchant",
        ),
        Index("ix_product_indexing_tasks_status", "status"),
        Index("ix_product_indexing_tasks_available_at", "available_at"),
        Index("ix_product_indexing_tasks_product_id", "product_id"),
    )

    product: Mapped[Product] = relationship(
        "Product",
        back_populates="indexing_tasks",
    )

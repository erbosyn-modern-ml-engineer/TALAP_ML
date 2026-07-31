from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, func, text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import Mapped, mapped_column, relationship

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from talap.db.models.merchant import Merchant


class CatalogImportStatus(StrEnum):
    PENDING = "pending"
    VALIDATING = "validating"
    IMPORTING = "importing"
    COMPLETED = "completed"
    FAILED = "failed"


_COUNTER_DEFAULT = 0
_COUNTER_SERVER_DEFAULT = text("0")


class CatalogImport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "catalog_imports"

    merchant_id: Mapped[PythonUUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[CatalogImportStatus] = mapped_column(
        ENUM(
            CatalogImportStatus,
            name="catalog_import_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            create_type=False,
        ),
        nullable=False,
        default=CatalogImportStatus.PENDING,
        server_default=text("'pending'"),
    )

    total_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )
    valid_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )
    invalid_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )

    created_products: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )
    updated_products: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )
    created_variants: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )
    updated_variants: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )
    updated_inventory_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=_COUNTER_DEFAULT,
        server_default=_COUNTER_SERVER_DEFAULT,
    )

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        CheckConstraint("total_rows >= 0", name="ck_catalog_imports_total_rows_non_negative"),
        CheckConstraint("valid_rows >= 0", name="ck_catalog_imports_valid_rows_non_negative"),
        CheckConstraint("invalid_rows >= 0", name="ck_catalog_imports_invalid_rows_non_negative"),
        CheckConstraint(
            "created_products >= 0", name="ck_catalog_imports_created_products_non_negative"
        ),
        CheckConstraint(
            "updated_products >= 0", name="ck_catalog_imports_updated_products_non_negative"
        ),
        CheckConstraint(
            "created_variants >= 0", name="ck_catalog_imports_created_variants_non_negative"
        ),
        CheckConstraint(
            "updated_variants >= 0", name="ck_catalog_imports_updated_variants_non_negative"
        ),
        CheckConstraint(
            "updated_inventory_rows >= 0",
            name="ck_catalog_imports_updated_inventory_rows_non_negative",
        ),
    )

    merchant: Mapped[Merchant] = relationship(
        "Merchant",
        back_populates="catalog_imports",
    )

    errors: Mapped[list[CatalogImportError]] = relationship(
        "CatalogImportError",
        back_populates="catalog_import",
        cascade="all, delete-orphan",
    )


class CatalogImportError(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "catalog_import_errors"

    catalog_import_id: Mapped[PythonUUID] = mapped_column(
        ForeignKey("catalog_imports.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(nullable=False)
    message: Mapped[str] = mapped_column(nullable=False)
    row_number: Mapped[int | None] = mapped_column(default=None)
    field: Mapped[str | None] = mapped_column(default=None)
    value: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    catalog_import: Mapped[CatalogImport] = relationship(
        "CatalogImport",
        back_populates="errors",
    )

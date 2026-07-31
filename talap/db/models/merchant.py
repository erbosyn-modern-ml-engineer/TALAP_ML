from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import true

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from talap.db.models.catalog import Product
    from talap.db.models.imports import CatalogImport


class Merchant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "merchants"

    slug: Mapped[str] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=true(),
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_merchants_slug"),
    )

    products: Mapped[list[Product]] = relationship(
        "Product",
        back_populates="merchant",
    )

    catalog_imports: Mapped[list[CatalogImport]] = relationship(
        "CatalogImport",
        back_populates="merchant",
    )

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import true

from talap.db.base import Base
from talap.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from talap.db.models.embeddings import ProductEmbedding
    from talap.db.models.indexing import ProductIndexingTask
    from talap.db.models.merchant import Merchant


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "products"

    merchant_id: Mapped[PythonUUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merchant_product_key: Mapped[str] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(
        nullable=False,
        default="",
        server_default=text("''"),
    )
    active: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=true(),
    )

    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "merchant_product_key",
            name="uq_products_merchant_product_key",
        ),
        # Composite uniqueness so ProductVariant can use a composite FK
        # proving Product and ProductVariant share the same Merchant.
        UniqueConstraint(
            "id",
            "merchant_id",
            name="uq_products_id_merchant_id",
        ),
    )

    merchant: Mapped[Merchant] = relationship(
        "Merchant",
        back_populates="products",
    )

    variants: Mapped[list[ProductVariant]] = relationship(
        "ProductVariant",
        back_populates="product",
    )

    indexing_tasks: Mapped[list[ProductIndexingTask]] = relationship(
        "ProductIndexingTask",
        back_populates="product",
        # The composite FK is non-null and uses ON DELETE CASCADE; SQLAlchemy
        # must not load the task collection or null out child FKs on delete.
        passive_deletes=True,
    )

    embedding: Mapped[ProductEmbedding | None] = relationship(
        "ProductEmbedding",
        back_populates="product",
        uselist=False,
        # The composite FK is non-null and uses ON DELETE CASCADE; SQLAlchemy
        # must never load or null out the embedding row on Product delete.
        passive_deletes=True,
    )


class ProductVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_variants"

    product_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    merchant_id: Mapped[PythonUUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    merchant_sku: Mapped[str] = mapped_column(nullable=False)
    size: Mapped[str | None] = mapped_column(default=None)
    color: Mapped[str | None] = mapped_column(default=None)
    material: Mapped[str | None] = mapped_column(default=None)
    price_kzt: Mapped[int] = mapped_column(nullable=False)
    image_url: Mapped[str | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=true(),
    )

    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "merchant_sku",
            name="uq_product_variants_merchant_sku",
        ),
        CheckConstraint(
            "price_kzt > 0",
            name="ck_product_variants_price_kzt_positive",
        ),
        # Composite FK ensures a variant cannot reference a Product
        # belonging to a different Merchant.
        ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["products.id", "products.merchant_id"],
            ondelete="RESTRICT",
        ),
    )

    product: Mapped[Product] = relationship(
        "Product",
        back_populates="variants",
    )

    inventory: Mapped[Inventory | None] = relationship(
        "Inventory",
        back_populates="product_variant",
        uselist=False,
    )


class Inventory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inventory"

    product_variant_id: Mapped[PythonUUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stock_quantity: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "product_variant_id",
            name="uq_inventory_product_variant_id",
        ),
        CheckConstraint(
            "stock_quantity >= 0",
            name="ck_inventory_stock_quantity_non_negative",
        ),
    )

    product_variant: Mapped[ProductVariant] = relationship(
        "ProductVariant",
        back_populates="inventory",
    )

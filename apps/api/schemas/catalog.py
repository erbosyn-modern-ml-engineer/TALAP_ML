from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from talap.catalog.import_types import CatalogImportSummary
from talap.db.models import (
    CatalogImport,
    CatalogImportStatus,
    Inventory,
    Product,
    ProductVariant,
)


class CatalogImportResponse(BaseModel):
    """Public representation of one catalog import attempt."""

    import_id: UUID
    merchant_id: UUID
    status: CatalogImportStatus

    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    invalid_rows: int = Field(ge=0)

    created_products: int = Field(ge=0)
    updated_products: int = Field(ge=0)
    created_variants: int = Field(ge=0)
    updated_variants: int = Field(ge=0)
    updated_inventory_rows: int = Field(ge=0)

    error_count: int = Field(ge=0)

    @classmethod
    def from_summary(cls, summary: CatalogImportSummary) -> CatalogImportResponse:
        return cls(
            import_id=summary.import_id,
            merchant_id=summary.merchant_id,
            status=summary.status,
            total_rows=summary.total_rows,
            valid_rows=summary.valid_rows,
            invalid_rows=summary.invalid_rows,
            created_products=summary.created_products,
            updated_products=summary.updated_products,
            created_variants=summary.created_variants,
            updated_variants=summary.updated_variants,
            updated_inventory_rows=summary.updated_inventory_rows,
            error_count=summary.error_count,
        )

    @classmethod
    def from_import_record(
        cls,
        import_record: CatalogImport,
        error_count: int,
    ) -> CatalogImportResponse:
        return cls(
            import_id=import_record.id,
            merchant_id=import_record.merchant_id,
            status=import_record.status,
            total_rows=import_record.total_rows,
            valid_rows=import_record.valid_rows,
            invalid_rows=import_record.invalid_rows,
            created_products=import_record.created_products,
            updated_products=import_record.updated_products,
            created_variants=import_record.created_variants,
            updated_variants=import_record.updated_variants,
            updated_inventory_rows=import_record.updated_inventory_rows,
            error_count=error_count,
        )


class ProductCreateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    merchant_sku: str = Field(min_length=1, max_length=150)
    name: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=10_000)

    size: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=100)
    material: str | None = Field(default=None, max_length=200)

    price_kzt: int = Field(gt=0)
    image_url: str | None = Field(default=None, max_length=2048)
    stock_quantity: int = Field(ge=0)
    active: bool = True

    @model_validator(mode="after")
    def _normalize_empty_optionals(self) -> Self:
        for field_name in ("size", "color", "material", "image_url"):
            if getattr(self, field_name) == "":
                setattr(self, field_name, None)
        return self


class ProductPatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=10_000)

    size: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=100)
    material: str | None = Field(default=None, max_length=200)

    price_kzt: int | None = Field(default=None, gt=0)
    image_url: str | None = Field(default=None, max_length=2048)
    stock_quantity: int | None = Field(default=None, ge=0)
    active: bool | None = None

    @model_validator(mode="after")
    def _validate_patch_request(self) -> Self:
        non_nullable = {
            "name",
            "category",
            "description",
            "price_kzt",
            "stock_quantity",
            "active",
        }
        for field_name in non_nullable:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be set to null.")
        for field_name in ("size", "color", "material", "image_url"):
            if getattr(self, field_name) == "":
                setattr(self, field_name, None)
        return self


class ProductResponse(BaseModel):
    product_id: UUID
    variant_id: UUID
    inventory_id: UUID
    merchant_id: UUID

    merchant_sku: str
    name: str
    category: str
    description: str

    size: str | None
    color: str | None
    material: str | None

    price_kzt: int
    image_url: str | None
    stock_quantity: int
    active: bool

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_records(
        cls,
        product: Product,
        variant: ProductVariant,
        inventory: Inventory,
    ) -> ProductResponse:
        updated_at = max(product.updated_at, variant.updated_at, inventory.updated_at)
        return cls(
            product_id=product.id,
            variant_id=variant.id,
            inventory_id=inventory.id,
            merchant_id=product.merchant_id,
            merchant_sku=variant.merchant_sku,
            name=product.name,
            category=product.category,
            description=product.description,
            size=variant.size,
            color=variant.color,
            material=variant.material,
            price_kzt=variant.price_kzt,
            image_url=variant.image_url,
            stock_quantity=inventory.stock_quantity,
            active=product.active,
            created_at=product.created_at,
            updated_at=updated_at,
        )

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from talap.catalog.import_types import CatalogImportSummary
from talap.db.models import CatalogImport, CatalogImportStatus


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

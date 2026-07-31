from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from talap.db.models import CatalogImportStatus

_COUNTER_FIELDS = (
    "total_rows",
    "valid_rows",
    "invalid_rows",
    "created_products",
    "updated_products",
    "created_variants",
    "updated_variants",
    "updated_inventory_rows",
    "error_count",
)


@dataclass(frozen=True)
class CatalogImportSummary:
    """Immutable result of one catalog import attempt."""

    import_id: UUID
    merchant_id: UUID
    status: CatalogImportStatus

    total_rows: int
    valid_rows: int
    invalid_rows: int

    created_products: int
    updated_products: int
    created_variants: int
    updated_variants: int
    updated_inventory_rows: int

    error_count: int

    def __post_init__(self) -> None:
        for field_name in _COUNTER_FIELDS:
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative.")


class CatalogImportErrorBase(Exception):
    pass


class MerchantNotFoundError(CatalogImportErrorBase):
    pass


class MerchantInactiveError(CatalogImportErrorBase):
    pass


class CatalogImportExecutionError(CatalogImportErrorBase):
    pass

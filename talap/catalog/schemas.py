from __future__ import annotations

from datetime import date
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from talap.catalog.errors import CatalogParseError


class CatalogRow(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        frozen=True,
    )

    merchant_slug: str = Field(min_length=1, max_length=100)
    merchant_name: str = Field(min_length=1, max_length=200)
    merchant_sku: str = Field(min_length=1, max_length=150)
    product_name: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)

    description: str = ""
    price_kzt: int = Field(gt=0)
    size: str | None = None
    color: str | None = None
    material: str | None = None
    stock_quantity: int = Field(ge=0)
    image_url: str | None = None
    active: StrictBool

    source_availability: str | None = None
    source_url: str | None = None
    source_checked_at: date | None = None
    data_mode: str | None = None


class CatalogParseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: tuple[CatalogRow, ...]
    errors: tuple[CatalogParseError, ...]
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    invalid_rows: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.valid_rows != len(self.rows):
            raise ValueError(
                f"valid_rows ({self.valid_rows}) must equal len(rows) ({len(self.rows)})."
            )
        if self.total_rows != self.valid_rows + self.invalid_rows:
            raise ValueError(
                f"total_rows ({self.total_rows}) must equal "
                f"valid_rows ({self.valid_rows}) + invalid_rows ({self.invalid_rows})."
            )
        return self

    @property
    def is_valid(self) -> bool:
        return not self.errors
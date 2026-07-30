from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CatalogParseErrorCode(StrEnum):
    EMPTY_FILE = "empty_file"
    DECODE_ERROR = "decode_error"
    MISSING_COLUMNS = "missing_columns"
    DUPLICATE_COLUMNS = "duplicate_columns"
    CSV_FORMAT_ERROR = "csv_format_error"
    ROW_VALIDATION_ERROR = "row_validation_error"


class CatalogParseError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: CatalogParseErrorCode
    message: str
    row_number: int | None = None
    field: str | None = None
    value: str | None = None
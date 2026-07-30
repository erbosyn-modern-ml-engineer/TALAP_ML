from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import ValidationError

from talap.catalog.errors import CatalogParseError, CatalogParseErrorCode
from talap.catalog.schemas import CatalogParseResult, CatalogRow

REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "merchant_slug",
        "merchant_name",
        "merchant_sku",
        "product_name",
        "category",
        "price_kzt",
        "stock_quantity",
        "active",
    }
)

OPTIONAL_COLUMNS: frozenset[str] = frozenset(
    {
        "description",
        "size",
        "color",
        "material",
        "image_url",
        "source_availability",
        "source_url",
        "source_checked_at",
        "data_mode",
    }
)

_INTEGER_FIELDS = {"price_kzt", "stock_quantity"}
_BOOLEAN_FIELDS = {"active"}
_DATE_FIELDS = {"source_checked_at"}
_OPTIONAL_STRING_FIELDS = {
    "size",
    "color",
    "material",
    "image_url",
    "source_availability",
    "source_url",
    "data_mode",
}
_LOWERCASE_FIELDS = {"merchant_slug", "category"}
_THOUSANDS_SEPARATORS = {" ", "\u00A0", "\u202F"}


def parse_catalog_csv(content: bytes) -> CatalogParseResult:
    decoded = _decode_content(content)
    if decoded is None:
        return CatalogParseResult(
            rows=(),
            errors=(
                CatalogParseError(
                    code=CatalogParseErrorCode.DECODE_ERROR,
                    message="Unable to decode catalog as UTF-8.",
                ),
            ),
            total_rows=0,
            valid_rows=0,
            invalid_rows=0,
        )

    if not decoded.strip():
        return CatalogParseResult(
            rows=(),
            errors=(
                CatalogParseError(
                    code=CatalogParseErrorCode.EMPTY_FILE,
                    message="Catalog file is empty.",
                ),
            ),
            total_rows=0,
            valid_rows=0,
            invalid_rows=0,
        )

    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
    except csv.Error as exc:
        return _format_error(str(exc))

    headers, header_errors = _normalize_headers(reader.fieldnames)
    if header_errors:
        return CatalogParseResult(
            rows=(),
            errors=header_errors,
            total_rows=0,
            valid_rows=0,
            invalid_rows=0,
        )

    reader.fieldnames = headers

    parsed_rows: list[CatalogRow] = []
    errors: list[CatalogParseError] = []
    invalid_row_numbers: set[int] = set()
    total_rows = 0

    try:
        for row_number, raw_row in enumerate(reader, start=2):
            total_rows += 1

            if None in raw_row:
                invalid_row_numbers.add(row_number)
                errors.append(
                    CatalogParseError(
                        code=CatalogParseErrorCode.CSV_FORMAT_ERROR,
                        message="Row contains more values than declared columns.",
                        row_number=row_number,
                    )
                )
                continue

            normalized_row = _normalize_row(raw_row, headers)
            try:
                parsed_rows.append(CatalogRow.model_validate(normalized_row))
            except ValidationError as exc:
                invalid_row_numbers.add(row_number)
                errors.extend(
                    _convert_validation_errors(
                        exc=exc,
                        row_number=row_number,
                        raw_row=normalized_row,
                    )
                )
    except csv.Error as exc:
        return _format_error(str(exc))

    return CatalogParseResult(
        rows=tuple(parsed_rows),
        errors=tuple(errors),
        total_rows=total_rows,
        valid_rows=len(parsed_rows),
        invalid_rows=len(invalid_row_numbers),
    )


def _decode_content(content: bytes) -> str | None:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _normalize_headers(
    fieldnames: Sequence[str] | None,
) -> tuple[list[str], tuple[CatalogParseError, ...]]:
    if not fieldnames:
        return [], (
            CatalogParseError(
                code=CatalogParseErrorCode.EMPTY_FILE,
                message="Catalog file is empty.",
            ),
        )

    normalized_headers = [field.strip() for field in fieldnames]
    duplicates = _find_duplicates(normalized_headers)
    if duplicates:
        return [], (
            CatalogParseError(
                code=CatalogParseErrorCode.DUPLICATE_COLUMNS,
                message=f"Duplicate columns: {', '.join(duplicates)}",
            ),
        )

    missing = sorted(column for column in REQUIRED_COLUMNS if column not in normalized_headers)
    if missing:
        return [], (
            CatalogParseError(
                code=CatalogParseErrorCode.MISSING_COLUMNS,
                message=f"Missing required columns: {', '.join(missing)}",
            ),
        )

    return normalized_headers, ()


def _find_duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return sorted(duplicates)


def _normalize_row(raw_row: Mapping[str, Any], headers: list[str]) -> dict[str, Any]:
    normalized_row: dict[str, Any] = {}
    for header in headers:
        raw_value = raw_row.get(header, "")
        normalized_row[header] = _normalize_value(header, raw_value)
    return normalized_row


def _normalize_value(field_name: str, value: Any) -> Any:
    if value is None:
        return None if field_name in _OPTIONAL_STRING_FIELDS or field_name in _DATE_FIELDS else ""

    if isinstance(value, str):
        stripped = value.strip()
        if field_name in _INTEGER_FIELDS:
            return _normalize_integer(stripped)
        if field_name in _BOOLEAN_FIELDS:
            return _normalize_boolean(stripped)
        if field_name in _DATE_FIELDS:
            return _normalize_date(stripped)
        if field_name in _OPTIONAL_STRING_FIELDS:
            return None if stripped == "" else stripped
        if field_name == "description":
            return "" if stripped == "" else stripped
        if field_name in _LOWERCASE_FIELDS:
            return stripped.lower()
        return stripped

    return value


def _normalize_integer(value: str) -> Any:
    return value.translate({ord(sep): None for sep in _THOUSANDS_SEPARATORS})


def _normalize_boolean(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return value


def _normalize_date(value: str) -> Any:
    if value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


def _convert_validation_errors(
    *,
    exc: ValidationError,
    row_number: int,
    raw_row: Mapping[str, Any],
) -> tuple[CatalogParseError, ...]:
    converted: list[CatalogParseError] = []
    for error in exc.errors(include_url=False):
        location = error.get("loc", ())
        field_name = location[0] if location and isinstance(location[0], str) else None
        message = error.get("msg", "Invalid value.")
        value = None
        if field_name is not None:
            raw_value = raw_row.get(field_name)
            if raw_value is not None:
                value = _stringify_value(raw_value)
        converted.append(
            CatalogParseError(
                code=CatalogParseErrorCode.ROW_VALIDATION_ERROR,
                message=message,
                row_number=row_number,
                field=field_name,
                value=value,
            )
        )
    return tuple(converted)


def _stringify_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _format_error(message: str) -> CatalogParseResult:
    return CatalogParseResult(
        rows=(),
        errors=(
            CatalogParseError(
                code=CatalogParseErrorCode.CSV_FORMAT_ERROR,
                message=message or "Invalid CSV format.",
            ),
        ),
        total_rows=0,
        valid_rows=0,
        invalid_rows=0,
    )
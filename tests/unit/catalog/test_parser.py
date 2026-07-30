from __future__ import annotations

import pytest
from pydantic import ValidationError

from talap.catalog import CatalogParseErrorCode, CatalogParseResult, parse_catalog_csv


def test_parse_valid_catalog_with_utf8_bom() -> None:
    csv_text = (
        "merchant_slug,merchant_name,merchant_sku,product_name,category,description,price_kzt,"
        "size,color,material,stock_quantity,image_url,active,source_availability,source_url,"
        "source_checked_at,data_mode\n"
        " merchant-a ,Merchant A,SKU-001,Product One,School,Description one,3 599, ,Blue,Cotton,12,,true,available,https://example.com/1,2026-07-30,demo\n"
        "merchant-b,Merchant B,SKU-002,Product Two,School,Description two,4490,M, Red ,Wool,0,,yes,available,https://example.com/2,2026-07-29,demo\n"
    )

    result = parse_catalog_csv(csv_text.encode("utf-8-sig"))

    assert result.is_valid is True
    assert result.total_rows == 2
    assert result.valid_rows == 2
    assert result.invalid_rows == 0
    assert result.errors == ()

    first_row = result.rows[0]
    assert first_row.price_kzt == 3599
    assert first_row.stock_quantity == 12
    assert first_row.active is True
    assert first_row.size is None
    assert first_row.merchant_slug == "merchant-a"


@pytest.mark.parametrize(
    ("bad_price",),
    [
        ("abc",),
        ("0",),
        ("-100",),
        ("3599.50",),
    ],
)
def test_parse_invalid_price_returns_structured_error(bad_price: str) -> None:
    csv_text = (
        "merchant_slug,merchant_name,merchant_sku,product_name,category,price_kzt,stock_quantity,active\n"
        f"merchant-a,Merchant A,SKU-001,Product One,School,{bad_price},1,true\n"
    )

    result = parse_catalog_csv(csv_text.encode("utf-8"))

    assert result.total_rows == 1
    assert result.valid_rows == 0
    assert result.invalid_rows == 1
    assert result.rows == ()
    assert any(
        error.code == CatalogParseErrorCode.ROW_VALIDATION_ERROR
        and error.row_number == 2
        and error.field == "price_kzt"
        and error.value == bad_price
        for error in result.errors
    )


def test_missing_required_column_returns_file_error() -> None:
    csv_text = (
        "merchant_slug,merchant_name,product_name,category,price_kzt,stock_quantity,active\n"
        "merchant-a,Merchant A,Product One,School,3599,1,true\n"
    )

    result = parse_catalog_csv(csv_text.encode("utf-8"))

    assert result.rows == ()
    assert result.total_rows == 0
    assert result.valid_rows == 0
    assert result.invalid_rows == 0
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.code == CatalogParseErrorCode.MISSING_COLUMNS
    assert error.row_number is None
    assert "merchant_sku" in error.message


def test_trimmed_headers_parse_successfully() -> None:
    """CSV with whitespace-surrounded required headers must parse correctly."""
    csv_text = (
        " merchant_slug , merchant_name ,merchant_sku, product_name ,category,"
        "price_kzt,stock_quantity,active\n"
        "merchant-a,Merchant A,SKU-001,Product One,School,3599,1,true\n"
    )

    result = parse_catalog_csv(csv_text.encode("utf-8"))

    assert result.is_valid is True
    assert result.total_rows == 1
    assert result.valid_rows == 1
    assert result.invalid_rows == 0
    assert result.rows[0].merchant_slug == "merchant-a"
    assert result.rows[0].merchant_name == "Merchant A"
    assert result.rows[0].price_kzt == 3599


@pytest.mark.parametrize(
    ("bad_bool",),
    [
        ("on",),
        ("off",),
        ("maybe",),
        ("active",),
    ],
)
def test_strict_boolean_rejects_invalid_vocabulary(bad_bool: str) -> None:
    """Values outside the accepted boolean vocabulary must fail validation."""
    csv_text = (
        "merchant_slug,merchant_name,merchant_sku,product_name,category,"
        f"price_kzt,stock_quantity,active\n"
        f"merchant-a,Merchant A,SKU-001,Product One,School,3599,1,{bad_bool}\n"
    )

    result = parse_catalog_csv(csv_text.encode("utf-8"))

    assert result.total_rows == 1
    assert result.valid_rows == 0
    assert result.invalid_rows == 1
    assert result.rows == ()
    assert any(
        error.code == CatalogParseErrorCode.ROW_VALIDATION_ERROR
        and error.field == "active"
        for error in result.errors
    )


@pytest.mark.parametrize(
    ("csv_content",),
    [
        pytest.param(
            b'merchant_slug,merchant_name,merchant_sku,product_name,category,price_kzt,stock_quantity,active\n"unclosed,val1,val2,val3,val4,100,1,true\n',
            id="unclosed_quoted_field",
        ),
        pytest.param(
            (
                b"merchant_slug,merchant_name,merchant_sku,product_name,category,"
                b"price_kzt,stock_quantity,active\n"
                b"merchant-a,Merchant A,SKU-001,Product One,School,3599,1,true,extra\n"
            ),
            id="extra_values_in_row",
        ),
    ],
)
def test_csv_format_errors(csv_content: bytes) -> None:
    """Malformed CSV must produce CSV_FORMAT_ERROR."""
    result = parse_catalog_csv(csv_content)

    assert any(
        error.code == CatalogParseErrorCode.CSV_FORMAT_ERROR
        for error in result.errors
    )


def test_extra_values_row_counts_properly() -> None:
    """A row with more values than headers is counted as 1 invalid row."""
    csv_text = (
        "merchant_slug,merchant_name,merchant_sku,product_name,category,"
        "price_kzt,stock_quantity,active\n"
        "merchant-a,Merchant A,SKU-001,Product One,School,3599,1,true,extra_value\n"
    )

    result = parse_catalog_csv(csv_text.encode("utf-8"))

    assert result.total_rows == 1
    assert result.valid_rows == 0
    assert result.invalid_rows == 1


def test_inconsistent_result_raises_validation_error() -> None:
    """Direct construction of an inconsistent CatalogParseResult must fail."""
    with pytest.raises(ValidationError):
        CatalogParseResult(
            rows=(),
            errors=(),
            total_rows=2,
            valid_rows=1,
            invalid_rows=0,
        )
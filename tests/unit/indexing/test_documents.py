from __future__ import annotations

from inspect import signature

from talap.indexing.documents import build_product_index_text


def test_canonical_exact_output() -> None:
    text = build_product_index_text(
        name="School Shirt",
        category="School",
        description="Blue cotton school shirt.",
        material="Cotton",
    )
    assert text == (
        "Name: School Shirt\n"
        "Category: School\n"
        "Description: Blue cotton school shirt.\n"
        "Material: Cotton"
    )


def test_strips_outer_whitespace_of_every_field() -> None:
    text = build_product_index_text(
        name="  School Shirt  ",
        category="\tSchool\t",
        description="  Blue cotton school shirt.  ",
        material="  Cotton  ",
    )
    assert text == (
        "Name: School Shirt\n"
        "Category: School\n"
        "Description: Blue cotton school shirt.\n"
        "Material: Cotton"
    )


def test_normalizes_crlf_and_cr_to_lf() -> None:
    text = build_product_index_text(
        name="School\r\nShirt",
        category="School",
        description="Line one\r\nLine two\rTrailing",
        material=None,
    )
    assert "\r" not in text
    assert text == (
        "Name: School\nShirt\n"
        "Category: School\n"
        "Description: Line one\nLine two\nTrailing\n"
        "Material: "
    )


def test_material_none_and_blank_are_identical() -> None:
    none_text = build_product_index_text(
        name="Shirt",
        category="School",
        description="",
        material=None,
    )
    blank_text = build_product_index_text(
        name="Shirt",
        category="School",
        description="",
        material="   \t ",
    )
    assert none_text == blank_text
    assert none_text.endswith("Material: ")


def test_same_values_always_produce_exact_same_text() -> None:
    values = {
        "name": "Shirt",
        "category": "School",
        "description": "  some description  ",
        "material": "Wool",
    }
    first = build_product_index_text(**values)
    second = build_product_index_text(**values)
    assert first == second


def test_function_contract_excludes_price_stock_sku_and_customer_data() -> None:
    parameters = set(signature(build_product_index_text).parameters)
    assert parameters == {"name", "category", "description", "material"}

    text = build_product_index_text(
        name="Shirt",
        category="School",
        description="A school shirt.",
        material=None,
    ).lower()
    assert "price" not in text
    assert "stock" not in text
    assert "sku" not in text

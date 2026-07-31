from __future__ import annotations

from talap.indexing import SEMANTIC_PRODUCT_FIELDS, semantic_changed_fields


def test_semantic_field_contract_is_deterministic() -> None:
    assert frozenset({"name", "description", "category", "material"}) == (
        SEMANTIC_PRODUCT_FIELDS
    )


def test_only_name_changed() -> None:
    changed = semantic_changed_fields(
        before={"name": "Shirt", "description": "d", "category": "c", "material": None},
        after={"name": "School Shirt", "description": "d", "category": "c", "material": None},
    )
    assert changed == frozenset({"name"})


def test_same_semantic_values_returns_empty() -> None:
    before = {"name": "Shirt", "description": "d", "category": "c", "material": "cotton"}
    changed = semantic_changed_fields(before=before, after=dict(before))
    assert changed == frozenset()


def test_description_change() -> None:
    changed = semantic_changed_fields(
        before={"name": "Shirt", "description": "old", "category": "c", "material": None},
        after={"name": "Shirt", "description": "new", "category": "c", "material": None},
    )
    assert changed == frozenset({"description"})


def test_category_change() -> None:
    changed = semantic_changed_fields(
        before={"name": "Shirt", "description": "d", "category": "school", "material": None},
        after={"name": "Shirt", "description": "d", "category": "uniform", "material": None},
    )
    assert changed == frozenset({"category"})


def test_material_none_to_value() -> None:
    changed = semantic_changed_fields(
        before={"name": "Shirt", "description": "d", "category": "c", "material": None},
        after={"name": "Shirt", "description": "d", "category": "c", "material": "cotton"},
    )
    assert changed == frozenset({"material"})


def test_material_value_to_none() -> None:
    changed = semantic_changed_fields(
        before={"name": "Shirt", "description": "d", "category": "c", "material": "cotton"},
        after={"name": "Shirt", "description": "d", "category": "c", "material": None},
    )
    assert changed == frozenset({"material"})


def test_price_and_stock_changes_ignored() -> None:
    changed = semantic_changed_fields(
        before={
            "name": "Shirt",
            "description": "d",
            "category": "c",
            "material": None,
            "price_kzt": 10000,
            "stock_quantity": 5,
        },
        after={
            "name": "Shirt",
            "description": "d",
            "category": "c",
            "material": None,
            "price_kzt": 12000,
            "stock_quantity": 2,
        },
    )
    assert changed == frozenset()


def test_unrelated_fields_ignored() -> None:
    changed = semantic_changed_fields(
        before={
            "name": "Shirt",
            "description": "d",
            "category": "c",
            "material": None,
            "size": "M",
            "color": "Blue",
            "image_url": "http://a",
            "active": True,
            "merchant_sku": "SKU-1",
        },
        after={
            "name": "Shirt",
            "description": "d",
            "category": "c",
            "material": None,
            "size": "L",
            "color": "Red",
            "image_url": "http://b",
            "active": False,
            "merchant_sku": "SKU-2",
        },
    )
    assert changed == frozenset()

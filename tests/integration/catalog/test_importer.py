from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.catalog import CatalogImportExecutionError, import_catalog_csv
from talap.db.models import (
    CatalogImport,
    CatalogImportError,
    CatalogImportStatus,
    Inventory,
    Merchant,
    Product,
    ProductVariant,
)

_HEADER = (
    "merchant_slug,merchant_name,merchant_sku,product_name,category,description,"
    "price_kzt,size,color,material,stock_quantity,image_url,active"
)

_ROW_1 = (
    "merchant-a,Merchant A,SKU-001,Product One,School,Description one,"
    "3599,M,Blue,Cotton,12,,true"
)
_ROW_2 = (
    "merchant-a,Merchant A,SKU-002,Product Two,School,Description two,"
    "4490,L,Red,Wool,0,,true"
)


def _csv(*rows: str) -> bytes:
    return ("\n".join((_HEADER, *rows)) + "\n").encode("utf-8")


async def _create_merchant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    name: str,
) -> Merchant:
    async with session_factory() as session:
        merchant = Merchant(slug=slug, name=name, active=True)
        session.add(merchant)
        await session.commit()
        return merchant


async def _table_count(session: AsyncSession, model: type[Any]) -> int:
    return (
        await session.execute(select(func.count()).select_from(model))
    ).scalar_one()


async def test_first_valid_import_creates_catalog(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    summary = await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_a.csv",
        content=_csv(_ROW_1, _ROW_2),
        session_factory=session_factory,
    )

    assert summary.status == CatalogImportStatus.COMPLETED
    assert summary.total_rows == 2
    assert summary.valid_rows == 2
    assert summary.invalid_rows == 0
    assert summary.created_products == 2
    assert summary.updated_products == 0
    assert summary.created_variants == 2
    assert summary.updated_variants == 0
    assert summary.updated_inventory_rows == 2
    assert summary.error_count == 0

    assert summary.created_products + summary.updated_products == summary.total_rows
    assert summary.created_variants + summary.updated_variants == summary.total_rows
    assert summary.updated_inventory_rows == summary.total_rows

    imports = (await db_session.execute(select(CatalogImport))).scalars().all()
    assert len(imports) == 1
    assert imports[0].status == CatalogImportStatus.COMPLETED
    assert imports[0].completed_at is not None
    assert imports[0].failed_at is None
    assert imports[0].created_products + imports[0].updated_products == imports[0].total_rows
    assert imports[0].created_variants + imports[0].updated_variants == imports[0].total_rows
    assert imports[0].updated_inventory_rows == imports[0].total_rows

    products = (
        await db_session.execute(select(Product).order_by(Product.merchant_product_key))
    ).scalars().all()
    assert len(products) == 2
    by_key = {product.merchant_product_key: product for product in products}
    assert by_key["SKU-001"].name == "Product One"
    assert by_key["SKU-002"].name == "Product Two"
    assert by_key["SKU-001"].category == "school"
    assert by_key["SKU-001"].description == "Description one"

    variants = (
        await db_session.execute(select(ProductVariant).order_by(ProductVariant.merchant_sku))
    ).scalars().all()
    assert len(variants) == 2
    variant_by_sku = {variant.merchant_sku: variant for variant in variants}
    assert variant_by_sku["SKU-001"].price_kzt == 3599
    assert variant_by_sku["SKU-002"].price_kzt == 4490
    assert variant_by_sku["SKU-001"].merchant_id == merchant.id

    inventory_rows = (await db_session.execute(select(Inventory))).scalars().all()
    assert len(inventory_rows) == 2
    assert {row.stock_quantity for row in inventory_rows} == {0, 12}

    assert await _table_count(db_session, CatalogImportError) == 0


async def test_repeated_import_updates_without_duplicates(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    first = await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_b1.csv",
        content=_csv(_ROW_1),
        session_factory=session_factory,
    )
    assert first.status == CatalogImportStatus.COMPLETED
    assert first.created_products == 1
    assert first.updated_products == 0

    second = await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_b2.csv",
        content=_csv(
            "merchant-a,Merchant A,SKU-001,Product One Updated,School,"
            "Updated description,4000,L,Red,Wool,5,,true"
        ),
        session_factory=session_factory,
    )

    assert second.status == CatalogImportStatus.COMPLETED
    assert second.total_rows == 1
    assert second.created_products == 0
    assert second.updated_products == 1
    assert second.created_variants == 0
    assert second.updated_variants == 1
    assert second.updated_inventory_rows == 1
    assert second.error_count == 0

    assert second.created_products + second.updated_products == second.total_rows
    assert second.created_variants + second.updated_variants == second.total_rows
    assert second.updated_inventory_rows == second.total_rows

    # A repeated import creates a second history row, never an overwrite.
    imports = (await db_session.execute(select(CatalogImport))).scalars().all()
    assert len(imports) == 2
    assert all(item.status == CatalogImportStatus.COMPLETED for item in imports)
    assert all(item.completed_at is not None for item in imports)
    assert all(item.failed_at is None for item in imports)
    assert len({item.id for item in imports}) == 2

    assert await _table_count(db_session, Product) == 1
    assert await _table_count(db_session, ProductVariant) == 1
    assert await _table_count(db_session, Inventory) == 1

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.name == "Product One Updated"
    assert product.description == "Updated description"

    variant = (await db_session.execute(select(ProductVariant))).scalar_one()
    assert variant.price_kzt == 4000

    inventory_row = (await db_session.execute(select(Inventory))).scalar_one()
    assert inventory_row.stock_quantity == 5


async def test_invalid_csv_is_all_or_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    summary = await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_c.csv",
        content=_csv(
            _ROW_1,
            "merchant-a,Merchant A,SKU-002,Product Two,School,Description two,"
            "abc,L,Red,Wool,5,,true",
        ),
        session_factory=session_factory,
    )

    assert summary.status == CatalogImportStatus.FAILED
    assert summary.total_rows == 2
    assert summary.valid_rows == 1
    assert summary.invalid_rows == 1
    assert summary.error_count >= 1
    assert summary.created_products == 0
    assert summary.updated_products == 0
    assert summary.created_variants == 0
    assert summary.updated_variants == 0
    assert summary.updated_inventory_rows == 0

    errors = (await db_session.execute(select(CatalogImportError))).scalars().all()
    assert len(errors) >= 1
    assert all(error.row_number == 3 for error in errors)

    import_row = (await db_session.execute(select(CatalogImport))).scalar_one()
    assert import_row.status == CatalogImportStatus.FAILED
    assert import_row.failed_at is not None
    assert import_row.completed_at is None
    assert import_row.created_products == 0
    assert import_row.updated_products == 0
    assert import_row.created_variants == 0
    assert import_row.updated_variants == 0
    assert import_row.updated_inventory_rows == 0

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0


async def test_duplicate_sku_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    summary = await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_d.csv",
        content=_csv(
            _ROW_1,
            "merchant-a,Merchant A,SKU-001,Product One Duplicate,School,"
            "Description two,4000,L,Red,Wool,5,,true",
        ),
        session_factory=session_factory,
    )

    assert summary.status == CatalogImportStatus.FAILED
    assert summary.total_rows == 2
    assert summary.valid_rows == 1
    assert summary.invalid_rows == 1
    assert summary.error_count == 1
    assert summary.created_products == 0
    assert summary.updated_products == 0
    assert summary.created_variants == 0
    assert summary.updated_variants == 0
    assert summary.updated_inventory_rows == 0

    error = (await db_session.execute(select(CatalogImportError))).scalar_one()
    assert error.code == "duplicate_merchant_sku"
    assert error.field == "merchant_sku"
    assert error.value == "SKU-001"
    assert error.row_number == 3

    import_row = (await db_session.execute(select(CatalogImport))).scalar_one()
    assert import_row.status == CatalogImportStatus.FAILED
    assert import_row.failed_at is not None
    assert import_row.completed_at is None
    assert import_row.created_products == 0
    assert import_row.updated_products == 0
    assert import_row.created_variants == 0
    assert import_row.updated_variants == 0
    assert import_row.updated_inventory_rows == 0

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0


async def test_merchant_slug_mismatch_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    summary = await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_e.csv",
        content=_csv(
            "merchant-b,Merchant B,SKU-001,Product One,School,Description one,"
            "3599,M,Blue,Cotton,12,,true"
        ),
        session_factory=session_factory,
    )

    assert summary.status == CatalogImportStatus.FAILED
    assert summary.total_rows == 1
    assert summary.valid_rows == 0
    assert summary.invalid_rows == 1
    assert summary.error_count == 1
    assert summary.created_products == 0
    assert summary.updated_products == 0
    assert summary.created_variants == 0
    assert summary.updated_variants == 0
    assert summary.updated_inventory_rows == 0

    error = (await db_session.execute(select(CatalogImportError))).scalar_one()
    assert error.code == "merchant_slug_mismatch"
    assert error.field == "merchant_slug"
    assert error.value == "merchant-b"
    assert error.row_number == 2

    import_row = (await db_session.execute(select(CatalogImport))).scalar_one()
    assert import_row.status == CatalogImportStatus.FAILED
    assert import_row.failed_at is not None
    assert import_row.completed_at is None
    assert import_row.created_products == 0
    assert import_row.updated_products == 0
    assert import_row.created_variants == 0
    assert import_row.updated_variants == 0
    assert import_row.updated_inventory_rows == 0

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0


async def test_unexpected_parser_exception_marks_import_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import talap.catalog.importer

    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    def raising_parser(content: bytes) -> None:
        raise RuntimeError("injected parser failure")

    monkeypatch.setattr(talap.catalog.importer, "parse_catalog_csv", raising_parser)

    with pytest.raises(CatalogImportExecutionError) as excinfo:
        await import_catalog_csv(
            merchant_id=merchant.id,
            filename="catalog_parser_crash.csv",
            content=b"irrelevant",
            session_factory=session_factory,
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value) == "Catalog import failed during validation."
    assert "injected parser failure" not in str(excinfo.value)

    imports = (await db_session.execute(select(CatalogImport))).scalars().all()
    assert len(imports) == 1
    assert imports[0].status == CatalogImportStatus.FAILED
    assert imports[0].failed_at is not None
    assert imports[0].completed_at is None

    errors = (await db_session.execute(select(CatalogImportError))).scalars().all()
    assert len(errors) == 1
    assert errors[0].code == "catalog_import_failed"
    assert errors[0].message == "Catalog import failed during validation."
    assert "injected parser failure" not in errors[0].message
    assert errors[0].row_number is None
    assert errors[0].field is None
    assert errors[0].value is None

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0


async def test_database_mutation_failure_rolls_back_catalog_and_marks_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import talap.catalog.importer

    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    async def raising_variant_upsert(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected mutation failure")

    monkeypatch.setattr(
        talap.catalog.importer,
        "_upsert_variants",
        raising_variant_upsert,
    )

    with pytest.raises(CatalogImportExecutionError) as excinfo:
        await import_catalog_csv(
            merchant_id=merchant.id,
            filename="catalog_mutation_crash.csv",
            content=_csv(_ROW_1, _ROW_2),
            session_factory=session_factory,
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value) == "Catalog import failed during database mutation."

    imports = (await db_session.execute(select(CatalogImport))).scalars().all()
    assert len(imports) == 1
    assert imports[0].status == CatalogImportStatus.FAILED
    assert imports[0].failed_at is not None
    assert imports[0].completed_at is None

    errors = (await db_session.execute(select(CatalogImportError))).scalars().all()
    assert len(errors) == 1
    assert errors[0].code == "catalog_import_failed"
    assert errors[0].message == "Catalog import failed during database mutation."
    assert "injected mutation failure" not in errors[0].message

    # The Product upsert was executed inside the rolled-back transaction.
    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0

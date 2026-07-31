from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.catalog import import_catalog_csv
from talap.db.models import (
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

    errors = (await db_session.execute(select(CatalogImportError))).scalars().all()
    assert len(errors) >= 1
    assert all(error.row_number == 3 for error in errors)

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

    error = (await db_session.execute(select(CatalogImportError))).scalar_one()
    assert error.code == "duplicate_merchant_sku"
    assert error.field == "merchant_sku"
    assert error.value == "SKU-001"
    assert error.row_number == 3

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

    error = (await db_session.execute(select(CatalogImportError))).scalar_one()
    assert error.code == "merchant_slug_mismatch"
    assert error.field == "merchant_slug"
    assert error.value == "merchant-b"
    assert error.row_number == 2

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0

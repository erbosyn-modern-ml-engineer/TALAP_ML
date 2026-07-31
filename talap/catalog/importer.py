from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.catalog.errors import CatalogParseError
from talap.catalog.import_types import (
    CatalogImportExecutionError,
    CatalogImportSummary,
    MerchantInactiveError,
    MerchantNotFoundError,
)
from talap.catalog.parser import parse_catalog_csv
from talap.catalog.schemas import CatalogRow
from talap.db import async_session_factory
from talap.db.models import (
    CatalogImport,
    CatalogImportError,
    CatalogImportStatus,
    Inventory,
    Merchant,
    Product,
    ProductVariant,
)

_MAX_FILENAME_LENGTH = 255
_MUTATION_ERROR_MESSAGE = "Catalog import failed during database mutation."
_VALIDATION_ERROR_MESSAGE = "Catalog import failed during validation."


async def import_catalog_csv(
    *,
    merchant_id: UUID,
    filename: str,
    content: bytes,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> CatalogImportSummary:
    """Import one CSV catalog for one Merchant, returning an immutable summary.

    Expected validation failures return a ``FAILED`` summary. Unexpected
    database or programming failures raise ``CatalogImportExecutionError``.
    """
    _validate_filename(filename)
    factory = session_factory or async_session_factory

    # Phase 1: target Merchant validation and a persistent PENDING record.
    async with factory() as session:
        merchant = await session.get(Merchant, merchant_id)
        if merchant is None:
            raise MerchantNotFoundError(f"Merchant {merchant_id} not found.")
        if not merchant.active:
            raise MerchantInactiveError(f"Merchant {merchant_id} is inactive.")
        merchant_slug = merchant.slug

        import_id = uuid4()
        session.add(
            CatalogImport(
                id=import_id,
                merchant_id=merchant_id,
                filename=filename,
                status=CatalogImportStatus.PENDING,
            )
        )
        await session.commit()

    # Phase 2: PENDING → VALIDATING in a new transaction, then parse.
    async with factory() as session:
        await session.execute(
            update(CatalogImport)
            .where(CatalogImport.id == import_id)
            .values(status=CatalogImportStatus.VALIDATING)
        )
        await session.commit()

    try:
        parse_result = parse_catalog_csv(content)
    except Exception as exc:
        try:
            async with factory() as session:
                await _mark_failed_with_safe_error(
                    session,
                    import_id=import_id,
                    message=_VALIDATION_ERROR_MESSAGE,
                )
        except Exception:
            # Recovery persistence failed (e.g., database unavailable). The
            # import may remain VALIDATING; the original parser error must
            # not be hidden behind the recovery failure.
            pass
        raise CatalogImportExecutionError(_VALIDATION_ERROR_MESSAGE) from exc

    if parse_result.errors:
        errors = [_parse_error_to_model(import_id, error) for error in parse_result.errors]
        async with factory() as session:
            await _mark_failed(
                session,
                import_id=import_id,
                total_rows=parse_result.total_rows,
                valid_rows=parse_result.valid_rows,
                invalid_rows=parse_result.invalid_rows,
                errors=errors,
            )
        return _failed_summary(
            import_id=import_id,
            merchant_id=merchant_id,
            total_rows=parse_result.total_rows,
            valid_rows=parse_result.valid_rows,
            invalid_rows=parse_result.invalid_rows,
            error_count=len(errors),
        )

    # Importer-level validation: merchant slug and duplicate SKU checks.
    validation_errors, affected_rows = _validate_rows(
        import_id,
        parse_result.rows,
        merchant_slug,
    )
    if validation_errors:
        invalid_rows = len(affected_rows)
        valid_rows = parse_result.total_rows - invalid_rows
        async with factory() as session:
            await _mark_failed(
                session,
                import_id=import_id,
                total_rows=parse_result.total_rows,
                valid_rows=valid_rows,
                invalid_rows=invalid_rows,
                errors=validation_errors,
            )
        return _failed_summary(
            import_id=import_id,
            merchant_id=merchant_id,
            total_rows=parse_result.total_rows,
            valid_rows=valid_rows,
            invalid_rows=invalid_rows,
            error_count=len(validation_errors),
        )

    # Phase 3: atomic catalog mutation.
    try:
        async with factory() as session:
            return await _run_catalog_mutation(
                session,
                import_id=import_id,
                merchant_id=merchant_id,
                rows=parse_result.rows,
            )
    except Exception as exc:
        try:
            async with factory() as session:
                await _mark_failed_with_safe_error(
                    session,
                    import_id=import_id,
                    message=_MUTATION_ERROR_MESSAGE,
                )
        except Exception:
            # Recovery persistence failed (e.g., database unavailable). The
            # import may remain IMPORTING until a future reconciliation step;
            # the original mutation error must not be hidden.
            pass
        raise CatalogImportExecutionError(_MUTATION_ERROR_MESSAGE) from exc


def _validate_filename(filename: str) -> None:
    if not filename.strip():
        raise ValueError("filename must not be blank.")
    if len(filename) > _MAX_FILENAME_LENGTH:
        raise ValueError("filename must not exceed 255 characters.")


def _validate_rows(
    import_id: UUID,
    rows: Sequence[CatalogRow],
    merchant_slug: str,
) -> tuple[list[CatalogImportError], set[int]]:
    """Collect every merchant-slug mismatch and duplicate-SKU error."""
    errors: list[CatalogImportError] = []
    affected_row_numbers: set[int] = set()
    seen_skus: set[str] = set()

    for row_number, row in enumerate(rows, start=2):
        if row.merchant_slug != merchant_slug:
            errors.append(
                CatalogImportError(
                    catalog_import_id=import_id,
                    code="merchant_slug_mismatch",
                    message="Merchant slug does not match the target merchant.",
                    row_number=row_number,
                    field="merchant_slug",
                    value=row.merchant_slug,
                )
            )
            affected_row_numbers.add(row_number)
        if row.merchant_sku in seen_skus:
            errors.append(
                CatalogImportError(
                    catalog_import_id=import_id,
                    code="duplicate_merchant_sku",
                    message="Duplicate merchant_sku in the same catalog file.",
                    row_number=row_number,
                    field="merchant_sku",
                    value=row.merchant_sku,
                )
            )
            affected_row_numbers.add(row_number)
        seen_skus.add(row.merchant_sku)

    return errors, affected_row_numbers


def _parse_error_to_model(
    import_id: UUID,
    error: CatalogParseError,
) -> CatalogImportError:
    return CatalogImportError(
        catalog_import_id=import_id,
        code=error.code,
        message=error.message,
        row_number=error.row_number,
        field=error.field,
        value=error.value,
    )


async def _mark_failed(
    session: AsyncSession,
    *,
    import_id: UUID,
    total_rows: int,
    valid_rows: int,
    invalid_rows: int,
    errors: Sequence[CatalogImportError],
) -> None:
    await session.execute(
        update(CatalogImport)
        .where(CatalogImport.id == import_id)
        .values(
            status=CatalogImportStatus.FAILED,
            failed_at=func.now(),
            total_rows=total_rows,
            valid_rows=valid_rows,
            invalid_rows=invalid_rows,
        )
    )
    session.add_all(errors)
    await session.commit()


async def _mark_failed_with_safe_error(
    session: AsyncSession,
    *,
    import_id: UUID,
    message: str,
) -> None:
    await session.execute(
        update(CatalogImport)
        .where(CatalogImport.id == import_id)
        .values(status=CatalogImportStatus.FAILED, failed_at=func.now())
    )
    session.add(
        CatalogImportError(
            catalog_import_id=import_id,
            code="catalog_import_failed",
            message=message,
        )
    )
    await session.commit()


async def _run_catalog_mutation(
    session: AsyncSession,
    *,
    import_id: UUID,
    merchant_id: UUID,
    rows: Sequence[CatalogRow],
) -> CatalogImportSummary:
    """Upsert Products, ProductVariants and Inventory atomically."""
    await session.execute(
        update(CatalogImport)
        .where(CatalogImport.id == import_id)
        .values(status=CatalogImportStatus.IMPORTING)
    )

    existing_product_keys = set(
        (
            await session.execute(
                select(Product.merchant_product_key).where(Product.merchant_id == merchant_id)
            )
        ).scalars()
    )
    existing_skus = set(
        (
            await session.execute(
                select(ProductVariant.merchant_sku).where(
                    ProductVariant.merchant_id == merchant_id
                )
            )
        ).scalars()
    )

    product_id_by_key = await _upsert_products(
        session,
        merchant_id=merchant_id,
        rows=rows,
    )
    variant_id_by_sku = await _upsert_variants(
        session,
        merchant_id=merchant_id,
        rows=rows,
        product_id_by_key=product_id_by_key,
    )
    await _upsert_inventory(
        session,
        rows=rows,
        variant_id_by_sku=variant_id_by_sku,
    )

    total_rows = len(rows)
    skus = [row.merchant_sku for row in rows]
    created_products = sum(1 for sku in skus if sku not in existing_product_keys)
    updated_products = total_rows - created_products
    created_variants = sum(1 for sku in skus if sku not in existing_skus)
    updated_variants = total_rows - created_variants
    updated_inventory_rows = total_rows

    await session.execute(
        update(CatalogImport)
        .where(CatalogImport.id == import_id)
        .values(
            status=CatalogImportStatus.COMPLETED,
            completed_at=func.now(),
            total_rows=total_rows,
            valid_rows=total_rows,
            invalid_rows=0,
            created_products=created_products,
            updated_products=updated_products,
            created_variants=created_variants,
            updated_variants=updated_variants,
            updated_inventory_rows=updated_inventory_rows,
        )
    )
    await session.commit()

    return CatalogImportSummary(
        import_id=import_id,
        merchant_id=merchant_id,
        status=CatalogImportStatus.COMPLETED,
        total_rows=total_rows,
        valid_rows=total_rows,
        invalid_rows=0,
        created_products=created_products,
        updated_products=updated_products,
        created_variants=created_variants,
        updated_variants=updated_variants,
        updated_inventory_rows=updated_inventory_rows,
        error_count=0,
    )


async def _upsert_products(
    session: AsyncSession,
    *,
    merchant_id: UUID,
    rows: Sequence[CatalogRow],
) -> dict[str, UUID]:
    """Batch upsert Products; return ``{merchant_product_key: id}``."""
    product_rows: list[dict[str, object]] = []
    for row in rows:
        product_rows.append(
            {
                "id": uuid4(),
                "merchant_id": merchant_id,
                "merchant_product_key": row.merchant_sku,
                "name": row.product_name,
                "category": row.category,
                "description": row.description,
                "active": row.active,
            }
        )
    product_stmt = insert(Product).values(product_rows)
    product_result = await session.execute(
        product_stmt.on_conflict_do_update(
            constraint="uq_products_merchant_product_key",
            set_={
                "name": product_stmt.excluded.name,
                "category": product_stmt.excluded.category,
                "description": product_stmt.excluded.description,
                "active": product_stmt.excluded.active,
                "updated_at": func.now(),
            },
        ).returning(Product.id, Product.merchant_product_key)
    )
    product_id_by_key: dict[str, UUID] = {}
    for product_id, product_key in product_result.all():
        product_id_by_key[product_key] = product_id
    return product_id_by_key


async def _upsert_variants(
    session: AsyncSession,
    *,
    merchant_id: UUID,
    rows: Sequence[CatalogRow],
    product_id_by_key: dict[str, UUID],
) -> dict[str, UUID]:
    """Batch upsert ProductVariants; return ``{merchant_sku: id}``."""
    variant_rows: list[dict[str, object]] = []
    for row in rows:
        variant_rows.append(
            {
                "id": uuid4(),
                "merchant_id": merchant_id,
                "product_id": product_id_by_key[row.merchant_sku],
                "merchant_sku": row.merchant_sku,
                "size": row.size,
                "color": row.color,
                "material": row.material,
                "price_kzt": row.price_kzt,
                "image_url": row.image_url,
                "active": row.active,
            }
        )
    variant_stmt = insert(ProductVariant).values(variant_rows)
    variant_result = await session.execute(
        variant_stmt.on_conflict_do_update(
            constraint="uq_product_variants_merchant_sku",
            set_={
                "product_id": variant_stmt.excluded.product_id,
                "size": variant_stmt.excluded.size,
                "color": variant_stmt.excluded.color,
                "material": variant_stmt.excluded.material,
                "price_kzt": variant_stmt.excluded.price_kzt,
                "image_url": variant_stmt.excluded.image_url,
                "active": variant_stmt.excluded.active,
                "updated_at": func.now(),
            },
        ).returning(ProductVariant.id, ProductVariant.merchant_sku)
    )
    variant_id_by_sku: dict[str, UUID] = {}
    for variant_id, sku in variant_result.all():
        variant_id_by_sku[sku] = variant_id
    return variant_id_by_sku


async def _upsert_inventory(
    session: AsyncSession,
    *,
    rows: Sequence[CatalogRow],
    variant_id_by_sku: dict[str, UUID],
) -> None:
    """Batch upsert Inventory rows keyed by ``product_variant_id``."""
    inventory_rows: list[dict[str, object]] = []
    for row in rows:
        inventory_rows.append(
            {
                "id": uuid4(),
                "product_variant_id": variant_id_by_sku[row.merchant_sku],
                "stock_quantity": row.stock_quantity,
            }
        )
    inventory_stmt = insert(Inventory).values(inventory_rows)
    await session.execute(
        inventory_stmt.on_conflict_do_update(
            constraint="uq_inventory_product_variant_id",
            set_={
                "stock_quantity": inventory_stmt.excluded.stock_quantity,
                "updated_at": func.now(),
            },
        )
    )


def _failed_summary(
    *,
    import_id: UUID,
    merchant_id: UUID,
    total_rows: int,
    valid_rows: int,
    invalid_rows: int,
    error_count: int,
) -> CatalogImportSummary:
    return CatalogImportSummary(
        import_id=import_id,
        merchant_id=merchant_id,
        status=CatalogImportStatus.FAILED,
        total_rows=total_rows,
        valid_rows=valid_rows,
        invalid_rows=invalid_rows,
        created_products=0,
        updated_products=0,
        created_variants=0,
        updated_variants=0,
        updated_inventory_rows=0,
        error_count=error_count,
    )

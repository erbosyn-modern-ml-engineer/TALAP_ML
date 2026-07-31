from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.schemas.catalog import (
    ProductCreateRequest,
    ProductPatchRequest,
    ProductResponse,
)
from talap.catalog.import_types import MerchantInactiveError, MerchantNotFoundError
from talap.db.models import Inventory, Merchant, Product, ProductVariant
from talap.indexing import schedule_product_indexing
from talap.indexing.decisions import semantic_changed_fields

_PRODUCT_DUPLICATE_CONSTRAINTS = frozenset(
    {
        "uq_products_merchant_product_key",
        "uq_product_variants_merchant_sku",
    }
)


class ProductAlreadyExistsError(Exception):
    pass


class ProductWriteError(Exception):
    pass


class ProductNotFoundError(Exception):
    pass


class ProductStateError(Exception):
    pass


async def create_product(
    *,
    merchant_id: UUID,
    payload: ProductCreateRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> ProductResponse:
    async with session_factory() as session:
        try:
            merchant = await session.get(Merchant, merchant_id)
            if merchant is None:
                raise MerchantNotFoundError(f"Merchant {merchant_id} not found.")
            if not merchant.active:
                raise MerchantInactiveError(f"Merchant {merchant_id} is inactive.")

            product = Product(
                id=uuid4(),
                merchant_id=merchant_id,
                merchant_product_key=payload.merchant_sku,
                name=payload.name,
                category=payload.category,
                description=payload.description,
                active=payload.active,
            )
            variant = ProductVariant(
                id=uuid4(),
                merchant_id=merchant_id,
                product_id=product.id,
                merchant_sku=payload.merchant_sku,
                size=payload.size,
                color=payload.color,
                material=payload.material,
                price_kzt=payload.price_kzt,
                image_url=payload.image_url,
                active=payload.active,
            )
            inventory = Inventory(
                id=uuid4(),
                product_variant_id=variant.id,
                stock_quantity=payload.stock_quantity,
            )
            session.add_all([product, variant, inventory])
            await schedule_product_indexing(
                session=session,
                merchant_id=merchant_id,
                product_id=product.id,
                changed_fields=["category", "description", "material", "name"],
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if _is_duplicate_sku(exc):
                raise ProductAlreadyExistsError(
                    "A product with this merchant SKU already exists."
                ) from exc
            raise ProductWriteError("Product could not be saved.") from exc
        except (MerchantNotFoundError, MerchantInactiveError):
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            raise ProductWriteError("Product could not be saved.") from exc

    return ProductResponse.from_records(product, variant, inventory)


async def patch_product(
    *,
    product_id: UUID,
    payload: ProductPatchRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> ProductResponse:
    if not payload.model_fields_set:
        raise ValueError("At least one product field must be provided.")

    async with session_factory() as session:
        try:
            product = (
                await session.execute(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
            ).scalar_one_or_none()
            if product is None:
                raise ProductNotFoundError(f"Product {product_id} not found.")

            variants = (
                await session.execute(
                    select(ProductVariant)
                    .where(ProductVariant.product_id == product_id)
                    .with_for_update()
                )
            ).scalars().all()
            if len(variants) != 1:
                raise ProductStateError("Product catalog state is inconsistent.")
            variant = variants[0]

            inventory = (
                await session.execute(
                    select(Inventory)
                    .where(Inventory.product_variant_id == variant.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if inventory is None:
                raise ProductStateError("Product catalog state is inconsistent.")

            before_semantic = _semantic_values(product, variant)

            changes = payload.model_dump(exclude_unset=True)

            product_changed = _apply_product_changes(product, changes)
            if product_changed:
                product.updated_at = cast(datetime, func.now())
                await session.flush()

            variant_changed = _apply_variant_changes(variant, changes)
            if variant_changed:
                variant.updated_at = cast(datetime, func.now())
                await session.flush()

            inventory_changed = _apply_inventory_changes(inventory, changes)
            if inventory_changed:
                inventory.updated_at = cast(datetime, func.now())
                await session.flush()

            after_semantic = _semantic_values(product, variant)
            semantic_fields = semantic_changed_fields(
                before=before_semantic,
                after=after_semantic,
            )
            if semantic_fields:
                await schedule_product_indexing(
                    session=session,
                    merchant_id=product.merchant_id,
                    product_id=product.id,
                    changed_fields=semantic_fields,
                )

            changed_entities: list[Product | ProductVariant | Inventory] = []
            if product_changed:
                changed_entities.append(product)
            if variant_changed:
                changed_entities.append(variant)
            if inventory_changed:
                changed_entities.append(inventory)
            for entity in changed_entities:
                await session.refresh(entity)

            await session.commit()
        except (ProductNotFoundError, ProductStateError):
            await session.rollback()
            raise
        except Exception as exc:
            await session.rollback()
            raise ProductWriteError("Product could not be saved.") from exc

    return ProductResponse.from_records(product, variant, inventory)


def _semantic_values(product: Product, variant: ProductVariant) -> dict[str, object]:
    return {
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "material": variant.material,
    }


def _apply_product_changes(
    product: Product,
    changes: Mapping[str, Any],
) -> bool:
    changed = False
    for field_name in ("name", "category", "description", "active"):
        if field_name in changes:
            setattr(product, field_name, changes[field_name])
            changed = True
    return changed


def _apply_variant_changes(
    variant: ProductVariant,
    changes: Mapping[str, Any],
) -> bool:
    changed = False
    for field_name in (
        "size",
        "color",
        "material",
        "price_kzt",
        "image_url",
        "active",
    ):
        if field_name in changes:
            setattr(variant, field_name, changes[field_name])
            changed = True
    return changed


def _apply_inventory_changes(
    inventory: Inventory,
    changes: Mapping[str, Any],
) -> bool:
    if "stock_quantity" in changes:
        inventory.stock_quantity = changes["stock_quantity"]
        return True
    return False


def _is_duplicate_sku(exc: IntegrityError) -> bool:
    constraint_name = _constraint_name_from_error(exc)
    return constraint_name in _PRODUCT_DUPLICATE_CONSTRAINTS


def _constraint_name_from_error(exc: IntegrityError) -> str | None:
    current: object = exc.orig
    for _ in range(4):
        constraint_name = getattr(current, "constraint_name", None)
        if isinstance(constraint_name, str) and constraint_name:
            return constraint_name
        current = getattr(current, "__cause__", None)
        if current is None:
            return None
    return None

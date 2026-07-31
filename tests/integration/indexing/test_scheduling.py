from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies.database import get_api_session_factory
from apps.api.main import app
from talap.catalog import CatalogImportExecutionError, import_catalog_csv
from talap.core.config import Settings, get_settings
from talap.db.models import (
    CatalogImport,
    CatalogImportStatus,
    Inventory,
    Merchant,
    Product,
    ProductIndexingTask,
    ProductIndexingTaskStatus,
    ProductVariant,
)

_TEST_TOKEN = "api-test-token"
_AUTH_HEADERS = {"X-Internal-Service-Token": _TEST_TOKEN}

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

_PRODUCT_PAYLOAD = {
    "merchant_sku": "SKU-MANUAL-001",
    "name": "Manual Product",
    "category": "school",
    "description": "Created via API",
    "price_kzt": 2500,
    "stock_quantity": 7,
    "active": True,
}

_FULL_SEMANTIC_FIELDS = ["category", "description", "material", "name"]


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


async def _tasks(session: AsyncSession) -> list[ProductIndexingTask]:
    return (
        await session.execute(
            select(ProductIndexingTask).order_by(
                ProductIndexingTask.created_at,
                ProductIndexingTask.id,
            )
        )
    ).scalars().all()


@pytest_asyncio.fixture
async def api_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    test_settings = Settings(internal_service_token=_TEST_TOKEN)
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_api_session_factory] = lambda: session_factory
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_manual_create_schedules_one_task(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json={**_PRODUCT_PAYLOAD, "material": "Cotton"},
    )
    assert response.status_code == 201

    tasks = await _tasks(db_session)
    assert len(tasks) == 1
    assert tasks[0].status == ProductIndexingTaskStatus.PENDING
    assert tasks[0].changed_fields == _FULL_SEMANTIC_FIELDS
    assert tasks[0].product_id == UUID(response.json()["product_id"])
    assert tasks[0].merchant_id == merchant.id
    assert tasks[0].attempts == 0
    assert tasks[0].started_at is None
    assert tasks[0].completed_at is None
    assert tasks[0].last_error is None


async def test_semantic_patch_schedules_one_additional_task(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )
    assert created.status_code == 201
    product_id = UUID(created.json()["product_id"])

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={"name": "Renamed Product"},
    )
    assert response.status_code == 200

    tasks = await _tasks(db_session)
    assert len(tasks) == 2
    assert tasks[1].status == ProductIndexingTaskStatus.PENDING
    assert tasks[1].changed_fields == ["name"]
    assert tasks[1].product_id == product_id


async def test_price_stock_patch_schedules_no_task(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )
    assert created.status_code == 201
    product_id = UUID(created.json()["product_id"])

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={"price_kzt": 3000, "stock_quantity": 3},
    )
    assert response.status_code == 200

    assert len(await _tasks(db_session)) == 1


async def test_same_value_semantic_patch_schedules_no_task(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )
    assert created.status_code == 201
    product_id = UUID(created.json()["product_id"])

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={"name": "Manual Product"},
    )
    assert response.status_code == 200

    assert len(await _tasks(db_session)) == 1


async def test_first_csv_import_schedules_two_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_first.csv",
        content=_csv(_ROW_1, _ROW_2),
        session_factory=session_factory,
    )

    tasks = await _tasks(db_session)
    assert len(tasks) == 2
    for task in tasks:
        assert task.status == ProductIndexingTaskStatus.PENDING
        assert task.changed_fields == _FULL_SEMANTIC_FIELDS


async def test_repeated_identical_csv_schedules_no_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    content = _csv(_ROW_1, _ROW_2)
    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_1.csv",
        content=content,
        session_factory=session_factory,
    )
    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_2.csv",
        content=content,
        session_factory=session_factory,
    )

    assert len(await _tasks(db_session)) == 2


async def test_csv_price_stock_update_schedules_no_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_1.csv",
        content=_csv(_ROW_1),
        session_factory=session_factory,
    )
    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_2.csv",
        content=_csv(
            "merchant-a,Merchant A,SKU-001,Product One,School,Description one,"
            "4000,M,Blue,Cotton,5,,true"
        ),
        session_factory=session_factory,
    )

    assert len(await _tasks(db_session)) == 1


async def test_csv_semantic_update_schedules_one_task(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_1.csv",
        content=_csv(_ROW_1),
        session_factory=session_factory,
    )
    await import_catalog_csv(
        merchant_id=merchant.id,
        filename="catalog_2.csv",
        content=_csv(
            "merchant-a,Merchant A,SKU-001,Renamed Product,School,Description one,"
            "3599,M,Blue,Cotton,12,,true"
        ),
        session_factory=session_factory,
    )

    tasks = await _tasks(db_session)
    assert len(tasks) == 2
    assert tasks[1].changed_fields == ["name"]


async def test_indexing_failure_rolls_back_manual_patch(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.services import catalog as catalog_service

    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )
    assert created.status_code == 201
    product_id = UUID(created.json()["product_id"])

    async def raising_schedule(session: AsyncSession, **kwargs: Any) -> None:
        raise RuntimeError("injected indexing failure")

    monkeypatch.setattr(
        catalog_service,
        "schedule_product_indexing",
        raising_schedule,
    )

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={"name": "Should Not Persist"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Product could not be saved."

    product = (
        await db_session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one()
    assert product.name == "Manual Product"

    tasks = await _tasks(db_session)
    assert len(tasks) == 1
    assert tasks[0].changed_fields == _FULL_SEMANTIC_FIELDS


async def test_indexing_failure_rolls_back_csv_import(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import talap.catalog.importer

    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    async def raising_schedule(session: AsyncSession, **kwargs: Any) -> None:
        raise RuntimeError("injected indexing failure")

    monkeypatch.setattr(
        talap.catalog.importer,
        "_schedule_indexing_tasks",
        raising_schedule,
    )

    with pytest.raises(CatalogImportExecutionError):
        await import_catalog_csv(
            merchant_id=merchant.id,
            filename="catalog_crash.csv",
            content=_csv(_ROW_1),
            session_factory=session_factory,
        )

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0
    assert await _table_count(db_session, ProductIndexingTask) == 0

    import_row = (await db_session.execute(select(CatalogImport))).scalar_one()
    assert import_row.status == CatalogImportStatus.FAILED
    assert import_row.failed_at is not None


async def test_deleting_product_cascades_indexing_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    product = Product(
        id=uuid4(),
        merchant_id=merchant.id,
        merchant_product_key="SKU-DELETE-001",
        name="To Delete",
        category="school",
        description="",
        active=True,
    )
    task = ProductIndexingTask(
        id=uuid4(),
        merchant_id=merchant.id,
        product_id=product.id,
        changed_fields=["name"],
    )

    async with session_factory() as session:
        session.add_all([product, task])
        await session.commit()
        product_id = product.id

    # Load the Product WITHOUT preloading indexing_tasks, then delete it. The
    # ORM must rely on PostgreSQL ON DELETE CASCADE (passive_deletes=True).
    async with session_factory() as session:
        loaded = await session.get(Product, product_id)
        assert loaded is not None
        await session.delete(loaded)
        await session.commit()

    async with session_factory() as session:
        assert await session.get(Product, product_id) is None
        remaining_tasks = (
            await session.execute(
                select(ProductIndexingTask.id).where(
                    ProductIndexingTask.product_id == product_id
                )
            )
        ).scalars().all()
        assert remaining_tasks == []

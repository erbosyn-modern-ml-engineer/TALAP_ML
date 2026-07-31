from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies.database import get_api_session_factory
from apps.api.main import app
from talap.core.config import Settings, get_settings
from talap.db.models import (
    CatalogImport,
    CatalogImportError,
    Inventory,
    Merchant,
    Product,
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


def _csv(*rows: str) -> bytes:
    return ("\n".join((_HEADER, *rows)) + "\n").encode("utf-8")


async def _create_merchant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    name: str,
    active: bool = True,
) -> Merchant:
    async with session_factory() as session:
        merchant = Merchant(slug=slug, name=name, active=active)
        session.add(merchant)
        await session.commit()
        return merchant


async def _table_count(session: AsyncSession, model: type[Any]) -> int:
    return (
        await session.execute(select(func.count()).select_from(model))
    ).scalar_one()


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


@pytest.mark.parametrize(
    "request_headers",
    [
        {},
        {"X-Internal-Service-Token": "wrong-token"},
    ],
)
async def test_auth_missing_or_wrong_token_rejected(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    request_headers: dict[str, str],
) -> None:
    response = await api_client.post(
        f"/api/v1/merchants/{uuid4()}/catalog/import",
        headers=request_headers,
        files={"file": ("catalog.csv", _csv(_ROW_1), "text/csv")},
    )

    assert response.status_code == 401
    assert await _table_count(db_session, CatalogImport) == 0


async def test_valid_import_returns_201(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/catalog/import",
        headers=_AUTH_HEADERS,
        files={"file": ("catalog_api.csv", _csv(_ROW_1), "text/csv")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["merchant_id"] == str(merchant.id)
    assert body["created_products"] == 1
    assert body["created_variants"] == 1
    assert body["updated_inventory_rows"] == 1
    assert response.headers["location"] == (
        f"/api/v1/catalog/imports/{body['import_id']}"
    )

    assert await _table_count(db_session, Product) == 1
    assert await _table_count(db_session, ProductVariant) == 1
    assert await _table_count(db_session, Inventory) == 1


async def test_failed_csv_attempt_returns_201_failed(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/catalog/import",
        headers=_AUTH_HEADERS,
        files={
            "file": (
                "catalog_bad.csv",
                _csv(
                    "merchant-a,Merchant A,SKU-001,Product One,School,"
                    "Description one,abc,M,Blue,Cotton,12,,true"
                ),
                "text/csv",
            )
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_count"] > 0

    assert await _table_count(db_session, CatalogImport) == 1
    assert await _table_count(db_session, CatalogImportError) >= 1
    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0


async def test_get_import_status(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    post_response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/catalog/import",
        headers=_AUTH_HEADERS,
        files={"file": ("catalog_api.csv", _csv(_ROW_1), "text/csv")},
    )
    assert post_response.status_code == 201
    post_body = post_response.json()

    get_response = await api_client.get(
        post_response.headers["location"],
        headers=_AUTH_HEADERS,
    )

    assert get_response.status_code == 200
    get_body = get_response.json()
    assert get_body["import_id"] == post_body["import_id"]
    assert get_body["merchant_id"] == post_body["merchant_id"]
    assert get_body["status"] == post_body["status"] == "completed"
    assert get_body["total_rows"] == post_body["total_rows"] == 1
    assert get_body["created_products"] == post_body["created_products"] == 1
    assert get_body["created_variants"] == post_body["created_variants"] == 1
    assert get_body["updated_inventory_rows"] == post_body["updated_inventory_rows"] == 1


async def test_get_missing_import_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get(
        f"/api/v1/catalog/imports/{uuid4()}",
        headers=_AUTH_HEADERS,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Catalog import not found."


async def test_unknown_merchant_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        f"/api/v1/merchants/{uuid4()}/catalog/import",
        headers=_AUTH_HEADERS,
        files={"file": ("catalog.csv", _csv(_ROW_1), "text/csv")},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Merchant not found."


async def test_inactive_merchant_returns_409(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(
        session_factory,
        slug="merchant-a",
        name="Merchant A",
        active=False,
    )

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/catalog/import",
        headers=_AUTH_HEADERS,
        files={"file": ("catalog.csv", _csv(_ROW_1), "text/csv")},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Merchant is inactive."


async def test_non_csv_filename_returns_422(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/catalog/import",
        headers=_AUTH_HEADERS,
        files={"file": ("catalog.txt", _csv(_ROW_1), "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid catalog upload."


async def test_oversized_file_returns_413(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    oversized = b"x" * (5 * 1024 * 1024 + 1)
    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/catalog/import",
        headers=_AUTH_HEADERS,
        files={"file": ("catalog.csv", oversized, "text/csv")},
    )

    assert response.status_code == 413


def test_openapi_contains_catalog_paths() -> None:
    openapi = app.openapi()
    paths = openapi["paths"]

    assert "/api/v1/merchants/{merchant_id}/catalog/import" in paths
    assert "/api/v1/catalog/imports/{import_id}" in paths

    import_operation = paths["/api/v1/merchants/{merchant_id}/catalog/import"]["post"]
    assert "multipart/form-data" in import_operation["requestBody"]["content"]

    security = import_operation.get("security")
    assert security, "import endpoint must declare security"

    schemes = openapi["components"]["securitySchemes"]
    declared = any(
        schemes[scheme_name]["type"] == "apiKey"
        and schemes[scheme_name]["in"] == "header"
        and schemes[scheme_name]["name"] == "X-Internal-Service-Token"
        for entry in security
        for scheme_name in entry
    )
    assert declared, (
        "import endpoint must declare X-Internal-Service-Token apiKey security"
    )

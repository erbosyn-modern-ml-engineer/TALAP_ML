from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

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

_PRODUCT_PAYLOAD = {
    "merchant_sku": "SKU-MANUAL-001",
    "name": "Manual Product",
    "category": "school",
    "description": "Created via API",
    "price_kzt": 2500,
    "stock_quantity": 7,
    "active": True,
}


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


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "/api/v1/merchants/{merchant_id}/products"),
        ("PATCH", "/api/v1/products/{product_id}"),
    ],
)
async def test_product_routes_require_token(
    api_client: httpx.AsyncClient,
    method: str,
    url: str,
) -> None:
    resolved = url.replace("{merchant_id}", str(uuid4())).replace(
        "{product_id}", str(uuid4())
    )
    response = await api_client.request(method, resolved, headers={})
    assert response.status_code == 401


async def test_create_product_returns_201(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["merchant_id"] == str(merchant.id)
    assert body["merchant_sku"] == "SKU-MANUAL-001"
    assert body["name"] == "Manual Product"
    assert body["price_kzt"] == 2500
    assert body["stock_quantity"] == 7
    assert body["product_id"] != body["variant_id"] != body["inventory_id"]
    assert response.headers["location"] == f"/api/v1/products/{body['product_id']}"

    assert await _table_count(db_session, Product) == 1
    assert await _table_count(db_session, ProductVariant) == 1
    assert await _table_count(db_session, Inventory) == 1

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.merchant_product_key == "SKU-MANUAL-001"
    assert product.merchant_id == merchant.id
    assert product.active is True

    variant = (await db_session.execute(select(ProductVariant))).scalar_one()
    assert variant.merchant_sku == "SKU-MANUAL-001"
    assert variant.price_kzt == 2500
    assert variant.active == product.active

    inventory_row = (await db_session.execute(select(Inventory))).scalar_one()
    assert inventory_row.stock_quantity == 7


async def test_duplicate_sku_returns_409(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    first = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )
    assert first.status_code == 201

    second = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )

    assert second.status_code == 409
    assert second.json()["detail"] == (
        "A product with this merchant SKU already exists."
    )
    assert await _table_count(db_session, Product) == 1
    assert await _table_count(db_session, ProductVariant) == 1
    assert await _table_count(db_session, Inventory) == 1


async def test_create_product_unknown_merchant_404(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        f"/api/v1/merchants/{uuid4()}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Merchant not found."


async def test_create_product_inactive_merchant_409(
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
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Merchant is inactive."


@pytest.mark.parametrize(
    "extra_field",
    [
        "merchant_id",
        "merchant_product_key",
        "product_id",
        "variant_id",
        "inventory_id",
        "created_at",
        "updated_at",
    ],
)
async def test_create_product_rejects_identity_fields(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    extra_field: str,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")

    response = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json={**_PRODUCT_PAYLOAD, extra_field: "some-value"},
    )

    assert response.status_code == 422
    error_types = [error.get("type", "") for error in response.json()["detail"]]
    assert "extra_forbidden" in error_types

    assert await _table_count(db_session, Product) == 0
    assert await _table_count(db_session, ProductVariant) == 0
    assert await _table_count(db_session, Inventory) == 0


@pytest.mark.parametrize(
    "extra_field",
    [
        "merchant_sku",
        "merchant_id",
        "merchant_product_key",
        "product_id",
    ],
)
async def test_patch_product_rejects_identity_fields(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    extra_field: str,
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
        json={extra_field: "some-value"},
    )

    assert response.status_code == 422
    error_types = [error.get("type", "") for error in response.json()["detail"]]
    assert "extra_forbidden" in error_types

    product = (
        await db_session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one()
    assert product.merchant_product_key == "SKU-MANUAL-001"
    assert product.name == "Manual Product"

    variant = (
        await db_session.execute(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        )
    ).scalar_one()
    assert variant.merchant_sku == "SKU-MANUAL-001"


async def test_patch_semantic_fields(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json={**_PRODUCT_PAYLOAD, "color": "Blue", "material": "Cotton"},
    )
    assert created.status_code == 201
    product_id = UUID(created.json()["product_id"])

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={
            "name": "Renamed Product",
            "description": "Updated description",
            "color": "Green",
            "material": "Linen",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Product"
    assert body["description"] == "Updated description"
    assert body["color"] == "Green"
    assert body["material"] == "Linen"
    assert body["merchant_sku"] == "SKU-MANUAL-001"

    product = (
        await db_session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one()
    assert product.name == "Renamed Product"
    assert product.description == "Updated description"
    assert product.merchant_id == merchant.id

    variant = (
        await db_session.execute(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        )
    ).scalar_one()
    assert variant.color == "Green"
    assert variant.material == "Linen"
    assert variant.merchant_sku == "SKU-MANUAL-001"
    assert variant.merchant_id == merchant.id


async def test_patch_price_and_stock(
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
    body = response.json()
    assert body["price_kzt"] == 3000
    assert body["stock_quantity"] == 3
    assert body["name"] == "Manual Product"

    assert await _table_count(db_session, Product) == 1
    assert await _table_count(db_session, ProductVariant) == 1
    assert await _table_count(db_session, Inventory) == 1

    variant = (
        await db_session.execute(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        )
    ).scalar_one()
    assert variant.price_kzt == 3000

    inventory_row = (
        await db_session.execute(
            select(Inventory).where(Inventory.product_variant_id == variant.id)
        )
    ).scalar_one()
    assert inventory_row.stock_quantity == 3


async def test_patch_active(
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
        json={"active": False},
    )

    assert response.status_code == 200
    assert response.json()["active"] is False

    product = (
        await db_session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one()
    assert product.active is False

    variant = (
        await db_session.execute(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        )
    ).scalar_one()
    assert variant.active is False


async def test_patch_clear_nullable_field(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json={**_PRODUCT_PAYLOAD, "color": "Blue"},
    )
    assert created.status_code == 201
    product_id = UUID(created.json()["product_id"])

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={"color": None},
    )

    assert response.status_code == 200
    assert response.json()["color"] is None

    variant = (
        await db_session.execute(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        )
    ).scalar_one()
    assert variant.color is None


async def test_patch_empty_payload_returns_422(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory, slug="merchant-a", name="Merchant A")
    created = await api_client.post(
        f"/api/v1/merchants/{merchant.id}/products",
        headers=_AUTH_HEADERS,
        json=_PRODUCT_PAYLOAD,
    )
    assert created.status_code == 201
    product_id = created.json()["product_id"]

    response = await api_client.patch(
        f"/api/v1/products/{product_id}",
        headers=_AUTH_HEADERS,
        json={},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "At least one product field must be provided."
    )


async def test_patch_missing_product_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.patch(
        f"/api/v1/products/{uuid4()}",
        headers=_AUTH_HEADERS,
        json={"name": "X"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found."


async def test_product_patch_failure_rolls_back_all_tables(
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

    def raising_apply_variant(_variant: object, _changes: object) -> bool:
        raise RuntimeError("injected patch failure")

    monkeypatch.setattr(
        catalog_service,
        "_apply_variant_changes",
        raising_apply_variant,
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

    variant = (
        await db_session.execute(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        )
    ).scalar_one()
    assert variant.price_kzt == 2500

    inventory_row = (
        await db_session.execute(
            select(Inventory).where(Inventory.product_variant_id == variant.id)
        )
    ).scalar_one()
    assert inventory_row.stock_quantity == 7


def _fake_integrity_error(
    *,
    constraint_name: str | None,
    sqlstate: str = "23505",
) -> Any:
    cause = SimpleNamespace(constraint_name=constraint_name, sqlstate=sqlstate)
    orig = SimpleNamespace(__cause__=cause, sqlstate=sqlstate)
    return SimpleNamespace(orig=orig)


def test_is_duplicate_sku_helper() -> None:
    from apps.api.services.catalog import _is_duplicate_sku

    known_product = _fake_integrity_error(
        constraint_name="uq_products_merchant_product_key"
    )
    assert _is_duplicate_sku(known_product) is True

    known_variant = _fake_integrity_error(
        constraint_name="uq_product_variants_merchant_sku"
    )
    assert _is_duplicate_sku(known_variant) is True

    # Any other unique violation (even sqlstate 23505) is NOT a duplicate SKU.
    unknown_unique = _fake_integrity_error(
        constraint_name="uq_some_other_constraint",
        sqlstate="23505",
    )
    assert _is_duplicate_sku(unknown_unique) is False

    missing_constraint = _fake_integrity_error(
        constraint_name=None,
        sqlstate="23505",
    )
    assert _is_duplicate_sku(missing_constraint) is False


def _declares_internal_token_security(
    operation: dict[str, Any],
    schemes: dict[str, Any],
) -> bool:
    security = operation.get("security")
    if not security:
        return False
    return any(
        schemes[scheme_name]["type"] == "apiKey"
        and schemes[scheme_name]["in"] == "header"
        and schemes[scheme_name]["name"] == "X-Internal-Service-Token"
        for entry in security
        for scheme_name in entry
    )


def test_openapi_contains_catalog_paths() -> None:
    openapi = app.openapi()
    paths = openapi["paths"]
    schemes = openapi["components"]["securitySchemes"]

    required_paths = {
        "/api/v1/merchants/{merchant_id}/catalog/import",
        "/api/v1/catalog/imports/{import_id}",
        "/api/v1/merchants/{merchant_id}/products",
        "/api/v1/products/{product_id}",
    }
    assert required_paths <= set(paths)

    import_operation = paths["/api/v1/merchants/{merchant_id}/catalog/import"]["post"]
    assert "multipart/form-data" in import_operation["requestBody"]["content"]
    assert _declares_internal_token_security(import_operation, schemes)

    create_operation = paths["/api/v1/merchants/{merchant_id}/products"]["post"]
    create_ref = create_operation["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert create_ref.endswith("ProductCreateRequest")
    assert _declares_internal_token_security(create_operation, schemes)

    patch_operation = paths["/api/v1/products/{product_id}"]["patch"]
    patch_ref = patch_operation["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert patch_ref.endswith("ProductPatchRequest")
    assert _declares_internal_token_security(patch_operation, schemes)

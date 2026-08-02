"""Final local end-to-end test for the WhatsApp-only TALAP MVP.

Exercises the real webhook route, signature verification, inbound
normalization/idempotency, PostgreSQL processing jobs, the real worker
function, the real DeepSeek extractor (fake HTTP), real pgvector product
search (fake embedding transport), recommendation-state persistence,
numeric selection, and unmet-demand persistence. Only DeepSeek HTTP and
WhatsApp outbound are faked; no Meta/DeepSeek/Jina API is called.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import hmac
import io
import json
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import apps.worker.jobs.whatsapp_echo as worker_module
from apps.api.dependencies.database import get_api_session_factory
from apps.api.main import app
from talap.ai.customer_request import extract_customer_request
from talap.catalog import import_catalog_csv
from talap.channels.whatsapp import SentWhatsAppMessage
from talap.core.config import Settings, get_settings
from talap.db.models import (
    CatalogImport,
    CatalogImportStatus,
    ChannelConnection,
    InboundEvent,
    InboundMessage,
    Inventory,
    Merchant,
    MessageProcessingJob,
    Product,
    ProductEmbedding,
    ProductVariant,
    UnmetDemand,
    WhatsAppRecommendationState,
)
from talap.embeddings.types import EmbeddingResult
from talap.indexing.documents import build_product_index_text
from talap.recommendations import (
    RECOMMENDATION_STATUS_ACTIVE,
    RECOMMENDATION_STATUS_SELECTED,
    unmet_demand_response,
)
from talap.search.products import search_products

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEMO_CATALOG = _REPO_ROOT / "data" / "demo" / "demo_catalog.csv"

_VERIFY_TOKEN = "wa-verify-token"
_APP_SECRET = "wa-app-secret-123"
_SIGNATURE_HEADER = "x-hub-signature-256"
_MANAGER_LINK = "https://wa.me/77000000000"
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DEEPSEEK_ENDPOINT = f"{_DEEPSEEK_BASE_URL}/chat/completions"

_SENDER = "77000000001"
_PRODUCTS = [
    ("Белая рубашка для девочки LCW Kids", 1990, "school_shirt", "LCW-KIDS-11"),
    ("Белая хлопковая рубашка для мальчика", 2490, "school_shirt", "COTTON-BOY-11"),
    ("Базовая белая рубашка с длинным рукавом", 8990, "school_shirt", "BASIC-LONG-11"),
    ("Белая поплиновая рубашка для девочки", 4490, "school_shirt", "POPLIN-GIRL-11"),
    ("Белая школьная рубашка Chessford Classic", 7499, "school_shirt", "CHESS-CLASSIC-152"),
]

_SCHOOL_REQUEST = {
    "intent": "product_search",
    "language": "ru",
    "category": "school_shirt",
    "query_text": "белая школьная рубашка",
    "attributes": {},
    "budget_max_kzt": None,
    "quantity": None,
    "missing_field": None,
}
_NO_RESULT_REQUEST = {
    "intent": "product_search",
    "language": "ru",
    "category": "space_ship",
    "query_text": "летающий корабль",
    "attributes": {"color": "red"},
    "budget_max_kzt": 5000,
    "quantity": 2,
    "missing_field": None,
}


def _signature(raw_body: bytes, secret: str = _APP_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _text_webhook_raw(*, message_id: str, sender: str, body: str) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "999999999999999",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "100000000000001",
                            },
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1783022400",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _deepseek_response(request_dict: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(request_dict)}}]},
    )


def _vector(text: str) -> list[float]:
    settings = get_settings()
    seed = sum(ord(character) for character in text)
    raw = [
        (math.sin(seed + index) + 1.0) * (index + 1)
        for index in range(settings.jina_embedding_dimensions)
    ]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


class _FakeEmbeddingClient:
    """Deterministic offline embedding client for indexing and querying."""

    provider = "jina"

    def __init__(self) -> None:
        settings = get_settings()
        self.model = settings.jina_embedding_model
        self.dimensions = settings.jina_embedding_dimensions
        self.calls = 0

    async def embed_document(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(
            vector=tuple(_vector(text)),
            provider=self.provider,
            model=self.model,
            dimensions=self.dimensions,
        )

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(
            vector=tuple(_vector(text)),
            provider=self.provider,
            model=self.model,
            dimensions=self.dimensions,
        )

    async def aclose(self) -> None:
        pass


class _FakeWhatsAppClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send_text(self, *, recipient: str, text: str) -> SentWhatsAppMessage:
        self.calls.append((recipient, text))
        return SentWhatsAppMessage(external_message_id="wamid.E2E_SENT")

    async def aclose(self) -> None:
        pass


class _CountingExtractor:
    def __init__(self, extractor: object) -> None:
        self._inner = extractor
        self.calls = 0

    async def __call__(self, *, text: str) -> object:
        self.calls += 1
        return await self._inner(text=text)  # type: ignore[misc]


class _CountingSearch:
    def __init__(self, search: object) -> None:
        self._inner = search
        self.calls = 0

    async def __call__(self, *, request: object, limit: int = 3) -> object:
        self.calls += 1
        return await self._inner(request=request, limit=limit)  # type: ignore[misc]


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


async def _seed_demo_products(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = get_settings()
    async with session_factory() as session:
        merchant = Merchant(slug="e2e-merchant", name="E2E Merchant", active=True)
        session.add(merchant)
        await session.commit()
        merchant_id = merchant.id
    for name, price, category, sku in _PRODUCTS:
        document = build_product_index_text(
            name=name,
            category=category,
            description=f"Demo {name}",
            material="хлопок",
        )
        async with session_factory() as session:
            product = Product(
                id=uuid4(),
                merchant_id=merchant_id,
                merchant_product_key=sku,
                name=name,
                category=category,
                description=f"Demo {name}",
                active=True,
            )
            session.add(product)
            await session.commit()
            product_id = product.id
        async with session_factory() as session:
            variant = ProductVariant(
                id=uuid4(),
                merchant_id=merchant_id,
                product_id=product_id,
                merchant_sku=sku,
                material="хлопок",
                price_kzt=price,
                active=True,
            )
            session.add_all(
                [variant, Inventory(product_variant_id=variant.id, stock_quantity=10)]
            )
            await session.commit()
        async with session_factory() as session:
            session.add(
                ProductEmbedding(
                    id=uuid4(),
                    merchant_id=merchant_id,
                    product_id=product_id,
                    provider="jina",
                    model=settings.jina_embedding_model,
                    dimensions=settings.jina_embedding_dimensions,
                    document_text=document,
                    document_sha256=hashlib.sha256(document.encode()).hexdigest(),
                    embedding=_vector(document),
                    embedded_at=datetime.now(UTC),
                )
            )
            await session.commit()


@pytest_asyncio.fixture
async def api_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    connection = ChannelConnection(channel="whatsapp", name="wa-e2e", active=True)
    async with session_factory() as session:
        session.add(connection)
        await session.commit()
        connection_id = connection.id
    test_settings = Settings(
        whatsapp_connection_id=connection_id,
        whatsapp_verify_token=_VERIFY_TOKEN,
        whatsapp_app_secret=_APP_SECRET,
    )
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


async def _submit(
    client: httpx.AsyncClient,
    raw: bytes,
) -> httpx.Response:
    return await client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={_SIGNATURE_HEADER: _signature(raw)},
    )


async def _process_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client: object,
    extractor: object,
    search: object,
) -> object:
    return await worker_module.process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,  # type: ignore[arg-type]
        extractor=extractor,  # type: ignore[arg-type]
        search=search,  # type: ignore[arg-type]
    )


async def _worker_parts(
    session_factory: async_sessionmaker[AsyncSession],
    embedding: _FakeEmbeddingClient,
    whatsapp: _FakeWhatsAppClient,
) -> tuple[_CountingExtractor, _CountingSearch]:
    extractor = _CountingExtractor(
        functools.partial(
            extract_customer_request,
            api_key="test-key",
            base_url=_DEEPSEEK_BASE_URL,
        )
    )
    search = _CountingSearch(
        functools.partial(
            search_products,
            session_factory=session_factory,
            embedding_client=embedding,  # type: ignore[arg-type]
        )
    )
    return extractor, search


@respx.mock
async def test_scenario_1_successful_product_flow(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: object,
) -> None:
    await _seed_demo_products(session_factory)
    monkeypatch.setattr(
        worker_module, "manager_whatsapp_link", lambda: _MANAGER_LINK
    )
    respx.post(_DEEPSEEK_ENDPOINT).mock(
        return_value=_deepseek_response(_SCHOOL_REQUEST)
    )
    embedding = _FakeEmbeddingClient()
    whatsapp = _FakeWhatsAppClient()
    extractor, search = await _worker_parts(session_factory, embedding, whatsapp)

    first_raw = _text_webhook_raw(
        message_id="wamid.E2E_SC1_1", sender=_SENDER, body="белая школьная рубашка"
    )
    assert (await _submit(api_client, first_raw)).status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1

    first_result = await _process_job(
        session_factory, client=whatsapp, extractor=extractor, search=search
    )
    assert first_result.outcome == worker_module.EchoOutcome.SENT
    assert extractor.calls == 1
    assert search.calls == 1
    assert len(whatsapp.calls) == 1
    response_text = whatsapp.calls[0][1]
    assert "1." in response_text and "2." in response_text and "3." in response_text
    assert "Ответьте номером товара: 1, 2 или 3." in response_text

    async with session_factory() as session:
        state = (
            await session.execute(
                select(WhatsAppRecommendationState).where(
                    WhatsAppRecommendationState.external_user_id == _SENDER,
                    WhatsAppRecommendationState.status == RECOMMENDATION_STATUS_ACTIVE,
                )
            )
        ).scalars().one()
    assert len(state.displayed_products) == 3

    second_raw = _text_webhook_raw(
        message_id="wamid.E2E_SC1_2", sender=_SENDER, body="1"
    )
    assert (await _submit(api_client, second_raw)).status_code == 200
    second_result = await _process_job(
        session_factory, client=whatsapp, extractor=extractor, search=search
    )
    assert second_result.outcome == worker_module.EchoOutcome.SENT
    assert extractor.calls == 1, "DeepSeek must not run for a valid selection"
    assert search.calls == 1, "product search must not run for a valid selection"
    assert len(whatsapp.calls) == 2
    confirmation = whatsapp.calls[1][1]
    selected = state.displayed_products[0]
    assert str(selected["name"]) in confirmation
    assert f"{selected['price_kzt']} ₸" in confirmation
    assert _MANAGER_LINK in confirmation

    async with session_factory() as session:
        selected_state = (
            await session.execute(
                select(WhatsAppRecommendationState).where(
                    WhatsAppRecommendationState.external_user_id == _SENDER,
                    WhatsAppRecommendationState.status == RECOMMENDATION_STATUS_SELECTED,
                )
            )
        ).scalars().one()
    assert selected_state.selected_index == 1
    assert selected_state.external_user_id == _SENDER
    assert await _count(session_factory, UnmetDemand) == 0


@respx.mock
async def test_scenario_2_no_matching_product_creates_unmet_demand(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: object,
) -> None:
    await _seed_demo_products(session_factory)
    monkeypatch.setattr(
        worker_module, "manager_whatsapp_link", lambda: _MANAGER_LINK
    )
    respx.post(_DEEPSEEK_ENDPOINT).mock(
        return_value=_deepseek_response(_NO_RESULT_REQUEST)
    )
    embedding = _FakeEmbeddingClient()
    whatsapp = _FakeWhatsAppClient()
    extractor, search = await _worker_parts(session_factory, embedding, whatsapp)

    raw = _text_webhook_raw(
        message_id="wamid.E2E_SC2_1", sender=_SENDER, body="летающий корабль"
    )
    assert (await _submit(api_client, raw)).status_code == 200
    result = await _process_job(
        session_factory, client=whatsapp, extractor=extractor, search=search
    )
    assert result.outcome == worker_module.EchoOutcome.SENT
    assert whatsapp.calls[0][1] == unmet_demand_response("ru")
    assert _MANAGER_LINK not in whatsapp.calls[0][1]
    assert await _count(session_factory, UnmetDemand) == 1
    async with session_factory() as session:
        demand = (await session.execute(select(UnmetDemand))).scalars().one()
    assert demand.category == "space_ship"
    assert demand.query_text == "летающий корабль"
    assert demand.attributes == {"color": "red"}
    assert demand.budget_max_kzt == 5000
    assert demand.quantity == 2
    assert demand.language == "ru"
    assert await _count(session_factory, WhatsAppRecommendationState) == 0


@respx.mock
async def test_scenario_3_duplicate_webhook_has_no_duplicate_side_effects(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: object,
) -> None:
    await _seed_demo_products(session_factory)
    monkeypatch.setattr(
        worker_module, "manager_whatsapp_link", lambda: _MANAGER_LINK
    )
    respx.post(_DEEPSEEK_ENDPOINT).mock(
        return_value=_deepseek_response(_SCHOOL_REQUEST)
    )
    embedding = _FakeEmbeddingClient()
    whatsapp = _FakeWhatsAppClient()
    extractor, search = await _worker_parts(session_factory, embedding, whatsapp)

    raw = _text_webhook_raw(
        message_id="wamid.E2E_SC3_1", sender=_SENDER, body="белая школьная рубашка"
    )
    assert (await _submit(api_client, raw)).status_code == 200
    assert (await _submit(api_client, raw)).status_code == 200
    assert await _count(session_factory, InboundEvent) == 1
    assert await _count(session_factory, InboundMessage) == 1
    assert await _count(session_factory, MessageProcessingJob) == 1

    result = await _process_job(
        session_factory, client=whatsapp, extractor=extractor, search=search
    )
    assert result.outcome == worker_module.EchoOutcome.SENT
    assert len(whatsapp.calls) == 1
    assert await _count(session_factory, WhatsAppRecommendationState) == 1
    assert await _count(session_factory, UnmetDemand) == 0


async def test_demo_catalog_imports_through_existing_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert _DEMO_CATALOG.exists()
    raw = _DEMO_CATALOG.read_bytes()
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows, "demo catalog must not be empty"
    slugs = {row["merchant_slug"] for row in rows}
    merchant_ids: dict[str, UUID] = {}
    for slug in slugs:
        async with session_factory() as session:
            merchant = Merchant(slug=slug, name=slug, active=True)
            session.add(merchant)
            await session.commit()
            merchant_ids[slug] = merchant.id
    for slug, merchant_id in merchant_ids.items():
        slice_rows = [row for row in rows if row["merchant_slug"] == slug]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(slice_rows)
        summary = await import_catalog_csv(
            merchant_id=merchant_id,
            filename=f"{slug}.csv",
            content=buffer.getvalue().encode("utf-8"),
            session_factory=session_factory,
        )
        assert summary.status == CatalogImportStatus.COMPLETED
    async with session_factory() as session:
        statuses = (
            await session.execute(select(CatalogImport.status))
        ).scalars().all()
        white_shirts = (
            await session.execute(
                select(Product).where(Product.category == "school_shirt")
            )
        ).scalars().all()
    assert all(status == CatalogImportStatus.COMPLETED for status in statuses)
    assert len(white_shirts) >= 3, "white school-shirt scenario must be present"
    assert any("рубашка" in product.name.lower() for product in white_shirts)
    assert await _count(session_factory, Product) >= len(rows)

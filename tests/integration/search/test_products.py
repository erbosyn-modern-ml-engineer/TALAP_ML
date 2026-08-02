from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.ai.customer_request import CustomerRequest
from talap.core import get_settings
from talap.db.models import (
    Inventory,
    Merchant,
    Product,
    ProductEmbedding,
    ProductVariant,
)
from talap.embeddings.types import EmbeddingResult
from talap.search.products import ProductSearchResult, search_products

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_SETTINGS = get_settings()


def _vector(text: str) -> list[float]:
    seed = sum(ord(character) for character in text)
    raw = [
        math.sin(seed + index) * (index + 1)
        for index in range(_SETTINGS.jina_embedding_dimensions)
    ]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b)


class _FakeEmbeddingClient:
    provider = "jina"
    dimensions = _SETTINGS.jina_embedding_dimensions

    def __init__(self) -> None:
        self.model = _SETTINGS.jina_embedding_model
        self.calls = 0

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


def _request(**overrides: object) -> CustomerRequest:
    base: dict[str, object] = {
        "intent": "product_search",
        "language": "ru",
        "query_text": "кроссовки",
        "category": None,
        "attributes": {},
        "budget_max_kzt": None,
        "quantity": None,
        "missing_field": None,
    }
    base.update(overrides)
    return CustomerRequest(**base)


async def _create_merchant(
    session_factory: async_sessionmaker[AsyncSession],
) -> Merchant:
    async with session_factory() as session:
        merchant = Merchant(slug=f"merchant-{uuid4()}", name="Merchant", active=True)
        session.add(merchant)
        await session.commit()
        return merchant


async def _create_product(
    session_factory: async_sessionmaker[AsyncSession],
    merchant_id: UUID,
    *,
    name: str,
    category: str = "school",
    product_id: UUID | None = None,
    active: bool = True,
) -> Product:
    product = Product(
        id=product_id or uuid4(),
        merchant_id=merchant_id,
        merchant_product_key=f"key-{uuid4()}",
        name=name,
        category=category,
        description=f"desc-{name}",
        active=active,
    )
    async with session_factory() as session:
        session.add(product)
        await session.commit()
        return product


async def _create_embedding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    merchant_id: UUID,
    product_id: UUID,
    doc_text: str,
) -> None:
    embedding = ProductEmbedding(
        id=uuid4(),
        merchant_id=merchant_id,
        product_id=product_id,
        provider="jina",
        model=_SETTINGS.jina_embedding_model,
        dimensions=_SETTINGS.jina_embedding_dimensions,
        document_text=doc_text,
        document_sha256=hashlib.sha256(doc_text.encode()).hexdigest(),
        embedding=_vector(doc_text),
        embedded_at=_NOW,
    )
    async with session_factory() as session:
        session.add(embedding)
        await session.commit()


async def _create_variant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    merchant_id: UUID,
    product_id: UUID,
    variant_id: UUID | None = None,
    sku: str | None = None,
    price: int = 1000,
    stock: int = 10,
    active: bool = True,
    material: str = "Cotton",
) -> ProductVariant:
    variant = ProductVariant(
        id=variant_id or uuid4(),
        merchant_id=merchant_id,
        product_id=product_id,
        merchant_sku=sku or f"sku-{uuid4()}",
        material=material,
        price_kzt=price,
        active=active,
    )
    inventory = Inventory(product_variant_id=variant.id, stock_quantity=stock)
    async with session_factory() as session:
        session.add_all([variant, inventory])
        await session.commit()
        return variant


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    merchant_id: UUID,
    *,
    name: str,
    category: str = "school",
    doc_text: str | None = None,
    product_id: UUID | None = None,
    product_active: bool = True,
    variant_active: bool = True,
    stock: int = 10,
    price: int = 1000,
    material: str = "Cotton",
    with_embedding: bool = True,
) -> tuple[UUID, UUID]:
    text = doc_text if doc_text is not None else name
    product = await _create_product(
        session_factory,
        merchant_id,
        name=name,
        category=category,
        product_id=product_id,
        active=product_active,
    )
    if with_embedding:
        await _create_embedding(
            session_factory,
            merchant_id=merchant_id,
            product_id=product.id,
            doc_text=text,
        )
    variant = await _create_variant(
        session_factory,
        merchant_id=merchant_id,
        product_id=product.id,
        price=price,
        stock=stock,
        active=variant_active,
        material=material,
    )
    return product.id, variant.id


async def _search(
    session_factory: async_sessionmaker[AsyncSession],
    fake: _FakeEmbeddingClient,
    request: CustomerRequest,
    *,
    limit: int = 3,
) -> tuple[ProductSearchResult, ...]:
    return await search_products(
        request=request,
        limit=limit,
        session_factory=session_factory,
        embedding_client=fake,  # type: ignore[arg-type]
    )


async def test_relevant_available_product_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product_id, variant_id = await _seed(
        session_factory, merchant.id, name="Кроссовки", doc_text="синие кроссовки"
    )
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="синие кроссовки")
    )

    assert len(results) == 1
    result = results[0]
    assert result.product_id == product_id
    assert result.name == "Кроссовки"
    assert result.category == "school"
    assert result.description == "desc-Кроссовки"
    assert result.price_kzt == 1000
    assert result.available_quantity == 10
    assert result.material == "Cotton"
    assert 0.0 <= result.similarity <= 1.0


async def test_cosine_ordering_returns_best_product_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    texts = ["синие кроссовки", "красные ботинки", "белая футболка"]
    ids = []
    for text in texts:
        product_id, _ = await _seed(
            session_factory, merchant.id, name=text, doc_text=text
        )
        ids.append(product_id)
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="синие кроссовки"), limit=3
    )

    query = _vector("синие кроссовки")
    expected = sorted(
        ids,
        key=lambda pid: (
            -_cosine_similarity(query, _vector(texts[ids.index(pid)])),
            pid,
        ),
    )
    assert [result.product_id for result in results] == expected
    assert results[0].product_id == ids[0]


def _find_negative_text(query_text: str) -> str:
    query = _vector(query_text)
    best_similarity = 1.0
    best = "neg0"
    for index in range(2000):
        candidate = f"neg{index}"
        similarity = _cosine_similarity(query, _vector(candidate))
        if similarity < best_similarity:
            best_similarity = similarity
            best = candidate
        if best_similarity < -0.5:
            break
    assert best_similarity < 0.0
    return best


async def test_negative_cosine_similarity_returned_and_ranked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    query_text = "поисковый запрос для негатива"
    negative_text = _find_negative_text(query_text)
    negative_id, _ = await _seed(
        session_factory,
        merchant.id,
        name="Антикорреляция",
        doc_text=negative_text,
    )
    positive_id, _ = await _seed(
        session_factory,
        merchant.id,
        name="Совпадение",
        doc_text=query_text,
    )
    fake = _FakeEmbeddingClient()

    results = await _search(session_factory, fake, _request(query_text=query_text))

    assert len(results) == 2
    by_id = {result.product_id: result for result in results}
    assert by_id[negative_id].similarity < 0.0
    assert by_id[positive_id].similarity > by_id[negative_id].similarity
    assert [result.product_id for result in results] == [positive_id, negative_id]


async def test_limit_three_enforced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    for index in range(5):
        await _seed(
            session_factory,
            merchant.id,
            name=f"Товар {index}",
            doc_text=f"товар номер {index}",
        )
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="товар"), limit=3
    )

    assert len(results) == 3


async def test_inactive_product_excluded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="Активный", doc_text="активный товар")
    await _seed(
        session_factory,
        merchant.id,
        name="Неактивный",
        doc_text="неактивный товар",
        product_active=False,
    )
    fake = _FakeEmbeddingClient()

    results = await _search(session_factory, fake, _request(query_text="товар"))

    assert len(results) == 1
    assert results[0].name == "Активный"


async def test_inactive_variant_excluded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="Активный", doc_text="активный товар")
    await _seed(
        session_factory,
        merchant.id,
        name="Неактивный",
        doc_text="неактивный товар",
        variant_active=False,
    )
    fake = _FakeEmbeddingClient()

    results = await _search(session_factory, fake, _request(query_text="товар"))

    assert len(results) == 1
    assert results[0].name == "Активный"


async def test_zero_stock_product_excluded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="В наличии", doc_text="товар один")
    await _seed(
        session_factory, merchant.id, name="Нет в наличии", doc_text="товар два", stock=0
    )
    fake = _FakeEmbeddingClient()

    results = await _search(session_factory, fake, _request(query_text="товар"))

    assert len(results) == 1
    assert results[0].name == "В наличии"


async def test_budget_filter_enforced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="Дешевый", doc_text="товар один", price=1000)
    await _seed(
        session_factory, merchant.id, name="Дорогой", doc_text="товар два", price=5000
    )
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="товар", budget_max_kzt=2000)
    )

    assert len(results) == 1
    assert results[0].name == "Дешевый"


async def test_requested_quantity_filter_enforced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="Мало", doc_text="товар один", stock=5)
    await _seed(session_factory, merchant.id, name="Много", doc_text="товар два", stock=50)
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="товар", quantity=10)
    )

    assert len(results) == 1
    assert results[0].name == "Много"


async def test_category_filter_is_case_insensitive(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(
        session_factory,
        merchant.id,
        name="Школьный",
        category="School",
        doc_text="товар один",
    )
    await _seed(
        session_factory,
        merchant.id,
        name="Офисный",
        category="office",
        doc_text="товар два",
    )
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="товар", category="school")
    )

    assert len(results) == 1
    assert results[0].name == "Школьный"


async def test_product_without_embedding_excluded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="С вектором", doc_text="товар один")
    await _seed(
        session_factory,
        merchant.id,
        name="Без вектора",
        doc_text="товар два",
        with_embedding=False,
    )
    fake = _FakeEmbeddingClient()

    results = await _search(session_factory, fake, _request(query_text="товар"))

    assert len(results) == 1
    assert results[0].name == "С вектором"


async def test_deterministic_product_id_tie_break(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    await _seed(
        session_factory,
        merchant.id,
        name="Первый",
        doc_text="одинаковый вектор",
        product_id=first_id,
    )
    await _seed(
        session_factory,
        merchant.id,
        name="Второй",
        doc_text="одинаковый вектор",
        product_id=second_id,
    )
    fake = _FakeEmbeddingClient()

    results = await _search(
        session_factory, fake, _request(query_text="одинаковый вектор")
    )

    assert [result.product_id for result in results] == [first_id, second_id]


async def test_exactly_one_query_embedding_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    await _seed(session_factory, merchant.id, name="Товар", doc_text="товар")
    fake = _FakeEmbeddingClient()

    await _search(session_factory, fake, _request(query_text="товар"))

    assert fake.calls == 1


async def test_no_product_embeddings_generated_or_modified(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product_id, _ = await _seed(
        session_factory, merchant.id, name="Товар", doc_text="исходный вектор"
    )
    fake = _FakeEmbeddingClient()

    async with session_factory() as session:
        before = (
            await session.execute(select(func.count()).select_from(ProductEmbedding))
        ).scalar_one()

    await _search(session_factory, fake, _request(query_text="товар"))

    async with session_factory() as session:
        after = (
            await session.execute(select(func.count()).select_from(ProductEmbedding))
        ).scalar_one()
        embedding = (
            await session.execute(
                select(ProductEmbedding).where(
                    ProductEmbedding.product_id == product_id
                )
            )
        ).scalar_one()
    assert after == before
    assert embedding.document_text == "исходный вектор"
    assert list(embedding.embedding) == pytest.approx(_vector("исходный вектор"))


async def test_result_contains_selected_variant_details(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id, name="Товар")
    await _create_embedding(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        doc_text="товар",
    )
    variant_a = await _create_variant(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        variant_id=UUID("00000000-0000-0000-0000-00000000000a"),
        sku="SKU-A",
        price=1500,
        stock=7,
        material="Wool",
    )
    await _create_variant(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        variant_id=UUID("00000000-0000-0000-0000-00000000000b"),
        sku="SKU-B",
        price=1200,
        stock=3,
    )
    fake = _FakeEmbeddingClient()

    results = await _search(session_factory, fake, _request(query_text="товар"))

    assert len(results) == 1
    assert results[0].product_id == product.id
    assert results[0].merchant_sku == variant_a.merchant_sku
    assert results[0].price_kzt == 1500
    assert results[0].available_quantity == 7
    assert results[0].material == "Wool"

from __future__ import annotations

import hashlib
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db.models import (
    Merchant,
    Product,
    ProductEmbedding,
    ProductIndexingTask,
    ProductIndexingTaskStatus,
    ProductVariant,
)
from talap.embeddings.jina import JinaEmbeddingError
from talap.embeddings.types import EmbeddingResult
from talap.indexing.documents import build_product_index_text
from talap.indexing.processor import (
    IndexingProcessResult,
    IndexingProcessStatus,
    process_claimed_indexing_task,
)
from talap.indexing.worker import ClaimedIndexingTask, claim_indexing_tasks

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_STALE_AFTER = timedelta(minutes=5)
_RETRY_DELAY = timedelta(minutes=5)
_MAX_ATTEMPTS = 3


class _FakeEmbeddingClient:
    """Deterministic offline client: same text -> same 1024-dim vector."""

    provider = "jina"
    model = "jina-embeddings-v5-text-small"
    dimensions = 1024

    def __init__(
        self,
        *,
        result_dimensions: int = 1024,
        failure: Exception | None = None,
        on_embed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._result_dimensions = result_dimensions
        self._failure = failure
        self._on_embed = on_embed
        self.calls = 0

    async def embed_document(self, text: str) -> EmbeddingResult:
        self.calls += 1
        if self._on_embed is not None:
            await self._on_embed()
        if self._failure is not None:
            raise self._failure
        seed = sum(ord(character) for character in text)
        vector = tuple(
            math.sin(seed + index) * (index + 1)
            for index in range(self._result_dimensions)
        )
        return EmbeddingResult(
            vector=vector,
            provider=self.provider,
            model=self.model,
            dimensions=self._result_dimensions,
        )

    async def aclose(self) -> None:
        pass


async def _create_merchant(
    session_factory: async_sessionmaker[AsyncSession],
) -> Merchant:
    async with session_factory() as session:
        merchant = Merchant(slug="merchant-embed", name="Merchant Embed", active=True)
        session.add(merchant)
        await session.commit()
        return merchant


async def _create_product_with_variant(
    session_factory: async_sessionmaker[AsyncSession],
    merchant_id: UUID,
    *,
    name: str = "Product",
    material: str = "Cotton",
) -> tuple[Product, ProductVariant]:
    async with session_factory() as session:
        product = Product(
            id=uuid4(),
            merchant_id=merchant_id,
            merchant_product_key=f"SKU-{uuid4()}",
            name=name,
            category="school",
            description="Description",
            active=True,
        )
        variant = ProductVariant(
            id=uuid4(),
            merchant_id=merchant_id,
            product_id=product.id,
            merchant_sku=f"SKU-V-{uuid4()}",
            material=material,
            price_kzt=1000,
            active=True,
        )
        session.add_all([product, variant])
        await session.commit()
        return product, variant


async def _add_task(
    session_factory: async_sessionmaker[AsyncSession],
    merchant_id: UUID,
    product_id: UUID,
) -> ProductIndexingTask:
    async with session_factory() as session:
        task = ProductIndexingTask(
            id=uuid4(),
            merchant_id=merchant_id,
            product_id=product_id,
            changed_fields=["name"],
            available_at=_NOW - timedelta(minutes=10),
        )
        session.add(task)
        await session.commit()
        return task


async def _claim_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimedIndexingTask:
    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert len(claimed) == 1
    return claimed[0]


async def _load_task(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
) -> ProductIndexingTask:
    async with session_factory() as session:
        task = await session.get(ProductIndexingTask, task_id)
        assert task is not None
        return task


async def _load_embedding(
    session_factory: async_sessionmaker[AsyncSession],
    product_id: UUID,
    merchant_id: UUID,
) -> ProductEmbedding | None:
    async with session_factory() as session:
        return (
            await session.execute(
                select(ProductEmbedding).where(
                    ProductEmbedding.product_id == product_id,
                    ProductEmbedding.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()


async def _table_count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


async def _process(
    session_factory: async_sessionmaker[AsyncSession],
    claimed_task: ClaimedIndexingTask,
    client: _FakeEmbeddingClient,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> IndexingProcessResult:
    return await process_claimed_indexing_task(
        claimed_task=claimed_task,
        session_factory=session_factory,
        embedding_client=client,
        max_attempts=max_attempts,
        retry_delay=_RETRY_DELAY,
        now=_NOW,
    )


async def test_first_task_creates_product_embedding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, variant = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)
    client = _FakeEmbeddingClient()

    result = await _process(session_factory, claimed, client)

    assert result.status == IndexingProcessStatus.EMBEDDED
    assert client.calls == 1
    task = await _load_task(session_factory, claimed.task_id)
    assert task.status == ProductIndexingTaskStatus.COMPLETED
    assert task.completed_at == _NOW

    embedding = await _load_embedding(session_factory, product.id, merchant.id)
    assert embedding is not None
    assert embedding.merchant_id == merchant.id
    assert embedding.product_id == product.id
    assert embedding.provider == "jina"
    assert embedding.model == "jina-embeddings-v5-text-small"
    assert embedding.dimensions == 1024
    assert len(embedding.embedding) == 1024
    assert embedding.embedded_at == _NOW
    document = build_product_index_text(
        name=product.name,
        category=product.category,
        description=product.description,
        material=variant.material,
    )
    expected_sha = hashlib.sha256(document.encode("utf-8")).hexdigest()
    assert embedding.document_sha256 == expected_sha


async def test_second_unchanged_task_skips_jina_and_does_not_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, _ = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)
    client = _FakeEmbeddingClient()
    first = await _process(session_factory, claimed, client)
    assert first.status == IndexingProcessStatus.EMBEDDED

    await _add_task(session_factory, merchant.id, product.id)
    claimed_second = await _claim_one(session_factory)
    second = await _process(session_factory, claimed_second, client)

    assert second.status == IndexingProcessStatus.SKIPPED_UNCHANGED
    assert client.calls == 1  # no second Jina call
    assert await _table_count(session_factory, ProductEmbedding) == 1
    task = await _load_task(session_factory, claimed_second.task_id)
    assert task.status == ProductIndexingTaskStatus.COMPLETED


async def test_semantic_document_change_updates_same_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, variant = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)
    client = _FakeEmbeddingClient()
    first = await _process(session_factory, claimed, client)
    assert first.status == IndexingProcessStatus.EMBEDDED
    assert await _table_count(session_factory, ProductEmbedding) == 1

    async with session_factory() as session:
        row = await session.get(Product, product.id)
        assert row is not None
        row.name = "Renamed Product"
        await session.commit()

    await _add_task(session_factory, merchant.id, product.id)
    claimed_second = await _claim_one(session_factory)
    second = await _process(session_factory, claimed_second, client)

    assert second.status == IndexingProcessStatus.EMBEDDED
    assert client.calls == 2
    assert await _table_count(session_factory, ProductEmbedding) == 1
    embedding = await _load_embedding(session_factory, product.id, merchant.id)
    assert embedding is not None
    document = build_product_index_text(
        name="Renamed Product",
        category=product.category,
        description=product.description,
        material=variant.material,
    )
    expected_sha = hashlib.sha256(document.encode("utf-8")).hexdigest()
    assert embedding.document_sha256 == expected_sha


async def test_dimension_mismatch_schedules_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, _ = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)
    client = _FakeEmbeddingClient(result_dimensions=512)

    result = await _process(session_factory, claimed, client)

    assert result.status == IndexingProcessStatus.RETRY_SCHEDULED
    assert client.calls == 1
    assert await _table_count(session_factory, ProductEmbedding) == 0
    task = await _load_task(session_factory, claimed.task_id)
    assert task.status == ProductIndexingTaskStatus.PENDING
    assert task.available_at == _NOW + _RETRY_DELAY
    assert task.last_error == "Embedding dimension mismatch."


async def test_dimension_mismatch_permanent_at_max_attempts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, _ = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)
    client = _FakeEmbeddingClient(result_dimensions=512)

    result = await _process(session_factory, claimed, client, max_attempts=1)

    assert result.status == IndexingProcessStatus.PERMANENTLY_FAILED
    assert await _table_count(session_factory, ProductEmbedding) == 0
    task = await _load_task(session_factory, claimed.task_id)
    assert task.status == ProductIndexingTaskStatus.FAILED
    assert task.completed_at == _NOW


async def test_stale_worker_cannot_write_embedding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, _ = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    worker_a = await _claim_one(session_factory)
    assert worker_a.attempts == 1

    # Make the task stale so worker B reclaims it (attempts=2).
    async with session_factory() as session:
        row = await session.get(ProductIndexingTask, worker_a.task_id)
        assert row is not None
        row.started_at = _NOW - timedelta(minutes=10)
        await session.commit()
    worker_b = await _claim_one(session_factory)
    assert worker_b.task_id == worker_a.task_id
    assert worker_b.attempts == 2

    client = _FakeEmbeddingClient()
    result = await _process(session_factory, worker_a, client)

    assert result.status == IndexingProcessStatus.STALE_CLAIM
    assert await _table_count(session_factory, ProductEmbedding) == 0
    task = await _load_task(session_factory, worker_a.task_id)
    assert task.status == ProductIndexingTaskStatus.PROCESSING
    assert task.attempts == 2


async def test_processor_failure_leaves_previous_embedding_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, _ = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)
    ok_client = _FakeEmbeddingClient()
    first = await _process(session_factory, claimed, ok_client)
    assert first.status == IndexingProcessStatus.EMBEDDED
    before = await _load_embedding(session_factory, product.id, merchant.id)
    assert before is not None

    async with session_factory() as session:
        row = await session.get(Product, product.id)
        assert row is not None
        row.name = "Changed"
        await session.commit()

    await _add_task(session_factory, merchant.id, product.id)
    claimed_second = await _claim_one(session_factory)
    failing_client = _FakeEmbeddingClient(failure=JinaEmbeddingError("boom"))
    second = await _process(session_factory, claimed_second, failing_client)

    assert second.status == IndexingProcessStatus.RETRY_SCHEDULED
    after = await _load_embedding(session_factory, product.id, merchant.id)
    assert after is not None
    assert after.document_sha256 == before.document_sha256
    assert after.embedding == before.embedding
    task = await _load_task(session_factory, claimed_second.task_id)
    assert task.status == ProductIndexingTaskStatus.PENDING


async def test_product_change_during_http_phase_prevents_stale_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product, _ = await _create_product_with_variant(session_factory, merchant.id)
    await _add_task(session_factory, merchant.id, product.id)
    claimed = await _claim_one(session_factory)

    async def _mutate_product_during_embed() -> None:
        async with session_factory() as session:
            row = await session.get(Product, product.id)
            assert row is not None
            row.name = "Changed During Embedding"
            await session.commit()

    client = _FakeEmbeddingClient(on_embed=_mutate_product_during_embed)
    result = await _process(session_factory, claimed, client)

    assert result.status == IndexingProcessStatus.RETRY_SCHEDULED
    assert result.message == "Product document changed during processing."
    assert await _table_count(session_factory, ProductEmbedding) == 0
    task = await _load_task(session_factory, claimed.task_id)
    assert task.status == ProductIndexingTaskStatus.PENDING
    assert task.last_error == "Product document changed during processing."


async def test_product_delete_cascades_embedding_for_variantless_product(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = Product(
        id=uuid4(),
        merchant_id=merchant.id,
        merchant_product_key="SKU-NO-VARIANT",
        name="No Variant",
        category="school",
        description="",
        active=True,
    )
    async with session_factory() as session:
        session.add(product)
        await session.commit()

    async with session_factory() as session:
        session.add(
            ProductEmbedding(
                id=uuid4(),
                merchant_id=merchant.id,
                product_id=product.id,
                provider="jina",
                model="jina-embeddings-v5-text-small",
                dimensions=1024,
                document_text="text",
                document_sha256="a" * 64,
                embedding=[0.0] * 1024,
                embedded_at=_NOW,
            )
        )
        await session.commit()

    async with session_factory() as session:
        loaded = await session.get(Product, product.id)
        assert loaded is not None
        await session.delete(loaded)
        await session.commit()

    async with session_factory() as session:
        assert await session.get(Product, product.id) is None
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(ProductEmbedding)
                .where(ProductEmbedding.product_id == product.id)
            )
        ).scalar_one()
        assert remaining == 0

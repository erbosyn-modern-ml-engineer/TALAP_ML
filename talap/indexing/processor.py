"""Process one claimed product indexing task end-to-end (Jina + durable storage).

Transaction phases (T-017B2B):

- A. the task was already claimed and committed by T-017B1,
- B. short DB read: product, single variant, canonical document, current
     embedding -- transaction closed,
- C. external Jina HTTP request with NO DB transaction held,
- D. short DB write: lock the task, verify the ``expected_attempt`` fencing
     token, verify the Product still exists and its document hash is
     unchanged, upsert ``ProductEmbedding``, and mark the task COMPLETED in
     one atomic commit.

If the product document changed between B and D, the stale embedding is never
written and the task is rescheduled/failed safely. Provider failures are
converted to short safe summaries; raw exception reprs and tracebacks are
never stored.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db.models import (
    Product,
    ProductEmbedding,
    ProductIndexingTask,
    ProductIndexingTaskStatus,
    ProductVariant,
)
from talap.embeddings.jina import JinaEmbeddingError
from talap.embeddings.types import EmbeddingClient, EmbeddingResult
from talap.indexing.documents import build_product_index_text
from talap.indexing.worker import (
    ClaimedIndexingTask,
    IndexingTaskNotFoundError,
    InvalidIndexingTaskTransitionError,
    StaleIndexingTaskClaimError,
    decide_failure_outcome,
    mark_indexing_task_completed,
    mark_indexing_task_failed,
    sanitize_error_message,
)

__all__ = [
    "IndexingProcessResult",
    "IndexingProcessStatus",
    "process_claimed_indexing_task",
]

_PRODUCT_MISSING_MESSAGE = "Product no longer exists."
_VARIANT_MISSING_MESSAGE = "Product variant is missing."
_DOCUMENT_CHANGED_MESSAGE = "Product document changed during processing."
_DIMENSION_MISMATCH_MESSAGE = "Embedding dimension mismatch."
_PROVIDER_FAILURE_MESSAGE = "Embedding provider failed."


class IndexingProcessStatus(StrEnum):
    EMBEDDED = "embedded"
    SKIPPED_UNCHANGED = "skipped_unchanged"
    RETRY_SCHEDULED = "retry_scheduled"
    PERMANENTLY_FAILED = "permanently_failed"
    STALE_CLAIM = "stale_claim"


@dataclass(frozen=True)
class IndexingProcessResult:
    status: IndexingProcessStatus
    task_id: UUID
    product_id: UUID
    merchant_id: UUID
    embedding_id: UUID | None = None
    message: str | None = None


@dataclass(frozen=True)
class _CatalogSnapshot:
    product: Product | None
    variant: ProductVariant | None
    existing_embedding: ProductEmbedding | None
    document_text: str | None
    document_sha256: str | None


async def process_claimed_indexing_task(
    *,
    claimed_task: ClaimedIndexingTask,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_client: EmbeddingClient,
    max_attempts: int,
    retry_delay: timedelta,
    now: datetime | None = None,
) -> IndexingProcessResult:
    """Process one claimed task to a durable ProductEmbedding.

    Returns an ``IndexingProcessResult`` for every outcome; stale claims and
    provider failures are never raised to the caller. ``now`` is injectable
    for deterministic tests.
    """
    process_time = now if now is not None else datetime.now(UTC)
    task_id = claimed_task.task_id
    expected_attempt = claimed_task.attempts
    product_id = claimed_task.product_id
    merchant_id = claimed_task.merchant_id

    # ── Phase B: short DB read (own transaction) ────────────────────────
    snapshot = await _read_catalog_state(session_factory, claimed_task)
    if snapshot.product is None:
        return await _fail_task_and_result(
            session_factory=session_factory,
            task_id=task_id,
            product_id=product_id,
            merchant_id=merchant_id,
            expected_attempt=expected_attempt,
            force_permanent=True,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            message=_PRODUCT_MISSING_MESSAGE,
            process_time=process_time,
        )
    if snapshot.variant is None:
        return await _fail_task_and_result(
            session_factory=session_factory,
            task_id=task_id,
            product_id=product_id,
            merchant_id=merchant_id,
            expected_attempt=expected_attempt,
            force_permanent=False,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            message=_VARIANT_MISSING_MESSAGE,
            process_time=process_time,
        )
    assert snapshot.document_text is not None
    assert snapshot.document_sha256 is not None

    # ── Idempotency: identical existing embedding → skip Jina ───────────
    if _embedding_identical(
        snapshot.existing_embedding, embedding_client, snapshot.document_sha256
    ):
        try:
            await mark_indexing_task_completed(
                session_factory=session_factory,
                task_id=task_id,
                expected_attempt=expected_attempt,
                now=process_time,
            )
        except StaleIndexingTaskClaimError:
            return _stale_result(task_id, product_id, merchant_id)
        return IndexingProcessResult(
            status=IndexingProcessStatus.SKIPPED_UNCHANGED,
            task_id=task_id,
            product_id=product_id,
            merchant_id=merchant_id,
            message="Embedding already current.",
        )

    # ── Phase C: external Jina request (no DB transaction held) ─────────
    try:
        embedding_result = await embedding_client.embed_document(snapshot.document_text)
    except Exception as exc:  # provider failures only; never BaseException
        message = _safe_provider_message(exc)
        return await _fail_task_and_result(
            session_factory=session_factory,
            task_id=task_id,
            product_id=product_id,
            merchant_id=merchant_id,
            expected_attempt=expected_attempt,
            force_permanent=False,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            message=message,
            process_time=process_time,
        )
    if (
        embedding_result.dimensions != embedding_client.dimensions
        or len(embedding_result.vector) != embedding_client.dimensions
    ):
        return await _fail_task_and_result(
            session_factory=session_factory,
            task_id=task_id,
            product_id=product_id,
            merchant_id=merchant_id,
            expected_attempt=expected_attempt,
            force_permanent=False,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            message=_DIMENSION_MISMATCH_MESSAGE,
            process_time=process_time,
        )

    # ── Phase D: short DB write (single atomic transaction) ─────────────
    try:
        return await _write_embedding_atomically(
            session_factory=session_factory,
            claimed_task=claimed_task,
            embedding_client=embedding_client,
            embedding_result=embedding_result,
            expected_document_text=snapshot.document_text,
            expected_document_sha256=snapshot.document_sha256,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            process_time=process_time,
        )
    except StaleIndexingTaskClaimError:
        return _stale_result(task_id, product_id, merchant_id)


async def _read_catalog_state(
    session_factory: async_sessionmaker[AsyncSession],
    claimed_task: ClaimedIndexingTask,
) -> _CatalogSnapshot:
    async with session_factory() as session:
        product = await session.get(Product, claimed_task.product_id)
        if product is None:
            return _CatalogSnapshot(None, None, None, None, None)
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == claimed_task.product_id,
                    ProductVariant.merchant_id == claimed_task.merchant_id,
                )
            )
        ).scalars().first()
        existing_embedding = (
            await session.execute(
                select(ProductEmbedding).where(
                    ProductEmbedding.product_id == claimed_task.product_id,
                    ProductEmbedding.merchant_id == claimed_task.merchant_id,
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            return _CatalogSnapshot(product, None, existing_embedding, None, None)
        document_text = build_product_index_text(
            name=product.name,
            category=product.category,
            description=product.description,
            material=variant.material,
        )
        document_sha256 = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
        return _CatalogSnapshot(
            product=product,
            variant=variant,
            existing_embedding=existing_embedding,
            document_text=document_text,
            document_sha256=document_sha256,
        )


def _embedding_identical(
    embedding: ProductEmbedding | None,
    client: EmbeddingClient,
    document_sha256: str,
) -> bool:
    return (
        embedding is not None
        and embedding.provider == client.provider
        and embedding.model == client.model
        and embedding.dimensions == client.dimensions
        and embedding.document_sha256 == document_sha256
    )


async def _write_embedding_atomically(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    claimed_task: ClaimedIndexingTask,
    embedding_client: EmbeddingClient,
    embedding_result: EmbeddingResult,
    expected_document_text: str,
    expected_document_sha256: str,
    max_attempts: int,
    retry_delay: timedelta,
    process_time: datetime,
) -> IndexingProcessResult:
    task_id = claimed_task.task_id
    product_id = claimed_task.product_id
    merchant_id = claimed_task.merchant_id
    expected_attempt = claimed_task.attempts

    async with session_factory() as session:
        task = await session.get(ProductIndexingTask, task_id, with_for_update=True)
        if task is None:
            raise IndexingTaskNotFoundError(f"Indexing task {task_id} does not exist.")
        if task.status != ProductIndexingTaskStatus.PROCESSING:
            raise InvalidIndexingTaskTransitionError(
                "Only a PROCESSING indexing task can be finalized; "
                f"task {task_id} is {task.status.value!r}."
            )
        if task.attempts != expected_attempt:
            raise StaleIndexingTaskClaimError("Indexing task claim is stale.")

        current_product = await session.get(Product, product_id)
        if current_product is None:
            outcome = _apply_failure_in_session(
                task,
                force_permanent=True,
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                message=_PRODUCT_MISSING_MESSAGE,
                process_time=process_time,
            )
            await session.commit()
            return _failure_result(
                outcome, task_id, product_id, merchant_id, _PRODUCT_MISSING_MESSAGE
            )
        current_variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == product_id,
                    ProductVariant.merchant_id == merchant_id,
                )
            )
        ).scalars().first()
        if current_variant is None:
            outcome = _apply_failure_in_session(
                task,
                force_permanent=False,
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                message=_VARIANT_MISSING_MESSAGE,
                process_time=process_time,
            )
            await session.commit()
            return _failure_result(
                outcome, task_id, product_id, merchant_id, _VARIANT_MISSING_MESSAGE
            )
        current_document = build_product_index_text(
            name=current_product.name,
            category=current_product.category,
            description=current_product.description,
            material=current_variant.material,
        )
        current_sha256 = hashlib.sha256(current_document.encode("utf-8")).hexdigest()
        if current_sha256 != expected_document_sha256:
            outcome = _apply_failure_in_session(
                task,
                force_permanent=False,
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                message=_DOCUMENT_CHANGED_MESSAGE,
                process_time=process_time,
            )
            await session.commit()
            return _failure_result(
                outcome, task_id, product_id, merchant_id, _DOCUMENT_CHANGED_MESSAGE
            )

        embedding_row = (
            await session.execute(
                select(ProductEmbedding).where(
                    ProductEmbedding.product_id == product_id,
                    ProductEmbedding.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if embedding_row is None:
            embedding_row = ProductEmbedding(
                id=uuid4(),
                merchant_id=merchant_id,
                product_id=product_id,
                provider=embedding_client.provider,
                model=embedding_client.model,
                dimensions=embedding_client.dimensions,
                document_text=expected_document_text,
                document_sha256=expected_document_sha256,
                embedding=list(embedding_result.vector),
                embedded_at=process_time,
            )
            session.add(embedding_row)
        else:
            embedding_row.provider = embedding_client.provider
            embedding_row.model = embedding_client.model
            embedding_row.dimensions = embedding_client.dimensions
            embedding_row.document_text = expected_document_text
            embedding_row.document_sha256 = expected_document_sha256
            embedding_row.embedding = list(embedding_result.vector)
            embedding_row.embedded_at = process_time
            embedding_row.updated_at = process_time

        task.status = ProductIndexingTaskStatus.COMPLETED
        task.completed_at = process_time
        task.last_error = None
        task.updated_at = process_time
        await session.commit()
        return IndexingProcessResult(
            status=IndexingProcessStatus.EMBEDDED,
            task_id=task_id,
            product_id=product_id,
            merchant_id=merchant_id,
            embedding_id=embedding_row.id,
        )


def _apply_failure_in_session(
    task: ProductIndexingTask,
    *,
    force_permanent: bool,
    max_attempts: int,
    retry_delay: timedelta,
    message: str,
    process_time: datetime,
) -> ProductIndexingTaskStatus:
    """Apply the retry/permanent-failure transition to an already-locked task."""
    effective_max_attempts = 1 if force_permanent else max_attempts
    outcome = decide_failure_outcome(
        attempts=task.attempts,
        max_attempts=effective_max_attempts,
    )
    if outcome is ProductIndexingTaskStatus.PENDING:
        task.status = ProductIndexingTaskStatus.PENDING
        task.available_at = process_time + retry_delay
        task.started_at = None
        task.completed_at = None
        task.last_error = sanitize_error_message(message)
    else:
        task.status = ProductIndexingTaskStatus.FAILED
        task.completed_at = process_time
        task.last_error = sanitize_error_message(message)
    task.updated_at = process_time
    return outcome


async def _fail_task_and_result(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    product_id: UUID,
    merchant_id: UUID,
    expected_attempt: int,
    force_permanent: bool,
    max_attempts: int,
    retry_delay: timedelta,
    message: str,
    process_time: datetime,
) -> IndexingProcessResult:
    try:
        outcome = await mark_indexing_task_failed(
            session_factory=session_factory,
            task_id=task_id,
            expected_attempt=expected_attempt,
            error_message=message,
            max_attempts=1 if force_permanent else max_attempts,
            retry_delay=retry_delay,
            now=process_time,
        )
    except StaleIndexingTaskClaimError:
        return _stale_result(task_id, product_id, merchant_id)
    return _failure_result(outcome, task_id, product_id, merchant_id, message)


def _failure_result(
    outcome: ProductIndexingTaskStatus,
    task_id: UUID,
    product_id: UUID,
    merchant_id: UUID,
    message: str,
) -> IndexingProcessResult:
    status = (
        IndexingProcessStatus.RETRY_SCHEDULED
        if outcome is ProductIndexingTaskStatus.PENDING
        else IndexingProcessStatus.PERMANENTLY_FAILED
    )
    return IndexingProcessResult(
        status=status,
        task_id=task_id,
        product_id=product_id,
        merchant_id=merchant_id,
        message=message,
    )


def _stale_result(task_id: UUID, product_id: UUID, merchant_id: UUID) -> IndexingProcessResult:
    return IndexingProcessResult(
        status=IndexingProcessStatus.STALE_CLAIM,
        task_id=task_id,
        product_id=product_id,
        merchant_id=merchant_id,
        message="Indexing task claim is stale.",
    )


def _safe_provider_message(exc: Exception) -> str:
    if isinstance(exc, JinaEmbeddingError):
        return str(exc).strip() or _PROVIDER_FAILURE_MESSAGE
    return _PROVIDER_FAILURE_MESSAGE

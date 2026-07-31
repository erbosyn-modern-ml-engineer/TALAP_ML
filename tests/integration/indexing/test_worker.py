from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db import create_session_factory
from talap.db.models import (
    Merchant,
    Product,
    ProductIndexingTask,
    ProductIndexingTaskStatus,
)
from talap.indexing.worker import (
    IndexingTaskNotFoundError,
    InvalidIndexingTaskTransitionError,
    StaleIndexingTaskClaimError,
    claim_indexing_tasks,
    mark_indexing_task_completed,
    mark_indexing_task_failed,
)

_NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
_STALE_AFTER = timedelta(minutes=5)


async def _create_merchant(
    session_factory: async_sessionmaker[AsyncSession],
) -> Merchant:
    async with session_factory() as session:
        merchant = Merchant(slug="merchant-worker", name="Merchant Worker", active=True)
        session.add(merchant)
        await session.commit()
        return merchant


async def _create_product(
    session_factory: async_sessionmaker[AsyncSession],
    merchant_id: UUID,
) -> Product:
    async with session_factory() as session:
        product = Product(
            id=uuid4(),
            merchant_id=merchant_id,
            merchant_product_key=f"SKU-{uuid4()}",
            name="Worker Product",
            category="school",
            description="",
            active=True,
        )
        session.add(product)
        await session.commit()
        return product


async def _add_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    merchant_id: UUID,
    product_id: UUID,
    status: ProductIndexingTaskStatus = ProductIndexingTaskStatus.PENDING,
    attempts: int = 0,
    available_at: datetime | None = None,
    started_at: datetime | None = None,
    changed_fields: list[str] | None = None,
) -> ProductIndexingTask:
    async with session_factory() as session:
        task = ProductIndexingTask(
            id=uuid4(),
            merchant_id=merchant_id,
            product_id=product_id,
            status=status,
            attempts=attempts,
            changed_fields=changed_fields or ["name"],
            available_at=available_at or (_NOW - timedelta(minutes=10)),
            started_at=started_at,
        )
        session.add(task)
        await session.commit()
        return task


async def _load_task(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
) -> ProductIndexingTask:
    async with session_factory() as session:
        task = await session.get(ProductIndexingTask, task_id)
        assert task is not None
        return task


async def test_claim_pending_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)

    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    assert len(claimed) == 1
    snapshot = claimed[0]
    assert snapshot.task_id == task.id
    assert snapshot.merchant_id == merchant.id
    assert snapshot.product_id == product.id
    assert snapshot.changed_fields == ["name"]
    assert snapshot.attempts == 1
    assert snapshot.started_at == _NOW

    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PROCESSING
    assert row.attempts == 1
    assert row.started_at == _NOW
    assert row.completed_at is None
    assert row.last_error is None


async def test_future_available_at_task_not_claimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        available_at=_NOW + timedelta(hours=1),
    )

    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    assert claimed == []
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PENDING
    assert row.attempts == 0
    assert row.started_at is None


async def test_two_concurrent_claimers_receive_disjoint_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    test_engine,
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)

    task_ids: list[UUID] = []
    async with session_factory() as session:
        for _ in range(4):
            task = ProductIndexingTask(
                id=uuid4(),
                merchant_id=merchant.id,
                product_id=product.id,
                changed_fields=["name"],
                available_at=_NOW - timedelta(minutes=10),
            )
            session.add(task)
            task_ids.append(task.id)
        await session.commit()

    second_factory = create_session_factory(test_engine)
    claimed_a, claimed_b = await asyncio.gather(
        claim_indexing_tasks(
            session_factory=session_factory,
            limit=2,
            stale_after=_STALE_AFTER,
            now=_NOW,
        ),
        claim_indexing_tasks(
            session_factory=second_factory,
            limit=2,
            stale_after=_STALE_AFTER,
            now=_NOW,
        ),
    )

    ids_a = {snapshot.task_id for snapshot in claimed_a}
    ids_b = {snapshot.task_id for snapshot in claimed_b}
    assert len(ids_a) == 2
    assert len(ids_b) == 2
    assert ids_a.isdisjoint(ids_b), "workers claimed the same task"
    assert ids_a | ids_b == set(task_ids)

    async with session_factory() as session:
        rows = (await session.execute(select(ProductIndexingTask))).scalars().all()
    assert len(rows) == 4
    assert all(row.status == ProductIndexingTaskStatus.PROCESSING for row in rows)
    assert all(row.attempts == 1 for row in rows)


async def test_recent_processing_task_not_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        status=ProductIndexingTaskStatus.PROCESSING,
        attempts=1,
        started_at=_NOW - timedelta(minutes=1),
    )

    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    assert claimed == []
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PROCESSING
    assert row.attempts == 1
    assert row.started_at == _NOW - timedelta(minutes=1)


async def test_stale_processing_task_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        status=ProductIndexingTaskStatus.PROCESSING,
        attempts=1,
        started_at=_NOW - timedelta(minutes=10),
    )

    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    assert len(claimed) == 1
    assert claimed[0].task_id == task.id
    assert claimed[0].attempts == 2
    assert claimed[0].started_at == _NOW

    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PROCESSING
    assert row.attempts == 2
    assert row.started_at == _NOW


async def test_attempts_increment_only_once_per_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)

    first = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert first[0].attempts == 1

    second = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert second == []

    row = await _load_task(session_factory, task.id)
    assert row.attempts == 1


async def test_completion_transition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)
    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    await mark_indexing_task_completed(
        session_factory=session_factory,
        task_id=task.id,
        expected_attempt=claimed[0].attempts,
        now=_NOW,
    )

    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.COMPLETED
    assert row.completed_at == _NOW
    assert row.last_error is None


async def test_retry_transition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)
    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    outcome = await mark_indexing_task_failed(
        session_factory=session_factory,
        task_id=task.id,
        expected_attempt=claimed[0].attempts,
        error_message="  boom  ",
        max_attempts=3,
        retry_delay=timedelta(minutes=10),
        now=_NOW,
    )

    assert outcome == ProductIndexingTaskStatus.PENDING
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PENDING
    assert row.available_at == _NOW + timedelta(minutes=10)
    assert row.started_at is None
    assert row.completed_at is None
    assert row.last_error == "boom"
    assert row.attempts == 1


async def test_permanent_failure_transition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)
    claimed = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )

    outcome = await mark_indexing_task_failed(
        session_factory=session_factory,
        task_id=task.id,
        expected_attempt=claimed[0].attempts,
        error_message="boom",
        max_attempts=1,
        retry_delay=timedelta(0),
        now=_NOW,
    )

    assert outcome == ProductIndexingTaskStatus.FAILED
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.FAILED
    assert row.completed_at == _NOW
    assert row.last_error == "boom"
    assert row.attempts == 1


async def test_invalid_transitions_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)

    # A COMPLETED task cannot be completed again.
    completed = await _add_task(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
        status=ProductIndexingTaskStatus.COMPLETED,
    )
    with pytest.raises(InvalidIndexingTaskTransitionError):
        await mark_indexing_task_completed(
            session_factory=session_factory,
            task_id=completed.id,
            expected_attempt=1,
            now=_NOW,
        )

    # A PENDING task cannot be marked failed (must be PROCESSING first).
    pending = await _add_task(
        session_factory,
        merchant_id=merchant.id,
        product_id=product.id,
    )
    with pytest.raises(InvalidIndexingTaskTransitionError):
        await mark_indexing_task_failed(
            session_factory=session_factory,
            task_id=pending.id,
            expected_attempt=1,
            error_message="boom",
            max_attempts=3,
            retry_delay=timedelta(0),
            now=_NOW,
        )

    # A missing task raises the domain not-found error.
    with pytest.raises(IndexingTaskNotFoundError):
        await mark_indexing_task_completed(
            session_factory=session_factory,
            task_id=uuid4(),
            expected_attempt=1,
            now=_NOW,
        )


async def test_stale_worker_cannot_complete_reclaimed_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)

    # Worker A claims the task: attempts = 1.
    worker_a = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert worker_a[0].task_id == task.id
    assert worker_a[0].attempts == 1

    # Make the task stale using a controlled timestamp.
    async with session_factory() as session:
        row = await session.get(ProductIndexingTask, task.id)
        assert row is not None
        row.started_at = _NOW - timedelta(minutes=10)
        await session.commit()

    # Worker B reclaims the stale task: attempts = 2.
    worker_b = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert worker_b[0].task_id == task.id
    assert worker_b[0].attempts == 2

    # Worker A resumes and tries to complete the claim it no longer owns.
    with pytest.raises(StaleIndexingTaskClaimError):
        await mark_indexing_task_completed(
            session_factory=session_factory,
            task_id=task.id,
            expected_attempt=1,
            now=_NOW,
        )

    # B's current claim is untouched: still processing, attempts=2, no end.
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PROCESSING
    assert row.attempts == 2
    assert row.completed_at is None

    # Worker B completes its own claim.
    await mark_indexing_task_completed(
        session_factory=session_factory,
        task_id=task.id,
        expected_attempt=2,
        now=_NOW,
    )
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.COMPLETED
    assert row.completed_at == _NOW


async def test_stale_worker_cannot_fail_reclaimed_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant = await _create_merchant(session_factory)
    product = await _create_product(session_factory, merchant.id)
    task = await _add_task(session_factory, merchant_id=merchant.id, product_id=product.id)

    # Worker A claims the task: attempt = 1.
    worker_a = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert worker_a[0].task_id == task.id
    assert worker_a[0].attempts == 1

    # Make the task stale using a controlled timestamp.
    async with session_factory() as session:
        row = await session.get(ProductIndexingTask, task.id)
        assert row is not None
        row.started_at = _NOW - timedelta(minutes=10)
        await session.commit()

    # Worker B stale-reclaims: attempt = 2.
    worker_b = await claim_indexing_tasks(
        session_factory=session_factory,
        limit=10,
        stale_after=_STALE_AFTER,
        now=_NOW,
    )
    assert worker_b[0].task_id == task.id
    assert worker_b[0].attempts == 2

    # Worker A resumes and tries to fail/retry the claim it no longer owns.
    with pytest.raises(StaleIndexingTaskClaimError):
        await mark_indexing_task_failed(
            session_factory=session_factory,
            task_id=task.id,
            expected_attempt=1,
            error_message="stale failure",
            max_attempts=3,
            retry_delay=timedelta(minutes=10),
            now=_NOW,
        )

    # B's claim remains: processing, attempts=2, started_at from B's claim,
    # and no error message was overwritten.
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PROCESSING
    assert row.attempts == 2
    assert row.started_at == _NOW
    assert row.last_error is None

    # Worker B fails its own claim: normal retry behavior.
    outcome = await mark_indexing_task_failed(
        session_factory=session_factory,
        task_id=task.id,
        expected_attempt=2,
        error_message="boom",
        max_attempts=3,
        retry_delay=timedelta(minutes=10),
        now=_NOW,
    )
    assert outcome == ProductIndexingTaskStatus.PENDING
    row = await _load_task(session_factory, task.id)
    assert row.status == ProductIndexingTaskStatus.PENDING
    assert row.available_at == _NOW + timedelta(minutes=10)
    assert row.last_error == "boom"

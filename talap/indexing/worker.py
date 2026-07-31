"""PostgreSQL-backed worker lifecycle for product indexing tasks.

T-017B1 implements task claiming (with ``FOR UPDATE SKIP LOCKED``), stale
recovery, completion, and retry/permanent-failure transitions. No embedding
API is called here; that arrives with a confirmed embedding provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db.models import ProductIndexingTask, ProductIndexingTaskStatus

__all__ = [
    "ClaimedIndexingTask",
    "IndexingTaskNotFoundError",
    "InvalidIndexingTaskTransitionError",
    "StaleIndexingTaskClaimError",
    "claim_indexing_tasks",
    "decide_failure_outcome",
    "mark_indexing_task_completed",
    "mark_indexing_task_failed",
    "sanitize_error_message",
    "validate_claim_limit",
    "validate_expected_attempt",
    "validate_failure_inputs",
]

_MAX_CLAIM_LIMIT = 100
_MAX_ERROR_LENGTH = 500
_BLANK_ERROR_MESSAGE = "Indexing task failed."


class IndexingTaskNotFoundError(Exception):
    """Raised when a task referenced by id does not exist."""


class InvalidIndexingTaskTransitionError(Exception):
    """Raised when a lifecycle transition is not allowed for the task state."""


class StaleIndexingTaskClaimError(Exception):
    """Raised when a worker tries to transition a claim it no longer owns."""


@dataclass(frozen=True)
class ClaimedIndexingTask:
    """Immutable snapshot of a task claimed by a worker.

    Deliberately decoupled from ORM instances so it stays safe to read after
    the claiming session is closed.
    """

    task_id: UUID
    merchant_id: UUID
    product_id: UUID
    changed_fields: list[str]
    attempts: int
    started_at: datetime

    @property
    def claim_attempt(self) -> int:
        """Generation of the current claim, used as a fencing token."""
        return self.attempts


def validate_claim_limit(limit: int) -> None:
    """Raise ``ValueError`` unless ``1 <= limit <= 100``."""
    if not 1 <= limit <= _MAX_CLAIM_LIMIT:
        raise ValueError("limit must satisfy 1 <= limit <= 100")


def validate_expected_attempt(expected_attempt: int) -> None:
    """Raise ``ValueError`` unless ``expected_attempt >= 1``."""
    if expected_attempt < 1:
        raise ValueError("expected_attempt must be at least 1")


def validate_failure_inputs(*, max_attempts: int, retry_delay: timedelta) -> None:
    """Raise ``ValueError`` for invalid failure-transition arguments."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay < timedelta(0):
        raise ValueError("retry_delay must not be negative")


def sanitize_error_message(message: str) -> str:
    """Normalize an error message before it is persisted.

    This helper only strips surrounding whitespace, substitutes a fixed
    default for blank input, and truncates to the ``last_error`` column limit
    (500 characters). It does NOT redact secrets or parse tracebacks: callers
    must pass a pre-sanitized, non-sensitive summary.
    """
    stripped = message.strip()
    if not stripped:
        return _BLANK_ERROR_MESSAGE
    return stripped[:_MAX_ERROR_LENGTH]


def decide_failure_outcome(*, attempts: int, max_attempts: int) -> ProductIndexingTaskStatus:
    """Return the target status after a failed attempt.

    ``pending`` means the task is retryable (attempts below the cap);
    ``failed`` means it is permanently failed (attempts reached the cap).
    """
    if attempts < max_attempts:
        return ProductIndexingTaskStatus.PENDING
    return ProductIndexingTaskStatus.FAILED


async def claim_indexing_tasks(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int,
    stale_after: timedelta,
    now: datetime | None = None,
) -> list[ClaimedIndexingTask]:
    """Claim up to ``limit`` eligible tasks in one PostgreSQL transaction.

    Eligible rows are PENDING with ``available_at <= now``, or PROCESSING
    tasks that went stale (``started_at <= now - stale_after``). Rows are
    locked with ``FOR UPDATE SKIP LOCKED`` so concurrent workers never claim
    the same task. Each claimed row is set to PROCESSING, ``attempts += 1``,
    ``started_at = now``, and cleared of completion/error state. The claim is
    committed before immutable snapshots are returned.
    """
    validate_claim_limit(limit)
    claim_time = now if now is not None else datetime.now(UTC)

    statement = (
        select(ProductIndexingTask)
        .where(
            or_(
                (ProductIndexingTask.status == ProductIndexingTaskStatus.PENDING)
                & (ProductIndexingTask.available_at <= claim_time),
                (ProductIndexingTask.status == ProductIndexingTaskStatus.PROCESSING)
                & (ProductIndexingTask.started_at <= claim_time - stale_after),
            )
        )
        .order_by(
            ProductIndexingTask.available_at,
            ProductIndexingTask.created_at,
            ProductIndexingTask.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

    async with session_factory() as session:
        tasks = list((await session.execute(statement)).scalars().all())
        for task in tasks:
            task.status = ProductIndexingTaskStatus.PROCESSING
            task.attempts += 1
            task.started_at = claim_time
            task.completed_at = None
            task.last_error = None
            task.updated_at = claim_time
        await session.commit()
        return [
            ClaimedIndexingTask(
                task_id=task.id,
                merchant_id=task.merchant_id,
                product_id=task.product_id,
                changed_fields=list(task.changed_fields),
                attempts=task.attempts,
                started_at=claim_time,
            )
            for task in tasks
        ]


async def mark_indexing_task_completed(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    expected_attempt: int,
    now: datetime | None = None,
) -> None:
    """Mark a PROCESSING task COMPLETED, locking its row first.

    ``expected_attempt`` is the claim generation returned by the claiming
    snapshot (``claimed.attempts`` / ``claimed.claim_attempt``). After
    locking, the task must be PROCESSING and ``attempts == expected_attempt``;
    otherwise the caller no longer owns the current claim and
    ``StaleIndexingTaskClaimError`` is raised without modifying the row. Only
    the current generation may transition ``processing -> completed``.
    """
    validate_expected_attempt(expected_attempt)
    completion_time = now if now is not None else datetime.now(UTC)

    async with session_factory() as session:
        task = await session.get(
            ProductIndexingTask,
            task_id,
            with_for_update=True,
        )
        if task is None:
            raise IndexingTaskNotFoundError(
                f"Indexing task {task_id} does not exist."
            )
        if task.status != ProductIndexingTaskStatus.PROCESSING:
            raise InvalidIndexingTaskTransitionError(
                "Only a PROCESSING indexing task can be marked completed; "
                f"task {task_id} is {task.status.value!r}."
            )
        if task.attempts != expected_attempt:
            raise StaleIndexingTaskClaimError("Indexing task claim is stale.")
        task.status = ProductIndexingTaskStatus.COMPLETED
        task.completed_at = completion_time
        task.last_error = None
        task.updated_at = completion_time
        await session.commit()


async def mark_indexing_task_failed(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    expected_attempt: int,
    error_message: str,
    max_attempts: int,
    retry_delay: timedelta,
    now: datetime | None = None,
) -> ProductIndexingTaskStatus:
    """Record a failed attempt and return the resulting task status.

    ``expected_attempt`` is the claim generation returned by the claiming
    snapshot (``claimed.attempts`` / ``claimed.claim_attempt``). Only the
    current claim owner may reschedule to PENDING or mark permanently FAILED:
    after locking, the task must be PROCESSING and
    ``attempts == expected_attempt``, otherwise ``StaleIndexingTaskClaimError``
    is raised and the row is left unchanged. With ``attempts < max_attempts``
    the task returns to PENDING with ``available_at = now + retry_delay``
    (returns ``pending``); with ``attempts >= max_attempts`` it becomes
    permanently FAILED (returns ``failed``). The error message is sanitized
    before it is persisted; no tracebacks are stored.
    """
    validate_expected_attempt(expected_attempt)
    validate_failure_inputs(max_attempts=max_attempts, retry_delay=retry_delay)
    failure_time = now if now is not None else datetime.now(UTC)
    safe_message = sanitize_error_message(error_message)

    async with session_factory() as session:
        task = await session.get(
            ProductIndexingTask,
            task_id,
            with_for_update=True,
        )
        if task is None:
            raise IndexingTaskNotFoundError(
                f"Indexing task {task_id} does not exist."
            )
        if task.status != ProductIndexingTaskStatus.PROCESSING:
            raise InvalidIndexingTaskTransitionError(
                "Only a PROCESSING indexing task can be marked failed; "
                f"task {task_id} is {task.status.value!r}."
            )
        if task.attempts != expected_attempt:
            raise StaleIndexingTaskClaimError("Indexing task claim is stale.")

        outcome = decide_failure_outcome(
            attempts=task.attempts,
            max_attempts=max_attempts,
        )
        if outcome is ProductIndexingTaskStatus.PENDING:
            task.status = ProductIndexingTaskStatus.PENDING
            task.available_at = failure_time + retry_delay
            task.started_at = None
            task.completed_at = None
            task.last_error = safe_message
        else:
            task.status = ProductIndexingTaskStatus.FAILED
            task.completed_at = failure_time
            task.last_error = safe_message
        task.updated_at = failure_time
        await session.commit()
        return outcome

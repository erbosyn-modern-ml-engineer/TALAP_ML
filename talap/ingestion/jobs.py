"""PostgreSQL-backed lifecycle for ``MessageProcessingJob`` rows (MVP-2).

Claiming uses ``FOR UPDATE SKIP LOCKED`` (one job at a time), commits before
the external HTTP call, and returns immutable snapshots with a fencing token
(``attempts``) so stale workers cannot transition a claim they no longer own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db.models import MessageProcessingJob, MessageProcessingJobStatus

__all__ = [
    "ClaimedMessageProcessingJob",
    "InvalidMessageProcessingJobTransitionError",
    "MessageProcessingJobNotFoundError",
    "StaleMessageProcessingJobClaimError",
    "claim_one_message_processing_job",
    "complete_message_processing_job",
    "decide_failure_outcome",
    "fail_message_processing_job",
    "release_message_processing_job",
    "sanitize_error_message",
]

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = timedelta(minutes=5)
DEFAULT_STALE_AFTER = timedelta(minutes=15)
_MAX_ERROR_LENGTH = 500
_BLANK_ERROR_MESSAGE = "WhatsApp echo processing failed."


class MessageProcessingJobNotFoundError(Exception):
    """Raised when a job referenced by id does not exist."""


class InvalidMessageProcessingJobTransitionError(Exception):
    """Raised when a lifecycle transition is not allowed for the job state."""


class StaleMessageProcessingJobClaimError(Exception):
    """Raised when a worker transitions a claim it no longer owns."""


@dataclass(frozen=True)
class ClaimedMessageProcessingJob:
    """Immutable snapshot of a job claimed by a worker."""

    job_id: UUID
    message_id: UUID
    attempts: int
    started_at: datetime

    @property
    def claim_attempt(self) -> int:
        """Generation of the current claim, used as a fencing token."""
        return self.attempts


def sanitize_error_message(message: str) -> str:
    """Normalize an error message before it is persisted (500-char cap)."""
    stripped = message.strip()
    if not stripped:
        return _BLANK_ERROR_MESSAGE
    return stripped[:_MAX_ERROR_LENGTH]


def decide_failure_outcome(
    *,
    attempts: int,
    max_attempts: int,
) -> MessageProcessingJobStatus:
    """Return the target status after a failed attempt (retry vs permanent)."""
    if attempts < max_attempts:
        return MessageProcessingJobStatus.PENDING
    return MessageProcessingJobStatus.FAILED


async def claim_one_message_processing_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> ClaimedMessageProcessingJob | None:
    """Claim exactly one eligible job and commit before returning a snapshot.

    Eligible rows are PENDING with ``available_at <= now``, or PROCESSING
    jobs that went stale (``started_at <= now - stale_after``). The row is
    locked with ``FOR UPDATE SKIP LOCKED`` so concurrent workers never claim
    the same job. The claim is committed before the immutable snapshot is
    returned.
    """
    claim_time = now if now is not None else datetime.now(UTC)
    statement = (
        select(MessageProcessingJob)
        .where(
            or_(
                (MessageProcessingJob.status == MessageProcessingJobStatus.PENDING)
                & (MessageProcessingJob.available_at <= claim_time),
                (
                    MessageProcessingJob.status
                    == MessageProcessingJobStatus.PROCESSING
                )
                & (MessageProcessingJob.started_at <= claim_time - stale_after),
            )
        )
        .order_by(
            MessageProcessingJob.available_at,
            MessageProcessingJob.created_at,
            MessageProcessingJob.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    async with session_factory() as session:
        job = (await session.execute(statement)).scalars().first()
        if job is None:
            return None
        job.status = MessageProcessingJobStatus.PROCESSING
        job.attempts += 1
        job.started_at = claim_time
        job.completed_at = None
        job.last_error = None
        job.updated_at = claim_time
        await session.commit()
        return ClaimedMessageProcessingJob(
            job_id=job.id,
            message_id=job.message_id,
            attempts=job.attempts,
            started_at=claim_time,
        )


async def _load_claimed_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    expected_attempt: int,
) -> MessageProcessingJob:
    job = await session.get(MessageProcessingJob, job_id, with_for_update=True)
    if job is None:
        raise MessageProcessingJobNotFoundError(
            f"Message processing job {job_id} does not exist."
        )
    if job.status != MessageProcessingJobStatus.PROCESSING:
        raise InvalidMessageProcessingJobTransitionError(
            "Only a PROCESSING job can be transitioned; "
            f"job {job_id} is {job.status.value!r}."
        )
    if job.attempts != expected_attempt:
        raise StaleMessageProcessingJobClaimError(
            "Message processing job claim is stale."
        )
    return job


async def complete_message_processing_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
    expected_attempt: int,
    now: datetime | None = None,
) -> None:
    """Mark a claimed PROCESSING job COMPLETED (fenced by ``expected_attempt``)."""
    completion_time = now if now is not None else datetime.now(UTC)
    async with session_factory() as session:
        job = await _load_claimed_job(
            session, job_id=job_id, expected_attempt=expected_attempt
        )
        job.status = MessageProcessingJobStatus.COMPLETED
        job.completed_at = completion_time
        job.last_error = None
        job.updated_at = completion_time
        await session.commit()


async def release_message_processing_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
    expected_attempt: int,
    now: datetime | None = None,
) -> None:
    """Return a claimed job to PENDING without consuming it (e.g. wrong channel).

    ``available_at`` is set to ``now`` so the job becomes immediately claimable
    by the correct worker.
    """
    claim_time = now if now is not None else datetime.now(UTC)
    async with session_factory() as session:
        job = await _load_claimed_job(
            session, job_id=job_id, expected_attempt=expected_attempt
        )
        job.status = MessageProcessingJobStatus.PENDING
        job.available_at = claim_time
        job.started_at = None
        job.completed_at = None
        job.updated_at = claim_time
        await session.commit()


async def fail_message_processing_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
    expected_attempt: int,
    error_message: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay: timedelta = DEFAULT_RETRY_DELAY,
    now: datetime | None = None,
) -> MessageProcessingJobStatus:
    """Record a failed attempt; reschedule to PENDING or mark permanently FAILED.

    Returns the resulting status. The error message is sanitized before it is
    persisted; no tracebacks are stored.
    """
    failure_time = now if now is not None else datetime.now(UTC)
    safe_message = sanitize_error_message(error_message)
    async with session_factory() as session:
        job = await _load_claimed_job(
            session, job_id=job_id, expected_attempt=expected_attempt
        )
        outcome = decide_failure_outcome(
            attempts=job.attempts,
            max_attempts=max_attempts,
        )
        if outcome is MessageProcessingJobStatus.PENDING:
            job.status = MessageProcessingJobStatus.PENDING
            job.available_at = failure_time + retry_delay
            job.started_at = None
            job.completed_at = None
            job.last_error = safe_message
        else:
            job.status = MessageProcessingJobStatus.FAILED
            job.completed_at = failure_time
            job.last_error = safe_message
        job.updated_at = failure_time
        await session.commit()
        return outcome

"""WhatsApp MVP-2 echo worker facades (one-shot, no loop).

Claims one PENDING ``MessageProcessingJob``, loads its ``InboundMessage``,
sends the fixed acknowledgement text via the WhatsApp client, and marks the
job completed. Text messages get an echo; voice/image/unsupported messages
complete without an outbound response; non-WhatsApp claims are released back
to PENDING. There is a documented at-least-once delivery window: Meta may
accept the message and then the process may crash before completion is
persisted, so a retry could send the echo twice. That is accepted for this
fixed MVP acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.channels.whatsapp import (
    SentWhatsAppMessage,
    WhatsAppClient,
    WhatsAppClientError,
)
from talap.core import get_settings
from talap.db import async_session_factory
from talap.db.models import InboundMessage, MessageProcessingJobStatus
from talap.ingestion.jobs import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY,
    claim_one_message_processing_job,
    complete_message_processing_job,
    fail_message_processing_job,
    release_message_processing_job,
)

__all__ = [
    "ECHO_TEXT",
    "EchoOutcome",
    "EchoProcessingResult",
    "process_one_whatsapp_echo_job",
    "run_whatsapp_echo_once",
]

ECHO_TEXT = "Сообщение получено"


class EchoOutcome(StrEnum):
    NO_JOB = "no_job"
    SENT = "sent"
    NO_RESPONSE = "no_response"
    RELEASED = "released"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"


@dataclass(frozen=True)
class EchoProcessingResult:
    """Outcome of processing one WhatsApp echo job."""

    outcome: EchoOutcome
    job_id: UUID | None = None
    sent_external_message_id: str | None = None


def _build_whatsapp_client() -> WhatsAppClient:
    settings = get_settings()
    access_token = (
        settings.whatsapp_access_token.get_secret_value()
        if settings.whatsapp_access_token is not None
        else None
    )
    return WhatsAppClient(
        access_token=access_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        graph_api_version=settings.whatsapp_graph_api_version,
    )


async def process_one_whatsapp_echo_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client: WhatsAppClient,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay: timedelta = DEFAULT_RETRY_DELAY,
    now: datetime | None = None,
) -> EchoProcessingResult:
    """Claim one job, send the fixed echo for text messages, and finalize it."""
    claimed = await claim_one_message_processing_job(
        session_factory=session_factory,
        now=now,
    )
    if claimed is None:
        return EchoProcessingResult(outcome=EchoOutcome.NO_JOB)

    async with session_factory() as session:
        message = await session.get(InboundMessage, claimed.message_id)

    if message is None:
        await complete_message_processing_job(
            session_factory=session_factory,
            job_id=claimed.job_id,
            expected_attempt=claimed.attempts,
            now=now,
        )
        return EchoProcessingResult(
            outcome=EchoOutcome.NO_RESPONSE,
            job_id=claimed.job_id,
        )

    if message.channel != "whatsapp":
        await release_message_processing_job(
            session_factory=session_factory,
            job_id=claimed.job_id,
            expected_attempt=claimed.attempts,
            now=now,
        )
        return EchoProcessingResult(
            outcome=EchoOutcome.RELEASED,
            job_id=claimed.job_id,
        )

    if message.message_type == "text":
        try:
            sent: SentWhatsAppMessage = await client.send_text(
                recipient=message.external_user_id,
                text=ECHO_TEXT,
            )
        except WhatsAppClientError:
            outcome = await fail_message_processing_job(
                session_factory=session_factory,
                job_id=claimed.job_id,
                expected_attempt=claimed.attempts,
                error_message="WhatsApp echo send failed.",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                now=now,
            )
            if outcome is MessageProcessingJobStatus.PENDING:
                return EchoProcessingResult(
                    outcome=EchoOutcome.RETRY_SCHEDULED,
                    job_id=claimed.job_id,
                )
            return EchoProcessingResult(
                outcome=EchoOutcome.FAILED,
                job_id=claimed.job_id,
            )
        await complete_message_processing_job(
            session_factory=session_factory,
            job_id=claimed.job_id,
            expected_attempt=claimed.attempts,
            now=now,
        )
        return EchoProcessingResult(
            outcome=EchoOutcome.SENT,
            job_id=claimed.job_id,
            sent_external_message_id=sent.external_message_id,
        )

    # voice / image / unsupported: no outbound response in this text-only MVP.
    await complete_message_processing_job(
        session_factory=session_factory,
        job_id=claimed.job_id,
        expected_attempt=claimed.attempts,
        now=now,
    )
    return EchoProcessingResult(
        outcome=EchoOutcome.NO_RESPONSE,
        job_id=claimed.job_id,
    )


async def run_whatsapp_echo_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
) -> EchoProcessingResult:
    """One-shot entrypoint: process at most one WhatsApp echo job."""
    client = _build_whatsapp_client()
    try:
        return await process_one_whatsapp_echo_job(
            session_factory=session_factory,
            client=client,
        )
    finally:
        await client.aclose()

"""WhatsApp product-recommendation worker facades (one-shot, no loop).

Claims one PENDING ``MessageProcessingJob``, loads its ``InboundMessage``,
extracts a validated ``CustomerRequest``, runs product search for product
requests, sends one concise WhatsApp text, and marks the job completed.
Voice/image/unsupported messages complete without an outbound response;
non-WhatsApp claims are released back to PENDING. There is a documented
at-least-once delivery window: Meta may accept the message and then the
process may crash before completion is persisted, so a retry could send the
response twice. That is accepted for this MVP.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.ai.customer_request import (
    CustomerRequest,
    CustomerRequestConfigurationError,
    CustomerRequestExtractionError,
    extract_customer_request,
)
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
    ClaimedMessageProcessingJob,
    claim_one_message_processing_job,
    complete_message_processing_job,
    fail_message_processing_job,
    release_message_processing_job,
)
from talap.search.products import (
    ProductSearchExecutionError,
    ProductSearchResult,
    ProductSearchValidationError,
    search_products,
)

__all__ = [
    "ECHO_TEXT",
    "EchoOutcome",
    "EchoProcessingResult",
    "process_one_whatsapp_echo_job",
    "run_whatsapp_echo_once",
]

ECHO_TEXT = "Сообщение получено"

RECOMMENDATION_HEADER = "Нашёл подходящие варианты:"
RECOMMENDATION_FOOTER = "Ответьте номером товара: 1, 2 или 3."
NO_RESULTS_TEXT = "К сожалению, подходящих товаров сейчас не найдено."
CLARIFICATION_PREFIX = "Уточните, пожалуйста: "
HANDOFF_TEXT = "Передаю ваш запрос менеджеру."
UNKNOWN_TEXT = "Опишите, пожалуйста, какой товар вы ищете."

RECOMMENDATION_LIMIT = 3

ExtractorCallable = Callable[..., Awaitable[CustomerRequest]]
SearchCallable = Callable[..., Awaitable[tuple[ProductSearchResult, ...]]]


def format_recommendations(
    results: tuple[ProductSearchResult, ...],
) -> str:
    """Format up to ``RECOMMENDATION_LIMIT`` results as one concise message."""
    items = "\n".join(
        f"{index}. {result.name} — {result.price_kzt} ₸"
        for index, result in enumerate(results, start=1)
    )
    return f"{RECOMMENDATION_HEADER}\n\n{items}\n\n{RECOMMENDATION_FOOTER}"


async def build_response_text(
    request: CustomerRequest,
    *,
    search: SearchCallable,
) -> str:
    """Build the outbound WhatsApp text for one validated request."""
    if request.intent == "handoff":
        return HANDOFF_TEXT
    if request.intent == "unknown":
        return UNKNOWN_TEXT
    if request.missing_field is not None:
        return CLARIFICATION_PREFIX + request.missing_field
    results = await search(request=request, limit=RECOMMENDATION_LIMIT)
    if not results:
        return NO_RESULTS_TEXT
    return format_recommendations(results)


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


async def _fail_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedMessageProcessingJob,
    error_message: str,
    max_attempts: int,
    retry_delay: timedelta,
    now: datetime | None,
) -> EchoProcessingResult:
    """Record a failed attempt via the existing retry/FAILED lifecycle."""
    outcome = await fail_message_processing_job(
        session_factory=session_factory,
        job_id=claimed.job_id,
        expected_attempt=claimed.attempts,
        error_message=error_message,
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


async def process_one_whatsapp_echo_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client: WhatsAppClient,
    extractor: ExtractorCallable = extract_customer_request,
    search: SearchCallable = search_products,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay: timedelta = DEFAULT_RETRY_DELAY,
    now: datetime | None = None,
) -> EchoProcessingResult:
    """Claim one job, recommend products for text messages, and finalize it."""
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
            request = await extractor(text=message.text or "")
        except (CustomerRequestConfigurationError, CustomerRequestExtractionError):
            return await _fail_job(
                session_factory=session_factory,
                claimed=claimed,
                error_message="Customer request extraction failed.",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                now=now,
            )
        try:
            response_text = await build_response_text(request, search=search)
        except (ProductSearchExecutionError, ProductSearchValidationError):
            return await _fail_job(
                session_factory=session_factory,
                claimed=claimed,
                error_message="Product search failed.",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                now=now,
            )
        try:
            sent: SentWhatsAppMessage = await client.send_text(
                recipient=message.external_user_id,
                text=response_text,
            )
        except WhatsAppClientError:
            return await _fail_job(
                session_factory=session_factory,
                claimed=claimed,
                error_message="WhatsApp send failed.",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                now=now,
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

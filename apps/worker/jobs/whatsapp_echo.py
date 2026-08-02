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
from talap.recommendations import (
    is_numeric_selection,
    load_active_recommendation,
    manager_whatsapp_link,
    mark_recommendation_selected,
    persist_unmet_demand,
    store_recommendation_set,
    unmet_demand_response,
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


def _displayed_snapshots(
    results: tuple[ProductSearchResult, ...],
) -> list[dict[str, object]]:
    """Compact product snapshots stored with the displayed set."""
    return [
        {
            "product_id": str(result.product_id),
            "name": result.name,
            "price_kzt": result.price_kzt,
        }
        for result in results
    ]


def _product_uuid(displayed: dict[str, object]) -> UUID | None:
    value = displayed.get("product_id")
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def format_selection_response(
    displayed: dict[str, object],
    manager_link: str | None,
) -> str:
    """Confirmation with the selected product and the configured manager link."""
    name = str(displayed.get("name") or "")
    price = displayed.get("price_kzt")
    price_part = f" — {price} ₸" if isinstance(price, int) else ""
    text = f"Вы выбрали: {name}{price_part}."
    if manager_link:
        text += f"\n\nНапишите менеджеру:\n{manager_link}"
    return text


def _out_of_range_selection_text(count: int) -> str:
    return f"Выберите, пожалуйста, номер от 1 до {count}."


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


async def _handle_selection(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client: WhatsAppClient,
    claimed: ClaimedMessageProcessingJob,
    message: InboundMessage,
    trimmed_text: str,
    max_attempts: int,
    retry_delay: timedelta,
    now: datetime | None,
) -> EchoProcessingResult | None:
    """Resolve a numeric selection against the active set; None if not a selection."""
    if not is_numeric_selection(trimmed_text):
        return None
    try:
        active = await load_active_recommendation(
            session_factory=session_factory,
            channel=message.channel,
            external_user_id=message.external_user_id,
        )
    except Exception:
        return await _fail_job(
            session_factory=session_factory,
            claimed=claimed,
            error_message="Recommendation state access failed.",
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            now=now,
        )
    if active is None:
        return None
    index = int(trimmed_text)
    if not 1 <= index <= len(active.displayed_products):
        response_text = _out_of_range_selection_text(len(active.displayed_products))
    else:
        displayed = active.displayed_products[index - 1]
        try:
            await mark_recommendation_selected(
                session_factory=session_factory,
                state_id=active.state_id,
                selected_index=index,
                selected_product_id=_product_uuid(displayed),
                now=now,
            )
        except Exception:
            return await _fail_job(
                session_factory=session_factory,
                claimed=claimed,
                error_message="Recommendation state access failed.",
                max_attempts=max_attempts,
                retry_delay=retry_delay,
                now=now,
            )
        response_text = format_selection_response(
            displayed, manager_whatsapp_link()
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
        selection_result = await _handle_selection(
            session_factory=session_factory,
            client=client,
            claimed=claimed,
            message=message,
            trimmed_text=(message.text or "").strip(),
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            now=now,
        )
        if selection_result is not None:
            return selection_result

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

        if request.intent == "product_search" and request.missing_field is None:
            try:
                results = await search(request=request, limit=RECOMMENDATION_LIMIT)
            except (ProductSearchExecutionError, ProductSearchValidationError):
                return await _fail_job(
                    session_factory=session_factory,
                    claimed=claimed,
                    error_message="Product search failed.",
                    max_attempts=max_attempts,
                    retry_delay=retry_delay,
                    now=now,
                )
            if results:
                try:
                    await store_recommendation_set(
                        session_factory=session_factory,
                        channel=message.channel,
                        external_user_id=message.external_user_id,
                        displayed_products=_displayed_snapshots(results),
                        source_message_id=message.id,
                        now=now,
                    )
                except Exception:
                    return await _fail_job(
                        session_factory=session_factory,
                        claimed=claimed,
                        error_message="Recommendation state storage failed.",
                        max_attempts=max_attempts,
                        retry_delay=retry_delay,
                        now=now,
                    )
                response_text = format_recommendations(results)
            else:
                try:
                    await persist_unmet_demand(
                        session_factory=session_factory,
                        channel=message.channel,
                        external_user_id=message.external_user_id,
                        source_message_id=message.id,
                        request=request,
                    )
                except Exception:
                    return await _fail_job(
                        session_factory=session_factory,
                        claimed=claimed,
                        error_message="Unmet demand storage failed.",
                        max_attempts=max_attempts,
                        retry_delay=retry_delay,
                        now=now,
                    )
                response_text = unmet_demand_response(request.language)
        else:
            response_text = await build_response_text(request, search=search)

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

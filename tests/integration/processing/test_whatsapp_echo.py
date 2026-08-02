"""Real-PostgreSQL integration tests for the WhatsApp recommendation worker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.jobs.whatsapp_echo import (
    CLARIFICATION_PREFIX,
    NO_RESULTS_TEXT,
    RECOMMENDATION_HEADER,
    EchoOutcome,
    process_one_whatsapp_echo_job,
)
from talap.ai.customer_request import (
    CustomerRequest,
    CustomerRequestExtractionError,
)
from talap.channels.telegram import normalize_telegram_update
from talap.channels.whatsapp import (
    SentWhatsAppMessage,
    normalize_whatsapp_webhook,
)
from talap.db.models import (
    ChannelConnection,
    InboundEvent,
    InboundMessage,
    MessageProcessingJob,
    MessageProcessingJobStatus,
)
from talap.ingestion import ingest_normalized_webhook
from talap.search.products import ProductSearchResult

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_RECEIVED = _NOW


class _FakeWhatsAppClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        sent_id: str = "wamid.SYNTHETIC_SENT_1",
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error
        self.sent_id = sent_id

    async def send_text(
        self, *, recipient: str, text: str
    ) -> SentWhatsAppMessage:
        self.calls.append((recipient, text))
        if self.error is not None:
            raise self.error
        return SentWhatsAppMessage(external_message_id=self.sent_id)

    async def aclose(self) -> None:
        pass


class _FakeExtractor:
    def __init__(
        self,
        request: CustomerRequest | None = None,
        error: Exception | None = None,
    ) -> None:
        self.request = request if request is not None else _product_request()
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, *, text: str) -> CustomerRequest:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.request


class _FakeSearch:
    def __init__(
        self,
        results: tuple[ProductSearchResult, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.results = results
        self.error = error
        self.calls: list[tuple[CustomerRequest, int]] = []

    async def __call__(
        self, *, request: CustomerRequest, limit: int = 3
    ) -> tuple[ProductSearchResult, ...]:
        self.calls.append((request, limit))
        if self.error is not None:
            raise self.error
        return self.results


def _product_request(**overrides: object) -> CustomerRequest:
    base: dict[str, object] = {
        "intent": "product_search",
        "language": "ru",
        "query_text": "синие кроссовки",
        "category": None,
        "attributes": {},
        "budget_max_kzt": None,
        "quantity": None,
        "missing_field": None,
    }
    base.update(overrides)
    return CustomerRequest(**base)


def _result(name: str, price: int) -> ProductSearchResult:
    return ProductSearchResult(
        product_id=uuid4(),
        name=name,
        category="school",
        description=None,
        price_kzt=price,
        available_quantity=5,
        merchant_sku="SKU-1",
        material=None,
        similarity=0.9,
    )


async def _create_connection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channel: str = "whatsapp",
) -> UUID:
    connection = ChannelConnection(channel=channel, name="echo-test")
    async with session_factory() as session:
        session.add(connection)
        await session.commit()
        return connection.id


def _whatsapp_text_raw(
    *, message_id: str, sender: str, body: str
) -> bytes:
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


def _whatsapp_unsupported_raw(
    *, message_id: str, sender: str
) -> bytes:
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
                                    "type": "sticker",
                                    "sticker": {
                                        "id": "322999999999999",
                                        "mime_type": "image/webp",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _ingest_raw(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw: bytes,
    channel: str,
) -> None:
    payload = json.loads(raw)
    if channel == "whatsapp":
        result = normalize_whatsapp_webhook(payload, received_at=_RECEIVED)
        messages = list(result.messages)
        statuses = list(result.statuses)
    else:
        message = normalize_telegram_update(payload, received_at=_RECEIVED)
        messages = [message]
        statuses = []
    connection_id = await _create_connection(session_factory, channel=channel)
    await ingest_normalized_webhook(
        connection_id=connection_id,
        channel=channel,
        raw_body=raw,
        payload=payload,
        received_at=_RECEIVED,
        messages=messages,
        whatsapp_statuses=statuses,
        session_factory=session_factory,
    )


async def _seed_text_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    sender: str = "77000000001",
    body: str = "hello",
    external_message_id: str = "wamid.SYNTHETIC_ECHO_1",
) -> tuple[UUID, UUID]:
    raw = _whatsapp_text_raw(
        message_id=external_message_id, sender=sender, body=body
    )
    await _ingest_raw(session_factory, raw=raw, channel="whatsapp")
    async with session_factory() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.external_message_id == external_message_id
                )
            )
        ).scalar_one()
        job = (
            await session.execute(
                select(MessageProcessingJob).where(
                    MessageProcessingJob.message_id == message.id
                )
            )
        ).scalar_one()
        await _make_job_due(session_factory, job.id)
        return message.id, job.id


async def _make_job_due(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
) -> None:
    """Move a job's available_at into the past relative to the fixed clock."""
    async with session_factory() as session:
        job = await session.get(MessageProcessingJob, job_id)
        assert job is not None
        job.available_at = _NOW - timedelta(minutes=10)
        await session.commit()


async def _load_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: UUID,
) -> MessageProcessingJob:
    async with session_factory() as session:
        return (await session.get(MessageProcessingJob, job_id))  # type: ignore[return-value]


async def _count(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


# ── 1–7. Recommendation flow ───────────────────────────────────────────


async def test_pending_text_job_produces_recommendation_response(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    extractor = _FakeExtractor()
    search = _FakeSearch(
        results=(_result("Кроссовки", 1000), _result("Ботинки", 2000))
    )
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=extractor,
        search=search,
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.SENT
    assert result.job_id == job_id
    text = client.calls[0][1]
    assert RECOMMENDATION_HEADER in text
    assert "1. Кроссовки — 1000 ₸" in text
    assert "2. Ботинки — 2000 ₸" in text


async def test_inbound_user_id_used_as_recipient(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_text_message(
        session_factory, sender="77000000007", external_message_id="wamid.SYNTHETIC_ECHO_R"
    )
    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=_FakeExtractor(),
        search=_FakeSearch(results=(_result("Кроссовки", 1000),)),
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.SENT
    assert client.calls[0][0] == "77000000007"


async def test_successful_recommendation_marks_job_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=_FakeExtractor(),
        search=_FakeSearch(results=(_result("Кроссовки", 1000),)),
        now=_NOW,
    )
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED
    assert job.attempts == 1
    assert job.completed_at == _NOW
    assert job.last_error is None


async def test_extractor_receives_exact_stored_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_text_message(
        session_factory, body="купите мне синие кроссовки"
    )
    client = _FakeWhatsAppClient()
    extractor = _FakeExtractor()
    await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=extractor,
        search=_FakeSearch(results=(_result("Кроссовки", 1000),)),
        now=_NOW,
    )
    assert extractor.calls == ["купите мне синие кроссовки"]


async def test_search_receives_validated_request_with_limit_three(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    extractor = _FakeExtractor()
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=extractor,
        search=search,
        now=_NOW,
    )
    assert len(search.calls) == 1
    assert search.calls[0][0] is extractor.request
    assert search.calls[0][1] == 3


async def test_no_result_response_completes_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=_FakeExtractor(),
        search=_FakeSearch(results=()),
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.SENT
    assert client.calls[0][1] == NO_RESULTS_TEXT
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED


async def test_clarification_branch_completes_without_search(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    extractor = _FakeExtractor(request=_product_request(missing_field="цвет"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=extractor,
        search=search,
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == CLARIFICATION_PREFIX + "цвет"
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED


# ── 8–10. Failure paths and side-effect freedom ────────────────────────


async def test_external_failure_schedules_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    extractor = _FakeExtractor(error=CustomerRequestExtractionError("boom"))
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=extractor,
        search=_FakeSearch(results=(_result("Кроссовки", 1000),)),
        max_attempts=3,
        retry_delay=timedelta(minutes=5),
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.RETRY_SCHEDULED
    assert client.calls == []
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.PENDING
    assert job.attempts == 1
    assert job.available_at == _NOW + timedelta(minutes=5)
    assert job.last_error == "Customer request extraction failed."
    assert job.started_at is None


async def test_max_attempt_external_failure_marks_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    extractor = _FakeExtractor(error=CustomerRequestExtractionError("boom"))
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=extractor,
        search=_FakeSearch(results=(_result("Кроссовки", 1000),)),
        max_attempts=1,
        retry_delay=timedelta(minutes=5),
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.FAILED
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.FAILED
    assert job.attempts == 1
    assert job.completed_at == _NOW
    assert job.last_error == "Customer request extraction failed."


async def test_no_new_inbound_event_message_or_job_created(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = (
        await _count(session_factory, InboundEvent),
        await _count(session_factory, InboundMessage),
        await _count(session_factory, MessageProcessingJob),
    )
    await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        extractor=_FakeExtractor(),
        search=_FakeSearch(results=(_result("Кроссовки", 1000),)),
        now=_NOW,
    )
    after = (
        await _count(session_factory, InboundEvent),
        await _count(session_factory, InboundMessage),
        await _count(session_factory, MessageProcessingJob),
    )
    # Seeding adds one row of each; processing itself must not create any.
    assert after == (before[0] + 1, before[1] + 1, before[2] + 1)
    assert after[1] == 1
    assert after[2] == 1


# ── Extra: non-WhatsApp job is released ─────────────────────────────────


async def test_non_whatsapp_job_is_released(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = json.dumps(
        {
            "update_id": 1000001,
            "message": {
                "message_id": 41001,
                "from": {"id": 771100001},
                "chat": {"id": 771100001},
                "text": "hello telegram",
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await _ingest_raw(session_factory, raw=raw, channel="telegram")
    async with session_factory() as session:
        job = (
            await session.execute(
                select(MessageProcessingJob).order_by(
                    MessageProcessingJob.created_at
                )
            )
        ).scalars().first()
        assert job is not None
        job_id = job.id
    await _make_job_due(session_factory, job_id)

    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    assert result.outcome == EchoOutcome.RELEASED
    assert client.calls == []
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.PENDING
    assert job.attempts == 1
    assert job.available_at == _NOW

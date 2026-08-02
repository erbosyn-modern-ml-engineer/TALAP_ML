"""Real-PostgreSQL integration tests for the WhatsApp MVP-2 echo worker."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.jobs.whatsapp_echo import (
    ECHO_TEXT,
    EchoOutcome,
    process_one_whatsapp_echo_job,
)
from talap.channels.telegram import normalize_telegram_update
from talap.channels.whatsapp import (
    SentWhatsAppMessage,
    WhatsAppClientError,
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

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_RECEIVED = _NOW


class _FakeWhatsAppClient:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        sent_id: str = "wamid.SYNTHETIC_SENT_1",
        on_send: object | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error
        self.sent_id = sent_id
        self.on_send = on_send

    async def send_text(
        self, *, recipient: str, text: str
    ) -> SentWhatsAppMessage:
        self.calls.append((recipient, text))
        if self.on_send is not None:
            await self.on_send()  # type: ignore[misc]
        if self.error is not None:
            raise self.error
        return SentWhatsAppMessage(external_message_id=self.sent_id)

    async def aclose(self) -> None:
        pass


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


async def _seed_unsupported_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    sender: str = "77000000002",
    external_message_id: str = "wamid.SYNTHETIC_ECHO_STICKER_1",
) -> tuple[UUID, UUID]:
    raw = _whatsapp_unsupported_raw(
        message_id=external_message_id, sender=sender
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


# ── 1–4. Happy path ─────────────────────────────────────────────────────


async def test_pending_job_claimed_once_and_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    assert result.outcome == EchoOutcome.SENT
    assert result.job_id == job_id
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED
    assert job.attempts == 1
    assert job.started_at == _NOW


async def test_inbound_external_user_id_passed_as_recipient(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_text_message(
        session_factory, sender="77000000007", external_message_id="wamid.SYNTHETIC_ECHO_R"
    )
    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    assert result.outcome == EchoOutcome.SENT
    assert client.calls[0][0] == "77000000007"


async def test_exact_fixed_text_passed_to_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    assert result.outcome == EchoOutcome.SENT
    assert client.calls[0][1] == ECHO_TEXT
    assert ECHO_TEXT == "Сообщение получено"
    assert result.sent_external_message_id == "wamid.SYNTHETIC_SENT_1"


async def test_successful_send_marks_job_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient()
    await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED
    assert job.completed_at == _NOW
    assert job.last_error is None


# ── 5. Concurrent claimers ──────────────────────────────────────────────


async def test_two_workers_do_not_claim_same_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client_a = _FakeWhatsAppClient()
    client_b = _FakeWhatsAppClient()
    result_a, result_b = await asyncio.gather(
        process_one_whatsapp_echo_job(
            session_factory=session_factory, client=client_a, now=_NOW
        ),
        process_one_whatsapp_echo_job(
            session_factory=session_factory, client=client_b, now=_NOW
        ),
    )
    assert {result_a.outcome, result_b.outcome} == {
        EchoOutcome.SENT,
        EchoOutcome.NO_JOB,
    }
    assert (len(client_a.calls) + len(client_b.calls)) == 1
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED
    assert job.attempts == 1


# ── 6–7. Failure paths ──────────────────────────────────────────────────


async def test_temporary_failure_schedules_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient(error=WhatsAppClientError("transient"))
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        max_attempts=3,
        retry_delay=timedelta(minutes=5),
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.RETRY_SCHEDULED
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.PENDING
    assert job.attempts == 1
    assert job.available_at == _NOW + timedelta(minutes=5)
    assert job.last_error == "WhatsApp echo send failed."
    assert job.started_at is None


async def test_max_attempt_failure_marks_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)
    client = _FakeWhatsAppClient(error=WhatsAppClientError("transient"))
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory,
        client=client,
        max_attempts=1,
        retry_delay=timedelta(minutes=5),
        now=_NOW,
    )
    assert result.outcome == EchoOutcome.FAILED
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.FAILED
    assert job.attempts == 1
    assert job.completed_at == _NOW
    assert job.last_error == "WhatsApp echo send failed."


# ── 8. Unsupported message ──────────────────────────────────────────────


async def test_unsupported_message_completes_without_send(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_unsupported_message(session_factory)
    client = _FakeWhatsAppClient()
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    assert result.outcome == EchoOutcome.NO_RESPONSE
    assert client.calls == []
    job = await _load_job(session_factory, job_id)
    assert job.status == MessageProcessingJobStatus.COMPLETED


# ── 9. No open DB transaction during HTTP wait ──────────────────────────


async def test_no_db_transaction_open_during_fake_http(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, job_id = await _seed_text_message(session_factory)

    async def _probe() -> None:
        # Runs inside send_text; a committed claim must be visible from a
        # separate session (PROCESSING, attempts=1).
        async with session_factory() as session:
            job = await session.get(MessageProcessingJob, job_id)
            assert job is not None
            assert job.status == MessageProcessingJobStatus.PROCESSING
            assert job.attempts == 1

    client = _FakeWhatsAppClient(on_send=_probe)
    result = await process_one_whatsapp_echo_job(
        session_factory=session_factory, client=client, now=_NOW
    )
    assert result.outcome == EchoOutcome.SENT


# ── 10. No new rows created ─────────────────────────────────────────────


async def test_echo_processor_creates_no_new_rows(
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
        session_factory=session_factory, client=client, now=_NOW
    )
    after = (
        await _count(session_factory, InboundEvent),
        await _count(session_factory, InboundMessage),
        await _count(session_factory, MessageProcessingJob),
    )
    # The echo processor adds one row of each during seeding (test setup),
    # but processing itself must not create any additional rows.
    assert after == (before[0] + 1, before[1] + 1, before[2] + 1)
    # Exactly one message and one job exist for the seeded message.
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

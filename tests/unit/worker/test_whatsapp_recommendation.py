from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from apps.worker.jobs import whatsapp_echo as module
from talap.ai.customer_request import (
    CustomerRequest,
    CustomerRequestExtractionError,
)
from talap.channels.whatsapp import SentWhatsAppMessage, WhatsAppClientError
from talap.db.models import MessageProcessingJobStatus
from talap.ingestion.jobs import ClaimedMessageProcessingJob
from talap.search.products import ProductSearchExecutionError, ProductSearchResult

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_JOB_ID = UUID("00000000-0000-0000-0000-0000000000aa")
_MESSAGE_ID = UUID("00000000-0000-0000-0000-0000000000bb")
_PRODUCT_ID = UUID("22222222-2222-2222-2222-222222222222")


class _FakeMessage:
    def __init__(
        self,
        *,
        channel: str = "whatsapp",
        message_type: str = "text",
        text: str = "синие кроссовки",
        external_user_id: str = "77000000001",
    ) -> None:
        self.channel = channel
        self.message_type = message_type
        self.text = text
        self.external_user_id = external_user_id


class _FakeSession:
    def __init__(self, message: _FakeMessage) -> None:
        self._message = message

    async def get(self, model: object, pk: object) -> _FakeMessage:
        return self._message

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, message: _FakeMessage) -> None:
        self._message = message

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._message)


class _Lifecycle:
    def __init__(self, fail_status: MessageProcessingJobStatus) -> None:
        self.fail_status = fail_status
        self.completed: list[UUID] = []
        self.failed: list[tuple[UUID, str]] = []

    async def complete(
        self,
        *,
        session_factory: object,
        job_id: UUID,
        expected_attempt: int,
        now: datetime | None = None,
    ) -> None:
        self.completed.append(job_id)

    async def fail(
        self,
        *,
        session_factory: object,
        job_id: UUID,
        expected_attempt: int,
        error_message: str,
        max_attempts: int,
        retry_delay: timedelta,
        now: datetime | None = None,
    ) -> MessageProcessingJobStatus:
        self.failed.append((job_id, error_message))
        return self.fail_status


def _request(**overrides: object) -> CustomerRequest:
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
        product_id=_PRODUCT_ID,
        name=name,
        category="school",
        description=None,
        price_kzt=price,
        available_quantity=5,
        merchant_sku="SKU-1",
        material=None,
        similarity=0.9,
    )


class _FakeExtractor:
    def __init__(
        self,
        request: CustomerRequest | None = None,
        error: Exception | None = None,
    ) -> None:
        self.request = request if request is not None else _request()
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


class _FakeWhatsAppClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def send_text(self, *, recipient: str, text: str) -> SentWhatsAppMessage:
        self.calls.append((recipient, text))
        if self.error is not None:
            raise self.error
        return SentWhatsAppMessage(external_message_id="wamid.UNIT")

    async def aclose(self) -> None:
        pass


async def _run(
    monkeypatch: object,
    *,
    message: _FakeMessage | None = None,
    extractor: _FakeExtractor | None = None,
    search: _FakeSearch | None = None,
    client: _FakeWhatsAppClient | None = None,
    fail_status: MessageProcessingJobStatus = MessageProcessingJobStatus.PENDING,
) -> tuple[object, _Lifecycle, _FakeExtractor, _FakeSearch, _FakeWhatsAppClient]:
    lifecycle = _Lifecycle(fail_status=fail_status)

    async def _claim(
        *, session_factory: object, now: datetime | None = None, stale_after: object = None
    ) -> ClaimedMessageProcessingJob:
        return ClaimedMessageProcessingJob(
            job_id=_JOB_ID,
            message_id=_MESSAGE_ID,
            attempts=1,
            started_at=_NOW,
        )

    monkeypatch.setattr(module, "claim_one_message_processing_job", _claim)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "complete_message_processing_job", lifecycle.complete)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "fail_message_processing_job", lifecycle.fail)  # type: ignore[attr-defined]

    extractor = extractor if extractor is not None else _FakeExtractor()
    search = search if search is not None else _FakeSearch(results=(_result("Кроссовки", 1000),))
    client = client if client is not None else _FakeWhatsAppClient()

    result = await module.process_one_whatsapp_echo_job(
        session_factory=_FakeSessionFactory(message or _FakeMessage()),
        client=client,  # type: ignore[arg-type]
        extractor=extractor,
        search=search,
        max_attempts=3,
        retry_delay=timedelta(minutes=5),
        now=_NOW,
    )
    return result, lifecycle, extractor, search, client


async def test_complete_product_request_calls_extractor_once(
    monkeypatch: object,
) -> None:
    result, _, extractor, _, client = await _run(monkeypatch)
    assert result.outcome == module.EchoOutcome.SENT
    assert extractor.calls == ["синие кроссовки"]
    assert len(client.calls) == 1


async def test_product_request_calls_search_once_with_limit_three(
    monkeypatch: object,
) -> None:
    result, _, extractor, search, _ = await _run(monkeypatch)
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == [(extractor.request, 3)]


async def test_three_products_formatted_in_deterministic_order(
    monkeypatch: object,
) -> None:
    results = (
        _result("Кроссовки", 1000),
        _result("Ботинки", 2000),
        _result("Футболка", 3000),
    )
    search = _FakeSearch(results=results)
    result, _, _, _, client = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert text == module.format_recommendations(results)
    assert "1. Кроссовки — 1000 ₸" in text
    assert "2. Ботинки — 2000 ₸" in text
    assert "3. Футболка — 3000 ₸" in text


async def test_fewer_than_three_results_format_correctly(
    monkeypatch: object,
) -> None:
    results = (_result("Кроссовки", 1000), _result("Ботинки", 2000))
    search = _FakeSearch(results=results)
    result, _, _, _, client = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert text == module.format_recommendations(results)
    assert "\n3. " not in text
    assert text.count("\n\n") == 2


async def test_internal_ids_sku_similarity_absent_from_response(
    monkeypatch: object,
) -> None:
    result_item = ProductSearchResult(
        product_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        name="Кроссовки",
        category="school",
        description=None,
        price_kzt=1000,
        available_quantity=5,
        merchant_sku="SKU-SECRET",
        material="Cotton",
        similarity=0.999,
    )
    search = _FakeSearch(results=(result_item,))
    result, _, _, _, client = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert "SKU-SECRET" not in text
    assert "0.999" not in text
    assert "aaaaaaaa" not in text
    assert "Cotton" not in text


async def test_no_results_sends_fallback(monkeypatch: object) -> None:
    search = _FakeSearch(results=())
    result, _, _, _, client = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    assert client.calls[0][1] == module.NO_RESULTS_TEXT


async def test_missing_field_sends_clarification_and_skips_search(
    monkeypatch: object,
) -> None:
    extractor = _FakeExtractor(request=_request(missing_field="цвет"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, _, search, client = await _run(monkeypatch, extractor=extractor, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == "Уточните, пожалуйста: цвет"


async def test_handoff_intent_sends_manager_message_and_skips_search(
    monkeypatch: object,
) -> None:
    extractor = _FakeExtractor(request=_request(intent="handoff"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, _, search, client = await _run(monkeypatch, extractor=extractor, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == module.HANDOFF_TEXT


async def test_unknown_intent_sends_guidance_and_skips_search(
    monkeypatch: object,
) -> None:
    extractor = _FakeExtractor(request=_request(intent="unknown"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, _, search, client = await _run(monkeypatch, extractor=extractor, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == module.UNKNOWN_TEXT


async def test_extractor_failure_follows_retry_path(monkeypatch: object) -> None:
    extractor = _FakeExtractor(error=CustomerRequestExtractionError("boom"))
    client = _FakeWhatsAppClient()
    result, lifecycle, _, _, client = await _run(
        monkeypatch, extractor=extractor, client=client
    )
    assert result.outcome == module.EchoOutcome.RETRY_SCHEDULED
    assert lifecycle.failed == [(_JOB_ID, "Customer request extraction failed.")]
    assert lifecycle.completed == []
    assert client.calls == []


async def test_search_failure_follows_retry_path(monkeypatch: object) -> None:
    search = _FakeSearch(error=ProductSearchExecutionError("boom"))
    client = _FakeWhatsAppClient()
    result, lifecycle, _, _, client = await _run(monkeypatch, search=search, client=client)
    assert result.outcome == module.EchoOutcome.RETRY_SCHEDULED
    assert lifecycle.failed == [(_JOB_ID, "Product search failed.")]
    assert lifecycle.completed == []
    assert client.calls == []


async def test_whatsapp_failure_follows_retry_path(monkeypatch: object) -> None:
    client = _FakeWhatsAppClient(error=WhatsAppClientError("boom"))
    result, lifecycle, _, _, client = await _run(monkeypatch, client=client)
    assert result.outcome == module.EchoOutcome.RETRY_SCHEDULED
    assert lifecycle.failed == [(_JOB_ID, "WhatsApp send failed.")]
    assert lifecycle.completed == []

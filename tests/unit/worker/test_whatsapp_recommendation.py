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
from talap.recommendations import ActiveRecommendation
from talap.search.products import ProductSearchExecutionError, ProductSearchResult

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_JOB_ID = UUID("00000000-0000-0000-0000-0000000000aa")
_MESSAGE_ID = UUID("00000000-0000-0000-0000-0000000000bb")
_STATE_ID = UUID("00000000-0000-0000-0000-0000000000cc")
_PRODUCT_ID = UUID("22222222-2222-2222-2222-222222222222")
_LINK = "https://wa.me/77000000000"


class _FakeMessage:
    def __init__(
        self,
        *,
        channel: str = "whatsapp",
        message_type: str = "text",
        text: str = "синие кроссовки",
        external_user_id: str = "77000000001",
    ) -> None:
        self.id = _MESSAGE_ID
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


class _Persistence:
    def __init__(self, active: ActiveRecommendation | None = None) -> None:
        self.active = active
        self.stored: list[tuple[str, str, list[dict[str, object]]]] = []
        self.persisted: list[tuple[str, str, object, CustomerRequest]] = []
        self.selected: list[tuple[UUID, int, UUID | None]] = []

    async def store(
        self,
        *,
        session_factory: object,
        channel: str,
        external_user_id: str,
        displayed_products: list[dict[str, object]],
        source_message_id: object,
        now: datetime | None = None,
    ) -> UUID:
        self.stored.append((channel, external_user_id, displayed_products))
        return _STATE_ID

    async def persist(
        self,
        *,
        session_factory: object,
        channel: str,
        external_user_id: str,
        source_message_id: object,
        request: CustomerRequest,
    ) -> bool:
        self.persisted.append((channel, external_user_id, source_message_id, request))
        return True

    async def load(
        self,
        *,
        session_factory: object,
        channel: str,
        external_user_id: str,
    ) -> ActiveRecommendation | None:
        return self.active

    async def mark(
        self,
        *,
        session_factory: object,
        state_id: UUID,
        selected_index: int,
        selected_product_id: UUID | None,
        now: datetime | None = None,
    ) -> None:
        self.selected.append((state_id, selected_index, selected_product_id))


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


def _displayed(name: str, price: int) -> dict[str, object]:
    return {"product_id": str(_PRODUCT_ID), "name": name, "price_kzt": price}


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
    manager_link: str | None = _LINK,
    active: ActiveRecommendation | None = None,
) -> tuple[
    object,
    _Lifecycle,
    _FakeExtractor,
    _FakeSearch,
    _FakeWhatsAppClient,
    _Persistence,
]:
    lifecycle = _Lifecycle(fail_status=fail_status)
    persistence = _Persistence(active=active)

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
    monkeypatch.setattr(module, "store_recommendation_set", persistence.store)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "persist_unmet_demand", persistence.persist)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "load_active_recommendation", persistence.load)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "mark_recommendation_selected", persistence.mark)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "manager_whatsapp_link", lambda: manager_link)  # type: ignore[attr-defined]

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
    return result, lifecycle, extractor, search, client, persistence


# ── Existing recommendation flow ────────────────────────────────────────


async def test_complete_product_request_calls_extractor_once(
    monkeypatch: object,
) -> None:
    result, _, extractor, _, client, _ = await _run(monkeypatch)
    assert result.outcome == module.EchoOutcome.SENT
    assert extractor.calls == ["синие кроссовки"]
    assert len(client.calls) == 1


async def test_product_request_calls_search_once_with_limit_three(
    monkeypatch: object,
) -> None:
    result, _, extractor, search, _, _ = await _run(monkeypatch)
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == [(extractor.request, 3)]


async def test_three_products_formatted_and_stored_in_order(
    monkeypatch: object,
) -> None:
    results = (
        _result("Кроссовки", 1000),
        _result("Ботинки", 2000),
        _result("Футболка", 3000),
    )
    search = _FakeSearch(results=results)
    result, _, _, _, client, persistence = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert text == module.format_recommendations(results)
    assert "1. Кроссовки — 1000 ₸" in text
    assert "3. Футболка — 3000 ₸" in text
    assert len(persistence.stored) == 1
    assert len(persistence.stored[0][2]) == 3


async def test_fewer_than_three_results_format_and_store(
    monkeypatch: object,
) -> None:
    results = (_result("Кроссовки", 1000), _result("Ботинки", 2000))
    search = _FakeSearch(results=results)
    result, _, _, _, client, persistence = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert text == module.format_recommendations(results)
    assert "\n3. " not in text
    assert len(persistence.stored[0][2]) == 2


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
    result, _, _, _, client, _ = await _run(monkeypatch, search=search)
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert "SKU-SECRET" not in text
    assert "0.999" not in text
    assert "aaaaaaaa" not in text
    assert "Cotton" not in text


async def test_no_results_persists_unmet_demand_and_sends_response(
    monkeypatch: object,
) -> None:
    search = _FakeSearch(results=())
    extractor = _FakeExtractor(request=_request(language="ru"))
    result, _, _, _, client, persistence = await _run(
        monkeypatch, search=search, extractor=extractor
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert client.calls[0][1] == module.unmet_demand_response("ru")
    assert len(persistence.persisted) == 1
    assert persistence.persisted[0][3] is extractor.request


async def test_missing_field_sends_clarification_and_skips_search(
    monkeypatch: object,
) -> None:
    extractor = _FakeExtractor(request=_request(missing_field="цвет"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, _, search, client, persistence = await _run(
        monkeypatch, extractor=extractor, search=search
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == "Уточните, пожалуйста: цвет"
    assert persistence.stored == []
    assert persistence.persisted == []


async def test_handoff_intent_sends_manager_message_and_skips_search(
    monkeypatch: object,
) -> None:
    extractor = _FakeExtractor(request=_request(intent="handoff"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, _, search, client, _ = await _run(
        monkeypatch, extractor=extractor, search=search
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == module.HANDOFF_TEXT


async def test_unknown_intent_sends_guidance_and_skips_search(
    monkeypatch: object,
) -> None:
    extractor = _FakeExtractor(request=_request(intent="unknown"))
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, _, search, client, _ = await _run(
        monkeypatch, extractor=extractor, search=search
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert search.calls == []
    assert client.calls[0][1] == module.UNKNOWN_TEXT


async def test_extractor_failure_follows_retry_path(monkeypatch: object) -> None:
    extractor = _FakeExtractor(error=CustomerRequestExtractionError("boom"))
    client = _FakeWhatsAppClient()
    result, lifecycle, _, _, client, _ = await _run(
        monkeypatch, extractor=extractor, client=client
    )
    assert result.outcome == module.EchoOutcome.RETRY_SCHEDULED
    assert lifecycle.failed == [(_JOB_ID, "Customer request extraction failed.")]
    assert lifecycle.completed == []
    assert client.calls == []


async def test_search_failure_follows_retry_path(monkeypatch: object) -> None:
    search = _FakeSearch(error=ProductSearchExecutionError("boom"))
    client = _FakeWhatsAppClient()
    result, lifecycle, _, _, client, _ = await _run(
        monkeypatch, search=search, client=client
    )
    assert result.outcome == module.EchoOutcome.RETRY_SCHEDULED
    assert lifecycle.failed == [(_JOB_ID, "Product search failed.")]
    assert lifecycle.completed == []
    assert client.calls == []


async def test_whatsapp_failure_follows_retry_path(monkeypatch: object) -> None:
    client = _FakeWhatsAppClient(error=WhatsAppClientError("boom"))
    result, lifecycle, _, _, client, _ = await _run(monkeypatch, client=client)
    assert result.outcome == module.EchoOutcome.RETRY_SCHEDULED
    assert lifecycle.failed == [(_JOB_ID, "WhatsApp send failed.")]
    assert lifecycle.completed == []


# ── MVP-6 selection flow ───────────────────────────────────────────────


def _active_two() -> ActiveRecommendation:
    return ActiveRecommendation(
        state_id=_STATE_ID,
        displayed_products=(_displayed("Кроссовки", 1000), _displayed("Ботинки", 2000)),
    )


async def test_selection_one_resolves_first_product_and_skips_extractor_search(
    monkeypatch: object,
) -> None:
    message = _FakeMessage(text="1")
    extractor = _FakeExtractor()
    search = _FakeSearch(results=(_result("Кроссовки", 1000),))
    result, _, extractor, search, client, persistence = await _run(
        monkeypatch,
        message=message,
        extractor=extractor,
        search=search,
        active=_active_two(),
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert client.calls[0][1] == (
        "Вы выбрали: Кроссовки — 1000 ₸.\n\nНапишите менеджеру:\n" + _LINK
    )
    assert extractor.calls == []
    assert search.calls == []
    assert persistence.selected == [(_STATE_ID, 1, _PRODUCT_ID)]


async def test_selection_two_resolves_second_product(
    monkeypatch: object,
) -> None:
    message = _FakeMessage(text="2")
    result, _, extractor, search, client, persistence = await _run(
        monkeypatch, message=message, active=_active_two()
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert "Вы выбрали: Ботинки — 2000 ₸." in client.calls[0][1]
    assert extractor.calls == []
    assert search.calls == []
    assert persistence.selected == [(_STATE_ID, 2, _PRODUCT_ID)]


async def test_out_of_range_selection_asks_valid_range(
    monkeypatch: object,
) -> None:
    message = _FakeMessage(text="3")
    result, _, extractor, search, client, persistence = await _run(
        monkeypatch, message=message, active=_active_two()
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert client.calls[0][1] == "Выберите, пожалуйста, номер от 1 до 2."
    assert extractor.calls == []
    assert search.calls == []
    assert persistence.selected == []


async def test_selection_without_active_state_preserves_normal_processing(
    monkeypatch: object,
) -> None:
    message = _FakeMessage(text="1")
    extractor = _FakeExtractor(request=_request(query_text="1"))
    result, _, extractor, search, _, persistence = await _run(
        monkeypatch, message=message, extractor=extractor, active=None
    )
    assert result.outcome == module.EchoOutcome.SENT
    assert extractor.calls == ["1"]
    assert search.calls == [(extractor.request, 3)]
    assert persistence.selected == []


async def test_missing_manager_link_fails_safely(monkeypatch: object) -> None:
    message = _FakeMessage(text="1")
    result, _, _, _, client, persistence = await _run(
        monkeypatch,
        message=message,
        active=_active_two(),
        manager_link=None,
    )
    assert result.outcome == module.EchoOutcome.SENT
    text = client.calls[0][1]
    assert "Вы выбрали: Кроссовки — 1000 ₸." in text
    assert "Напишите менеджеру:" not in text
    assert persistence.selected == [(_STATE_ID, 1, _PRODUCT_ID)]


def test_format_selection_response_with_and_without_link() -> None:
    displayed = _displayed("Кроссовки", 1000)
    with_link = module.format_selection_response(displayed, _LINK)
    assert "Вы выбрали: Кроссовки — 1000 ₸." in with_link
    assert "Напишите менеджеру:" in with_link
    assert _LINK in with_link
    without_link = module.format_selection_response(displayed, None)
    assert "Вы выбрали: Кроссовки — 1000 ₸." in without_link
    assert "Напишите менеджеру:" not in without_link

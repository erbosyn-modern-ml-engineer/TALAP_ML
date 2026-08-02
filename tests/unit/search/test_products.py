from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from talap.ai.customer_request import CustomerRequest
from talap.embeddings.jina import JinaEmbeddingError
from talap.embeddings.types import EmbeddingResult
from talap.search.products import (
    ProductSearchExecutionError,
    ProductSearchResult,
    ProductSearchValidationError,
    build_query_text,
    search_products,
)

_PRODUCT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _request(**overrides: object) -> CustomerRequest:
    base: dict[str, object] = {
        "intent": "product_search",
        "language": "ru",
        "query_text": "синие кроссовки",
        "category": "sneakers",
        "attributes": {"color": "blue"},
        "budget_max_kzt": None,
        "quantity": None,
        "missing_field": None,
    }
    base.update(overrides)
    return CustomerRequest(**base)


class _FakeEmbeddingClient:
    provider = "jina"
    model = "jina-embeddings-v5-text-small"
    dimensions = 1024

    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure
        self.calls = 0

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return EmbeddingResult(
            vector=tuple(0.0 for _ in range(self.dimensions)),
            provider=self.provider,
            model=self.model,
            dimensions=self.dimensions,
        )

    async def aclose(self) -> None:
        pass


def test_query_text_construction() -> None:
    request = _request(
        query_text="кроссовки",
        category="sneakers",
        attributes={"color": "blue", "size": "42"},
    )
    assert (
        build_query_text(request)
        == "кроссовки category: sneakers color: blue size: 42"
    )


def test_attribute_ordering_is_deterministic() -> None:
    request = _request(
        query_text="куртка",
        category=None,
        attributes={"z": "последний", "a": "первый", "m": "средний"},
    )
    assert build_query_text(request) == "куртка a: первый m: средний z: последний"


async def test_non_product_search_intent_returns_empty_without_http_or_db() -> None:
    request = _request(intent="handoff")
    result = await search_products(
        request=request,
        limit=3,
        session_factory=None,
        embedding_client=None,
    )
    assert result == ()


@pytest.mark.asyncio
async def test_invalid_limit_rejected_before_any_io() -> None:
    request = _request()
    for bad_limit in (0, 11):
        with pytest.raises(ProductSearchValidationError):
            await search_products(
                request=request,
                limit=bad_limit,
                session_factory=None,
                embedding_client=None,
            )


@pytest.mark.asyncio
async def test_embedding_failure_raises_safe_execution_error() -> None:
    fake = _FakeEmbeddingClient(failure=JinaEmbeddingError("boom"))
    with pytest.raises(ProductSearchExecutionError) as excinfo:
        await search_products(
            request=_request(query_text="УНИКАЛЬНЫЙТЕКСТПОКУПАТЕЛЯ"),
            limit=3,
            session_factory=None,
            embedding_client=fake,  # type: ignore[arg-type]
        )
    assert fake.calls == 1
    assert "УНИКАЛЬНЫЙТЕКСТПОКУПАТЕЛЯ" not in str(excinfo.value)
    assert "SECRET" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_api_key_and_customer_text_absent_from_errors() -> None:
    fake = _FakeEmbeddingClient(
        failure=JinaEmbeddingError("SECRET-KEY leaked customer text")
    )
    with pytest.raises(ProductSearchExecutionError) as excinfo:
        await search_products(
            request=_request(query_text="ТЕКСТПОКУПАТЕЛЯ"),
            limit=3,
            session_factory=None,
            embedding_client=fake,  # type: ignore[arg-type]
        )
    message = str(excinfo.value)
    assert "SECRET-KEY" not in message
    assert "ТЕКСТПОКУПАТЕЛЯ" not in message
    assert message == "Product search embedding failed."


def test_result_contract_validation() -> None:
    result = ProductSearchResult(
        product_id=_PRODUCT_ID,
        name="Кроссовки",
        category="sneakers",
        description="Описание",
        price_kzt=30000,
        available_quantity=5,
        merchant_sku="SKU-1",
        material="Cotton",
        similarity=0.85,
    )
    assert result.similarity == 0.85
    assert result.available_quantity == 5

    negative = ProductSearchResult(
        product_id=_PRODUCT_ID,
        name="Кроссовки",
        category="sneakers",
        price_kzt=30000,
        available_quantity=5,
        merchant_sku="SKU-1",
        similarity=-0.3,
    )
    assert negative.similarity == -0.3

    with pytest.raises(ValidationError):
        ProductSearchResult(
            product_id=_PRODUCT_ID,
            name="Кроссовки",
            category="sneakers",
            price_kzt=30000,
            available_quantity=5,
            merchant_sku="SKU-1",
            similarity=1.5,
        )
    with pytest.raises(ValidationError):
        ProductSearchResult(
            product_id=_PRODUCT_ID,
            name="Кроссовки",
            category="sneakers",
            price_kzt=30000,
            available_quantity=5,
            merchant_sku="SKU-1",
            similarity=-1.5,
        )
    with pytest.raises(ValidationError):
        ProductSearchResult(
            product_id=_PRODUCT_ID,
            name="Кроссовки",
            category="sneakers",
            price_kzt=30000,
            available_quantity=5,
            merchant_sku="SKU-1",
            similarity=0.5,
            unexpected_field=True,
        )

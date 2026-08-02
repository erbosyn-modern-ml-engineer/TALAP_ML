from __future__ import annotations

import json

import httpx
import pytest
import respx

from talap.embeddings.jina import (
    JinaEmbeddingClient,
    JinaEmbeddingConfigurationError,
    JinaEmbeddingDimensionError,
    JinaEmbeddingError,
)

_BASE_URL = "https://api.jina.ai/v1"
_ENDPOINT = f"{_BASE_URL}/embeddings"


def _vector(dimension: int = 1024) -> list[float]:
    return [0.01 * (index % 100) for index in range(dimension)]


def _embedding_payload(vector: list[float]) -> dict[str, object]:
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "model": "jina-embeddings-v5-text-small",
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }


async def _no_sleep(_seconds: float) -> None:
    return None


def _client(max_retries: int = 2) -> JinaEmbeddingClient:
    return JinaEmbeddingClient(
        api_key="test-key",
        base_url=_BASE_URL,
        model="jina-embeddings-v5-text-small",
        dimensions=1024,
        timeout_seconds=5.0,
        max_retries=max_retries,
        sleep=_no_sleep,
    )


@respx.mock
async def test_valid_1024_vector_contract() -> None:
    client = _client()
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_embedding_payload(_vector()))
    )

    result = await client.embed_document(
        "Name: Shirt\nCategory: School\nDescription: Cotton shirt\nMaterial: Cotton"
    )

    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "jina-embeddings-v5-text-small"
    assert body["input"] == [
        "Name: Shirt\nCategory: School\nDescription: Cotton shirt\nMaterial: Cotton"
    ]
    assert body["task"] == "retrieval.passage"
    assert body["dimensions"] == 1024
    assert result.provider == "jina"
    assert result.model == "jina-embeddings-v5-text-small"
    assert result.dimensions == 1024
    assert len(result.vector) == 1024
    assert result.vector == tuple(_vector())
    await client.aclose()


@respx.mock
async def test_embed_query_uses_retrieval_query_task() -> None:
    client = _client()
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_embedding_payload(_vector()))
    )

    result = await client.embed_query("синие кроссовки")

    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == ["синие кроссовки"]
    assert body["task"] == "retrieval.query"
    assert body["model"] == "jina-embeddings-v5-text-small"
    assert body["dimensions"] == 1024
    assert "test-key" not in json.dumps(body)
    assert result.provider == "jina"
    assert result.model == "jina-embeddings-v5-text-small"
    assert result.dimensions == 1024
    assert len(result.vector) == 1024
    assert result.vector == tuple(_vector())
    await client.aclose()


@respx.mock
async def test_wrong_dimension_rejected_and_not_retried() -> None:
    client = _client(max_retries=2)
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_embedding_payload(_vector(512)))
    )

    with pytest.raises(JinaEmbeddingDimensionError):
        await client.embed_document("text")

    assert len(route.calls) == 1
    await client.aclose()


@respx.mock
async def test_multiple_embeddings_rejected() -> None:
    client = _client()
    payload = {
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": _vector()},
            {"object": "embedding", "index": 1, "embedding": _vector()},
        ],
    }
    route = respx.post(_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(JinaEmbeddingError):
        await client.embed_document("text")

    assert len(route.calls) == 1
    await client.aclose()


@respx.mock
async def test_non_finite_value_rejected() -> None:
    client = _client()
    # NaN is not JSON-compliant for httpx.Response(json=...), so inject it as
    # raw JSON text; the client's json.loads still parses NaN to float('nan').
    raw = (
        '{"object":"list","data":[{"object":"embedding","index":0,'
        '"embedding":[0.0, NaN, 0.0]}]}'
    )
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(200, content=raw)
    )

    with pytest.raises(JinaEmbeddingError):
        await client.embed_document("text")

    assert len(route.calls) == 1
    await client.aclose()


@respx.mock
async def test_401_not_retried() -> None:
    client = _client(max_retries=2)
    route = respx.post(_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )

    with pytest.raises(JinaEmbeddingError):
        await client.embed_document("text")

    assert len(route.calls) == 1
    await client.aclose()


@respx.mock
async def test_429_retried_then_succeeds() -> None:
    client = _client(max_retries=2)
    route = respx.post(_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}, json={}),
            httpx.Response(200, json=_embedding_payload(_vector())),
        ]
    )

    result = await client.embed_document("text")

    assert len(route.calls) == 2
    assert len(result.vector) == 1024
    await client.aclose()


@respx.mock
async def test_500_retried_then_succeeds() -> None:
    client = _client(max_retries=2)
    route = respx.post(_ENDPOINT).mock(
        side_effect=[
            httpx.Response(500, json={}),
            httpx.Response(200, json=_embedding_payload(_vector())),
        ]
    )

    result = await client.embed_document("text")

    assert len(route.calls) == 2
    assert len(result.vector) == 1024
    await client.aclose()


@respx.mock
async def test_timeout_retried_then_succeeds() -> None:
    client = _client(max_retries=2)
    route = respx.post(_ENDPOINT).mock(
        side_effect=[
            httpx.TimeoutException("request timed out"),
            httpx.Response(200, json=_embedding_payload(_vector())),
        ]
    )

    result = await client.embed_document("text")

    assert len(route.calls) == 2
    assert len(result.vector) == 1024
    await client.aclose()


@respx.mock
async def test_max_request_attempts_respected() -> None:
    # max_retries=2 means 2 retries AFTER the first request: 3 total attempts.
    client = _client(max_retries=2)
    route = respx.post(_ENDPOINT).mock(return_value=httpx.Response(500, json={}))

    with pytest.raises(JinaEmbeddingError):
        await client.embed_document("text")

    assert len(route.calls) == 3
    await client.aclose()


def test_api_key_absent_raises_configuration_error() -> None:
    with pytest.raises(JinaEmbeddingConfigurationError):
        JinaEmbeddingClient(api_key=None)
    with pytest.raises(JinaEmbeddingConfigurationError):
        JinaEmbeddingClient(api_key="   ")


@respx.mock
async def test_errors_and_repr_never_contain_key() -> None:
    client = JinaEmbeddingClient(
        api_key="super-secret-key",
        base_url=_BASE_URL,
        model="jina-embeddings-v5-text-small",
        dimensions=1024,
        timeout_seconds=5.0,
        max_retries=0,
        sleep=_no_sleep,
    )
    respx.post(_ENDPOINT).mock(return_value=httpx.Response(401, json={}))

    with pytest.raises(JinaEmbeddingError) as excinfo:
        await client.embed_document("text")

    assert "super-secret-key" not in str(excinfo.value)
    assert "super-secret-key" not in repr(client)
    await client.aclose()

from __future__ import annotations

import json

import httpx
import pytest
import respx

from talap.evaluation.embeddings.adapters import (
    UnknownEmbeddingProviderError,
    build_provider,
)
from talap.evaluation.embeddings.adapters.jina import (
    DEFAULT_JINA_BASE_URL,
    JinaEmbeddingProvider,
)
from talap.evaluation.embeddings.adapters.openai_compatible import (
    DEFAULT_OPENAI_BASE_URL,
    OpenAICompatibleEmbeddingProvider,
)
from talap.evaluation.embeddings.interface import (
    TASK_DOCUMENT,
    TASK_QUERY,
    EmbeddingDimensionMismatchError,
    EmbeddingProviderError,
)
from talap.evaluation.embeddings.parsing import parse_openai_embedding_response


def _embedding_payload(vectors: list[list[float]], *, tokens: int = 6) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "model": "test-model",
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


@respx.mock
def test_openai_request_body_and_response_parsing() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=DEFAULT_OPENAI_BASE_URL,
        api_key="test-key",
        model="text-embedding-3-small",
    )
    route = respx.post(f"{DEFAULT_OPENAI_BASE_URL}/embeddings").mock(
        return_value=httpx.Response(
            200,
            json=_embedding_payload([[0.1, 0.2], [0.3, 0.4]]),
        )
    )

    result = provider.embed(texts=["first", "second"], task=TASK_DOCUMENT)

    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "text-embedding-3-small"
    assert body["input"] == ["first", "second"]
    assert body["encoding_format"] == "float"
    assert "task" not in body
    assert result.total_tokens == 6
    assert [item.vector for item in result.embeddings] == [(0.1, 0.2), (0.3, 0.4)]
    provider.close()


@respx.mock
def test_openai_forwards_configured_task_and_dimensions() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=DEFAULT_OPENAI_BASE_URL,
        api_key="k",
        model="m",
        dimensions=3,
        task="retrieval.passage",
    )
    route = respx.post(f"{DEFAULT_OPENAI_BASE_URL}/embeddings").mock(
        return_value=httpx.Response(200, json=_embedding_payload([[1.0, 0.0, 0.0]]))
    )

    provider.embed(texts=["doc"], task=TASK_DOCUMENT)

    body = json.loads(route.calls[0].request.content)
    assert body["dimensions"] == 3
    assert body["task"] == "retrieval.passage"
    provider.close()


@respx.mock
def test_jina_forwards_per_call_retrieval_task() -> None:
    provider = JinaEmbeddingProvider(api_key="k", model="jina-embeddings-v3")
    route = respx.post(f"{DEFAULT_JINA_BASE_URL}/embeddings").mock(
        return_value=httpx.Response(200, json=_embedding_payload([[0.1, 0.2]]))
    )

    provider.embed(texts=["doc"], task=TASK_DOCUMENT)
    assert json.loads(route.calls[0].request.content)["task"] == "retrieval.passage"

    provider.embed(texts=["query"], task=TASK_QUERY)
    assert json.loads(route.calls[1].request.content)["task"] == "retrieval.query"
    provider.close()


@respx.mock
def test_dimension_mismatch_within_batch_detected() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=DEFAULT_OPENAI_BASE_URL,
        api_key="k",
        model="m",
    )
    respx.post(f"{DEFAULT_OPENAI_BASE_URL}/embeddings").mock(
        return_value=httpx.Response(
            200,
            json=_embedding_payload([[0.1, 0.2], [0.3, 0.4, 0.5]]),
        )
    )

    with pytest.raises(EmbeddingDimensionMismatchError):
        provider.embed(texts=["a", "b"], task=TASK_DOCUMENT)
    provider.close()


def test_parser_detects_expected_dimension_mismatch() -> None:
    with pytest.raises(EmbeddingDimensionMismatchError):
        parse_openai_embedding_response(_embedding_payload([[0.1, 0.2]]), expected_dimension=3)


@respx.mock
def test_http_error_raised_without_exposing_key() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=DEFAULT_OPENAI_BASE_URL,
        api_key="super-secret-value",
        model="m",
    )
    respx.post(f"{DEFAULT_OPENAI_BASE_URL}/embeddings").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )

    with pytest.raises(EmbeddingProviderError) as excinfo:
        provider.embed(texts=["a"], task=TASK_DOCUMENT)
    assert "super-secret-value" not in str(excinfo.value)
    provider.close()


def test_build_provider_skips_without_api_key() -> None:
    assert build_provider("openai", api_key=None, model="m") is None
    assert build_provider("openai", api_key="   ", model="m") is None
    assert build_provider("jina", api_key=None, model="m") is None


def test_build_provider_builds_with_api_key() -> None:
    provider = build_provider("openai", api_key="k", model="m", base_url=DEFAULT_OPENAI_BASE_URL)
    assert provider is not None
    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
    assert provider.name == "openai"


def test_build_provider_unknown_name_raises() -> None:
    with pytest.raises(UnknownEmbeddingProviderError):
        build_provider("deepseek", api_key="k", model="m")

"""Parsing of OpenAI-compatible ``/embeddings`` JSON responses (OpenAI, Jina)."""

from __future__ import annotations

from collections.abc import Mapping

from talap.evaluation.embeddings.interface import (
    EmbeddingDimensionMismatchError,
    EmbeddingProviderError,
    EmbeddingResult,
)


def parse_openai_embedding_response(
    payload: object,
    *,
    expected_dimension: int | None = None,
) -> tuple[tuple[EmbeddingResult, ...], int | None]:
    """Parse an OpenAI-compatible embeddings response.

    Returns ``(results, total_tokens)`` with results ordered by the ``index``
    field. Raises ``EmbeddingDimensionMismatchError`` when vectors do not all
    share one dimension or differ from ``expected_dimension``. Error messages
    never include raw payload content or secrets.
    """
    if not isinstance(payload, Mapping):
        raise EmbeddingProviderError("Provider returned a non-object response.")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise EmbeddingProviderError("Provider returned no embedding data.")

    indexed: list[tuple[int, EmbeddingResult]] = []
    dimension: int | None = None
    for item in data:
        if not isinstance(item, Mapping):
            raise EmbeddingProviderError("Provider returned a malformed embedding item.")
        index = item.get("index", 0)
        if not isinstance(index, int):
            raise EmbeddingProviderError("Provider returned a malformed embedding index.")
        vector = item.get("embedding")
        if not isinstance(vector, (list, tuple)) or not vector:
            raise EmbeddingProviderError("Provider returned an empty embedding vector.")
        try:
            values = tuple(float(value) for value in vector)
        except (TypeError, ValueError):
            raise EmbeddingProviderError(
                "Provider returned a non-numeric embedding vector."
            ) from None
        if dimension is None:
            dimension = len(values)
        elif len(values) != dimension:
            raise EmbeddingDimensionMismatchError(
                f"Provider returned mixed embedding dimensions: {dimension} and {len(values)}."
            )
        indexed.append((index, EmbeddingResult(vector=values)))

    if expected_dimension is not None and dimension != expected_dimension:
        raise EmbeddingDimensionMismatchError(
            f"Embedding dimension {dimension} does not match expected {expected_dimension}."
        )

    indexed.sort(key=lambda pair: pair[0])
    results = tuple(result for _, result in indexed)

    total_tokens: int | None = None
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        raw = usage.get("total_tokens", usage.get("prompt_tokens"))
        if isinstance(raw, int):
            total_tokens = raw
    return results, total_tokens

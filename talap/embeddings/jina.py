"""Production Jina embedding client (independent from evaluation code).

Implements only the document embedding path (``retrieval.passage``) with the
fixed MVP contract: provider ``jina``, model ``jina-embeddings-v5-text-small``,
dimensions ``1024``. No model auto-selection and no silent fallback.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from talap.embeddings.types import (
    PROVIDER_JINA,
    TASK_DOCUMENT,
    EmbeddingResult,
)

__all__ = [
    "JinaEmbeddingClient",
    "JinaEmbeddingConfigurationError",
    "JinaEmbeddingDimensionError",
    "JinaEmbeddingError",
]

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 30.0
_DEFAULT_RETRY_DELAY_SECONDS = 0.5


class JinaEmbeddingError(Exception):
    """A Jina embedding request failed; the message never contains the API key."""


class JinaEmbeddingConfigurationError(JinaEmbeddingError):
    """Raised when the Jina client is not configured (API key absent)."""


class JinaEmbeddingDimensionError(JinaEmbeddingError):
    """Raised when the provider returns a vector with the wrong dimension."""


class _RetryableJinaError(Exception):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class JinaEmbeddingClient:
    """Async client for the Jina ``/embeddings`` endpoint.

    ``max_retries`` is the number of retries AFTER the first request, so the
    total number of request attempts is ``max_retries + 1``. Only transient
    failures are retried: HTTP 408/429/500/502/503/504, HTTPX timeouts, and
    temporary transport errors. HTTP 400/401/403, invalid response schemas,
    dimension mismatches, and non-finite vectors are never retried.
    """

    provider: str = PROVIDER_JINA

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.jina.ai/v1",
        model: str = "jina-embeddings-v5-text-small",
        dimensions: int = 1024,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if api_key is None or api_key.strip() == "":
            raise JinaEmbeddingConfigurationError(
                "Jina embedding client requires an API key."
            )
        if dimensions < 1:
            raise ValueError("dimensions must be a positive integer.")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self._timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._http_client: httpx.AsyncClient | None = None

    async def embed_document(self, text: str) -> EmbeddingResult:
        """Embed one canonical product document (task=``retrieval.passage``)."""
        if text.strip() == "":
            raise JinaEmbeddingError("Cannot embed an empty document.")
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [text],
            "task": TASK_DOCUMENT,
            "dimensions": self.dimensions,
        }
        last_error: _RetryableJinaError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._embed_once(payload)
            except _RetryableJinaError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await self._sleep(min(exc.retry_after, _MAX_RETRY_AFTER_SECONDS))
                    continue
                break
        assert last_error is not None
        raise JinaEmbeddingError(str(last_error)) from None

    async def _embed_once(self, payload: dict[str, Any]) -> EmbeddingResult:
        try:
            response = await self._http().post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException:
            raise _RetryableJinaError(
                "Jina embedding request timed out.",
                _DEFAULT_RETRY_DELAY_SECONDS,
            ) from None
        except httpx.TransportError:
            raise _RetryableJinaError(
                "Jina embedding request failed due to a transport error.",
                _DEFAULT_RETRY_DELAY_SECONDS,
            ) from None

        if response.status_code in _RETRYABLE_STATUS_CODES:
            raise _RetryableJinaError(
                f"Jina embedding request failed with HTTP {response.status_code}.",
                _retry_after_seconds(response),
            )
        if response.status_code >= 400:
            raise JinaEmbeddingError(
                f"Jina embedding request failed with HTTP {response.status_code}."
            )
        try:
            response_payload = response.json()
        except ValueError:
            raise JinaEmbeddingError(
                "Jina embedding response is not valid JSON."
            ) from None
        return _parse_embedding_response(
            response_payload,
            model=self.model,
            expected_dimension=self.dimensions,
        )

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _DEFAULT_RETRY_DELAY_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        return _DEFAULT_RETRY_DELAY_SECONDS
    if seconds < 0:
        return _DEFAULT_RETRY_DELAY_SECONDS
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _finite_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JinaEmbeddingError(
            "Jina embedding vector contains a non-numeric value."
        )
    result = float(value)
    if not math.isfinite(result):
        raise JinaEmbeddingError("Jina embedding vector contains a non-finite value.")
    return result


def _parse_embedding_response(
    payload: object,
    *,
    model: str,
    expected_dimension: int,
) -> EmbeddingResult:
    if not isinstance(payload, dict):
        raise JinaEmbeddingError("Jina embedding response is not an object.")
    data = payload.get("data")
    if not isinstance(data, list):
        raise JinaEmbeddingError("Jina embedding response has no data list.")
    if len(data) != 1:
        raise JinaEmbeddingError(
            f"Jina embedding response must contain exactly one embedding, got {len(data)}."
        )
    item = data[0]
    if not isinstance(item, dict):
        raise JinaEmbeddingError("Jina embedding response item is malformed.")
    index = item.get("index", 0)
    if index != 0:
        raise JinaEmbeddingError(
            f"Jina embedding response index is unexpected: {index!r}."
        )
    vector = item.get("embedding")
    if not isinstance(vector, (list, tuple)) or not vector:
        raise JinaEmbeddingError("Jina embedding response vector is missing.")
    values = tuple(_finite_float(value) for value in vector)
    if len(values) != expected_dimension:
        raise JinaEmbeddingDimensionError(
            f"Jina embedding dimension {len(values)} does not match expected "
            f"{expected_dimension}."
        )
    return EmbeddingResult(
        vector=values,
        provider=PROVIDER_JINA,
        model=model,
        dimensions=expected_dimension,
    )

"""OpenAI-compatible ``/embeddings`` adapter."""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx

from talap.evaluation.embeddings.interface import (
    EmbedBatchResult,
    EmbeddingProviderError,
)
from talap.evaluation.embeddings.parsing import parse_openai_embedding_response

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleEmbeddingProvider:
    """Embeddings via any OpenAI-compatible ``POST {base_url}/embeddings`` API.

    All configuration (base URL, API key, model, optional dimensions, and an
    optional fixed ``task``) comes from the constructor; credentials are never
    hardcoded. When ``task`` is set it is forwarded in the request body for
    endpoints that require a task type; when unset (standard OpenAI) it is
    omitted.
    """

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int | None = None,
        task: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._task = task
        self._timeout_seconds = timeout_seconds
        self._http_client: httpx.Client | None = None

    def embed(self, *, texts: Sequence[str], task: str) -> EmbedBatchResult:
        """Embed a batch of texts and return the parsed batch result."""
        if not texts:
            raise EmbeddingProviderError("Cannot embed an empty text batch.")
        payload: dict[str, object] = {
            "model": self._model,
            "input": list(texts),
            "encoding_format": "float",
        }
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions
        task_value = self._task_for_request(task)
        if task_value is not None:
            payload["task"] = task_value

        started = time.perf_counter()
        response = self._http().post(
            f"{self._base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 400:
            raise EmbeddingProviderError(
                f"Embedding request failed with HTTP {response.status_code}."
            )
        try:
            response_payload = response.json()
        except ValueError:
            raise EmbeddingProviderError(
                f"Embedding request returned a non-JSON response (HTTP {response.status_code})."
            ) from None
        results, total_tokens = parse_openai_embedding_response(response_payload)
        return EmbedBatchResult(
            embeddings=results,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            request_count=1,
        )

    def _task_for_request(self, task: str) -> str | None:
        """Return the task to forward in the request body, if any."""
        return self._task

    def _http(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self._timeout_seconds)
        return self._http_client

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

"""Jina embeddings adapter (OpenAI-compatible endpoint at api.jina.ai)."""

from __future__ import annotations

from talap.evaluation.embeddings.adapters.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)

DEFAULT_JINA_BASE_URL = "https://api.jina.ai/v1"


class JinaEmbeddingProvider(OpenAICompatibleEmbeddingProvider):
    """Jina v3 embeddings via the OpenAI-compatible endpoint.

    Jina v3 supports per-request retrieval task types, so the per-call task is
    always forwarded (``retrieval.passage`` for documents,
    ``retrieval.query`` for queries).
    """

    name = "jina"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            base_url=DEFAULT_JINA_BASE_URL,
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            task=None,
            timeout_seconds=timeout_seconds,
        )

    def _task_for_request(self, task: str) -> str | None:
        return task

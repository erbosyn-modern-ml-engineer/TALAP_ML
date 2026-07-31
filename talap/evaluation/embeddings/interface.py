"""Provider-neutral embedding interface for the evaluation harness."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

TASK_DOCUMENT = "retrieval.passage"
TASK_QUERY = "retrieval.query"


class EmbeddingProviderError(Exception):
    """A provider request failed; the message never exposes the API key."""


class EmbeddingDimensionMismatchError(Exception):
    """Returned vectors do not share the expected embedding dimension."""


@dataclass(frozen=True)
class EmbeddingResult:
    vector: tuple[float, ...]


@dataclass(frozen=True)
class EmbedBatchResult:
    embeddings: tuple[EmbeddingResult, ...]
    total_tokens: int | None = None
    latency_ms: float = 0.0
    request_count: int = 1


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Provider-neutral embedding API used by the evaluation harness."""

    name: str

    def embed(self, *, texts: Sequence[str], task: str) -> EmbedBatchResult:
        """Embed a batch of texts for the given retrieval task.

        ``task`` is one of ``TASK_DOCUMENT`` / ``TASK_QUERY``.
        """
        ...

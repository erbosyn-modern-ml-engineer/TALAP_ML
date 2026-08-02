"""Shared embedding types for the production embedding pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

PROVIDER_JINA = "jina"
TASK_DOCUMENT = "retrieval.passage"
TASK_QUERY = "retrieval.query"


@dataclass(frozen=True)
class EmbeddingResult:
    """Immutable embedding outcome; never contains the API key."""

    vector: tuple[float, ...]
    provider: str
    model: str
    dimensions: int


@runtime_checkable
class EmbeddingClient(Protocol):
    """Embedding client used by the indexing processor (document task only)."""

    provider: str
    model: str
    dimensions: int

    async def embed_document(self, text: str) -> EmbeddingResult:
        """Embed one canonical product document for ``retrieval.passage``."""
        ...

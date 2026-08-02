"""Production embedding pipeline.

The Jina client here is independent from ``talap.evaluation``; production
runtime code never imports from the evaluation harness.
"""

from talap.embeddings.jina import (
    JinaEmbeddingClient,
    JinaEmbeddingConfigurationError,
    JinaEmbeddingDimensionError,
    JinaEmbeddingError,
)
from talap.embeddings.types import (
    PROVIDER_JINA,
    TASK_DOCUMENT,
    TASK_QUERY,
    EmbeddingClient,
    EmbeddingResult,
)

__all__ = [
    "PROVIDER_JINA",
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "EmbeddingClient",
    "EmbeddingResult",
    "JinaEmbeddingClient",
    "JinaEmbeddingConfigurationError",
    "JinaEmbeddingDimensionError",
    "JinaEmbeddingError",
]

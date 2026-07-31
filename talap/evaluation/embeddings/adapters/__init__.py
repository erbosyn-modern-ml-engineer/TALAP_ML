"""Provider registry for the embedding evaluation harness."""

from __future__ import annotations

from talap.evaluation.embeddings.adapters.jina import (
    DEFAULT_JINA_BASE_URL,
    JinaEmbeddingProvider,
)
from talap.evaluation.embeddings.adapters.openai_compatible import (
    DEFAULT_OPENAI_BASE_URL,
    OpenAICompatibleEmbeddingProvider,
)
from talap.evaluation.embeddings.interface import EmbeddingProvider

__all__ = [
    "DEFAULT_JINA_BASE_URL",
    "DEFAULT_OPENAI_BASE_URL",
    "JinaEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "UnknownEmbeddingProviderError",
    "available_providers",
    "build_provider",
]


class UnknownEmbeddingProviderError(Exception):
    """Raised for an unknown provider name."""


def available_providers() -> tuple[str, ...]:
    """Names of providers with an optional adapter."""
    return ("openai", "jina")


def build_provider(
    provider_name: str,
    *,
    api_key: str | None,
    model: str,
    base_url: str | None = None,
    dimensions: int | None = None,
    task: str | None = None,
    timeout_seconds: float = 30.0,
) -> EmbeddingProvider | None:
    """Build a provider, or return ``None`` when its API key is absent.

    A provider without an API key is skipped clearly; callers must report the
    skip without exposing the key value.
    """
    if api_key is None or api_key.strip() == "":
        return None
    if provider_name == "openai":
        return OpenAICompatibleEmbeddingProvider(
            base_url=base_url or DEFAULT_OPENAI_BASE_URL,
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            task=task,
            timeout_seconds=timeout_seconds,
        )
    if provider_name == "jina":
        return JinaEmbeddingProvider(
            api_key=api_key,
            model=model,
            dimensions=dimensions,
            timeout_seconds=timeout_seconds,
        )
    raise UnknownEmbeddingProviderError(f"Unknown embedding provider {provider_name!r}.")

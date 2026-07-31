"""Offline embedding-provider evaluation runner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from talap.evaluation.embeddings.adapters import build_provider
from talap.evaluation.embeddings.dataset import (
    LANGUAGES,
    SCENARIOS,
    EvalDataset,
    load_dataset_bytes,
)
from talap.evaluation.embeddings.interface import (
    TASK_DOCUMENT,
    TASK_QUERY,
    EmbeddingDimensionMismatchError,
    EmbeddingProvider,
    EmbeddingProviderError,
)
from talap.evaluation.embeddings.metrics import (
    QualityMetrics,
    QueryOutcome,
    RetrievalMetrics,
    compute_metrics,
)
from talap.evaluation.embeddings.retrieval import normalize_vector, rank_documents
from talap.indexing.documents import build_product_index_text


class ProviderSkippedError(Exception):
    """Raised when the requested provider has no API key and is skipped."""


@dataclass(frozen=True)
class EvaluationConfig:
    provider_name: str
    model: str
    api_key: str | None
    base_url: str | None = None
    dimensions: int | None = None
    task: str | None = None
    price_per_1m_tokens: float | None = None
    timeout_seconds: float = 30.0
    top_k: int = 5

    def without_secrets(self) -> dict[str, object]:
        """Configuration safe to persist (never includes the API key)."""
        return {
            "provider": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "dimensions": self.dimensions,
            "task": self.task,
            "price_per_1m_tokens": self.price_per_1m_tokens,
            "timeout_seconds": self.timeout_seconds,
            "top_k": self.top_k,
        }


@dataclass(frozen=True)
class EvaluationReport:
    provider: str
    model: str
    dataset: str
    dataset_sha256: str
    product_count: int
    query_count: int
    language_distribution: dict[str, int]
    scenario_distribution: dict[str, int]
    configuration_without_secrets: dict[str, object]
    generated_at: str
    metrics: RetrievalMetrics


def run_evaluation(
    *,
    config: EvaluationConfig,
    dataset_path: str | Path,
    output_path: str | Path,
) -> EvaluationReport:
    """Run the benchmark for one provider and write the JSON result report.

    The dataset SHA-256 is computed over the exact dataset bytes used for the
    run. Raises ``ProviderSkippedError`` when the provider has no API key. The
    written report never contains API keys or raw HTTP headers.
    """
    dataset_path_object = Path(dataset_path)
    dataset_bytes = dataset_path_object.read_bytes()
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    dataset = load_dataset_bytes(dataset_bytes)

    provider = build_provider(
        config.provider_name,
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        dimensions=config.dimensions,
        task=config.task,
        timeout_seconds=config.timeout_seconds,
    )
    if provider is None:
        raise ProviderSkippedError(
            f"Provider {config.provider_name!r} skipped: no API key configured."
        )
    try:
        return _run_with_provider(
            provider=provider,
            config=config,
            dataset=dataset,
            dataset_path=dataset_path_object,
            dataset_sha256=dataset_sha256,
            output_path=output_path,
        )
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            close()


def _run_with_provider(
    *,
    provider: EmbeddingProvider,
    config: EvaluationConfig,
    dataset: EvalDataset,
    dataset_path: Path,
    dataset_sha256: str,
    output_path: str | Path,
) -> EvaluationReport:
    product_ids = [product.product_id for product in dataset.products]
    document_texts = [
        build_product_index_text(
            name=product.name,
            category=product.category,
            description=product.description,
            material=product.material,
        )
        for product in dataset.products
    ]

    document_batch = provider.embed(texts=document_texts, task=TASK_DOCUMENT)
    if not document_batch.embeddings:
        raise EmbeddingProviderError("Provider returned no document embeddings.")
    document_vectors = [
        normalize_vector(result.vector) for result in document_batch.embeddings
    ]
    embedding_dimension = len(document_vectors[0])
    if any(len(vector) != embedding_dimension for vector in document_vectors):
        raise EmbeddingDimensionMismatchError(
            "Provider returned inconsistent document dimensions."
        )

    latencies_ms = [document_batch.latency_ms]
    requests = document_batch.request_count
    total_tokens = document_batch.total_tokens
    outcomes: list[QueryOutcome] = []

    for query in dataset.queries:
        try:
            query_batch = provider.embed(texts=[query.query], task=TASK_QUERY)
        except EmbeddingProviderError:
            # The query stays in the denominator as a failed outcome with zero
            # contributions; provider failures must never improve metrics.
            outcomes.append(
                QueryOutcome(
                    ranked_ids=(),
                    relevant_ids=frozenset(query.relevant_product_ids),
                    language=query.language,
                    retrieval_scenario=query.retrieval_scenario,
                    succeeded=False,
                )
            )
            continue
        latencies_ms.append(query_batch.latency_ms)
        requests += query_batch.request_count
        if query_batch.total_tokens is not None:
            total_tokens = (total_tokens or 0) + query_batch.total_tokens
        if len(query_batch.embeddings) != 1:
            raise EmbeddingProviderError(
                "Provider returned the wrong number of query embeddings."
            )
        query_vector = normalize_vector(query_batch.embeddings[0].vector)
        if len(query_vector) != embedding_dimension:
            raise EmbeddingDimensionMismatchError(
                f"Query dimension {len(query_vector)} does not match document "
                f"dimension {embedding_dimension}."
            )
        ranked = rank_documents(
            query_vector=query_vector,
            document_vectors=document_vectors,
            document_ids=product_ids,
            top_k=config.top_k,
        )
        outcomes.append(
            QueryOutcome(
                ranked_ids=tuple(product_id for product_id, _ in ranked),
                relevant_ids=frozenset(query.relevant_product_ids),
                language=query.language,
                retrieval_scenario=query.retrieval_scenario,
                succeeded=True,
            )
        )

    metrics = compute_metrics(
        per_query=outcomes,
        latencies_ms=latencies_ms,
        embedding_dimension=embedding_dimension,
        total_tokens=total_tokens,
        requests=requests,
        price_per_1m_tokens=config.price_per_1m_tokens,
    )
    language_distribution = {
        language: sum(1 for query in dataset.queries if query.language == language)
        for language in LANGUAGES
    }
    scenario_distribution = {
        scenario: sum(1 for query in dataset.queries if query.retrieval_scenario == scenario)
        for scenario in SCENARIOS
    }
    report = EvaluationReport(
        provider=provider.name,
        model=config.model,
        dataset=str(dataset_path),
        dataset_sha256=dataset_sha256,
        product_count=len(dataset.products),
        query_count=len(dataset.queries),
        language_distribution=language_distribution,
        scenario_distribution=scenario_distribution,
        configuration_without_secrets=config.without_secrets(),
        generated_at=datetime.now(UTC).isoformat(),
        metrics=metrics,
    )
    _write_report(report, output_path)
    return report


def report_to_dict(report: EvaluationReport) -> dict[str, object]:
    """Serialize a report to a JSON-safe dict (never contains secrets)."""
    metrics = report.metrics
    overall = metrics.overall
    return {
        "provider": report.provider,
        "model": report.model,
        "dataset": report.dataset,
        "dataset_sha256": report.dataset_sha256,
        "product_count": report.product_count,
        "query_count": report.query_count,
        "language_distribution": report.language_distribution,
        "scenario_distribution": report.scenario_distribution,
        "configuration_without_secrets": report.configuration_without_secrets,
        "generated_at": report.generated_at,
        "metrics": {
            "total_query_count": overall.query_count,
            "successful_query_count": overall.successful_query_count,
            "failed_query_count": overall.failed_query_count,
            "hit_rate_at_1": round(overall.hit_rate_at_1, 4),
            "hit_rate_at_3": round(overall.hit_rate_at_3, 4),
            "hit_rate_at_5": round(overall.hit_rate_at_5, 4),
            "recall_at_1": round(overall.recall_at_1, 4),
            "recall_at_3": round(overall.recall_at_3, 4),
            "recall_at_5": round(overall.recall_at_5, 4),
            "mrr": round(overall.mrr, 4),
            "mean_latency_ms": round(metrics.mean_latency_ms, 2),
            "p95_latency_ms": round(metrics.p95_latency_ms, 2),
            "failed_requests": overall.failed_query_count,
            "embedding_dimension": metrics.embedding_dimension,
            "estimated_cost_usd": (
                round(metrics.estimated_cost_usd, 6)
                if metrics.estimated_cost_usd is not None
                else None
            ),
            "total_tokens": metrics.total_tokens,
            "requests": metrics.requests,
            "successful_queries_only": _quality_to_dict(metrics.successful_queries_only),
            "by_language": {
                language: _quality_to_dict(quality)
                for language, quality in metrics.by_language.items()
            },
            "by_scenario": {
                scenario: _quality_to_dict(quality)
                for scenario, quality in metrics.by_scenario.items()
            },
        },
    }


def _quality_to_dict(quality: QualityMetrics) -> dict[str, object]:
    return {
        "query_count": quality.query_count,
        "successful_query_count": quality.successful_query_count,
        "failed_query_count": quality.failed_query_count,
        "hit_rate_at_1": round(quality.hit_rate_at_1, 4),
        "hit_rate_at_3": round(quality.hit_rate_at_3, 4),
        "hit_rate_at_5": round(quality.hit_rate_at_5, 4),
        "recall_at_1": round(quality.recall_at_1, 4),
        "recall_at_3": round(quality.recall_at_3, 4),
        "recall_at_5": round(quality.recall_at_5, 4),
        "mrr": round(quality.mrr, 4),
    }


def _write_report(report: EvaluationReport, output_path: str | Path) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report_to_dict(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

"""Retrieval metrics: Hit Rate@K, true Recall@K, MRR, latency, cost.

Hit Rate@K and Recall@K are reported separately. Failed queries (their
embedding request failed) remain in the denominator and contribute zero to
every quality score, so provider failures can never improve results.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence, Set
from dataclasses import dataclass

from talap.evaluation.embeddings.dataset import LANGUAGES, SCENARIOS


@dataclass(frozen=True)
class QueryOutcome:
    ranked_ids: tuple[str, ...]
    relevant_ids: frozenset[str]
    language: str
    retrieval_scenario: str
    succeeded: bool = True


@dataclass(frozen=True)
class QualityMetrics:
    """Ranking quality over a query set; denominator is always ``query_count``.

    A failed query is represented by ``succeeded=False`` with empty
    ``ranked_ids``; it contributes zero to every score while still counting
    toward ``query_count``.
    """

    query_count: int
    successful_query_count: int
    failed_query_count: int
    hit_rate_at_1: float
    hit_rate_at_3: float
    hit_rate_at_5: float
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float


@dataclass(frozen=True)
class RetrievalMetrics:
    overall: QualityMetrics
    successful_queries_only: QualityMetrics
    by_language: dict[str, QualityMetrics]
    by_scenario: dict[str, QualityMetrics]
    mean_latency_ms: float
    p95_latency_ms: float
    embedding_dimension: int | None
    estimated_cost_usd: float | None
    total_tokens: int | None
    requests: int


def hit_rate_at_k(
    *,
    ranked_ids: Sequence[str],
    relevant_ids: Set[str],
    k: int,
) -> float:
    """1.0 if at least one relevant document is in the top ``k``, else 0.0."""
    top_k = set(ranked_ids[:k])
    return 1.0 if any(relevant_id in top_k for relevant_id in relevant_ids) else 0.0


def recall_at_k(
    *,
    ranked_ids: Sequence[str],
    relevant_ids: Set[str],
    k: int,
) -> float:
    """Fraction of relevant documents retrieved in the top ``k`` results."""
    if not relevant_ids:
        return 0.0
    top_k = set(ranked_ids[:k])
    hits = sum(1 for relevant_id in relevant_ids if relevant_id in top_k)
    return hits / len(relevant_ids)


def reciprocal_rank(
    *,
    ranked_ids: Sequence[str],
    relevant_ids: Set[str],
) -> float:
    """1/rank of the first relevant document, or 0.0 when none is retrieved."""
    relevant = frozenset(relevant_ids)
    for position, ranked_id in enumerate(ranked_ids, start=1):
        if ranked_id in relevant:
            return 1.0 / position
    return 0.0


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile over ``[0, 1]``; 0.0 for empty input."""
    sorted_values = sorted(values)
    if not sorted_values:
        return 0.0
    rank = fraction * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (rank - lower)


def quality_metrics(outcomes: Sequence[QueryOutcome]) -> QualityMetrics:
    """Aggregate per-query scores over a set of outcomes (all queries count)."""
    query_count = len(outcomes)
    successful_query_count = sum(1 for outcome in outcomes if outcome.succeeded)
    failed_query_count = query_count - successful_query_count
    if query_count == 0:
        hit_1 = hit_3 = hit_5 = rec_1 = rec_3 = rec_5 = mrr = 0.0
    else:
        hit_1 = (
            sum(
                hit_rate_at_k(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids, k=1)
                for o in outcomes
            )
            / query_count
        )
        hit_3 = (
            sum(
                hit_rate_at_k(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids, k=3)
                for o in outcomes
            )
            / query_count
        )
        hit_5 = (
            sum(
                hit_rate_at_k(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids, k=5)
                for o in outcomes
            )
            / query_count
        )
        rec_1 = (
            sum(
                recall_at_k(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids, k=1)
                for o in outcomes
            )
            / query_count
        )
        rec_3 = (
            sum(
                recall_at_k(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids, k=3)
                for o in outcomes
            )
            / query_count
        )
        rec_5 = (
            sum(
                recall_at_k(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids, k=5)
                for o in outcomes
            )
            / query_count
        )
        mrr = (
            sum(
                reciprocal_rank(ranked_ids=o.ranked_ids, relevant_ids=o.relevant_ids)
                for o in outcomes
            )
            / query_count
        )
    return QualityMetrics(
        query_count=query_count,
        successful_query_count=successful_query_count,
        failed_query_count=failed_query_count,
        hit_rate_at_1=hit_1,
        hit_rate_at_3=hit_3,
        hit_rate_at_5=hit_5,
        recall_at_1=rec_1,
        recall_at_3=rec_3,
        recall_at_5=rec_5,
        mrr=mrr,
    )


def compute_metrics(
    *,
    per_query: Sequence[QueryOutcome],
    latencies_ms: Sequence[float],
    embedding_dimension: int | None,
    total_tokens: int | None,
    requests: int,
    price_per_1m_tokens: float | None,
) -> RetrievalMetrics:
    """Compute deterministic retrieval metrics with per-slice breakdowns.

    Primary quality metrics are computed over ALL queries (failed queries
    contribute zero). ``successful_queries_only`` is a clearly named secondary
    view. Language and retrieval-scenario slices always report every expected
    slice key, including zero-query slices. Latencies are real per-request
    latencies (the only network-dependent part of the results).
    """
    overall = quality_metrics(per_query)
    successful_only = quality_metrics([o for o in per_query if o.succeeded])
    by_language = {
        language: quality_metrics([o for o in per_query if o.language == language])
        for language in LANGUAGES
    }
    by_scenario = {
        scenario: quality_metrics([o for o in per_query if o.retrieval_scenario == scenario])
        for scenario in SCENARIOS
    }

    mean_latency_ms = statistics.fmean(latencies_ms) if latencies_ms else 0.0
    p95_latency_ms = percentile(latencies_ms, 0.95)

    estimated_cost_usd: float | None = None
    if price_per_1m_tokens is not None and total_tokens is not None:
        estimated_cost_usd = total_tokens / 1_000_000.0 * price_per_1m_tokens

    return RetrievalMetrics(
        overall=overall,
        successful_queries_only=successful_only,
        by_language=by_language,
        by_scenario=by_scenario,
        mean_latency_ms=mean_latency_ms,
        p95_latency_ms=p95_latency_ms,
        embedding_dimension=embedding_dimension,
        estimated_cost_usd=estimated_cost_usd,
        total_tokens=total_tokens,
        requests=requests,
    )

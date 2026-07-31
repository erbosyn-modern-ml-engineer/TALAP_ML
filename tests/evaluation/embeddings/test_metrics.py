from __future__ import annotations

import pytest

from talap.evaluation.embeddings.metrics import (
    QueryOutcome,
    compute_metrics,
    hit_rate_at_k,
    percentile,
    quality_metrics,
    recall_at_k,
    reciprocal_rank,
)


def _outcome(
    ranked: list[str],
    relevant: list[str],
    *,
    language: str = "ru",
    scenario: str = "same_language",
    succeeded: bool = True,
) -> QueryOutcome:
    return QueryOutcome(
        ranked_ids=tuple(ranked),
        relevant_ids=frozenset(relevant),
        language=language,
        retrieval_scenario=scenario,
        succeeded=succeeded,
    )


def test_hit_rate_at_k_hit_within_k() -> None:
    assert hit_rate_at_k(ranked_ids=["a", "b", "c"], relevant_ids={"b"}, k=3) == 1.0


def test_hit_rate_at_k_miss_beyond_k() -> None:
    assert hit_rate_at_k(ranked_ids=["a", "b", "c"], relevant_ids={"c"}, k=2) == 0.0


def test_recall_at_k_is_a_fraction() -> None:
    assert recall_at_k(
        ranked_ids=["a", "b", "c"], relevant_ids={"a", "b", "d"}, k=3
    ) == pytest.approx(2 / 3)


def test_recall_at_k_miss() -> None:
    assert recall_at_k(ranked_ids=["a", "b", "c"], relevant_ids={"z"}, k=3) == 0.0


def test_hit_rate_and_recall_are_distinct() -> None:
    # relevant = {A, B, C, D}, top-3 = [A, X, Y]
    ranked = ["A", "X", "Y"]
    relevant = {"A", "B", "C", "D"}
    assert hit_rate_at_k(ranked_ids=ranked, relevant_ids=relevant, k=3) == 1.0
    assert recall_at_k(ranked_ids=ranked, relevant_ids=relevant, k=3) == pytest.approx(0.25)


def test_reciprocal_rank_at_second_position() -> None:
    assert reciprocal_rank(ranked_ids=["a", "b", "c"], relevant_ids={"b"}) == 0.5


def test_reciprocal_rank_first_position() -> None:
    assert reciprocal_rank(ranked_ids=["a", "b", "c"], relevant_ids={"a", "c"}) == 1.0


def test_reciprocal_rank_no_relevant() -> None:
    assert reciprocal_rank(ranked_ids=["a", "b"], relevant_ids={"z"}) == 0.0


def test_percentile_empty_is_zero() -> None:
    assert percentile([], 0.95) == 0.0


def test_percentile_interpolates() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)


def test_quality_metrics_hit_recall_mrr() -> None:
    outcomes = [
        _outcome(["p1", "p2", "p3", "p4", "p5"], ["p1"]),
        _outcome(["p1", "p2", "p3", "p4", "p5"], ["p3"]),
        _outcome(["p1", "p2", "p3", "p4", "p5"], ["p9"]),
    ]
    quality = quality_metrics(outcomes)
    assert quality.query_count == 3
    assert quality.successful_query_count == 3
    assert quality.failed_query_count == 0
    assert quality.hit_rate_at_1 == pytest.approx(1 / 3)
    assert quality.hit_rate_at_3 == pytest.approx(2 / 3)
    assert quality.hit_rate_at_5 == pytest.approx(2 / 3)
    assert quality.recall_at_1 == pytest.approx(1 / 3)
    assert quality.recall_at_3 == pytest.approx(2 / 3)
    assert quality.recall_at_5 == pytest.approx(2 / 3)
    assert quality.mrr == pytest.approx((1.0 + 1 / 3 + 0.0) / 3)


def test_failed_query_contributes_zero_but_stays_in_denominator() -> None:
    outcomes = [
        _outcome(["p1", "p2"], ["p1"]),  # perfect success
        _outcome([], ["p7"], succeeded=False),  # failed query
    ]
    quality = quality_metrics(outcomes)
    assert quality.query_count == 2
    assert quality.successful_query_count == 1
    assert quality.failed_query_count == 1
    assert quality.hit_rate_at_1 == pytest.approx(0.5)
    assert quality.recall_at_1 == pytest.approx(0.5)
    assert quality.mrr == pytest.approx(0.5)


def test_successful_queries_only_secondary_view() -> None:
    outcomes = [
        _outcome(["p1", "p2"], ["p1"]),
        _outcome([], ["p7"], succeeded=False),
    ]
    metrics = compute_metrics(
        per_query=outcomes,
        latencies_ms=[1.0],
        embedding_dimension=2,
        total_tokens=None,
        requests=2,
        price_per_1m_tokens=None,
    )
    assert metrics.overall.hit_rate_at_1 == pytest.approx(0.5)
    assert metrics.overall.query_count == 2
    assert metrics.successful_queries_only.query_count == 1
    assert metrics.successful_queries_only.hit_rate_at_1 == pytest.approx(1.0)


def test_compute_metrics_per_language_slices() -> None:
    outcomes = [
        _outcome(["p1"], ["p1"], language="kk", scenario="same_language"),
        _outcome(["p1"], ["p2"], language="ru", scenario="same_language"),
        _outcome(["p3"], ["p3"], language="mixed", scenario="mixed"),
    ]
    metrics = compute_metrics(
        per_query=outcomes,
        latencies_ms=[1.0, 2.0, 3.0],
        embedding_dimension=2,
        total_tokens=9,
        requests=3,
        price_per_1m_tokens=None,
    )
    assert set(metrics.by_language) == {"kk", "ru", "mixed"}
    assert metrics.by_language["kk"].query_count == 1
    assert metrics.by_language["kk"].hit_rate_at_1 == 1.0
    assert metrics.by_language["ru"].query_count == 1
    assert metrics.by_language["ru"].hit_rate_at_1 == 0.0
    assert metrics.by_language["mixed"].query_count == 1
    assert metrics.by_language["mixed"].hit_rate_at_1 == 1.0


def test_compute_metrics_per_scenario_slices() -> None:
    outcomes = [
        _outcome(["p1"], ["p1"], language="kk", scenario="same_language"),
        _outcome(["p2"], ["p2"], language="ru", scenario="cross_language"),
        _outcome(["p3"], ["p3"], language="mixed", scenario="mixed"),
    ]
    metrics = compute_metrics(
        per_query=outcomes,
        latencies_ms=[1.0],
        embedding_dimension=2,
        total_tokens=None,
        requests=1,
        price_per_1m_tokens=None,
    )
    assert set(metrics.by_scenario) == {"same_language", "cross_language", "mixed"}
    assert metrics.by_scenario["same_language"].query_count == 1
    assert metrics.by_scenario["same_language"].hit_rate_at_1 == 1.0
    assert metrics.by_scenario["cross_language"].query_count == 1
    assert metrics.by_scenario["cross_language"].hit_rate_at_1 == 1.0
    assert metrics.by_scenario["mixed"].query_count == 1
    assert metrics.by_scenario["mixed"].hit_rate_at_1 == 1.0


def test_compute_metrics_latency_and_counts() -> None:
    outcomes = [
        _outcome(["p1"], ["p1"], language="kk", scenario="same_language"),
        _outcome(["p1"], ["p2"], language="ru", scenario="same_language"),
    ]
    metrics = compute_metrics(
        per_query=outcomes,
        latencies_ms=[10.0, 20.0, 30.0],
        embedding_dimension=3,
        total_tokens=100,
        requests=3,
        price_per_1m_tokens=None,
    )
    assert metrics.mean_latency_ms == pytest.approx(20.0)
    assert metrics.p95_latency_ms == pytest.approx(29.0)
    assert metrics.embedding_dimension == 3
    assert metrics.total_tokens == 100
    assert metrics.requests == 3
    assert metrics.estimated_cost_usd is None


def test_compute_metrics_cost_estimation() -> None:
    metrics = compute_metrics(
        per_query=[_outcome(["a"], ["a"])],
        latencies_ms=[5.0],
        embedding_dimension=2,
        total_tokens=1_000_000,
        requests=1,
        price_per_1m_tokens=0.10,
    )
    assert metrics.estimated_cost_usd == pytest.approx(0.10)


def test_compute_metrics_empty_queries_are_zeros() -> None:
    metrics = compute_metrics(
        per_query=[],
        latencies_ms=[],
        embedding_dimension=None,
        total_tokens=None,
        requests=0,
        price_per_1m_tokens=None,
    )
    overall = metrics.overall
    assert overall.query_count == 0
    assert overall.hit_rate_at_1 == 0.0
    assert overall.recall_at_1 == 0.0
    assert overall.mrr == 0.0
    assert metrics.mean_latency_ms == 0.0
    assert metrics.p95_latency_ms == 0.0
    assert metrics.estimated_cost_usd is None

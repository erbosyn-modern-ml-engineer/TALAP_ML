from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from talap.evaluation.embeddings import runner as runner_module
from talap.evaluation.embeddings.interface import (
    TASK_QUERY,
    EmbedBatchResult,
    EmbeddingProviderError,
    EmbeddingResult,
)
from talap.evaluation.embeddings.runner import (
    EvaluationConfig,
    ProviderSkippedError,
    report_to_dict,
    run_evaluation,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATASET = _REPO_ROOT / "data" / "evaluation" / "talap_embedding_eval.json"


class _FakeProvider:
    """Deterministic offline provider: same text always yields the same vector."""

    name = "fake"

    def __init__(self, dimension: int = 4) -> None:
        self._dimension = dimension

    def embed(self, *, texts: Sequence[str], task: str) -> EmbedBatchResult:
        vectors = [self._vector(text) for text in texts]
        return EmbedBatchResult(
            embeddings=tuple(EmbeddingResult(vector=vector) for vector in vectors),
            total_tokens=sum(len(text.split()) for text in texts),
            latency_ms=1.0,
            request_count=1,
        )

    def _vector(self, text: str) -> tuple[float, ...]:
        seed = sum(ord(character) for character in text)
        return tuple(math.sin(seed + index) * (index + 1) for index in range(self._dimension))

    def close(self) -> None:
        pass


class _FailingQueryProvider(_FakeProvider):
    """Fails every embedding request for the query task."""

    def embed(self, *, texts: Sequence[str], task: str) -> EmbedBatchResult:
        if task == TASK_QUERY:
            raise EmbeddingProviderError("injected query failure")
        return super().embed(texts=texts, task=task)


def test_runner_end_to_end_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "build_provider", lambda *a, **k: _FakeProvider())
    config = EvaluationConfig(provider_name="fake", model="fake-model", api_key="k")
    output = tmp_path / "report.json"

    first = run_evaluation(config=config, dataset_path=_DATASET, output_path=output)
    second = run_evaluation(config=config, dataset_path=_DATASET, output_path=output)

    assert first.metrics == second.metrics
    assert first.metrics.embedding_dimension == 4
    assert first.metrics.requests == 41  # 1 document batch + 40 queries
    assert first.metrics.overall.query_count == 40
    assert first.metrics.overall.failed_query_count == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["provider"] == "fake"
    assert payload["model"] == "fake-model"
    serialized = json.dumps(payload)
    assert "api_key" not in serialized.lower()
    assert "super-secret" not in serialized


def test_runner_failed_queries_stay_in_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "build_provider",
        lambda *a, **k: _FailingQueryProvider(),
    )
    config = EvaluationConfig(provider_name="fake", model="fake-model", api_key="k")

    report = run_evaluation(
        config=config,
        dataset_path=_DATASET,
        output_path=tmp_path / "report.json",
    )

    overall = report.metrics.overall
    assert overall.query_count == 40
    assert overall.successful_query_count == 0
    assert overall.failed_query_count == 40
    # failures must not improve metrics: everything is zero
    assert overall.hit_rate_at_1 == 0.0
    assert overall.recall_at_1 == 0.0
    assert overall.mrr == 0.0


def test_runner_skips_when_provider_has_no_api_key(tmp_path: Path) -> None:
    config = EvaluationConfig(provider_name="openai", model="m", api_key=None)
    with pytest.raises(ProviderSkippedError):
        run_evaluation(
            config=config,
            dataset_path=_DATASET,
            output_path=tmp_path / "report.json",
        )


def test_report_dataset_sha256_matches_file_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "build_provider", lambda *a, **k: _FakeProvider())
    config = EvaluationConfig(provider_name="fake", model="fake-model", api_key="k")

    report = run_evaluation(
        config=config,
        dataset_path=_DATASET,
        output_path=tmp_path / "report.json",
    )

    expected = hashlib.sha256(_DATASET.read_bytes()).hexdigest()
    assert report.dataset_sha256 == expected
    assert report.product_count == 24
    assert report.query_count == 40
    assert set(report.language_distribution) == {"kk", "ru", "mixed"}
    assert set(report.scenario_distribution) == {
        "same_language",
        "cross_language",
        "mixed",
    }


def test_report_contains_no_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "build_provider", lambda *a, **k: _FakeProvider())
    config = EvaluationConfig(
        provider_name="fake",
        model="fake-model",
        api_key="super-secret-value",
    )

    report = run_evaluation(
        config=config,
        dataset_path=_DATASET,
        output_path=tmp_path / "report.json",
    )

    configuration = report.configuration_without_secrets
    assert "api_key" not in configuration
    serialized = json.dumps(report_to_dict(report))
    assert "super-secret-value" not in serialized
    assert "Authorization" not in serialized
    assert "api_key" not in serialized.lower()


def test_report_to_dict_contains_all_required_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "build_provider", lambda *a, **k: _FakeProvider())
    config = EvaluationConfig(
        provider_name="fake",
        model="fake-model",
        api_key="k",
        price_per_1m_tokens=0.10,
    )
    report = run_evaluation(
        config=config,
        dataset_path=_DATASET,
        output_path=tmp_path / "report.json",
    )

    payload = report_to_dict(report)
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    assert set(metrics) == {
        "total_query_count",
        "successful_query_count",
        "failed_query_count",
        "hit_rate_at_1",
        "hit_rate_at_3",
        "hit_rate_at_5",
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "mrr",
        "mean_latency_ms",
        "p95_latency_ms",
        "failed_requests",
        "embedding_dimension",
        "estimated_cost_usd",
        "total_tokens",
        "requests",
        "successful_queries_only",
        "by_language",
        "by_scenario",
    }
    assert metrics["embedding_dimension"] == 4
    assert isinstance(metrics["by_language"], dict)
    assert set(metrics["by_language"]) == {"kk", "ru", "mixed"}
    assert isinstance(metrics["by_scenario"], dict)
    assert set(metrics["by_scenario"]) == {"same_language", "cross_language", "mixed"}

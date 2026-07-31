"""Offline embedding-provider evaluation harness (T-017B2A)."""

from talap.evaluation.embeddings.dataset import (
    DatasetValidationError,
    EvalDataset,
    EvalProduct,
    EvalQuery,
    load_dataset,
    validate_dataset,
)
from talap.evaluation.embeddings.interface import (
    TASK_DOCUMENT,
    TASK_QUERY,
    EmbedBatchResult,
    EmbeddingDimensionMismatchError,
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingResult,
)
from talap.evaluation.embeddings.metrics import (
    QualityMetrics,
    QueryOutcome,
    RetrievalMetrics,
    compute_metrics,
    hit_rate_at_k,
    percentile,
    recall_at_k,
    reciprocal_rank,
)
from talap.evaluation.embeddings.retrieval import (
    cosine_similarity,
    l2_norm,
    normalize_vector,
    rank_documents,
)
from talap.evaluation.embeddings.runner import (
    EvaluationConfig,
    EvaluationReport,
    ProviderSkippedError,
    report_to_dict,
    run_evaluation,
)

__all__ = [
    "DatasetValidationError",
    "EmbedBatchResult",
    "EmbeddingDimensionMismatchError",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "EmbeddingResult",
    "EvalDataset",
    "EvalProduct",
    "EvalQuery",
    "EvaluationConfig",
    "EvaluationReport",
    "ProviderSkippedError",
    "QueryOutcome",
    "QualityMetrics",
    "RetrievalMetrics",
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "compute_metrics",
    "cosine_similarity",
    "hit_rate_at_k",
    "l2_norm",
    "load_dataset",
    "normalize_vector",
    "percentile",
    "rank_documents",
    "recall_at_k",
    "reciprocal_rank",
    "report_to_dict",
    "run_evaluation",
    "validate_dataset",
]

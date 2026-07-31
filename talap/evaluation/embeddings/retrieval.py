"""Cosine-similarity retrieval over embedded product documents."""

from __future__ import annotations

import math
from collections.abc import Sequence

from talap.evaluation.embeddings.interface import EmbeddingDimensionMismatchError


def l2_norm(vector: Sequence[float]) -> float:
    """Euclidean norm of a vector."""
    return math.sqrt(sum(value * value for value in vector))


def normalize_vector(vector: Sequence[float]) -> tuple[float, ...]:
    """Return the L2-normalized vector (idempotent for already-unit vectors)."""
    norm = l2_norm(vector)
    if norm == 0.0:
        raise ValueError("Cannot normalize a zero vector.")
    return tuple(value / norm for value in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity between two vectors of the same dimension."""
    if len(left) != len(right):
        raise EmbeddingDimensionMismatchError(
            f"Cosine similarity requires equal dimensions, got {len(left)} and {len(right)}."
        )
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = l2_norm(left)
    right_norm = l2_norm(right)
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def rank_documents(
    *,
    query_vector: Sequence[float],
    document_vectors: Sequence[Sequence[float]],
    document_ids: Sequence[str],
    top_k: int,
) -> tuple[tuple[str, float], ...]:
    """Rank documents by cosine similarity, most similar first, capped at top_k.

    Ties (equal cosine scores) are broken deterministically by ``product_id``
    ascending, so identical vectors always produce the same ordering.
    """
    if len(document_vectors) != len(document_ids):
        raise ValueError("document_vectors and document_ids must have equal length.")
    if top_k < 1:
        raise ValueError("top_k must be at least 1.")
    query_dimension = len(query_vector)
    for vector in document_vectors:
        if len(vector) != query_dimension:
            raise EmbeddingDimensionMismatchError(
                f"Document dimension {len(vector)} does not match query dimension "
                f"{query_dimension}."
            )
    scored = sorted(
        (
            (document_id, cosine_similarity(query_vector, vector))
            for document_id, vector in zip(document_ids, document_vectors, strict=True)
        ),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return tuple(scored[:top_k])

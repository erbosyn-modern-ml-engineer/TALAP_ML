from __future__ import annotations

import math

import pytest

from talap.evaluation.embeddings.interface import EmbeddingDimensionMismatchError
from talap.evaluation.embeddings.retrieval import (
    cosine_similarity,
    l2_norm,
    normalize_vector,
    rank_documents,
)


def test_l2_norm() -> None:
    assert math.isclose(l2_norm([3.0, 4.0]), 5.0)


def test_normalize_vector_has_unit_length() -> None:
    vector = normalize_vector([3.0, 4.0])
    assert vector == pytest.approx((0.6, 0.8))
    assert math.isclose(l2_norm(vector), 1.0)


def test_normalize_vector_is_idempotent() -> None:
    normalized = normalize_vector([1.0, 2.0, 3.0])
    assert normalize_vector(normalized) == pytest.approx(normalized)


def test_normalize_zero_vector_raises() -> None:
    with pytest.raises(ValueError):
        normalize_vector([0.0, 0.0])


def test_cosine_similarity_identical_vectors() -> None:
    assert math.isclose(cosine_similarity([1.0, 2.0], [1.0, 2.0]), 1.0)


def test_cosine_similarity_orthogonal_vectors() -> None:
    assert math.isclose(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)


def test_cosine_similarity_opposite_vectors() -> None:
    assert math.isclose(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)


def test_cosine_similarity_dimension_mismatch() -> None:
    with pytest.raises(EmbeddingDimensionMismatchError):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


def test_rank_documents_orders_by_similarity() -> None:
    query = [1.0, 0.0]
    documents = [[0.9, 0.1], [0.1, 0.9], [1.0, 0.0]]
    ids = ["a", "b", "c"]
    ranked = rank_documents(
        query_vector=query,
        document_vectors=documents,
        document_ids=ids,
        top_k=3,
    )
    assert [item[0] for item in ranked] == ["c", "a", "b"]


def test_rank_documents_respects_top_k() -> None:
    query = [1.0, 0.0]
    documents = [[0.9, 0.1], [0.1, 0.9], [1.0, 0.0]]
    ids = ["a", "b", "c"]
    ranked = rank_documents(
        query_vector=query,
        document_vectors=documents,
        document_ids=ids,
        top_k=1,
    )
    assert len(ranked) == 1
    assert ranked[0][0] == "c"


def test_rank_documents_detects_dimension_mismatch() -> None:
    with pytest.raises(EmbeddingDimensionMismatchError):
        rank_documents(
            query_vector=[1.0, 0.0],
            document_vectors=[[1.0]],
            document_ids=["a"],
            top_k=1,
        )


def test_rank_documents_tie_breaks_by_product_id_ascending() -> None:
    query = [1.0, 0.0]
    documents = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]  # equal vectors -> equal scores
    ids = ["b", "a", "c"]  # deliberately not sorted
    ranked = rank_documents(
        query_vector=query,
        document_vectors=documents,
        document_ids=ids,
        top_k=3,
    )
    assert [item[0] for item in ranked] == ["a", "b", "c"]
    scores = [item[1] for item in ranked]
    assert scores[0] == scores[1] == scores[2]

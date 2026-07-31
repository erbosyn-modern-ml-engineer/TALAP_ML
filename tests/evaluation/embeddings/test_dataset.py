from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from talap.evaluation.embeddings.dataset import (
    DatasetValidationError,
    EvalDataset,
    load_dataset,
    validate_dataset,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATASET = _REPO_ROOT / "data" / "evaluation" / "talap_embedding_eval.json"


def test_checked_in_dataset_exists_and_is_valid() -> None:
    dataset = load_dataset(_DATASET)
    assert isinstance(dataset, EvalDataset)
    assert 20 <= len(dataset.products) <= 30
    assert 30 <= len(dataset.queries) <= 50


def test_language_distribution_covers_all_three() -> None:
    dataset = load_dataset(_DATASET)
    languages = [query.language for query in dataset.queries]
    assert set(languages) == {"kk", "ru", "mixed"}
    for language in ("kk", "ru", "mixed"):
        assert language in languages


def test_dataset_language_distribution_meets_contract() -> None:
    dataset = load_dataset(_DATASET)
    counts = Counter(query.language for query in dataset.queries)
    assert counts["kk"] >= 10
    assert counts["ru"] >= 10
    assert counts["mixed"] >= 6


def test_dataset_scenario_distribution_meets_contract() -> None:
    dataset = load_dataset(_DATASET)
    counts = Counter(query.retrieval_scenario for query in dataset.queries)
    assert counts["same_language"] >= 1
    assert counts["cross_language"] >= 6
    assert counts["mixed"] >= 1


def test_dataset_document_language_distribution_meets_contract() -> None:
    dataset = load_dataset(_DATASET)
    counts = Counter(product.document_language for product in dataset.products)
    assert counts["ru"] >= 8
    assert counts["kk"] >= 8
    assert counts["bilingual"] <= 8


def test_cross_language_covers_both_directions() -> None:
    dataset = load_dataset(_DATASET)
    document_language = {
        product.product_id: product.document_language for product in dataset.products
    }
    directions: set[tuple[str, str]] = set()
    for query in dataset.queries:
        if query.retrieval_scenario != "cross_language":
            continue
        for relevant_id in query.relevant_product_ids:
            if document_language[relevant_id] != query.language:
                directions.add((query.language, document_language[relevant_id]))
    assert ("kk", "ru") in directions
    assert ("ru", "kk") in directions


def test_same_language_queries_target_same_language_documents() -> None:
    dataset = load_dataset(_DATASET)
    document_language = {
        product.product_id: product.document_language for product in dataset.products
    }
    for query in dataset.queries:
        if query.retrieval_scenario != "same_language":
            continue
        assert query.language in ("kk", "ru")
        for relevant_id in query.relevant_product_ids:
            assert document_language[relevant_id] == query.language


def test_every_product_has_required_fields_and_unique_ids() -> None:
    dataset = load_dataset(_DATASET)
    ids = [product.product_id for product in dataset.products]
    assert len(ids) == len(set(ids))
    for product in dataset.products:
        assert product.product_id != ""
        assert product.name != ""
        assert product.category != ""
        assert product.description != ""
        assert product.material is None or product.material != ""
        assert product.document_language in {"kk", "ru", "bilingual"}


def test_every_query_has_required_fields_and_unique_ids() -> None:
    dataset = load_dataset(_DATASET)
    ids = [query.query_id for query in dataset.queries]
    assert len(ids) == len(set(ids))
    for query in dataset.queries:
        assert query.query_id != ""
        assert query.query != ""
        assert query.relevant_product_ids
        assert query.language in {"kk", "ru", "mixed"}
        assert query.retrieval_scenario in {"same_language", "cross_language", "mixed"}
        assert isinstance(query.notes, str)


def test_all_relevant_ids_exist_in_products() -> None:
    dataset = load_dataset(_DATASET)
    product_ids = {product.product_id for product in dataset.products}
    for query in dataset.queries:
        assert set(query.relevant_product_ids) <= product_ids


def _valid_payload() -> dict[str, object]:
    return {
        "products": [
            {
                "product_id": "p1",
                "name": "Рюкзак",
                "category": "school",
                "description": "Школьный рюкзак.",
                "material": "Полиэстер",
                "document_language": "ru",
            }
        ],
        "queries": [
            {
                "query_id": "q1",
                "query": "рюкзак",
                "relevant_product_ids": ["p1"],
                "language": "ru",
                "retrieval_scenario": "same_language",
                "notes": "Simple query.",
            }
        ],
    }


def test_validate_accepts_valid_payload() -> None:
    dataset = validate_dataset(_valid_payload())
    assert len(dataset.products) == 1
    assert len(dataset.queries) == 1


def test_validate_rejects_missing_product_field() -> None:
    payload = _valid_payload()
    del payload["products"][0]["description"]  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_unknown_language() -> None:
    payload = _valid_payload()
    payload["queries"][0]["language"] = "fr"  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_unknown_retrieval_scenario() -> None:
    payload = _valid_payload()
    payload["queries"][0]["retrieval_scenario"] = "unknown_scenario"  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_unknown_document_language() -> None:
    payload = _valid_payload()
    payload["products"][0]["document_language"] = "fr"  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_same_language_query_targeting_other_language() -> None:
    payload = _valid_payload()
    # Product p1 is ru-only; switching the same_language query to kk must fail.
    payload["queries"][0]["language"] = "kk"  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_cross_language_query_without_differing_document() -> None:
    payload = _valid_payload()
    # ru query + ru-only document: no document differs -> must fail.
    payload["queries"][0]["retrieval_scenario"] = "cross_language"  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_accepts_cross_language_query_with_differing_document() -> None:
    payload = _valid_payload()
    payload["queries"][0]["language"] = "kk"  # type: ignore[index]
    payload["queries"][0]["retrieval_scenario"] = "cross_language"  # type: ignore[index]
    dataset = validate_dataset(payload)
    assert dataset.queries[0].retrieval_scenario == "cross_language"


def test_validate_rejects_unknown_relevant_product_id() -> None:
    payload = _valid_payload()
    payload["queries"][0]["relevant_product_ids"] = ["p999"]  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_duplicate_product_ids() -> None:
    payload = _valid_payload()
    payload["products"].append(dict(payload["products"][0]))  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_empty_query_text() -> None:
    payload = _valid_payload()
    payload["queries"][0]["query"] = "   "  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)


def test_validate_rejects_empty_relevant_ids() -> None:
    payload = _valid_payload()
    payload["queries"][0]["relevant_product_ids"] = []  # type: ignore[index]
    with pytest.raises(DatasetValidationError):
        validate_dataset(payload)

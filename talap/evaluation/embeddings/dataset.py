"""JSON benchmark dataset loading and validation for embedding evaluation."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VALID_LANGUAGES = frozenset({"kk", "ru", "mixed"})
VALID_SCENARIOS = frozenset({"same_language", "cross_language", "mixed"})
VALID_DOCUMENT_LANGUAGES = frozenset({"kk", "ru", "bilingual"})

LANGUAGES = ("kk", "ru", "mixed")
SCENARIOS = ("same_language", "cross_language", "mixed")

_MIN_QUERIES_PER_LANGUAGE = {"kk": 10, "ru": 10, "mixed": 6}
_MIN_CROSS_LANGUAGE_QUERIES = 6
_MIN_DOCUMENTS_PER_LANGUAGE = {"kk": 8, "ru": 8}
_MAX_BILINGUAL_DOCUMENTS = 8


class DatasetValidationError(Exception):
    """The benchmark dataset is malformed."""


@dataclass(frozen=True)
class EvalProduct:
    product_id: str
    name: str
    category: str
    description: str
    material: str | None
    document_language: str


@dataclass(frozen=True)
class EvalQuery:
    query_id: str
    query: str
    relevant_product_ids: tuple[str, ...]
    language: str
    retrieval_scenario: str
    notes: str


@dataclass(frozen=True)
class EvalDataset:
    products: tuple[EvalProduct, ...]
    queries: tuple[EvalQuery, ...]


def load_dataset(path: str | Path) -> EvalDataset:
    """Load, validate, and coverage-check the JSON benchmark dataset at ``path``."""
    content = Path(path).read_bytes()
    return load_dataset_bytes(content)


def load_dataset_bytes(content: bytes) -> EvalDataset:
    """Parse dataset bytes, validate structure, and check benchmark coverage.

    The exact bytes are what a caller hashes for report reproducibility.
    """
    payload = json.loads(content.decode("utf-8"))
    dataset = validate_dataset(payload)
    check_benchmark_coverage(dataset)
    return dataset


def _require_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise DatasetValidationError(f"Dataset field {key!r} must be a non-empty string.")
    return value


def validate_dataset(payload: object) -> EvalDataset:
    """Validate a parsed dataset payload and return a typed ``EvalDataset``.

    Enforces per-item structure plus the same_language / cross_language
    targeting contract. Dataset-wide coverage is enforced separately by
    ``check_benchmark_coverage``.
    """
    if not isinstance(payload, Mapping):
        raise DatasetValidationError("Dataset must be a JSON object.")
    raw_products = payload.get("products")
    raw_queries = payload.get("queries")
    if not isinstance(raw_products, list) or not raw_products:
        raise DatasetValidationError("Dataset must contain a non-empty 'products' list.")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise DatasetValidationError("Dataset must contain a non-empty 'queries' list.")

    products: list[EvalProduct] = []
    product_ids: set[str] = set()
    document_language_by_id: dict[str, str] = {}
    for raw in raw_products:
        if not isinstance(raw, Mapping):
            raise DatasetValidationError("Every product must be a JSON object.")
        product_id = _require_string(raw, "product_id")
        if product_id in product_ids:
            raise DatasetValidationError(f"Duplicate product_id {product_id!r}.")
        product_ids.add(product_id)
        material = raw.get("material")
        if material is not None and (not isinstance(material, str) or material == ""):
            raise DatasetValidationError(
                f"Product {product_id!r} material must be a non-empty string or null."
            )
        document_language = _require_string(raw, "document_language")
        if document_language not in VALID_DOCUMENT_LANGUAGES:
            raise DatasetValidationError(
                f"Product {product_id!r} has unsupported document_language "
                f"{document_language!r}."
            )
        document_language_by_id[product_id] = document_language
        products.append(
            EvalProduct(
                product_id=product_id,
                name=_require_string(raw, "name"),
                category=_require_string(raw, "category"),
                description=_require_string(raw, "description"),
                material=material,
                document_language=document_language,
            )
        )

    queries: list[EvalQuery] = []
    query_ids: set[str] = set()
    for raw in raw_queries:
        if not isinstance(raw, Mapping):
            raise DatasetValidationError("Every query must be a JSON object.")
        query_id = _require_string(raw, "query_id")
        if query_id in query_ids:
            raise DatasetValidationError(f"Duplicate query_id {query_id!r}.")
        query_ids.add(query_id)
        query = _require_string(raw, "query")
        language = _require_string(raw, "language")
        if language not in VALID_LANGUAGES:
            raise DatasetValidationError(
                f"Query {query_id!r} has unsupported language {language!r}."
            )
        scenario = _require_string(raw, "retrieval_scenario")
        if scenario not in VALID_SCENARIOS:
            raise DatasetValidationError(
                f"Query {query_id!r} has unsupported retrieval_scenario {scenario!r}."
            )
        notes = raw.get("notes")
        if not isinstance(notes, str):
            raise DatasetValidationError(f"Query {query_id!r} notes must be a string.")
        relevant = raw.get("relevant_product_ids")
        if not isinstance(relevant, list) or not relevant:
            raise DatasetValidationError(
                f"Query {query_id!r} must contain a non-empty relevant_product_ids list."
            )
        relevant_ids_list: list[str] = []
        for item in relevant:
            if not isinstance(item, str) or item == "":
                raise DatasetValidationError(
                    f"Query {query_id!r} relevant_product_ids must be non-empty strings."
                )
            relevant_ids_list.append(item)
        relevant_ids = tuple(relevant_ids_list)
        for relevant_id in relevant_ids:
            if relevant_id not in product_ids:
                raise DatasetValidationError(
                    f"Query {query_id!r} references unknown product_id {relevant_id!r}."
                )
        _check_scenario_consistency(
            query_id=query_id,
            language=language,
            scenario=scenario,
            relevant_ids=relevant_ids,
            document_language_by_id=document_language_by_id,
        )
        queries.append(
            EvalQuery(
                query_id=query_id,
                query=query,
                relevant_product_ids=relevant_ids,
                language=language,
                retrieval_scenario=scenario,
                notes=notes,
            )
        )

    return EvalDataset(products=tuple(products), queries=tuple(queries))


def _check_scenario_consistency(
    *,
    query_id: str,
    language: str,
    scenario: str,
    relevant_ids: tuple[str, ...],
    document_language_by_id: Mapping[str, str],
) -> None:
    """Enforce the same_language and cross_language targeting rules."""
    if scenario == "same_language":
        if language not in ("kk", "ru"):
            raise DatasetValidationError(
                f"Query {query_id!r} with same_language scenario must use a kk or ru language."
            )
        for relevant_id in relevant_ids:
            if document_language_by_id[relevant_id] != language:
                raise DatasetValidationError(
                    f"Query {query_id!r} is same_language but relevant product "
                    f"{relevant_id!r} is not written in {language}."
                )
    elif scenario == "cross_language":
        if language not in ("kk", "ru"):
            raise DatasetValidationError(
                f"Query {query_id!r} with cross_language scenario must use a kk or ru language."
            )
        if not any(
            document_language_by_id[relevant_id] != language for relevant_id in relevant_ids
        ):
            raise DatasetValidationError(
                f"Query {query_id!r} is cross_language but no relevant product "
                "differs from the query language."
            )


def check_benchmark_coverage(dataset: EvalDataset) -> None:
    """Raise when dataset-wide coverage drops below the benchmark contract."""
    language_counts = Counter(query.language for query in dataset.queries)
    for language, minimum in _MIN_QUERIES_PER_LANGUAGE.items():
        if language_counts.get(language, 0) < minimum:
            raise DatasetValidationError(
                f"Dataset needs at least {minimum} {language} queries, got "
                f"{language_counts.get(language, 0)}."
            )
    if set(language_counts) != VALID_LANGUAGES:
        raise DatasetValidationError(
            "Dataset must contain queries in every language: kk, ru, mixed."
        )

    scenario_counts = Counter(query.retrieval_scenario for query in dataset.queries)
    if scenario_counts.get("cross_language", 0) < _MIN_CROSS_LANGUAGE_QUERIES:
        raise DatasetValidationError(
            f"Dataset needs at least {_MIN_CROSS_LANGUAGE_QUERIES} cross_language "
            f"queries, got {scenario_counts.get('cross_language', 0)}."
        )
    if set(scenario_counts) != VALID_SCENARIOS:
        raise DatasetValidationError(
            "Dataset must contain queries in every retrieval_scenario."
        )

    document_counts = Counter(product.document_language for product in dataset.products)
    for language, minimum in _MIN_DOCUMENTS_PER_LANGUAGE.items():
        if document_counts.get(language, 0) < minimum:
            raise DatasetValidationError(
                f"Dataset needs at least {minimum} {language}-only documents, got "
                f"{document_counts.get(language, 0)}."
            )
    if document_counts.get("bilingual", 0) > _MAX_BILINGUAL_DOCUMENTS:
        raise DatasetValidationError(
            f"Dataset allows at most {_MAX_BILINGUAL_DOCUMENTS} bilingual documents, "
            f"got {document_counts.get('bilingual', 0)}."
        )

    _check_cross_language_directions(dataset)


def _check_cross_language_directions(dataset: EvalDataset) -> None:
    """Require both kk->ru and ru->kk cross-language directions."""
    document_language_by_id = {
        product.product_id: product.document_language for product in dataset.products
    }
    directions: set[tuple[str, str]] = set()
    for query in dataset.queries:
        if query.retrieval_scenario != "cross_language":
            continue
        for relevant_id in query.relevant_product_ids:
            target_language = document_language_by_id[relevant_id]
            if target_language != query.language:
                directions.add((query.language, target_language))
    missing = {("kk", "ru"), ("ru", "kk")} - directions
    if missing:
        raise DatasetValidationError(
            "Cross-language scenarios must cover both kk->ru and ru->kk "
            f"directions; missing {sorted(missing)}."
        )

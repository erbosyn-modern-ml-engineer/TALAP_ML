"""Minimal product vector search (MVP-4).

Accepts an already-validated ``CustomerRequest`` and returns up to ``limit``
available catalog products ranked by cosine similarity. Not connected to the
WhatsApp worker yet.
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.ai.customer_request import CustomerRequest
from talap.core import get_settings
from talap.db.models import Inventory, Product, ProductEmbedding, ProductVariant
from talap.db.session import async_session_factory as _default_session_factory
from talap.embeddings import PROVIDER_JINA, JinaEmbeddingClient, JinaEmbeddingError

__all__ = [
    "ProductSearchResult",
    "ProductSearchValidationError",
    "ProductSearchExecutionError",
    "search_products",
]

MAX_LIMIT = 10
_SIMILARITY_EPSILON = 1e-6


class ProductSearchValidationError(RuntimeError):
    """Invalid arguments passed to product search."""


class ProductSearchExecutionError(RuntimeError):
    """Product search failed; never includes secrets, vectors, or customer text."""


class ProductSearchResult(BaseModel):
    """One available product ranked by similarity; no ORM/embedding objects."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    product_id: UUID
    name: str
    category: str
    description: str | None = None
    price_kzt: int
    available_quantity: int
    merchant_sku: str
    material: str | None = None
    similarity: float = Field(ge=-1.0, le=1.0)


def build_query_text(request: CustomerRequest) -> str:
    """Build one compact query string; budget and quantity are not embedded."""
    parts = [request.query_text]
    if request.category:
        parts.append(f"category: {request.category}")
    for key in sorted(request.attributes):
        parts.append(f"{key}: {request.attributes[key]}")
    return " ".join(parts)


def _clamp_similarity(cosine_distance: float) -> float:
    similarity = 1.0 - cosine_distance
    if not math.isfinite(similarity):
        raise ProductSearchExecutionError(
            "Product search returned an invalid similarity value."
        )
    if 1.0 < similarity <= 1.0 + _SIMILARITY_EPSILON:
        return 1.0
    if -1.0 - _SIMILARITY_EPSILON <= similarity < -1.0:
        return -1.0
    if similarity < -1.0 or similarity > 1.0:
        raise ProductSearchExecutionError(
            "Product search returned an invalid similarity value."
        )
    return similarity


def _embedding_client_from_settings() -> JinaEmbeddingClient:
    settings = get_settings()
    if settings.jina_api_key is None:
        raise ProductSearchExecutionError("Product search is not configured.")
    return JinaEmbeddingClient(
        api_key=settings.jina_api_key.get_secret_value(),
        base_url=settings.jina_base_url,
        model=settings.jina_embedding_model,
        dimensions=settings.jina_embedding_dimensions,
        timeout_seconds=settings.jina_timeout_seconds,
        max_retries=settings.jina_max_retries,
    )


async def _search_rows(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    query_vector: list[float],
    request: CustomerRequest,
    limit: int,
) -> list[Any]:
    settings = get_settings()
    distance = ProductEmbedding.embedding.cosine_distance(query_vector)
    variant_rank = func.row_number().over(
        partition_by=Product.id,
        order_by=(distance.asc(), ProductVariant.id.asc()),
    )
    conditions = [
        Product.active.is_(True),
        ProductVariant.active.is_(True),
        Inventory.stock_quantity > 0,
        ProductEmbedding.provider == PROVIDER_JINA,
        ProductEmbedding.model == settings.jina_embedding_model,
        ProductEmbedding.dimensions == settings.jina_embedding_dimensions,
    ]
    if request.budget_max_kzt is not None:
        conditions.append(ProductVariant.price_kzt <= request.budget_max_kzt)
    if request.quantity is not None:
        conditions.append(Inventory.stock_quantity >= request.quantity)
    if request.category is not None:
        conditions.append(func.lower(Product.category) == func.lower(request.category))

    ranked = (
        select(
            Product.id.label("product_id"),
            Product.name.label("name"),
            Product.category.label("category"),
            Product.description.label("description"),
            ProductVariant.merchant_sku.label("merchant_sku"),
            ProductVariant.material.label("material"),
            ProductVariant.price_kzt.label("price_kzt"),
            Inventory.stock_quantity.label("available_quantity"),
            distance.label("cosine_distance"),
            variant_rank.label("variant_rank"),
        )
        .join(ProductVariant, ProductVariant.product_id == Product.id)
        .join(Inventory, Inventory.product_variant_id == ProductVariant.id)
        .join(ProductEmbedding, ProductEmbedding.product_id == Product.id)
        .where(*conditions)
        .subquery()
    )
    statement = (
        select(ranked)
        .where(ranked.c.variant_rank == 1)
        .order_by(ranked.c.cosine_distance.asc(), ranked.c.product_id.asc())
        .limit(limit)
    )
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()
    return list(rows)


async def search_products(
    *,
    request: CustomerRequest,
    limit: int = 3,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: JinaEmbeddingClient | None = None,
) -> tuple[ProductSearchResult, ...]:
    """Return up to ``limit`` available products ranked by cosine similarity."""
    if request.intent != "product_search":
        return ()
    if not 1 <= limit <= MAX_LIMIT:
        raise ProductSearchValidationError(
            f"limit must be between 1 and {MAX_LIMIT}."
        )

    settings = get_settings()
    own_client = embedding_client is None
    if embedding_client is None:
        embedding_client = _embedding_client_from_settings()

    try:
        embedding = await embedding_client.embed_query(build_query_text(request))
    except JinaEmbeddingError as exc:
        raise ProductSearchExecutionError("Product search embedding failed.") from exc

    if embedding.dimensions != settings.jina_embedding_dimensions:
        raise ProductSearchExecutionError(
            "Product search embedding dimension is invalid."
        )

    resolved_factory = (
        session_factory if session_factory is not None else _default_session_factory
    )
    try:
        rows = await _search_rows(
            session_factory=resolved_factory,
            query_vector=list(embedding.vector),
            request=request,
            limit=limit,
        )
    except Exception as exc:
        raise ProductSearchExecutionError("Product search failed.") from exc
    finally:
        if own_client:
            await embedding_client.aclose()

    results: list[ProductSearchResult] = []
    for row in rows:
        results.append(
            ProductSearchResult(
                product_id=row.product_id,
                name=row.name,
                category=row.category,
                description=row.description,
                price_kzt=row.price_kzt,
                available_quantity=row.available_quantity,
                merchant_sku=row.merchant_sku,
                material=row.material,
                similarity=_clamp_similarity(row.cosine_distance),
            )
        )
    return tuple(results)

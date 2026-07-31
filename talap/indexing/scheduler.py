from __future__ import annotations

from collections.abc import Collection
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from talap.db.models import ProductIndexingTask, ProductIndexingTaskStatus
from talap.indexing.decisions import SEMANTIC_PRODUCT_FIELDS


async def schedule_product_indexing(
    *,
    session: AsyncSession,
    merchant_id: UUID,
    product_id: UUID,
    changed_fields: Collection[str],
) -> ProductIndexingTask | None:
    """Create one PENDING task in the caller transaction when semantic fields changed.

    Returns ``None`` (and inserts nothing) when no semantic field is present.
    Never commits, never opens a new session, and participates in the caller's
    transaction: if that transaction rolls back, the task rolls back with it.
    """
    semantic_fields = sorted(
        {
            field_name
            for field_name in changed_fields
            if field_name in SEMANTIC_PRODUCT_FIELDS
        }
    )
    if not semantic_fields:
        return None

    task = ProductIndexingTask(
        id=uuid4(),
        merchant_id=merchant_id,
        product_id=product_id,
        status=ProductIndexingTaskStatus.PENDING,
        changed_fields=semantic_fields,
        attempts=0,
    )
    session.add(task)
    return task

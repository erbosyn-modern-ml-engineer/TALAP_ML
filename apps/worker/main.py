"""Background-worker entrypoint for the product-indexing lifecycle.

T-017B1 exposes a one-shot claim function for diagnostics only; an ARQ loop
and an embedding processor arrive in later tasks. Nothing in this module runs
at import time, and it is NOT a complete indexing worker.
"""

from __future__ import annotations

from datetime import timedelta

from talap.db import async_session_factory
from talap.indexing.worker import claim_indexing_tasks

_DEFAULT_STALE_AFTER = timedelta(minutes=5)


async def run_indexing_claim_once(limit: int = 10) -> int:
    """Claim up to ``limit`` due indexing tasks and return how many were claimed.

    DIAGNOSTIC ONLY - this is not a production processing loop and must never
    run at import time. No embedding processor exists yet (DeepSeek has no
    confirmed embedding contract), so claimed tasks are intentionally left
    PROCESSING and will be reclaimed by a future real worker once stale.
    """
    claimed = await claim_indexing_tasks(
        session_factory=async_session_factory,
        limit=limit,
        stale_after=_DEFAULT_STALE_AFTER,
    )
    return len(claimed)
"""Background-worker entrypoints (one-shot, no loop).

Nothing in this module runs at import time. ARQ/scheduler loops arrive in
later tasks; MVP workers expose one-shot functions for manual execution and
integration tests.
"""

from __future__ import annotations

from datetime import timedelta

from apps.worker.jobs.whatsapp_echo import (
    EchoProcessingResult,
    run_whatsapp_echo_once,
)
from talap.db import async_session_factory
from talap.indexing.worker import claim_indexing_tasks

_DEFAULT_STALE_AFTER = timedelta(minutes=5)

__all__ = [
    "EchoProcessingResult",
    "run_indexing_claim_once",
    "run_whatsapp_echo_once",
]


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
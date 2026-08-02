from talap.indexing.decisions import SEMANTIC_PRODUCT_FIELDS, semantic_changed_fields
from talap.indexing.documents import build_product_index_text
from talap.indexing.processor import (
    IndexingProcessResult,
    IndexingProcessStatus,
    process_claimed_indexing_task,
)
from talap.indexing.scheduler import schedule_product_indexing
from talap.indexing.worker import (
    ClaimedIndexingTask,
    IndexingTaskNotFoundError,
    InvalidIndexingTaskTransitionError,
    StaleIndexingTaskClaimError,
    claim_indexing_tasks,
    decide_failure_outcome,
    mark_indexing_task_completed,
    mark_indexing_task_failed,
    sanitize_error_message,
    validate_claim_limit,
    validate_expected_attempt,
    validate_failure_inputs,
)

__all__ = [
    "ClaimedIndexingTask",
    "IndexingProcessResult",
    "IndexingProcessStatus",
    "IndexingTaskNotFoundError",
    "InvalidIndexingTaskTransitionError",
    "SEMANTIC_PRODUCT_FIELDS",
    "StaleIndexingTaskClaimError",
    "build_product_index_text",
    "claim_indexing_tasks",
    "decide_failure_outcome",
    "mark_indexing_task_completed",
    "mark_indexing_task_failed",
    "process_claimed_indexing_task",
    "sanitize_error_message",
    "schedule_product_indexing",
    "semantic_changed_fields",
    "validate_claim_limit",
    "validate_expected_attempt",
    "validate_failure_inputs",
]

from __future__ import annotations

from datetime import timedelta

import pytest

from talap.db.models import ProductIndexingTaskStatus
from talap.indexing.worker import (
    decide_failure_outcome,
    sanitize_error_message,
    validate_claim_limit,
    validate_expected_attempt,
    validate_failure_inputs,
)


def test_claim_limit_accepts_boundaries() -> None:
    validate_claim_limit(1)
    validate_claim_limit(100)


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_claim_limit_rejects_out_of_range(limit: int) -> None:
    with pytest.raises(ValueError):
        validate_claim_limit(limit)


def test_expected_attempt_accepts_positive_values() -> None:
    validate_expected_attempt(1)
    validate_expected_attempt(100)


@pytest.mark.parametrize("attempt", [0, -1])
def test_expected_attempt_rejects_zero_and_negative(attempt: int) -> None:
    with pytest.raises(ValueError):
        validate_expected_attempt(attempt)


def test_sanitize_strips_surrounding_whitespace() -> None:
    assert sanitize_error_message("  boom  \n") == "boom"


def test_sanitize_blank_message_becomes_safe_default() -> None:
    assert sanitize_error_message("   ") == "Indexing task failed."
    assert sanitize_error_message("\n\t ") == "Indexing task failed."


def test_sanitize_truncates_to_500_characters() -> None:
    result = sanitize_error_message("x" * 900)
    assert len(result) == 500


def test_sanitize_only_strips_outer_whitespace_of_multiline_input() -> None:
    # sanitize_error_message does NOT redact secrets or parse tracebacks; it
    # only strips outer whitespace, defaults blank input, and truncates.
    message = "  line one\nline two  "
    assert sanitize_error_message(message) == "line one\nline two"


def test_retry_decision_below_max_attempts() -> None:
    assert (
        decide_failure_outcome(attempts=1, max_attempts=3)
        == ProductIndexingTaskStatus.PENDING
    )


def test_retry_decision_at_max_minus_one() -> None:
    assert (
        decide_failure_outcome(attempts=2, max_attempts=3)
        == ProductIndexingTaskStatus.PENDING
    )


def test_permanent_failure_at_equal_max_attempts() -> None:
    assert (
        decide_failure_outcome(attempts=3, max_attempts=3)
        == ProductIndexingTaskStatus.FAILED
    )


def test_permanent_failure_above_max_attempts() -> None:
    assert (
        decide_failure_outcome(attempts=4, max_attempts=3)
        == ProductIndexingTaskStatus.FAILED
    )


def test_failure_inputs_accept_boundaries() -> None:
    validate_failure_inputs(max_attempts=1, retry_delay=timedelta(0))


def test_failure_inputs_reject_max_attempts_below_one() -> None:
    with pytest.raises(ValueError):
        validate_failure_inputs(max_attempts=0, retry_delay=timedelta(0))


def test_failure_inputs_reject_negative_retry_delay() -> None:
    with pytest.raises(ValueError):
        validate_failure_inputs(max_attempts=1, retry_delay=timedelta(seconds=-1))

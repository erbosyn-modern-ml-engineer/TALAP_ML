"""Unit tests for the deterministic ingestion fingerprints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from talap.ingestion import payload_sha256, whatsapp_status_fingerprint

_OCCURRED = datetime(2026, 7, 2, 20, 0, 3, tzinfo=UTC)


def test_payload_hash_matches_hashlib_exact_bytes() -> None:
    body = b'{"object": "whatsapp_business_account"}'
    assert payload_sha256(body) == hashlib.sha256(body).hexdigest()


def test_payload_one_byte_difference_changes_hash() -> None:
    assert payload_sha256(b"hello") != payload_sha256(b"hellp")


def test_payload_empty_body_rejected() -> None:
    with pytest.raises(ValueError):
        payload_sha256(b"")
    with pytest.raises(ValueError):
        payload_sha256(b"   ")


def test_status_fingerprint_deterministic() -> None:
    kwargs = {
        "external_message_id": "wamid.SYNTHETIC_1",
        "recipient_id": "77000000001",
        "status": "delivered",
        "occurred_at": _OCCURRED,
        "error_codes": (),
    }
    assert whatsapp_status_fingerprint(**kwargs) == whatsapp_status_fingerprint(
        **kwargs
    )


def test_status_fingerprint_is_key_order_independent() -> None:
    # sort_keys=True in the canonical JSON means key insertion order cannot
    # affect the fingerprint.
    canonical = {
        "status": "read",
        "recipient_id": "77000000001",
        "occurred_at": _OCCURRED.astimezone(UTC).isoformat(),
        "external_message_id": "wamid.SYNTHETIC_2",
        "error_codes": [1, 2],
    }
    raw = json.dumps(
        canonical, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected = hashlib.sha256(raw).hexdigest()
    actual = whatsapp_status_fingerprint(
        external_message_id="wamid.SYNTHETIC_2",
        recipient_id="77000000001",
        status="read",
        occurred_at=_OCCURRED,
        error_codes=[1, 2],
    )
    assert actual == expected


def test_status_fingerprint_error_code_order_matters() -> None:
    a = whatsapp_status_fingerprint(
        external_message_id="wamid.SYNTHETIC_3",
        recipient_id="77000000001",
        status="failed",
        occurred_at=_OCCURRED,
        error_codes=[1, 2],
    )
    b = whatsapp_status_fingerprint(
        external_message_id="wamid.SYNTHETIC_3",
        recipient_id="77000000001",
        status="failed",
        occurred_at=_OCCURRED,
        error_codes=[2, 1],
    )
    assert a != b


def test_status_fingerprint_non_utc_normalizes_to_utc() -> None:
    non_utc = whatsapp_status_fingerprint(
        external_message_id="wamid.SYNTHETIC_4",
        recipient_id="77000000001",
        status="sent",
        occurred_at=datetime(
            2026, 7, 3, 1, 0, 3, tzinfo=timezone(timedelta(hours=5))
        ),
        error_codes=(),
    )
    utc = whatsapp_status_fingerprint(
        external_message_id="wamid.SYNTHETIC_4",
        recipient_id="77000000001",
        status="sent",
        occurred_at=_OCCURRED,
        error_codes=(),
    )
    assert non_utc == utc


def test_status_fingerprint_naive_occurred_at_rejected() -> None:
    with pytest.raises(ValueError):
        whatsapp_status_fingerprint(
            external_message_id="wamid.SYNTHETIC_5",
            recipient_id="77000000001",
            status="sent",
            occurred_at=datetime(2026, 7, 2, 20, 0, 3),
            error_codes=(),
        )


def test_status_fingerprint_bool_error_code_rejected() -> None:
    with pytest.raises(ValueError):
        whatsapp_status_fingerprint(
            external_message_id="wamid.SYNTHETIC_6",
            recipient_id="77000000001",
            status="failed",
            occurred_at=_OCCURRED,
            error_codes=[True],
        )


def test_status_fingerprint_negative_error_code_rejected() -> None:
    with pytest.raises(ValueError):
        whatsapp_status_fingerprint(
            external_message_id="wamid.SYNTHETIC_7",
            recipient_id="77000000001",
            status="failed",
            occurred_at=_OCCURRED,
            error_codes=[-1],
        )


@pytest.mark.parametrize(
    "field",
    ["external_message_id", "recipient_id", "status"],
)
def test_status_fingerprint_empty_identifiers_rejected(field: str) -> None:
    kwargs: dict[str, object] = {
        "external_message_id": "wamid.SYNTHETIC_8",
        "recipient_id": "77000000001",
        "status": "read",
        "occurred_at": _OCCURRED,
        "error_codes": (),
    }
    kwargs[field] = "   "
    with pytest.raises(ValueError):
        whatsapp_status_fingerprint(**kwargs)


def test_status_fingerprint_output_is_64_lowercase_hex() -> None:
    digest = whatsapp_status_fingerprint(
        external_message_id="wamid.SYNTHETIC_9",
        recipient_id="77000000001",
        status="deleted",
        occurred_at=_OCCURRED,
        error_codes=[131047],
    )
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)

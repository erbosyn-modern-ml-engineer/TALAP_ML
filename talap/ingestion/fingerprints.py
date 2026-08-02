"""Deterministic fingerprint helpers for TALAP inbound ingestion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime


def payload_sha256(raw_body: bytes) -> str:
    """SHA-256 of the exact raw webhook body bytes (lowercase hex)."""
    if not isinstance(raw_body, bytes):
        raise ValueError("raw_body must be bytes.")
    if not raw_body or not raw_body.strip():
        raise ValueError("raw_body must not be empty.")
    return hashlib.sha256(raw_body).hexdigest()


def whatsapp_status_fingerprint(
    *,
    external_message_id: str,
    recipient_id: str,
    status: str,
    occurred_at: datetime,
    error_codes: Sequence[int],
) -> str:
    """Deterministic SHA-256 fingerprint of a normalized WhatsApp status.

    Uses compact canonical JSON with sorted keys, UTC-normalized ISO-8601
    timestamps, and preserved error-code order. No ``repr``, no locale or
    current-time dependence.
    """
    if not isinstance(external_message_id, str) or not external_message_id.strip():
        raise ValueError("external_message_id must be a non-empty string.")
    if not isinstance(recipient_id, str) or not recipient_id.strip():
        raise ValueError("recipient_id must be a non-empty string.")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string.")
    if occurred_at.tzinfo is None or occurred_at.tzinfo.utcoffset(occurred_at) is None:
        raise ValueError("occurred_at must be timezone-aware.")

    codes: list[int] = []
    for code in error_codes:
        if isinstance(code, bool) or not isinstance(code, int):
            raise ValueError("error_codes entries must be integers.")
        if code < 0:
            raise ValueError("error_codes entries must be non-negative.")
        codes.append(code)

    canonical = {
        "external_message_id": external_message_id.strip(),
        "recipient_id": recipient_id.strip(),
        "status": status.strip(),
        "occurred_at": occurred_at.astimezone(UTC).isoformat(),
        "error_codes": codes,
    }
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

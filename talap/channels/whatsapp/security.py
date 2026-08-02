"""WhatsApp webhook security helpers (Meta challenge + x-hub-signature-256).

Pure helpers: safe token comparison with ``hmac.compare_digest`` and HMAC
SHA-256 signature verification over the exact raw body bytes. Secrets and
signatures are never included in exception messages.
"""

from __future__ import annotations

import hashlib
import hmac
import re

_SIGNATURE_PATTERN = re.compile(r"^sha256=[0-9a-f]{64}$")


class WhatsAppWebhookSecurityError(ValueError):
    """Raised when the supplied signature input is malformed."""


def verify_whatsapp_challenge_token(
    *,
    provided_token: str,
    expected_token: str,
) -> bool:
    """Constant-time comparison of the Meta verification token."""
    if not isinstance(provided_token, str) or not isinstance(expected_token, str):
        return False
    return hmac.compare_digest(provided_token, expected_token)


def verify_whatsapp_signature(
    *,
    raw_body: bytes,
    signature_header: str,
    app_secret: str,
) -> bool:
    """Verify ``x-hub-signature-256`` against the exact raw body bytes.

    Raises ``WhatsAppWebhookSecurityError`` for missing/malformed inputs and
    returns ``False`` for a well-formed but mismatched signature.
    """
    if (
        not isinstance(raw_body, bytes)
        or not isinstance(signature_header, str)
        or not isinstance(app_secret, str)
    ):
        raise WhatsAppWebhookSecurityError(
            "Invalid WhatsApp webhook signature input."
        )
    if not _SIGNATURE_PATTERN.fullmatch(signature_header):
        raise WhatsAppWebhookSecurityError("Invalid WhatsApp webhook signature.")
    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(provided, expected)

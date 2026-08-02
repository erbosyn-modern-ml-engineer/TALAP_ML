"""Unit tests for WhatsApp webhook security helpers."""

from __future__ import annotations

import hashlib
import hmac

import pytest

import talap.channels.whatsapp.security as security_module
from talap.channels.whatsapp import (
    WhatsAppWebhookSecurityError,
    verify_whatsapp_challenge_token,
    verify_whatsapp_signature,
)

_APP_SECRET = "wa-app-secret-123"
_BODY = b'{"object":"whatsapp_business_account"}'


def _signature(raw_body: bytes, secret: str = _APP_SECRET) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def test_correct_verify_token_accepted() -> None:
    assert (
        verify_whatsapp_challenge_token(
            provided_token="wa-verify-token",
            expected_token="wa-verify-token",
        )
        is True
    )


def test_wrong_verify_token_rejected() -> None:
    assert (
        verify_whatsapp_challenge_token(
            provided_token="wrong-token",
            expected_token="wa-verify-token",
        )
        is False
    )


def test_exact_raw_body_signature_accepted() -> None:
    assert (
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header=_signature(_BODY),
            app_secret=_APP_SECRET,
        )
        is True
    )


def test_changed_body_rejected() -> None:
    header = _signature(_BODY)
    changed = b'{"object":"whatsapp_business_account","x":1}'
    assert (
        verify_whatsapp_signature(
            raw_body=changed,
            signature_header=header,
            app_secret=_APP_SECRET,
        )
        is False
    )


def test_wrong_app_secret_rejected() -> None:
    header = _signature(_BODY, secret="different-secret")
    assert (
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header=header,
            app_secret=_APP_SECRET,
        )
        is False
    )


def test_missing_sha256_prefix_rejected() -> None:
    digest = hmac.new(
        _APP_SECRET.encode("utf-8"), _BODY, hashlib.sha256
    ).hexdigest()
    with pytest.raises(WhatsAppWebhookSecurityError):
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header=f"sha512={digest}",
            app_secret=_APP_SECRET,
        )
    with pytest.raises(WhatsAppWebhookSecurityError):
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header=digest,
            app_secret=_APP_SECRET,
        )


def test_malformed_hex_rejected() -> None:
    with pytest.raises(WhatsAppWebhookSecurityError):
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header="sha256=not-a-hex",
            app_secret=_APP_SECRET,
        )
    with pytest.raises(WhatsAppWebhookSecurityError):
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header="sha256=" + "z" * 64,
            app_secret=_APP_SECRET,
        )
    with pytest.raises(WhatsAppWebhookSecurityError):
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header="sha256=" + "a" * 63,
            app_secret=_APP_SECRET,
        )


def test_secrets_never_appear_in_errors() -> None:
    secret = "super-secret-app-secret"
    try:
        verify_whatsapp_signature(
            raw_body=_BODY,
            signature_header="sha256=bad",
            app_secret=secret,
        )
    except WhatsAppWebhookSecurityError as exc:
        assert secret not in str(exc)
    # Challenge comparison is safe and never raises with the values.
    assert (
        verify_whatsapp_challenge_token(
            provided_token="a-secret-token", expected_token="another-token"
        )
        is False
    )


def test_compare_digest_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real_compare = hmac.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(security_module.hmac, "compare_digest", _spy)

    verify_whatsapp_challenge_token(
        provided_token="token-a", expected_token="token-a"
    )
    assert len(calls) == 1
    left, right = calls[-1]
    assert left == "token-a" and right == "token-a"

    header = _signature(_BODY)
    digest = header.split("=", 1)[1]
    verify_whatsapp_signature(
        raw_body=_BODY, signature_header=header, app_secret=_APP_SECRET
    )
    # Challenge compare + one signature compare.
    assert len(calls) == 2
    left, right = calls[-1]
    assert left == digest
    assert right == digest

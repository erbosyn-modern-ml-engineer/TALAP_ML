"""Unit tests for Telegram webhook secret helpers."""

from __future__ import annotations

import hashlib
import hmac

import pytest

import talap.channels.telegram.security as security_module
from talap.channels.telegram import (
    TelegramWebhookSecretError,
    telegram_webhook_secret_sha256,
    validate_telegram_webhook_secret,
    verify_telegram_webhook_secret,
)

_SECRET = "tg-test-secret-123"


def test_valid_minimum_secret() -> None:
    assert validate_telegram_webhook_secret("a") == "a"


def test_valid_256_character_secret() -> None:
    secret = "A1-" * 85 + "a"
    assert len(secret) == 256
    assert validate_telegram_webhook_secret(secret) == secret


def test_empty_secret_rejected() -> None:
    with pytest.raises(TelegramWebhookSecretError):
        validate_telegram_webhook_secret("")


def test_257_character_secret_rejected() -> None:
    with pytest.raises(TelegramWebhookSecretError):
        validate_telegram_webhook_secret("a" * 257)


def test_whitespace_rejected() -> None:
    with pytest.raises(TelegramWebhookSecretError):
        validate_telegram_webhook_secret("abc def")
    with pytest.raises(TelegramWebhookSecretError):
        validate_telegram_webhook_secret("  abc  ")


def test_punctuation_outside_allowed_rejected() -> None:
    for ch in "!@#$%^&*()+=[]{}|;:.,<>?/~`'\\\"":
        with pytest.raises(TelegramWebhookSecretError):
            validate_telegram_webhook_secret(f"ab{ch}cd")


def test_non_string_secret_rejected() -> None:
    with pytest.raises(TelegramWebhookSecretError):
        validate_telegram_webhook_secret(123)  # type: ignore[arg-type]


def test_hash_equals_hashlib_exact_bytes() -> None:
    assert telegram_webhook_secret_sha256(_SECRET) == hashlib.sha256(
        _SECRET.encode("utf-8")
    ).hexdigest()


def test_same_secret_verifies() -> None:
    digest = telegram_webhook_secret_sha256(_SECRET)
    assert (
        verify_telegram_webhook_secret(
            provided_secret=_SECRET, expected_sha256=digest
        )
        is True
    )


def test_different_secret_fails() -> None:
    digest = telegram_webhook_secret_sha256(_SECRET)
    assert (
        verify_telegram_webhook_secret(
            provided_secret="a-different-secret", expected_sha256=digest
        )
        is False
    )


def test_uppercase_and_lowercase_remain_distinct() -> None:
    assert telegram_webhook_secret_sha256("Token") != telegram_webhook_secret_sha256(
        "token"
    )


def test_malformed_expected_hash_rejected() -> None:
    with pytest.raises(TelegramWebhookSecretError):
        verify_telegram_webhook_secret(
            provided_secret=_SECRET,
            expected_sha256="A" * 64,  # uppercase is not a valid stored hash
        )
    with pytest.raises(TelegramWebhookSecretError):
        verify_telegram_webhook_secret(
            provided_secret=_SECRET,
            expected_sha256="a" * 63,
        )
    with pytest.raises(TelegramWebhookSecretError):
        verify_telegram_webhook_secret(
            provided_secret=_SECRET,
            expected_sha256="z" * 64,
        )


def test_no_secret_appears_in_exception_strings() -> None:
    secret = _SECRET
    try:
        validate_telegram_webhook_secret(secret + "!")
    except TelegramWebhookSecretError as exc:
        assert secret not in str(exc)
    try:
        verify_telegram_webhook_secret(
            provided_secret=secret,
            expected_sha256="a" * 63,
        )
    except TelegramWebhookSecretError as exc:
        assert secret not in str(exc)


def test_helpers_do_not_mutate_input() -> None:
    secret = _SECRET
    snapshot = secret
    validate_telegram_webhook_secret(secret)
    telegram_webhook_secret_sha256(secret)
    verify_telegram_webhook_secret(
        provided_secret=secret,
        expected_sha256=telegram_webhook_secret_sha256(secret),
    )
    assert secret == snapshot


def test_comparison_uses_digest_not_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    real_compare_digest = hmac.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(security_module.hmac, "compare_digest", _spy)
    digest = telegram_webhook_secret_sha256(_SECRET)
    assert (
        verify_telegram_webhook_secret(
            provided_secret=_SECRET, expected_sha256=digest
        )
        is True
    )
    assert len(calls) == 1
    left, right = calls[0]
    # Both sides are digests, never the plaintext secret.
    assert _SECRET not in (left, right)
    assert left == digest
    assert right == digest

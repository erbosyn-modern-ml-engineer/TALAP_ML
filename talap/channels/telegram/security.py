"""Telegram webhook security helpers and verification service.

Pure helpers validate and hash the official Telegram ``secret_token``
(``X-Telegram-Bot-Api-Secret-Token``) and compare digests with
``hmac.compare_digest``. The async verifier loads the channel connection and
its one-to-one webhook config and maps every verification failure to one
safe authentication error.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.db import async_session_factory
from talap.db.models import ChannelConnection, TelegramWebhookConfig

_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_INVALID_CREDENTIALS = "Invalid Telegram webhook credentials."


class TelegramWebhookSecretError(ValueError):
    """Raised when a Telegram webhook secret (or stored hash) is invalid."""


class TelegramWebhookAuthenticationError(RuntimeError):
    """Raised when webhook verification fails; all modes look identical."""


class TelegramWebhookServiceUnavailableError(RuntimeError):
    """Raised when verification cannot complete due to an unexpected failure."""


def validate_telegram_webhook_secret(secret: str) -> str:
    """Validate the official secret token and return it unchanged.

    Trimming is intentionally NOT performed: the exact supplied token is the
    token. Only ``[A-Za-z0-9_-]`` with length 1-256 is accepted.
    """
    if not isinstance(secret, str):
        raise TelegramWebhookSecretError("Telegram webhook secret is invalid.")
    if not _SECRET_PATTERN.fullmatch(secret):
        raise TelegramWebhookSecretError("Telegram webhook secret is invalid.")
    return secret


def telegram_webhook_secret_sha256(secret: str) -> str:
    """Return the lowercase SHA-256 hex digest of the validated secret."""
    validate_telegram_webhook_secret(secret)
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_telegram_webhook_secret(
    *,
    provided_secret: str,
    expected_sha256: str,
) -> bool:
    """Constant-time digest comparison; never compares plaintext secrets."""
    validate_telegram_webhook_secret(provided_secret)
    if not isinstance(expected_sha256, str) or not _HASH_PATTERN.fullmatch(
        expected_sha256
    ):
        raise TelegramWebhookSecretError("Telegram webhook secret hash is invalid.")
    provided_hash = telegram_webhook_secret_sha256(provided_secret)
    return hmac.compare_digest(provided_hash, expected_sha256)


async def verify_telegram_webhook_request(
    *,
    connection_id: UUID,
    provided_secret: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Verify a webhook request's connection and secret.

    Every verification failure raises ``TelegramWebhookAuthenticationError``
    (same message for missing/invalid secret, unknown/inactive/wrong-channel
    connection, or missing/mismatched config). Unexpected DB failures raise
    ``TelegramWebhookServiceUnavailableError``.
    """
    if provided_secret is None:
        raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS)
    try:
        validate_telegram_webhook_secret(provided_secret)
    except TelegramWebhookSecretError:
        raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS) from None

    factory = session_factory or async_session_factory
    async with factory() as session:
        try:
            connection = await session.get(ChannelConnection, connection_id)
            if connection is None:
                raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS)
            if not connection.active:
                raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS)
            if connection.channel != "telegram":
                raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS)
            config = await session.get(TelegramWebhookConfig, connection_id)
            if config is None:
                raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS)
        except TelegramWebhookAuthenticationError:
            raise
        except Exception as exc:
            raise TelegramWebhookServiceUnavailableError(
                "Telegram webhook verification is temporarily unavailable."
            ) from exc

    try:
        verified = verify_telegram_webhook_secret(
            provided_secret=provided_secret,
            expected_sha256=config.webhook_secret_sha256,
        )
    except TelegramWebhookSecretError as exc:
        raise TelegramWebhookServiceUnavailableError(
            "Telegram webhook verification is temporarily unavailable."
        ) from exc
    if not verified:
        raise TelegramWebhookAuthenticationError(_INVALID_CREDENTIALS)

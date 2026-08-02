"""Telegram webhook payload normalizer for TALAP.

Converts Telegram Update dictionaries into the platform-neutral
``NormalizedInboundMessage`` contract from ``talap.channels.inbound``.

The function is synchronous and pure: it never performs I/O, never reads the
environment, never looks up the current time, and never mutates the input.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import ValidationError

from talap.channels.inbound import (
    InboundMessageType,
    MediaReference,
    NormalizedInboundMessage,
)


class TelegramNormalizationError(ValueError):
    """Raised when a Telegram update cannot be normalized."""


def _optional_string(value: object, label: str) -> str | None:
    """Return a present string or None; reject present non-strings."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TelegramNormalizationError(f"{label} must be a string.")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    """Return a present int or None; reject bools and other non-ints."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelegramNormalizationError(f"{label} must be an integer.")
    return value


def _build_media(
    *,
    external_media_id: str,
    mime_type: str | None,
    size_bytes: int | None,
    duration_seconds: int | None,
) -> MediaReference:
    try:
        return MediaReference(
            external_media_id=external_media_id,
            mime_type=mime_type,
            size_bytes=size_bytes,
            duration_seconds=duration_seconds,
        )
    except ValidationError as exc:
        raise TelegramNormalizationError(
            "Telegram update produced invalid media metadata."
        ) from exc


def _build_message(
    *,
    external_chat_id: str,
    external_user_id: str,
    external_message_id: str,
    message_type: InboundMessageType,
    text: str | None,
    media: MediaReference | None,
    received_at: datetime,
) -> NormalizedInboundMessage:
    try:
        return NormalizedInboundMessage(
            business_scope="talap_global",
            channel="telegram",
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type=message_type,
            text=text,
            media=media,
            received_at=received_at,
        )
    except ValidationError as exc:
        raise TelegramNormalizationError(
            "Telegram update produced an invalid normalized message."
        ) from exc


def _resolve_callback_chat_id(callback_query: Mapping[str, object]) -> str | None:
    """Resolve the chat identity for a callback query.

    Prefers ``callback_query.message.chat.id`` and falls back to
    ``callback_query.chat_instance``.
    """
    message = callback_query.get("message")
    if isinstance(message, Mapping):
        chat = message.get("chat")
        if isinstance(chat, Mapping) and chat.get("id") is not None:
            return str(chat["id"])
    # chat_instance is an opaque string per the Bot API; do not coerce other
    # types (ints, bools, lists, mappings) into a chat identity.
    chat_instance = callback_query.get("chat_instance")
    if isinstance(chat_instance, str):
        candidate = chat_instance.strip()
        if candidate:
            return candidate
    return None


def _normalize_message(
    message: Mapping[str, object],
    received_at: datetime,
) -> NormalizedInboundMessage:
    message_id = message.get("message_id")
    if message_id is None or (isinstance(message_id, str) and not message_id.strip()):
        raise TelegramNormalizationError("message.message_id is required.")
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or chat.get("id") is None:
        raise TelegramNormalizationError("message.chat.id is required.")
    sender = message.get("from")
    if not isinstance(sender, Mapping) or sender.get("id") is None:
        raise TelegramNormalizationError("message.from.id is required.")

    chat_id = str(chat["id"])
    user_id = str(sender["id"])
    # Telegram message_id is only unique within one chat, so the canonical
    # external identity must include the chat id.
    external_message_id = f"message:{chat_id}:{message_id}"

    # Deterministic priority: non-blank text, then voice, then unsupported.
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return _build_message(
            external_chat_id=chat_id,
            external_user_id=user_id,
            external_message_id=external_message_id,
            message_type="text",
            text=text,
            media=None,
            received_at=received_at,
        )

    voice = message.get("voice")
    if voice is not None:
        if not isinstance(voice, Mapping):
            raise TelegramNormalizationError("voice must be a mapping.")
        file_id = voice.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise TelegramNormalizationError("voice.file_id is required.")
        media = _build_media(
            external_media_id=file_id,
            mime_type=_optional_string(voice.get("mime_type"), "voice.mime_type"),
            size_bytes=_optional_integer(
                voice.get("file_size"), "voice.file_size"
            ),
            duration_seconds=_optional_integer(
                voice.get("duration"), "voice.duration"
            ),
        )
        return _build_message(
            external_chat_id=chat_id,
            external_user_id=user_id,
            external_message_id=external_message_id,
            message_type="voice",
            text=None,
            media=media,
            received_at=received_at,
        )

    return _build_message(
        external_chat_id=chat_id,
        external_user_id=user_id,
        external_message_id=external_message_id,
        message_type="unsupported",
        text=None,
        media=None,
        received_at=received_at,
    )


def _normalize_callback_query(
    callback_query: Mapping[str, object],
    received_at: datetime,
) -> NormalizedInboundMessage:
    callback_id = callback_query.get("id")
    if callback_id is None or (
        isinstance(callback_id, str) and not callback_id.strip()
    ):
        raise TelegramNormalizationError("callback_query.id is required.")
    sender = callback_query.get("from")
    if not isinstance(sender, Mapping) or sender.get("id") is None:
        raise TelegramNormalizationError("callback_query.from.id is required.")

    user_id = str(sender["id"])
    chat_id = _resolve_callback_chat_id(callback_query)
    if chat_id is None:
        raise TelegramNormalizationError(
            "callback_query has neither message.chat.id nor chat_instance."
        )

    # The callback query id is the event identity (not the original
    # message_id), so it becomes the canonical external_message_id.
    external_message_id = f"callback:{callback_id}"

    data = callback_query.get("data")
    if isinstance(data, str) and data.strip():
        return _build_message(
            external_chat_id=chat_id,
            external_user_id=user_id,
            external_message_id=external_message_id,
            message_type="text",
            text=data,
            media=None,
            received_at=received_at,
        )

    return _build_message(
        external_chat_id=chat_id,
        external_user_id=user_id,
        external_message_id=external_message_id,
        message_type="unsupported",
        text=None,
        media=None,
        received_at=received_at,
    )


def normalize_telegram_update(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
) -> NormalizedInboundMessage:
    """Normalize one Telegram Update into a ``NormalizedInboundMessage``.

    ``received_at`` is the moment TALAP received the webhook request; it is
    passed to the T-018 contract, which validates timezone awareness and
    normalizes it to UTC.
    """
    if not isinstance(payload, Mapping):
        raise TelegramNormalizationError("Payload must be a mapping.")
    if "message" in payload:
        message = payload["message"]
        if not isinstance(message, Mapping):
            raise TelegramNormalizationError("message must be a mapping.")
        return _normalize_message(message, received_at)
    if "callback_query" in payload:
        callback_query = payload["callback_query"]
        if not isinstance(callback_query, Mapping):
            raise TelegramNormalizationError("callback_query must be a mapping.")
        return _normalize_callback_query(callback_query, received_at)
    raise TelegramNormalizationError("Update has neither message nor callback_query.")

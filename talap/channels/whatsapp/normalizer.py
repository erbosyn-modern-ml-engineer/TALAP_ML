"""WhatsApp Cloud API webhook normalizer for TALAP.

Converts Meta webhook payloads into platform-neutral events: customer
messages become ``NormalizedInboundMessage`` (T-018) and delivery statuses
become ``WhatsAppDeliveryStatusEvent``. A single webhook may contain many
messages and statuses across multiple entries/changes; all are preserved in
deterministic order.

The function is synchronous and pure: it never performs I/O, never reads the
environment, never looks up the current time, and never mutates the input.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from talap.channels.inbound import (
    InboundMessageType,
    MediaReference,
    NormalizedInboundMessage,
)

META_OBJECT = "whatsapp_business_account"

_KNOWN_DELIVERY_STATUSES: frozenset[str] = frozenset(
    {"sent", "delivered", "read", "failed", "deleted"}
)


class WhatsAppNormalizationError(ValueError):
    """Raised when a WhatsApp webhook payload cannot be normalized."""


WhatsAppDeliveryStatus = Literal[
    "sent",
    "delivered",
    "read",
    "failed",
    "deleted",
    "unknown",
]


class WhatsAppDeliveryStatusEvent(BaseModel):
    """Platform-neutral delivery status event for a WhatsApp message."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    channel: Literal["whatsapp"] = "whatsapp"
    external_message_id: str
    recipient_id: str
    status: WhatsAppDeliveryStatus
    occurred_at: datetime
    error_codes: tuple[int, ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("occurred_at must be timezone-aware.")
        return v.astimezone(UTC)


class WhatsAppNormalizationResult(BaseModel):
    """Batch of normalized events produced from one Meta webhook payload."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    messages: tuple[NormalizedInboundMessage, ...] = ()
    statuses: tuple[WhatsAppDeliveryStatusEvent, ...] = ()


def _required_string(value: object, label: str) -> str:
    """Return a non-blank string or raise a deterministic error."""
    if not isinstance(value, str):
        raise WhatsAppNormalizationError(f"{label} must be a string.")
    stripped = value.strip()
    if not stripped:
        raise WhatsAppNormalizationError(f"{label} is required.")
    return stripped


def _optional_string(value: object, label: str) -> str | None:
    """Return a present string or None; reject present non-strings."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise WhatsAppNormalizationError(f"{label} must be a string.")
    return value


def _parse_unix_seconds_timestamp(value: object, label: str) -> datetime:
    """Strictly parse a Meta Unix-seconds decimal string into aware UTC."""
    if not isinstance(value, str):
        raise WhatsAppNormalizationError(f"{label} must be Unix seconds.")
    raw = value.strip()
    if not raw or not raw.isdigit():
        raise WhatsAppNormalizationError(f"{label} must be Unix seconds.")
    try:
        seconds = int(raw)
    except ValueError:
        raise WhatsAppNormalizationError(f"{label} must be Unix seconds.") from None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise WhatsAppNormalizationError(f"{label} must be Unix seconds.") from None


def _resolve_received_at(
    message: Mapping[str, object],
    label: str,
    received_at: datetime,
) -> datetime:
    # Absent timestamp falls back to received_at; a present null is malformed
    # and is rejected by the strict parser.
    if "timestamp" not in message:
        return received_at
    return _parse_unix_seconds_timestamp(message["timestamp"], label)


def _build_media(
    *,
    external_media_id: str,
    mime_type: str | None,
    checksum_sha256: str | None,
) -> MediaReference:
    try:
        return MediaReference(
            external_media_id=external_media_id,
            mime_type=mime_type,
            checksum_sha256=checksum_sha256,
        )
    except ValidationError as exc:
        raise WhatsAppNormalizationError(
            "WhatsApp message produced invalid media metadata."
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
            channel="whatsapp",
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type=message_type,
            text=text,
            media=media,
            received_at=received_at,
        )
    except ValidationError as exc:
        raise WhatsAppNormalizationError(
            "WhatsApp message violates the normalized inbound contract."
        ) from exc


def _build_status_event(
    *,
    external_message_id: str,
    recipient_id: str,
    status: WhatsAppDeliveryStatus,
    occurred_at: datetime,
    error_codes: tuple[int, ...],
) -> WhatsAppDeliveryStatusEvent:
    try:
        return WhatsAppDeliveryStatusEvent(
            external_message_id=external_message_id,
            recipient_id=recipient_id,
            status=status,
            occurred_at=occurred_at,
            error_codes=error_codes,
        )
    except ValidationError as exc:
        raise WhatsAppNormalizationError(
            "WhatsApp status violates the status contract."
        ) from exc


def _interactive_reply_message(
    *,
    external_chat_id: str,
    external_user_id: str,
    external_message_id: str,
    reply_id: str | None,
    title: str | None,
    received_at: datetime,
) -> NormalizedInboundMessage:
    # Stable reply id preferred over the display title; otherwise unsupported.
    if reply_id is not None and reply_id.strip():
        return _build_message(
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type="text",
            text=reply_id,
            media=None,
            received_at=received_at,
        )
    if title is not None and title.strip():
        return _build_message(
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type="text",
            text=title,
            media=None,
            received_at=received_at,
        )
    return _build_message(
        external_chat_id=external_chat_id,
        external_user_id=external_user_id,
        external_message_id=external_message_id,
        message_type="unsupported",
        text=None,
        media=None,
        received_at=received_at,
    )


def _normalize_customer_message(
    message: Mapping[str, object],
    received_at: datetime,
) -> NormalizedInboundMessage:
    external_message_id = _required_string(message.get("id"), "message.id")
    sender = _required_string(message.get("from"), "message.from")
    # In the current WhatsApp 1:1 chat model the sender WA id is both the
    # user identity and the chat identity.
    external_chat_id = sender
    external_user_id = sender

    received = _resolve_received_at(message, "message.timestamp", received_at)

    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type.strip():
        raise WhatsAppNormalizationError("message.type is required.")
    message_type = message_type.strip()

    if message_type == "text":
        text_object = message.get("text")
        if not isinstance(text_object, Mapping):
            raise WhatsAppNormalizationError("text must be a mapping.")
        body = text_object.get("body")
        if not isinstance(body, str):
            raise WhatsAppNormalizationError("text.body must be a string.")
        return _build_message(
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type="text",
            text=body,
            media=None,
            received_at=received,
        )

    if message_type == "audio":
        audio = message.get("audio")
        if not isinstance(audio, Mapping):
            raise WhatsAppNormalizationError("audio must be a mapping.")
        audio_id = _required_string(audio.get("id"), "audio.id")
        mime_type = _optional_string(audio.get("mime_type"), "audio.mime_type")
        sha256 = _optional_string(audio.get("sha256"), "audio.sha256")
        media = _build_media(
            external_media_id=audio_id,
            mime_type=mime_type,
            checksum_sha256=sha256,
        )
        return _build_message(
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type="voice",
            text=None,
            media=media,
            received_at=received,
        )

    if message_type == "interactive":
        interactive = message.get("interactive")
        if not isinstance(interactive, Mapping):
            raise WhatsAppNormalizationError("interactive must be a mapping.")
        interactive_type = interactive.get("type")
        if not isinstance(interactive_type, str):
            raise WhatsAppNormalizationError("interactive.type must be a string.")
        if interactive_type == "button_reply":
            reply = interactive.get("button_reply")
            if not isinstance(reply, Mapping):
                raise WhatsAppNormalizationError("button_reply must be a mapping.")
            return _interactive_reply_message(
                external_chat_id=external_chat_id,
                external_user_id=external_user_id,
                external_message_id=external_message_id,
                reply_id=_optional_string(reply.get("id"), "button_reply.id"),
                title=_optional_string(reply.get("title"), "button_reply.title"),
                received_at=received,
            )
        if interactive_type == "list_reply":
            reply = interactive.get("list_reply")
            if not isinstance(reply, Mapping):
                raise WhatsAppNormalizationError("list_reply must be a mapping.")
            return _interactive_reply_message(
                external_chat_id=external_chat_id,
                external_user_id=external_user_id,
                external_message_id=external_message_id,
                reply_id=_optional_string(reply.get("id"), "list_reply.id"),
                title=_optional_string(reply.get("title"), "list_reply.title"),
                received_at=received,
            )
        # Unknown interactive type: valid message identity, unsupported body.
        return _build_message(
            external_chat_id=external_chat_id,
            external_user_id=external_user_id,
            external_message_id=external_message_id,
            message_type="unsupported",
            text=None,
            media=None,
            received_at=received,
        )

    # image, video, document, location, contacts, sticker, reaction, etc.
    return _build_message(
        external_chat_id=external_chat_id,
        external_user_id=external_user_id,
        external_message_id=external_message_id,
        message_type="unsupported",
        text=None,
        media=None,
        received_at=received,
    )


def _extract_error_codes(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise WhatsAppNormalizationError("status.errors must be a list.")
    codes: list[int] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise WhatsAppNormalizationError(
                "status.errors entries must be mappings."
            )
        code = item.get("code")
        if code is None:
            raise WhatsAppNormalizationError("status.errors code is required.")
        if isinstance(code, bool) or not isinstance(code, int):
            raise WhatsAppNormalizationError(
                "status.errors code must be an integer."
            )
        codes.append(code)
    return tuple(codes)


def _normalize_status(
    status: Mapping[str, object],
    received_at: datetime,
) -> WhatsAppDeliveryStatusEvent:
    status_id = _required_string(status.get("id"), "status.id")
    recipient_id = _required_string(status.get("recipient_id"), "status.recipient_id")
    status_value = status.get("status")
    if not isinstance(status_value, str) or not status_value.strip():
        raise WhatsAppNormalizationError("status.status is required.")
    status_value = status_value.strip()
    if status_value in _KNOWN_DELIVERY_STATUSES:
        mapped: WhatsAppDeliveryStatus = status_value  # type: ignore[assignment]
    else:
        mapped = "unknown"

    occurred_at = _resolve_received_at(status, "status.timestamp", received_at)
    error_codes = _extract_error_codes(status.get("errors"))
    return _build_status_event(
        external_message_id=status_id,
        recipient_id=recipient_id,
        status=mapped,
        occurred_at=occurred_at,
        error_codes=error_codes,
    )


def normalize_whatsapp_webhook(
    payload: Mapping[str, object],
    *,
    received_at: datetime,
) -> WhatsAppNormalizationResult:
    """Normalize one Meta WhatsApp webhook payload into a batch of events.

    ``received_at`` is the moment TALAP received the webhook request; it is
    used as a fallback when a message/status carries no timestamp.
    """
    if not isinstance(payload, Mapping):
        raise WhatsAppNormalizationError("Payload must be a mapping.")
    if payload.get("object") != META_OBJECT:
        raise WhatsAppNormalizationError(
            "object must be 'whatsapp_business_account'."
        )
    entry = payload.get("entry")
    if entry is None:
        raise WhatsAppNormalizationError("entry is required.")
    if not isinstance(entry, list):
        raise WhatsAppNormalizationError("entry must be a list.")

    messages: list[NormalizedInboundMessage] = []
    statuses: list[WhatsAppDeliveryStatusEvent] = []

    for entry_item in entry:
        if not isinstance(entry_item, Mapping):
            raise WhatsAppNormalizationError("entry items must be mappings.")
        changes = entry_item.get("changes")
        if changes is None:
            raise WhatsAppNormalizationError("entry.changes is required.")
        if not isinstance(changes, list):
            raise WhatsAppNormalizationError("changes must be a list.")
        for change in changes:
            if not isinstance(change, Mapping):
                raise WhatsAppNormalizationError("change must be a mapping.")
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if value is None:
                raise WhatsAppNormalizationError("change.value is required.")
            if not isinstance(value, Mapping):
                raise WhatsAppNormalizationError("change.value must be a mapping.")
            raw_messages = value.get("messages")
            if raw_messages is not None:
                if not isinstance(raw_messages, list):
                    raise WhatsAppNormalizationError("messages must be a list.")
                for raw_message in raw_messages:
                    if not isinstance(raw_message, Mapping):
                        raise WhatsAppNormalizationError(
                            "message must be a mapping."
                        )
                    messages.append(
                        _normalize_customer_message(raw_message, received_at)
                    )
            raw_statuses = value.get("statuses")
            if raw_statuses is not None:
                if not isinstance(raw_statuses, list):
                    raise WhatsAppNormalizationError("statuses must be a list.")
                for raw_status in raw_statuses:
                    if not isinstance(raw_status, Mapping):
                        raise WhatsAppNormalizationError("status must be a mapping.")
                    statuses.append(_normalize_status(raw_status, received_at))

    return WhatsAppNormalizationResult(
        messages=tuple(messages),
        statuses=tuple(statuses),
    )

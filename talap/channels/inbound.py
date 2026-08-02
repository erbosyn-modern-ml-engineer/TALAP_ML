"""Platform-neutral canonical inbound-message contract for TALAP.

This module defines the normalized, provider-agnostic representation of an
inbound customer message. It is the single input type for downstream TALAP
modules and intentionally contains no Telegram/WhatsApp-specific structures,
no database identity, and no processing state (later tasks).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Channel = Literal["whatsapp", "telegram"]

InboundMessageType = Literal["text", "voice", "image", "unsupported"]

BusinessScope = Literal["talap_global"]

_HEX_DIGITS = frozenset("0123456789abcdef")


class MediaReference(BaseModel):
    """Stable reference to a media payload on the source channel.

    Holds the platform media identifier plus optional metadata. Provider
    download URLs are intentionally absent: they may be temporary, and URL
    resolution happens later, outside this domain contract.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    external_media_id: str = Field(min_length=1)
    mime_type: str | None = None
    file_name: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    checksum_sha256: str | None = None

    @field_validator("mime_type", "file_name", mode="before")
    @classmethod
    def _blank_string_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("checksum_sha256")
    @classmethod
    def _normalize_checksum_sha256(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = v.lower()
        if len(normalized) != 64 or any(c not in _HEX_DIGITS for c in normalized):
            raise ValueError(
                "checksum_sha256 must contain exactly 64 hexadecimal characters."
            )
        return normalized


class NormalizedInboundMessage(BaseModel):
    """Canonical inbound message normalized across channels.

    Immutable and strictly validated; safe to use as the only input type for
    downstream TALAP modules. Raw provider payloads, delivery status, and
    internal identifiers belong to later tasks and are intentionally absent.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    business_scope: BusinessScope = "talap_global"
    channel: Channel
    external_chat_id: str = Field(min_length=1, max_length=512)
    external_user_id: str = Field(min_length=1, max_length=512)
    external_message_id: str = Field(min_length=1, max_length=512)
    message_type: InboundMessageType
    text: str | None
    media: MediaReference | None
    received_at: datetime

    @field_validator("text", mode="before")
    @classmethod
    def _blank_text_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("received_at")
    @classmethod
    def _normalize_received_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("received_at must be timezone-aware.")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _check_message_type_invariants(self) -> Self:
        if self.message_type == "text":
            if self.text is None:
                raise ValueError("A text message must contain non-empty text.")
            if self.media is not None:
                raise ValueError("A text message must not contain media.")
        elif self.message_type == "voice":
            if self.media is None:
                raise ValueError("A voice message must contain media.")
        elif self.message_type == "image":
            if self.media is None:
                raise ValueError("An image message must contain media.")
        elif self.message_type == "unsupported":
            if self.text is not None:
                raise ValueError("An unsupported message must not contain text.")
            if self.media is not None:
                raise ValueError("An unsupported message must not contain media.")
        return self

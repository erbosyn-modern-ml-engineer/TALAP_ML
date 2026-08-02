"""Unit tests for the TALAP canonical inbound-message domain contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from talap.channels import (
    BusinessScope,
    Channel,
    InboundMessageType,
    MediaReference,
    NormalizedInboundMessage,
)

_UTC = UTC
_RECEIVED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=_UTC)


def _media(**overrides: object) -> MediaReference:
    payload: dict[str, object] = {"external_media_id": "media-1"}
    payload.update(overrides)
    return MediaReference(**payload)


def _message(**overrides: object) -> NormalizedInboundMessage:
    payload: dict[str, object] = {
        "channel": "telegram",
        "external_chat_id": "chat-1",
        "external_user_id": "user-1",
        "external_message_id": "msg-1",
        "message_type": "text",
        "text": "Hello TALAP",
        "media": None,
        "received_at": _RECEIVED_AT,
    }
    payload.update(overrides)
    return NormalizedInboundMessage(**payload)


def test_valid_telegram_text_message() -> None:
    message = _message()
    assert message.channel == "telegram"
    assert message.message_type == "text"
    assert message.text == "Hello TALAP"
    assert message.media is None


def test_valid_whatsapp_voice_message() -> None:
    message = _message(
        channel="whatsapp",
        message_type="voice",
        text=None,
        media=_media(mime_type="audio/ogg", duration_seconds=12),
    )
    assert message.channel == "whatsapp"
    assert message.message_type == "voice"
    assert message.text is None
    assert message.media is not None
    assert message.media.mime_type == "audio/ogg"
    assert message.media.duration_seconds == 12


def test_valid_image_message_with_optional_caption() -> None:
    message = _message(
        message_type="image",
        text="Nice product",
        media=_media(mime_type="image/jpeg"),
    )
    assert message.message_type == "image"
    assert message.text == "Nice product"
    assert message.media is not None


def test_valid_image_message_without_caption() -> None:
    message = _message(message_type="image", text=None, media=_media())
    assert message.text is None


def test_valid_unsupported_message() -> None:
    message = _message(message_type="unsupported", text=None, media=None)
    assert message.message_type == "unsupported"
    assert message.text is None
    assert message.media is None


def test_default_business_scope_is_talap_global() -> None:
    message = _message()
    assert message.business_scope == "talap_global"


def test_invalid_business_scope_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(business_scope="other")


def test_invalid_channel_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(channel="signal")


def test_invalid_message_type_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="video")


def test_text_message_without_text_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="text", text=None)


def test_text_message_with_media_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="text", text="hello", media=_media())


def test_voice_message_without_media_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="voice", text=None, media=None)


def test_image_message_without_media_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="image", text=None, media=None)


def test_unsupported_message_with_text_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="unsupported", text="nope", media=None)


def test_unsupported_message_with_media_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(message_type="unsupported", text=None, media=_media())


@pytest.mark.parametrize(
    "field",
    ["external_chat_id", "external_user_id", "external_message_id"],
)
def test_blank_identifiers_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _message(**{field: "   "})


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(unexpected_field="nope")
    with pytest.raises(ValidationError):
        _media(unexpected_field="nope")


def test_naive_received_at_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(received_at=datetime(2026, 8, 1, 12, 0, 0))


def test_aware_received_at_normalized_to_utc() -> None:
    message = _message(
        received_at=datetime(
            2026, 8, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=5))
        )
    )
    assert message.received_at == datetime(2026, 8, 1, 12, 0, 0, tzinfo=_UTC)
    assert message.received_at.utcoffset() == timedelta(0)


def test_blank_optional_text_becomes_none() -> None:
    message = _message(message_type="unsupported", text="   ", media=None)
    assert message.text is None


def test_media_negative_size_rejected() -> None:
    with pytest.raises(ValidationError):
        _media(size_bytes=-1)


def test_media_negative_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        _media(duration_seconds=-1)


def test_valid_sha256_normalized_to_lowercase() -> None:
    media = _media(checksum_sha256="A" * 64)
    assert media.checksum_sha256 == "a" * 64


def test_invalid_sha256_rejected() -> None:
    with pytest.raises(ValidationError):
        _media(checksum_sha256="not-a-sha256")
    with pytest.raises(ValidationError):
        _media(checksum_sha256="a" * 63)
    with pytest.raises(ValidationError):
        _media(checksum_sha256="z" * 64)


def test_models_are_frozen() -> None:
    message = _message()
    with pytest.raises(ValidationError):
        message.text = "mutated"  # type: ignore[misc]
    media = _media()
    with pytest.raises(ValidationError):
        media.mime_type = "audio/ogg"  # type: ignore[misc]


def test_model_dump_json_is_deterministic_and_json_safe() -> None:
    message = _message(
        message_type="image",
        text="Caption",
        media=_media(
            mime_type="image/png",
            file_name="photo.png",
            checksum_sha256="B" * 64,
        ),
        received_at=datetime(
            2026, 8, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=5))
        ),
    )
    dumped = message.model_dump(mode="json")
    assert dumped["business_scope"] == "talap_global"
    assert dumped["channel"] == "telegram"
    assert dumped["external_chat_id"] == "chat-1"
    assert dumped["external_user_id"] == "user-1"
    assert dumped["external_message_id"] == "msg-1"
    assert dumped["message_type"] == "image"
    assert dumped["text"] == "Caption"
    assert dumped["received_at"] == "2026-08-01T12:00:00Z"
    assert isinstance(dumped["media"], dict)
    assert dumped["media"]["external_media_id"] == "media-1"
    assert dumped["media"]["checksum_sha256"] == "b" * 64
    assert message.model_dump(mode="json") == dumped


# ── Additional edge coverage beyond the required list ───────────────────


def test_channel_literal_members() -> None:
    assert get_args(Channel) == ("whatsapp", "telegram")


def test_inbound_message_type_literal_members() -> None:
    assert get_args(InboundMessageType) == ("text", "voice", "image", "unsupported")


def test_business_scope_literal_members() -> None:
    assert get_args(BusinessScope) == ("talap_global",)


def test_media_blank_mime_type_and_file_name_become_none() -> None:
    media = _media(mime_type="   ", file_name="  ")
    assert media.mime_type is None
    assert media.file_name is None


def test_media_zero_size_and_duration_allowed() -> None:
    media = _media(size_bytes=0, duration_seconds=0)
    assert media.size_bytes == 0
    assert media.duration_seconds == 0


def test_media_checksum_none_allowed() -> None:
    assert _media(checksum_sha256=None).checksum_sha256 is None


def test_media_external_id_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        _media(external_media_id="   ")


def test_identifiers_are_stripped() -> None:
    message = _message(external_chat_id="  chat-1  ")
    assert message.external_chat_id == "chat-1"


def test_text_whitespace_is_stripped() -> None:
    message = _message(text="  Hello TALAP  ")
    assert message.text == "Hello TALAP"


def test_missing_required_identifiers_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(external_message_id=None)


def test_identifier_over_512_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        _message(external_chat_id="x" * 513)

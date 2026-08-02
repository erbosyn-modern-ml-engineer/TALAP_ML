"""Unit tests for the Telegram webhook payload normalizer."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from talap.channels import NormalizedInboundMessage
from talap.channels.telegram import (
    TelegramNormalizationError,
    normalize_telegram_update,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_RECEIVED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, object]:
    with (_FIXTURES / name).open(encoding="utf-8") as f:
        return json.load(f)


# ── Valid payloads ──────────────────────────────────────────────────────


def test_text_fixture_normalizes_to_text_message() -> None:
    message = normalize_telegram_update(
        _load("text_update.json"), received_at=_RECEIVED_AT
    )
    assert message.channel == "telegram"
    assert message.business_scope == "talap_global"
    assert message.message_type == "text"
    assert message.text == "Show me blue sneakers"
    assert message.media is None
    assert message.external_chat_id == "771100001"
    assert message.external_user_id == "771100001"


def test_text_is_stripped() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["text"] = "   hello   "
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.text == "hello"


def test_voice_fixture_normalizes_to_voice_message() -> None:
    message = normalize_telegram_update(
        _load("voice_update.json"), received_at=_RECEIVED_AT
    )
    assert message.message_type == "voice"
    assert message.text is None
    assert message.media is not None


def test_voice_media_reference_fields() -> None:
    message = normalize_telegram_update(
        _load("voice_update.json"), received_at=_RECEIVED_AT
    )
    assert message.media is not None
    assert message.media.external_media_id == "VoiceFileIdDemo001"
    assert message.media.mime_type == "audio/ogg"
    assert message.media.size_bytes == 25121
    assert message.media.duration_seconds == 3
    assert message.media.file_name is None
    assert message.media.checksum_sha256 is None


def test_callback_fixture_normalizes_to_text() -> None:
    message = normalize_telegram_update(
        _load("callback_update.json"), received_at=_RECEIVED_AT
    )
    assert message.message_type == "text"


def test_callback_text_equals_data() -> None:
    message = normalize_telegram_update(
        _load("callback_update.json"), received_at=_RECEIVED_AT
    )
    assert message.text == "product:sku-blue-sneakers"


def test_callback_external_message_id_uses_callback_id() -> None:
    message = normalize_telegram_update(
        _load("callback_update.json"), received_at=_RECEIVED_AT
    )
    assert message.external_message_id == "callback:722200001"


def test_callback_external_chat_id_uses_message_chat_id() -> None:
    message = normalize_telegram_update(
        _load("callback_update.json"), received_at=_RECEIVED_AT
    )
    assert message.external_chat_id == "771100003"
    assert message.external_user_id == "771100003"


def test_callback_falls_back_to_chat_instance() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("message")
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.external_chat_id == "3829450123456789"


def test_callback_without_data_is_unsupported() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("data")
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.message_type == "unsupported"
    assert message.text is None
    assert message.media is None


def test_callback_with_blank_data_is_unsupported() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback["data"] = "   "
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.message_type == "unsupported"
    assert message.text is None


def test_sticker_fixture_is_unsupported() -> None:
    message = normalize_telegram_update(
        _load("sticker_update.json"), received_at=_RECEIVED_AT
    )
    assert message.message_type == "unsupported"
    assert message.text is None
    assert message.media is None


# ── Canonical external_message_id formats (idempotency contract) ────────


def test_canonical_message_id_format_is_message_chat_message() -> None:
    message = normalize_telegram_update(
        _load("text_update.json"), received_at=_RECEIVED_AT
    )
    assert message.external_message_id == "message:771100001:41001"


def test_canonical_callback_id_format_is_callback_callback_id() -> None:
    message = normalize_telegram_update(
        _load("callback_update.json"), received_at=_RECEIVED_AT
    )
    assert message.external_message_id == "callback:722200001"


def test_voice_uses_same_canonical_message_id_format() -> None:
    message = normalize_telegram_update(
        _load("voice_update.json"), received_at=_RECEIVED_AT
    )
    assert message.external_message_id == "message:771100002:41002"


def test_same_message_id_in_two_chats_has_distinct_ids() -> None:
    first = normalize_telegram_update(
        _load("text_update.json"), received_at=_RECEIVED_AT
    )
    second_payload = _load("text_update.json")
    message_obj = second_payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["chat"] = {"id": 999888777, "type": "private"}
    second = normalize_telegram_update(second_payload, received_at=_RECEIVED_AT)
    assert first.external_message_id != second.external_message_id
    assert second.external_message_id == "message:999888777:41001"


def test_update_id_does_not_affect_external_message_id() -> None:
    first = normalize_telegram_update(
        _load("text_update.json"), received_at=_RECEIVED_AT
    )
    changed = _load("text_update.json")
    changed["update_id"] = 999999999
    second = normalize_telegram_update(changed, received_at=_RECEIVED_AT)
    assert first.external_message_id == second.external_message_id


# ── Priority rules ──────────────────────────────────────────────────────


def test_text_takes_priority_over_voice() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["text"] = "hello"
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.message_type == "text"
    assert message.text == "hello"
    assert message.media is None


def test_blank_text_is_not_a_text_message() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["text"] = "   "
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.message_type == "unsupported"


# ── Malformed payloads ──────────────────────────────────────────────────


def test_non_mapping_payload_raises() -> None:
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update([1, 2, 3], received_at=_RECEIVED_AT)  # type: ignore[arg-type]


def test_missing_message_and_callback_query_raises() -> None:
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update({"update_id": 1}, received_at=_RECEIVED_AT)


def test_message_not_a_mapping_raises() -> None:
    payload = _load("text_update.json")
    payload["message"] = "not-a-mapping"
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_callback_query_not_a_mapping_raises() -> None:
    payload = _load("callback_update.json")
    payload["callback_query"] = None
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_missing_message_id_raises() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj.pop("message_id")
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_missing_chat_id_raises() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["chat"] = {"type": "private"}
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_missing_from_id_raises() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["from"] = {"is_bot": False}
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_malformed_voice_raises() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj["text"] = None
    message_obj["voice"] = "not-a-mapping"
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_missing_voice_file_id_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice.pop("file_id")
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_blank_voice_file_id_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["file_id"] = "   "
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_non_string_voice_file_id_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["file_id"] = 12345
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_non_string_voice_mime_type_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["mime_type"] = 42
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_string_voice_file_size_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["file_size"] = "25121"
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_bool_voice_file_size_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["file_size"] = True
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_negative_voice_file_size_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["file_size"] = -1
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_string_voice_duration_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["duration"] = "3"
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_bool_voice_duration_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["duration"] = True
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_negative_voice_duration_raises() -> None:
    payload = _load("voice_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    voice = message_obj["voice"]
    assert isinstance(voice, dict)
    voice["duration"] = -1
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_integer_chat_instance_rejected() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("message")
    callback["chat_instance"] = 3829450123456789
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_blank_chat_instance_rejected() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("message")
    callback["chat_instance"] = "   "
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_valid_string_chat_instance_accepted() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("message")
    callback["chat_instance"] = "  9876543210123456  "
    message = normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert message.external_chat_id == "9876543210123456"


def test_missing_callback_id_raises() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("id")
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_missing_callback_user_id_raises() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback["from"] = {"is_bot": False}
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_callback_without_chat_identity_raises() -> None:
    payload = _load("callback_update.json")
    callback = payload["callback_query"]
    assert isinstance(callback, dict)
    callback.pop("message")
    callback.pop("chat_instance")
    with pytest.raises(TelegramNormalizationError):
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)


def test_error_messages_do_not_contain_payload_data() -> None:
    payload = _load("text_update.json")
    message_obj = payload["message"]
    assert isinstance(message_obj, dict)
    message_obj.pop("message_id")
    with pytest.raises(TelegramNormalizationError) as excinfo:
        normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    message = str(excinfo.value)
    assert "41001" not in message
    assert "Show me blue sneakers" not in message


# ── received_at handling through the T-018 contract ─────────────────────


def test_naive_received_at_rejected() -> None:
    with pytest.raises(TelegramNormalizationError) as excinfo:
        normalize_telegram_update(
            _load("text_update.json"),
            received_at=datetime(2026, 8, 1, 12, 0, 0),
        )
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_aware_received_at_normalized_to_utc() -> None:
    message = normalize_telegram_update(
        _load("text_update.json"),
        received_at=datetime(
            2026, 8, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=5))
        ),
    )
    assert message.received_at == datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    assert message.received_at.utcoffset() == timedelta(0)


# ── Purity and output guarantees ────────────────────────────────────────


def test_input_payload_is_not_mutated() -> None:
    payload = _load("text_update.json")
    snapshot = json.dumps(payload, sort_keys=True)
    normalize_telegram_update(payload, received_at=_RECEIVED_AT)
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_function_is_synchronous_and_requires_received_at() -> None:
    # No async, no current-time lookup: received_at is a required keyword.
    assert not inspect.iscoroutinefunction(normalize_telegram_update)
    with pytest.raises(TypeError):
        normalize_telegram_update(_load("text_update.json"))  # type: ignore[call-arg]


def test_output_type_is_exactly_normalized_inbound_message() -> None:
    message = normalize_telegram_update(
        _load("text_update.json"), received_at=_RECEIVED_AT
    )
    assert type(message) is NormalizedInboundMessage


def test_fixtures_contain_no_secrets() -> None:
    secret_markers = (
        "token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "authorization",
        "bearer ",
    )
    for fixture in sorted(_FIXTURES.glob("*.json")):
        raw = fixture.read_text(encoding="utf-8").lower()
        assert not any(marker in raw for marker in secret_markers), (
            f"possible secret marker in {fixture.name}"
        )

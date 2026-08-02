"""Unit tests for the WhatsApp Cloud API webhook normalizer."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from talap.channels import NormalizedInboundMessage
from talap.channels.whatsapp import (
    WhatsAppDeliveryStatusEvent,
    WhatsAppNormalizationError,
    WhatsAppNormalizationResult,
    normalize_whatsapp_webhook,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_RECEIVED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, object]:
    with (_FIXTURES / name).open(encoding="utf-8") as f:
        return json.load(f)


def _normalize(payload: dict[str, object]) -> WhatsAppNormalizationResult:
    return normalize_whatsapp_webhook(payload, received_at=_RECEIVED_AT)


def _base_message() -> dict[str, object]:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    messages = value["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    return message


def _webhook_with_messages(messages: list[dict[str, object]]) -> dict[str, object]:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    value["messages"] = messages
    value.pop("statuses", None)
    return payload


def _webhook_with_statuses(statuses: list[dict[str, object]]) -> dict[str, object]:
    payload = _load("status_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    value["statuses"] = statuses
    value.pop("messages", None)
    return payload


def _status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "wamid.SYNTHETIC_STATUS_X",
        "recipient_id": "77000000001",
        "status": "delivered",
        "timestamp": "1783022403",
    }
    payload.update(overrides)
    return payload


# ── Envelope and batching ───────────────────────────────────────────────


def test_text_webhook_normalizes() -> None:
    result = _normalize(_load("text_webhook.json"))
    assert len(result.messages) == 1
    assert len(result.statuses) == 0
    assert result.messages[0].message_type == "text"


def test_audio_webhook_normalizes() -> None:
    result = _normalize(_load("audio_webhook.json"))
    assert len(result.messages) == 1
    assert result.messages[0].message_type == "voice"


def test_interactive_button_webhook_normalizes() -> None:
    result = _normalize(_load("interactive_button_webhook.json"))
    assert len(result.messages) == 1
    assert result.messages[0].message_type == "text"


def test_status_webhook_creates_no_customer_message() -> None:
    result = _normalize(_load("status_webhook.json"))
    assert result.messages == ()


def test_status_webhook_creates_one_status_event() -> None:
    result = _normalize(_load("status_webhook.json"))
    assert len(result.statuses) == 1


def test_empty_valid_webhook_returns_empty_tuples() -> None:
    result = _normalize({"object": "whatsapp_business_account", "entry": []})
    assert result.messages == ()
    assert result.statuses == ()


def test_empty_entry_returns_empty_tuples() -> None:
    result = _normalize({"object": "whatsapp_business_account", "entry": []})
    assert result.messages == ()
    assert result.statuses == ()


def test_missing_entry_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize({"object": "whatsapp_business_account"})


def test_entry_null_rejected() -> None:
    payload = _load("text_webhook.json")
    payload["entry"] = None
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_entry_mapping_rejected() -> None:
    payload = _load("text_webhook.json")
    payload["entry"] = {"id": "999999999999999"}
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_entry_item_not_mapping_rejected() -> None:
    payload = _load("text_webhook.json")
    payload["entry"] = ["not-a-mapping"]
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_missing_changes_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    first.pop("changes")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_changes_null_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    first["changes"] = None
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_missing_value_on_messages_change_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    changes = first["changes"]
    assert isinstance(changes, list)
    changes[0].pop("value")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_value_null_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    changes = first["changes"]
    assert isinstance(changes, list)
    changes[0]["value"] = None
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_absent_messages_and_statuses_in_valid_value_allowed() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    changes = first["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    value.pop("messages")
    value.pop("statuses", None)
    result = _normalize(payload)
    assert result.messages == ()
    assert result.statuses == ()


def test_multiple_messages_all_preserved() -> None:
    first = _base_message()
    second = _base_message()
    first["id"] = "wamid.SYNTHETIC_MULTI_1"
    second["id"] = "wamid.SYNTHETIC_MULTI_2"
    result = _normalize(_webhook_with_messages([first, second]))
    assert [m.external_message_id for m in result.messages] == [
        "wamid.SYNTHETIC_MULTI_1",
        "wamid.SYNTHETIC_MULTI_2",
    ]


def test_multiple_statuses_all_preserved() -> None:
    result = _normalize(
        _webhook_with_statuses(
            [_status(id="wamid.SYNTHETIC_S1"), _status(id="wamid.SYNTHETIC_S2")]
        )
    )
    assert [s.external_message_id for s in result.statuses] == [
        "wamid.SYNTHETIC_S1",
        "wamid.SYNTHETIC_S2",
    ]


def test_mixed_messages_and_statuses_preserved() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    message = _base_message()
    message["id"] = "wamid.SYNTHETIC_MIXED_1"
    value["messages"] = [message]
    value["statuses"] = [_status(id="wamid.SYNTHETIC_MIXED_S1")]
    result = _normalize(payload)
    assert [m.external_message_id for m in result.messages] == [
        "wamid.SYNTHETIC_MIXED_1"
    ]
    assert [s.external_message_id for s in result.statuses] == [
        "wamid.SYNTHETIC_MIXED_S1"
    ]


def test_traversal_order_is_deterministic() -> None:
    first = _base_message()
    second = _base_message()
    third = _base_message()
    first["id"] = "wamid.SYNTHETIC_ORDER_A"
    second["id"] = "wamid.SYNTHETIC_ORDER_B"
    third["id"] = "wamid.SYNTHETIC_ORDER_C"
    status_one = _status(id="wamid.SYNTHETIC_ORDER_S1", status="read")
    status_two = _status(id="wamid.SYNTHETIC_ORDER_S2", status="failed")

    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first_entry = entry[0]
    assert isinstance(first_entry, dict)
    first_entry["changes"] = [
        {"field": "messages", "value": {"messages": [first, second], "statuses": [status_one]}},
        {"field": "messages", "value": {"messages": [third], "statuses": [status_two]}},
    ]
    entry.append(
        {
            "id": "999999999999999",
            "changes": [{"field": "messages", "value": {"messages": [third]}}],
        }
    )
    result = _normalize(payload)
    assert [m.external_message_id for m in result.messages] == [
        "wamid.SYNTHETIC_ORDER_A",
        "wamid.SYNTHETIC_ORDER_B",
        "wamid.SYNTHETIC_ORDER_C",
        "wamid.SYNTHETIC_ORDER_C",
    ]
    assert [s.external_message_id for s in result.statuses] == [
        "wamid.SYNTHETIC_ORDER_S1",
        "wamid.SYNTHETIC_ORDER_S2",
    ]


def test_changes_with_other_fields_are_ignored() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first_entry = entry[0]
    assert isinstance(first_entry, dict)
    first_entry["changes"] = [
        {"field": "messages", "value": {"messages": [_base_message()]}},
        {"field": "account_update", "value": {"some": "data"}},
    ]
    result = _normalize(payload)
    assert len(result.messages) == 1


# ── Text messages ───────────────────────────────────────────────────────


def test_exact_wamid_used_as_external_message_id() -> None:
    message = _normalize(_load("text_webhook.json")).messages[0]
    assert message.external_message_id == "wamid.SYNTHETIC_TEXT_0001"


def test_chat_id_equals_user_id_equals_from() -> None:
    message = _normalize(_load("text_webhook.json")).messages[0]
    assert message.external_chat_id == "77000000001"
    assert message.external_user_id == "77000000001"


def test_timestamp_parsed_from_unix_seconds_to_utc() -> None:
    message = _normalize(_load("text_webhook.json")).messages[0]
    assert message.received_at == datetime(2026, 7, 2, 20, 0, 0, tzinfo=UTC)


def test_timestamp_absent_falls_back_to_received_at() -> None:
    message = _base_message()
    message.pop("timestamp")
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].received_at == _RECEIVED_AT


def test_text_body_stripped_by_contract() -> None:
    message = _base_message()
    text_object = message["text"]
    assert isinstance(text_object, dict)
    text_object["body"] = "   hello   "
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].text == "hello"


def test_blank_text_rejected() -> None:
    message = _base_message()
    text_object = message["text"]
    assert isinstance(text_object, dict)
    text_object["body"] = "   "
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_malformed_text_object_rejected() -> None:
    message = _base_message()
    message["text"] = "not-a-mapping"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))
    message["text"] = {"body": 123}
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_message_type_missing_rejected() -> None:
    message = _base_message()
    message.pop("type")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_message_type_null_rejected() -> None:
    message = _base_message()
    message["type"] = None
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_message_type_integer_rejected() -> None:
    message = _base_message()
    message["type"] = 42
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_message_type_blank_rejected() -> None:
    message = _base_message()
    message["type"] = "   "
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_message_unknown_type_is_unsupported() -> None:
    message = _base_message()
    message["type"] = "order"
    message.pop("text")
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].message_type == "unsupported"
    assert result.messages[0].text is None
    assert result.messages[0].media is None


# ── Audio messages ──────────────────────────────────────────────────────


def test_audio_external_media_id_mapped() -> None:
    message = _normalize(_load("audio_webhook.json")).messages[0]
    assert message.media is not None
    assert message.media.external_media_id == "322666666666666"


def test_audio_mime_type_mapped() -> None:
    message = _normalize(_load("audio_webhook.json")).messages[0]
    assert message.media is not None
    assert message.media.mime_type == "audio/ogg; codecs=opus"


def test_audio_sha256_normalized_to_lowercase() -> None:
    message = _normalize(_load("audio_webhook.json")).messages[0]
    assert message.media is not None
    assert message.media.checksum_sha256 == "5a" * 32


def test_audio_missing_id_rejected() -> None:
    payload = _load("audio_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    messages = value["messages"]
    assert isinstance(messages, list)
    audio_message = messages[0]
    assert isinstance(audio_message, dict)
    audio = audio_message["audio"]
    assert isinstance(audio, dict)
    audio.pop("id")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_audio_non_string_mime_type_rejected() -> None:
    payload = _load("audio_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    messages = value["messages"]
    assert isinstance(messages, list)
    audio_message = messages[0]
    assert isinstance(audio_message, dict)
    audio = audio_message["audio"]
    assert isinstance(audio, dict)
    audio["mime_type"] = 123
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_audio_invalid_sha256_rejected() -> None:
    payload = _load("audio_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    messages = value["messages"]
    assert isinstance(messages, list)
    audio_message = messages[0]
    assert isinstance(audio_message, dict)
    audio = audio_message["audio"]
    assert isinstance(audio, dict)
    audio["sha256"] = "not-a-sha256"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_audio_media_has_no_url() -> None:
    message = _normalize(_load("audio_webhook.json")).messages[0]
    assert message.media is not None
    assert "url" not in message.media.model_dump()


def test_audio_message_without_audio_object_rejected() -> None:
    payload = _load("audio_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    changes = entry[0]["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    messages = value["messages"]
    assert isinstance(messages, list)
    audio_message = messages[0]
    assert isinstance(audio_message, dict)
    audio_message.pop("audio")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


# ── Interactive replies ─────────────────────────────────────────────────


def test_button_reply_id_becomes_text() -> None:
    message = _normalize(_load("interactive_button_webhook.json")).messages[0]
    assert message.message_type == "text"
    assert message.text == "catalog-shoe-blue-sneakers"


def test_button_title_not_preferred_over_id() -> None:
    message = _normalize(_load("interactive_button_webhook.json")).messages[0]
    assert message.text == "catalog-shoe-blue-sneakers"
    assert message.text != "Blue Sneakers"


def test_list_reply_id_supported() -> None:
    message = _base_message()
    message["type"] = "interactive"
    message.pop("text")
    message["interactive"] = {
        "type": "list_reply",
        "list_reply": {
            "id": "catalog-shoe-red-sneakers",
            "title": "Red Sneakers",
            "description": "Synthetic list row",
        },
    }
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].message_type == "text"
    assert result.messages[0].text == "catalog-shoe-red-sneakers"


def test_title_fallback_when_id_blank() -> None:
    message = _base_message()
    message["type"] = "interactive"
    message.pop("text")
    message["interactive"] = {
        "type": "button_reply",
        "button_reply": {"id": "   ", "title": "Blue Sneakers"},
    }
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].message_type == "text"
    assert result.messages[0].text == "Blue Sneakers"


def test_unknown_interactive_type_is_unsupported() -> None:
    message = _base_message()
    message["type"] = "interactive"
    message.pop("text")
    message["interactive"] = {"type": "catalog_selection"}
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].message_type == "unsupported"
    assert result.messages[0].text is None
    assert result.messages[0].media is None


def test_malformed_button_reply_rejected() -> None:
    message = _base_message()
    message["type"] = "interactive"
    message.pop("text")
    message["interactive"] = {"type": "button_reply", "button_reply": "bad"}
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))
    message["interactive"] = {
        "type": "button_reply",
        "button_reply": {"id": 42, "title": "Blue"},
    }
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_malformed_list_reply_rejected() -> None:
    message = _base_message()
    message["type"] = "interactive"
    message.pop("text")
    message["interactive"] = {"type": "list_reply", "list_reply": "bad"}
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


# ── Unsupported customer messages ───────────────────────────────────────


def test_image_message_is_unsupported() -> None:
    message = _base_message()
    message["type"] = "image"
    message.pop("text")
    message["image"] = {"id": "322777777777777", "mime_type": "image/jpeg"}
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].message_type == "unsupported"


def test_unsupported_has_no_text_or_media() -> None:
    message = _base_message()
    message["type"] = "sticker"
    message.pop("text")
    result = _normalize(_webhook_with_messages([message]))
    assert result.messages[0].message_type == "unsupported"
    assert result.messages[0].text is None
    assert result.messages[0].media is None


# ── Delivery status events ──────────────────────────────────────────────


def test_delivered_status_mapping() -> None:
    result = _normalize(_load("status_webhook.json"))
    status = result.statuses[0]
    assert status.status == "delivered"
    assert status.external_message_id == "wamid.SYNTHETIC_STATUS_0001"
    assert status.recipient_id == "77000000004"
    assert status.channel == "whatsapp"
    assert status.error_codes == ()


def test_failed_status_error_codes() -> None:
    result = _normalize(
        _webhook_with_statuses(
            [
                _status(
                    id="wamid.SYNTHETIC_FAIL_1",
                    status="failed",
                    errors=[{"code": 131047, "title": "Re-engagement message"}],
                )
            ]
        )
    )
    status = result.statuses[0]
    assert status.status == "failed"
    assert status.error_codes == (131047,)


def test_unknown_status_maps_to_unknown() -> None:
    result = _normalize(
        _webhook_with_statuses([_status(id="wamid.SYNTHETIC_UNK_1", status="queued")])
    )
    assert result.statuses[0].status == "unknown"


def test_malformed_error_code_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(
            _webhook_with_statuses(
                [_status(errors=[{"code": "131047"}])]
            )
        )
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(
            _webhook_with_statuses([_status(errors=[{"code": True}])])
        )
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(
            _webhook_with_statuses([_status(errors=[{"title": "no code"}])])
        )
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(
            _webhook_with_statuses([_status(errors=[131047])])
        )


def test_status_never_appears_in_messages() -> None:
    result = _normalize(_load("status_webhook.json"))
    assert result.messages == ()
    assert isinstance(result.statuses[0], WhatsAppDeliveryStatusEvent)


def test_status_timestamp_parsed_to_utc() -> None:
    result = _normalize(_load("status_webhook.json"))
    assert result.statuses[0].occurred_at == datetime(2026, 7, 2, 20, 0, 3, tzinfo=UTC)


def test_status_timestamp_absent_falls_back() -> None:
    status = _status(id="wamid.SYNTHETIC_NOTS_1")
    status.pop("timestamp")
    result = _normalize(_webhook_with_statuses([status]))
    assert result.statuses[0].occurred_at == _RECEIVED_AT


def test_status_missing_id_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(id="")]))


def test_status_missing_recipient_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(recipient_id="  ")]))


def test_status_missing_status_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(status=None)]))


# ── Errors and safety ───────────────────────────────────────────────────


def test_wrong_object_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize({"object": "instagram"})
    with pytest.raises(WhatsAppNormalizationError):
        _normalize({})


def test_malformed_entry_rejected() -> None:
    payload = _load("text_webhook.json")
    payload["entry"] = "not-a-list"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_malformed_changes_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    first["changes"] = "not-a-list"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_malformed_value_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    changes = first["changes"]
    assert isinstance(changes, list)
    changes[0]["value"] = "not-a-mapping"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_malformed_messages_array_rejected() -> None:
    payload = _load("text_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    changes = first["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    value["messages"] = "not-a-list"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_malformed_statuses_array_rejected() -> None:
    payload = _load("status_webhook.json")
    entry = payload["entry"]
    assert isinstance(entry, list)
    first = entry[0]
    assert isinstance(first, dict)
    changes = first["changes"]
    assert isinstance(changes, list)
    value = changes[0]["value"]
    assert isinstance(value, dict)
    value["statuses"] = "not-a-list"
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(payload)


def test_missing_message_id_rejected() -> None:
    message = _base_message()
    message.pop("id")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_missing_sender_rejected() -> None:
    message = _base_message()
    message.pop("from")
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


@pytest.mark.parametrize(
    "bad_timestamp",
    [1783022400, True, 178.5, -1, "", "   ", "abc", "1783022400.5", "-1"],
)
def test_malformed_unix_timestamp_rejected(bad_timestamp: object) -> None:
    message = _base_message()
    message["timestamp"] = bad_timestamp
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_message_timestamp_null_rejected() -> None:
    message = _base_message()
    message["timestamp"] = None
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_messages([message]))


def test_status_timestamp_integer_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(timestamp=1783022403)]))


def test_status_timestamp_bool_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(timestamp=True)]))


def test_status_timestamp_negative_string_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(timestamp="-1")]))


def test_status_timestamp_malformed_string_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(timestamp="abc")]))


def test_status_timestamp_null_rejected() -> None:
    with pytest.raises(WhatsAppNormalizationError):
        _normalize(_webhook_with_statuses([_status(timestamp=None)]))


def test_naive_received_at_rejected_when_fallback_needed() -> None:
    message = _base_message()
    message.pop("timestamp")
    with pytest.raises(WhatsAppNormalizationError) as excinfo:
        normalize_whatsapp_webhook(
            _webhook_with_messages([message]),
            received_at=datetime(2026, 8, 1, 12, 0, 0),
        )
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_payload_is_not_mutated() -> None:
    payload = _load("text_webhook.json")
    snapshot = json.dumps(payload, sort_keys=True)
    _normalize(payload)
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_output_models_are_frozen() -> None:
    result = _normalize(_load("text_webhook.json"))
    message = result.messages[0]
    with pytest.raises(ValidationError):
        message.text = "mutated"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.messages = ()  # type: ignore[misc]
    status_result = _normalize(_load("status_webhook.json"))
    status = status_result.statuses[0]
    with pytest.raises(ValidationError):
        status.recipient_id = "mutated"  # type: ignore[misc]


def test_result_holds_exact_model_types() -> None:
    result = _normalize(_load("text_webhook.json"))
    assert type(result.messages[0]) is NormalizedInboundMessage
    status_result = _normalize(_load("status_webhook.json"))
    assert type(status_result.statuses[0]) is WhatsAppDeliveryStatusEvent


def test_fixtures_contain_no_secrets() -> None:
    secret_markers = (
        "token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "authorization",
        "bearer ",
        "access_token",
    )
    for fixture in sorted(_FIXTURES.glob("*.json")):
        raw = fixture.read_text(encoding="utf-8").lower()
        assert not any(marker in raw for marker in secret_markers), (
            f"possible secret marker in {fixture.name}"
        )


def test_function_has_no_network_environment_dependencies() -> None:
    # Synchronous and requires received_at explicitly (no current-time lookup).
    assert not inspect.iscoroutinefunction(normalize_whatsapp_webhook)
    with pytest.raises(TypeError):
        normalize_whatsapp_webhook(_load("text_webhook.json"))  # type: ignore[call-arg]

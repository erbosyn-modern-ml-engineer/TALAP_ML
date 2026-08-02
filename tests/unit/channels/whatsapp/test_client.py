"""Unit tests for the WhatsApp outbound client (fake HTTP only)."""

from __future__ import annotations

import pytest

from talap.channels.whatsapp import (
    SentWhatsAppMessage,
    WhatsAppClient,
    WhatsAppClientError,
)

_ACCESS_TOKEN = "test-access-token"
_PHONE_NUMBER_ID = "100000000000001"
_GRAPH_VERSION = "v21.0"
_RECIPIENT = "77000000001"
_ECHO_TEXT = "Сообщение получено"

_EXPECTED_URL = (
    "https://graph.facebook.com/v21.0/100000000000001/messages"
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        data: object = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._data = data
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._data


class _FakeHTTPClient:
    def __init__(
        self,
        *,
        status_code: int = 200,
        data: object = None,
        json_error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._status_code = status_code
        self._data = data
        self._json_error = json_error

    async def post(
        self,
        url: str,
        *,
        json: object = None,
        headers: object = None,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(
            status_code=self._status_code,
            data=self._data,
            json_error=self._json_error,
        )


def _client(
    http_client: _FakeHTTPClient,
    *,
    token: str = _ACCESS_TOKEN,
    phone_number_id: str = _PHONE_NUMBER_ID,
) -> WhatsAppClient:
    return WhatsAppClient(
        access_token=token,
        phone_number_id=phone_number_id,
        graph_api_version=_GRAPH_VERSION,
        http_client=http_client,  # type: ignore[arg-type]
    )


def _ok_data() -> dict[str, object]:
    return {"messages": [{"id": "wamid.SYNTHETIC_SENT_1"}]}


async def test_correct_url() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    assert http.calls[0]["url"] == _EXPECTED_URL


async def test_correct_phone_number_id_in_url() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    assert _PHONE_NUMBER_ID in str(http.calls[0]["url"])


async def test_bearer_token_header() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    headers = http.calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert headers["Content-Type"] == "application/json"


async def test_exact_recipient() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    payload = http.calls[0]["json"]
    assert isinstance(payload, dict)
    assert payload["to"] == _RECIPIENT
    assert payload["recipient_type"] == "individual"
    assert payload["type"] == "text"
    assert payload["messaging_product"] == "whatsapp"


async def test_exact_fixed_text_body() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    payload = http.calls[0]["json"]
    assert isinstance(payload, dict)
    text = payload["text"]
    assert isinstance(text, dict)
    assert text["body"] == _ECHO_TEXT


async def test_preview_url_false() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    payload = http.calls[0]["json"]
    assert isinstance(payload, dict)
    text = payload["text"]
    assert isinstance(text, dict)
    assert text["preview_url"] is False


async def test_successful_wamid_parsed() -> None:
    http = _FakeHTTPClient(data=_ok_data())
    sent = await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    assert isinstance(sent, SentWhatsAppMessage)
    assert sent.external_message_id == "wamid.SYNTHETIC_SENT_1"


@pytest.mark.parametrize(
    "data",
    [
        None,
        "not-a-dict",
        {"messages": []},
        {"messages": [{"id": "wamid.X"}, {"id": "wamid.Y"}]},
        {"messages": ["not-a-dict"]},
        {"messages": [{"id": "  "}]},
        {"messages": [{}]},
        {"messages": [{"id": 123}]},
    ],
)
async def test_malformed_response_rejected(data: object) -> None:
    http = _FakeHTTPClient(data=data)
    with pytest.raises(WhatsAppClientError):
        await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)


async def test_malformed_json_rejected() -> None:
    http = _FakeHTTPClient(json_error=ValueError("bad json"))
    with pytest.raises(WhatsAppClientError):
        await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)


async def test_4xx_rejected_safely() -> None:
    http = _FakeHTTPClient(status_code=400, data={"error": {"code": 131047}})
    with pytest.raises(WhatsAppClientError):
        await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)


async def test_5xx_rejected_safely() -> None:
    http = _FakeHTTPClient(status_code=500, data={})
    with pytest.raises(WhatsAppClientError):
        await _client(http).send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)


async def test_token_absent_from_exceptions() -> None:
    secret_token = "super-secret-access-token"
    http = _FakeHTTPClient(status_code=400, data={"error": {"code": 1}})
    client = _client(http, token=secret_token)
    with pytest.raises(WhatsAppClientError) as excinfo:
        await client.send_text(recipient=_RECIPIENT, text=_ECHO_TEXT)
    assert secret_token not in str(excinfo.value)


@pytest.mark.parametrize(
    "recipient,text",
    [
        ("   ", _ECHO_TEXT),
        (_RECIPIENT, "   "),
        ("", _ECHO_TEXT),
        (_RECIPIENT, ""),
    ],
)
async def test_blank_recipient_or_text_rejected(recipient: str, text: str) -> None:
    http = _FakeHTTPClient(data=_ok_data())
    with pytest.raises(WhatsAppClientError):
        await _client(http).send_text(recipient=recipient, text=text)
    assert http.calls == []

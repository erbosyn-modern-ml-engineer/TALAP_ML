from __future__ import annotations

import json

import pytest

from talap.ai.customer_request import (
    CustomerRequest,
    CustomerRequestConfigurationError,
    CustomerRequestExtractionError,
    extract_customer_request,
)

_URL = "https://api.deepseek.com/chat/completions"


class _FakeResponse:
    def __init__(self, status_code: int = 200, data: object | None = None) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> object:
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _FakeHTTPClient:
    """Records requests and returns a scripted sequence of responses."""

    def __init__(self, *responses: _FakeResponse) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = list(responses)

    async def post(
        self,
        url: str,
        *,
        json: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        return self._responses.pop(0)


def _ok(content: str) -> _FakeResponse:
    return _FakeResponse(
        200,
        {"choices": [{"message": {"content": content}}]},
    )


def _ok_object(obj: dict[str, object]) -> _FakeResponse:
    return _ok(json.dumps(obj))


def _valid_object() -> dict[str, object]:
    return {
        "intent": "product_search",
        "language": "ru",
        "category": "sneakers",
        "query_text": "синие кроссовки",
        "attributes": {"color": "blue"},
        "budget_max_kzt": 30000,
        "quantity": 2,
        "missing_field": None,
    }


async def _extract(
    fake: _FakeHTTPClient,
    *,
    text: str = "Мне нужны синие кроссовки",
    api_key: str = "test-key",
) -> CustomerRequest:
    return await extract_customer_request(
        text=text,
        http_client=fake,  # type: ignore[arg-type]
        api_key=api_key,
    )


@pytest.mark.asyncio
async def test_russian_product_request_parsed() -> None:
    fake = _FakeHTTPClient(_ok_object(_valid_object()))
    result = await _extract(fake)
    assert result.intent == "product_search"
    assert result.language == "ru"
    assert result.query_text == "синие кроссовки"
    assert result.category == "sneakers"


@pytest.mark.asyncio
async def test_kazakh_request_parsed() -> None:
    body = _valid_object()
    body["language"] = "kk"
    body["query_text"] = "көк кроссовкалар"
    fake = _FakeHTTPClient(_ok_object(body))
    result = await _extract(fake, text="Маған көк кроссовкалар керек")
    assert result.language == "kk"
    assert result.query_text == "көк кроссовкалар"


@pytest.mark.asyncio
async def test_mixed_language_parsed() -> None:
    body = _valid_object()
    body["language"] = "mixed"
    fake = _FakeHTTPClient(_ok_object(body))
    result = await _extract(fake, text="Кроссовки керек, синие")
    assert result.language == "mixed"


@pytest.mark.asyncio
async def test_complete_fields_validated() -> None:
    fake = _FakeHTTPClient(_ok_object(_valid_object()))
    result = await _extract(fake)
    assert result.attributes == {"color": "blue"}
    assert result.budget_max_kzt == 30000
    assert result.quantity == 2
    assert result.missing_field is None


@pytest.mark.asyncio
async def test_optional_fields_may_be_null() -> None:
    body = _valid_object()
    body["category"] = None
    body["attributes"] = {}
    body["budget_max_kzt"] = None
    body["quantity"] = None
    body["missing_field"] = "размер"
    fake = _FakeHTTPClient(_ok_object(body))
    result = await _extract(fake)
    assert result.category is None
    assert result.attributes == {}
    assert result.budget_max_kzt is None
    assert result.quantity is None
    assert result.missing_field == "размер"


@pytest.mark.asyncio
async def test_blank_input_rejected_without_http() -> None:
    fake = _FakeHTTPClient()
    with pytest.raises(CustomerRequestExtractionError):
        await _extract(fake, text="   ")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_invalid_json_triggers_one_repair() -> None:
    fake = _FakeHTTPClient(_ok("this is not json"), _ok_object(_valid_object()))
    result = await _extract(fake)
    assert result.query_text == "синие кроссовки"
    assert len(fake.calls) == 2
    for call in fake.calls:
        payload = call["json"]  # type: ignore[index]
        assert isinstance(payload, dict)
        assert payload["thinking"] == {"type": "disabled"}
    repair_user = fake.calls[1]["json"]  # type: ignore[index]
    assert isinstance(repair_user, dict)
    messages = repair_user["messages"]
    assert isinstance(messages, list)
    assert "this is not json" in messages[1]["content"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_invalid_schema_triggers_one_repair() -> None:
    bad = _valid_object()
    bad.pop("query_text")
    fake = _FakeHTTPClient(_ok_object(bad), _ok_object(_valid_object()))
    result = await _extract(fake)
    assert result.query_text == "синие кроссовки"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_second_invalid_response_fails_safely() -> None:
    fake = _FakeHTTPClient(_ok("not json"), _ok("still not json"))
    with pytest.raises(CustomerRequestExtractionError) as excinfo:
        await _extract(fake)
    assert len(fake.calls) == 2
    assert "test-key" not in str(excinfo.value)
    assert "синие кроссовки" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_authentication_failure_not_retried() -> None:
    fake = _FakeHTTPClient(_FakeResponse(401, {"error": "invalid api key"}))
    with pytest.raises(CustomerRequestConfigurationError) as excinfo:
        await _extract(fake)
    assert len(fake.calls) == 1
    assert "test-key" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_api_key_raises_configuration_error_without_http() -> None:
    fake = _FakeHTTPClient()
    with pytest.raises(CustomerRequestConfigurationError):
        await extract_customer_request(
            text="Мне нужны синие кроссовки",
            http_client=fake,  # type: ignore[arg-type]
            api_key="   ",
        )
    assert fake.calls == []


@pytest.mark.asyncio
async def test_request_uses_json_output_mode_and_bounded_max_tokens() -> None:
    fake = _FakeHTTPClient(_ok_object(_valid_object()))
    await _extract(fake)
    assert len(fake.calls) == 1
    payload = fake.calls[0]["json"]  # type: ignore[index]
    assert isinstance(payload, dict)
    assert payload["response_format"] == {"type": "json_object"}
    assert isinstance(payload["max_tokens"], int) and payload["max_tokens"] <= 500
    assert isinstance(payload["model"], str) and payload["model"]
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"  # type: ignore[index]
    assert "JSON" in messages[0]["content"]  # type: ignore[index]
    assert messages[1] == {"role": "user", "content": "Мне нужны синие кроссовки"}

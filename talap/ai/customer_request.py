"""DeepSeek-powered structured extraction of a WhatsApp customer request (MVP-3).

Isolated component: converts one customer text into a validated
``CustomerRequest``. Not wired into the WhatsApp worker yet.
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from talap.core import get_settings

__all__ = [
    "CustomerRequest",
    "CustomerRequestConfigurationError",
    "CustomerRequestExtractionError",
    "extract_customer_request",
]

MAX_OUTPUT_TOKENS = 500
_MAX_INVALID_OUTPUT_CHARS = 1000

_SYSTEM_PROMPT = (
    "You extract structured customer requests. Respond ONLY with JSON, "
    "no explanations and no Markdown. The customer message may be in Russian "
    "(ru), Kazakh (kk), or mixed. Output exactly these fields: "
    "intent: \"product_search\"|\"handoff\"|\"unknown\"; "
    "language: \"kk\"|\"ru\"|\"mixed\"|\"unknown\"; "
    "category: string|null; query_text: string; "
    "attributes: object with string keys and string values; "
    "budget_max_kzt: integer>=0|null; quantity: integer>=1|null; "
    "missing_field: string|null. "
    'Example: {"intent":"product_search","language":"ru","category":"sneakers",'
    '"query_text":"синие кроссовки","attributes":{},"budget_max_kzt":null,'
    '"quantity":null,"missing_field":null}'
)

_REPAIR_SYSTEM_PROMPT = (
    "Return only valid JSON matching the original extraction schema. "
    "No explanations, no Markdown."
)


class CustomerRequestConfigurationError(RuntimeError):
    """DeepSeek is not configured or rejected the request."""


class CustomerRequestExtractionError(RuntimeError):
    """Customer text could not be extracted into a valid CustomerRequest."""


class _InvalidCustomerResponseError(Exception):
    """Internal: model output could not be parsed or validated."""


class CustomerRequest(BaseModel):
    """Strict immutable structured customer request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    intent: Literal["product_search", "handoff", "unknown"]
    language: Literal["kk", "ru", "mixed", "unknown"]
    category: str | None = None
    query_text: str
    attributes: dict[str, str] = Field(default_factory=dict)
    budget_max_kzt: int | None = Field(default=None, ge=0)
    quantity: int | None = Field(default=None, ge=1)
    missing_field: str | None = None

    @field_validator("category", "missing_field", mode="before")
    @classmethod
    def _blank_optional_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("query_text")
    @classmethod
    def _query_text_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query_text must be non-blank.")
        return v

    @field_validator("attributes")
    @classmethod
    def _attributes_only_non_blank_strings(
        cls, v: dict[str, str]
    ) -> dict[str, str]:
        for key, value in v.items():
            if not key.strip() or not value.strip():
                raise ValueError(
                    "attributes must contain only non-blank strings."
                )
        return v


def _parse_and_validate(content: str) -> CustomerRequest:
    try:
        data = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise _InvalidCustomerResponseError() from exc
    if not isinstance(data, dict):
        raise _InvalidCustomerResponseError()
    try:
        return CustomerRequest(**data)
    except ValidationError as exc:
        raise _InvalidCustomerResponseError() from exc


def _request_payload(
    messages: list[dict[str, str]],
    model: str,
) -> dict[str, object]:
    return {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_OUTPUT_TOKENS,
        "thinking": {"type": "disabled"},
    }


def _repair_messages(
    text: str,
    invalid_content: str,
) -> list[dict[str, str]]:
    truncated = invalid_content[:_MAX_INVALID_OUTPUT_CHARS]
    return [
        {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Customer text:\n{text}\n\n"
                f"Invalid previous output (truncated):\n{truncated}\n\n"
                "Return corrected JSON only."
            ),
        },
    ]


async def _fetch_content(
    client: httpx.AsyncClient,
    *,
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = await client.post(
            url,
            json=_request_payload(messages, model),
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise CustomerRequestExtractionError("DeepSeek request failed.") from exc
    if response.status_code >= 400:
        raise CustomerRequestConfigurationError("DeepSeek request was rejected.")
    try:
        data = response.json()
    except ValueError as exc:
        raise CustomerRequestExtractionError("DeepSeek response was malformed.") from exc
    if not isinstance(data, dict):
        raise CustomerRequestExtractionError("DeepSeek response was malformed.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CustomerRequestExtractionError("DeepSeek response was malformed.")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise CustomerRequestExtractionError("DeepSeek response was malformed.")
    return content


async def extract_customer_request(
    *,
    text: str,
    http_client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout_seconds: float = 30.0,
) -> CustomerRequest:
    """Extract one CustomerRequest from one customer text.

    One extraction request; on invalid JSON/schema exactly one short repair
    request; otherwise a safe typed error. API key and customer text are
    never included in errors.
    """
    if not isinstance(text, str) or not text.strip():
        raise CustomerRequestExtractionError("customer text must be non-blank.")

    settings = get_settings()
    resolved_key = api_key if api_key and api_key.strip() else None
    if resolved_key is None and settings.deepseek_api_key is not None:
        resolved_key = settings.deepseek_api_key.get_secret_value()
    if resolved_key is None:
        raise CustomerRequestConfigurationError(
            "DeepSeek API key is not configured."
        )
    resolved_url = base_url or settings.deepseek_base_url
    resolved_model = model or settings.deepseek_model_primary
    url = f"{resolved_url.rstrip('/')}/chat/completions"

    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        content = await _fetch_content(
            client,
            url=url,
            api_key=resolved_key,
            model=resolved_model,
            messages=messages,
        )
        try:
            return _parse_and_validate(content)
        except _InvalidCustomerResponseError:
            repaired = await _fetch_content(
                client,
                url=url,
                api_key=resolved_key,
                model=resolved_model,
                messages=_repair_messages(text, content),
            )
            try:
                return _parse_and_validate(repaired)
            except _InvalidCustomerResponseError as exc:
                raise CustomerRequestExtractionError(
                    "Failed to extract a valid customer request."
                ) from exc
    finally:
        if own_client:
            await client.aclose()

"""WhatsApp Cloud API outbound client (MVP-2: send_text echo).

Only ``send_text`` is implemented. The HTTP client may be injected for tests;
no real Meta request is ever made by tests. Tokens, headers, and raw
responses are never included in exception messages.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_GRAPH_URL = "https://graph.facebook.com/{version}/{phone_number_id}/messages"


class WhatsAppClientError(RuntimeError):
    """Raised when an outbound WhatsApp message cannot be sent."""


@dataclass(frozen=True)
class SentWhatsAppMessage:
    """The provider-side message id returned by a successful send."""

    external_message_id: str


def _parse_send_response(response: httpx.Response) -> SentWhatsAppMessage:
    try:
        data = response.json()
    except ValueError as exc:
        raise WhatsAppClientError("WhatsApp send response was malformed.") from exc
    if not isinstance(data, dict):
        raise WhatsAppClientError("WhatsApp send response was malformed.")
    messages = data.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise WhatsAppClientError("WhatsApp send response was malformed.")
    message = messages[0]
    if not isinstance(message, dict):
        raise WhatsAppClientError("WhatsApp send response was malformed.")
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise WhatsAppClientError("WhatsApp send response was malformed.")
    return SentWhatsAppMessage(external_message_id=message_id)


class WhatsAppClient:
    """Thin client for the WhatsApp Cloud API ``/messages`` endpoint."""

    def __init__(
        self,
        *,
        access_token: str | None,
        phone_number_id: str | None,
        graph_api_version: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if access_token is None or not access_token.strip():
            raise WhatsAppClientError(
                "WhatsApp client is not configured (access token)."
            )
        if phone_number_id is None or not phone_number_id.strip():
            raise WhatsAppClientError(
                "WhatsApp client is not configured (phone number id)."
            )
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._graph_api_version = graph_api_version
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Close the internally-created HTTP client (not an injected one)."""
        if self._owns_http:
            await self._http.aclose()

    async def send_text(
        self,
        *,
        recipient: str,
        text: str,
    ) -> SentWhatsAppMessage:
        """Send a plain-text message and return the provider message id."""
        if not isinstance(recipient, str) or not recipient.strip():
            raise WhatsAppClientError("recipient must be a non-blank string.")
        if not isinstance(text, str) or not text.strip():
            raise WhatsAppClientError("text must be a non-blank string.")
        url = _GRAPH_URL.format(
            version=self._graph_api_version,
            phone_number_id=self._phone_number_id,
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text,
            },
        }
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WhatsAppClientError("WhatsApp send request failed.") from exc
        if response.status_code >= 400:
            raise WhatsAppClientError("WhatsApp send request was rejected.")
        return _parse_send_response(response)

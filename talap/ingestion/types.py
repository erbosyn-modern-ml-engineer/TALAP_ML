"""Public types and error classes for TALAP inbound ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

_COUNTER_FIELDS = (
    "messages_created",
    "messages_duplicate",
    "processing_jobs_created",
    "statuses_created",
    "statuses_duplicate",
)


class InboundIngestionError(RuntimeError):
    """Base class for all inbound-ingestion domain errors."""


class ChannelConnectionNotFoundError(InboundIngestionError):
    """The requested channel connection does not exist."""


class ChannelConnectionInactiveError(InboundIngestionError):
    """The requested channel connection is inactive."""


class ChannelConnectionMismatchError(InboundIngestionError):
    """The connection's channel does not match the requested channel."""


class InboundIngestionValidationError(InboundIngestionError):
    """The supplied ingestion inputs are invalid."""


class InboundIngestionExecutionError(InboundIngestionError):
    """An unexpected failure occurred while ingesting a webhook."""


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    """Result of one ``ingest_normalized_webhook`` call."""

    inbound_event_id: UUID
    event_created: bool
    messages_created: int
    messages_duplicate: int
    processing_jobs_created: int
    statuses_created: int
    statuses_duplicate: int

    def __post_init__(self) -> None:
        for field_name in _COUNTER_FIELDS:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"{field_name} must be a non-negative integer."
                )

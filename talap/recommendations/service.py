"""Recommendation-state and unmet-demand persistence for WhatsApp MVP-6."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talap.ai.customer_request import CustomerRequest
from talap.core import get_settings
from talap.db.models import UnmetDemand, WhatsAppRecommendationState
from talap.db.models.recommendations import (
    RECOMMENDATION_STATUS_ACTIVE,
    RECOMMENDATION_STATUS_SELECTED,
    RECOMMENDATION_STATUS_SUPERSEDED,
)

__all__ = [
    "ActiveRecommendation",
    "RECOMMENDATION_STATUS_ACTIVE",
    "RECOMMENDATION_STATUS_SELECTED",
    "RECOMMENDATION_STATUS_SUPERSEDED",
    "is_numeric_selection",
    "load_active_recommendation",
    "manager_whatsapp_link",
    "mark_recommendation_selected",
    "persist_unmet_demand",
    "store_recommendation_set",
    "unmet_demand_response",
]

UNMET_DEMAND_TEXT_RU = (
    "К сожалению, подходящих товаров сейчас не нашли. "
    "Мы сохранили ваш запрос, чтобы магазин увидел этот спрос."
)
UNMET_DEMAND_TEXT_KK = (
    "Өкінішке қарай, қазір сәйкес тауар табылмады. "
    "Дүкен бұл сұранысты көруі үшін оны сақтап қойдық."
)


@dataclass(frozen=True)
class ActiveRecommendation:
    """Immutable snapshot of the active displayed recommendation set."""

    state_id: UUID
    displayed_products: tuple[dict[str, object], ...]


def is_numeric_selection(text: str) -> bool:
    """True only when the trimmed text is exactly a canonical integer."""
    return text.isdigit() and text == str(int(text))


def manager_whatsapp_link() -> str | None:
    """Return the configured manager link, or None when unset/blank."""
    value = get_settings().manager_whatsapp_link
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def unmet_demand_response(language: str) -> str:
    """Customer-facing no-results text; Kazakh for kk, Russian otherwise."""
    if language == "kk":
        return UNMET_DEMAND_TEXT_KK
    return UNMET_DEMAND_TEXT_RU


async def store_recommendation_set(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    channel: str,
    external_user_id: str,
    displayed_products: list[dict[str, object]],
    source_message_id: UUID,
    now: datetime | None = None,
) -> UUID:
    """Supersede the previous active set and store a new active one."""
    stamp = now if now is not None else datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            update(WhatsAppRecommendationState)
            .where(
                WhatsAppRecommendationState.channel == channel,
                WhatsAppRecommendationState.external_user_id == external_user_id,
                WhatsAppRecommendationState.status == RECOMMENDATION_STATUS_ACTIVE,
            )
            .values(
                status=RECOMMENDATION_STATUS_SUPERSEDED,
                updated_at=stamp,
            )
        )
        state = WhatsAppRecommendationState(
            channel=channel,
            external_user_id=external_user_id,
            displayed_products=displayed_products,
            status=RECOMMENDATION_STATUS_ACTIVE,
            source_message_id=source_message_id,
        )
        session.add(state)
        await session.commit()
        return state.id


async def load_active_recommendation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    channel: str,
    external_user_id: str,
) -> ActiveRecommendation | None:
    """Return the customer's active displayed set, if any."""
    async with session_factory() as session:
        state = (
            await session.execute(
                select(WhatsAppRecommendationState).where(
                    WhatsAppRecommendationState.channel == channel,
                    WhatsAppRecommendationState.external_user_id == external_user_id,
                    WhatsAppRecommendationState.status == RECOMMENDATION_STATUS_ACTIVE,
                )
            )
        ).scalars().first()
        if state is None:
            return None
        return ActiveRecommendation(
            state_id=state.id,
            displayed_products=tuple(state.displayed_products),
        )


async def mark_recommendation_selected(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    state_id: UUID,
    selected_index: int,
    selected_product_id: UUID | None,
    now: datetime | None = None,
) -> None:
    """Mark the recommendation state as selected."""
    stamp = now if now is not None else datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            update(WhatsAppRecommendationState)
            .where(WhatsAppRecommendationState.id == state_id)
            .values(
                status=RECOMMENDATION_STATUS_SELECTED,
                selected_index=selected_index,
                selected_product_id=selected_product_id,
                updated_at=stamp,
            )
        )
        await session.commit()


async def persist_unmet_demand(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    channel: str,
    external_user_id: str,
    source_message_id: UUID,
    request: CustomerRequest,
) -> bool:
    """Persist one unmet-demand row; returns True if a new row was inserted.

    Idempotent per source message via ``uq_unmet_demand_source_message_id``.
    """
    async with session_factory() as session:
        try:
            session.add(
                UnmetDemand(
                    channel=channel,
                    external_user_id=external_user_id,
                    source_message_id=source_message_id,
                    query_text=request.query_text,
                    category=request.category,
                    attributes=dict(request.attributes),
                    budget_max_kzt=request.budget_max_kzt,
                    quantity=request.quantity,
                    language=request.language,
                )
            )
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False

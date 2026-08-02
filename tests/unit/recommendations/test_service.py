from __future__ import annotations

from types import SimpleNamespace

from talap.recommendations import service
from talap.recommendations.service import (
    UNMET_DEMAND_TEXT_KK,
    UNMET_DEMAND_TEXT_RU,
    is_numeric_selection,
    manager_whatsapp_link,
    unmet_demand_response,
)


def test_numeric_selection_only_exact_canonical_integers() -> None:
    for valid in ("1", "2", "3", "0", "10"):
        assert is_numeric_selection(valid), valid
    for invalid in (
        "01",
        " 1",
        "1 ",
        "хочу 2 штуки",
        "номер 1 пожалуйста",
        "1 и 2",
        "4.",
        "1.5",
        "+1",
        "-1",
        "",
        "abc",
    ):
        assert not is_numeric_selection(invalid), invalid


def test_unmet_demand_response_language() -> None:
    assert unmet_demand_response("kk") == UNMET_DEMAND_TEXT_KK
    assert unmet_demand_response("ru") == UNMET_DEMAND_TEXT_RU
    assert unmet_demand_response("mixed") == UNMET_DEMAND_TEXT_RU
    assert unmet_demand_response("unknown") == UNMET_DEMAND_TEXT_RU


def test_manager_link_loaded_from_config(monkeypatch: object) -> None:
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(manager_whatsapp_link="https://wa.me/77000000000"),
    )
    assert manager_whatsapp_link() == "https://wa.me/77000000000"


def test_manager_link_missing_returns_none(monkeypatch: object) -> None:
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(manager_whatsapp_link=None),
    )
    assert manager_whatsapp_link() is None


def test_manager_link_blank_returns_none(monkeypatch: object) -> None:
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(manager_whatsapp_link="   "),
    )
    assert manager_whatsapp_link() is None

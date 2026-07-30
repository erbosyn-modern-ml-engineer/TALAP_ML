from __future__ import annotations

from talap.db.base import Base, metadata


def test_base_uses_shared_metadata() -> None:
    assert Base.metadata is metadata


def test_naming_convention_keys() -> None:
    nc = Base.metadata.naming_convention
    assert nc is not None
    for key in ("pk", "fk", "uq", "ck", "ix"):
        assert key in nc, f"Missing naming convention key: {key}"


def test_no_tables_registered_yet() -> None:
    assert len(Base.metadata.tables) == 0

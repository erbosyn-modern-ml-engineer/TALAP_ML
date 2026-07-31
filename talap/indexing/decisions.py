from __future__ import annotations

from collections.abc import Mapping

SEMANTIC_PRODUCT_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "category",
        "material",
    }
)


def semantic_changed_fields(
    *,
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> frozenset[str]:
    """Return semantic fields whose stored value actually changed.

    Unrelated keys are ignored; only ``SEMANTIC_PRODUCT_FIELDS`` are compared.
    """
    changed: set[str] = set()
    for field_name in SEMANTIC_PRODUCT_FIELDS:
        if before.get(field_name) != after.get(field_name):
            changed.add(field_name)
    return frozenset(changed)

"""Zero-order flowsheet placement helpers."""

from __future__ import annotations


def zero_order_unit(unit_id: str, unit_type: str, config: dict | None = None) -> dict:
    """Return a declarative zero-order unit spec."""

    return {
        "unit_id": unit_id,
        "unit_type": unit_type,
        "config": dict(config or {}),
    }

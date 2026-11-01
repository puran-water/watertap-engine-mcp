"""Local re-export and mappings for the shared canonical UnitOpType enum."""

from __future__ import annotations

from typing import Any

from puran_mcp_support.engineering.unit_op_type import UNIT_OP_TYPE_VALUES, UnitOpType, normalize_unit_op_type, unit_op_type_values


WATERTAP_CATEGORY_TO_UNIT_OP_TYPE: dict[str, UnitOpType] = {
    "membrane": UnitOpType.MEMBRANE,
    "membrane_zero_order": UnitOpType.MEMBRANE_ZERO_ORDER,
    "thermal": UnitOpType.THERMAL,
    "pump": UnitOpType.PUMP,
    "pump_zero_order": UnitOpType.PUMP_ZERO_ORDER,
    "energy_recovery": UnitOpType.ENERGY_RECOVERY,
    "mixer": UnitOpType.MIXER,
    "splitter": UnitOpType.SPLITTER,
    "feed": UnitOpType.FEED,
    "feed_zero_order": UnitOpType.FEED_ZERO_ORDER,
    "product": UnitOpType.PRODUCT,
    "crystallizer": UnitOpType.CRYSTALLIZER,
    "biological": UnitOpType.BIOLOGICAL,
    "other": UnitOpType.OTHER,
}

QSDSAN_CATEGORY_TO_UNIT_OP_TYPE: dict[str, UnitOpType] = {
    "reactor": UnitOpType.REACTOR,
    "separator": UnitOpType.SEPARATOR,
    "clarifier": UnitOpType.CLARIFIER,
    "sludge": UnitOpType.SLUDGE,
    "pump": UnitOpType.PUMP,
    "junction": UnitOpType.JUNCTION,
    "utility": UnitOpType.UTILITY,
    "pretreatment": UnitOpType.PRETREATMENT,
}


def _category_value(category: Any) -> str:
    value = getattr(category, "value", category)
    return str(value).strip().lower()


def watertap_category_to_unit_op_type(category: Any) -> UnitOpType:
    """Map a WaterTAP registry category onto the canonical ontology."""

    key = _category_value(category)
    try:
        return WATERTAP_CATEGORY_TO_UNIT_OP_TYPE[key]
    except KeyError as exc:
        valid = ", ".join(sorted(WATERTAP_CATEGORY_TO_UNIT_OP_TYPE))
        raise ValueError(f"Unknown WaterTAP unit category {category!r}. Valid: {valid}") from exc


def qsdsan_category_to_unit_op_type(category: Any) -> UnitOpType:
    """Map a QSDsan registry category onto the canonical ontology."""

    key = _category_value(category)
    try:
        return QSDSAN_CATEGORY_TO_UNIT_OP_TYPE[key]
    except KeyError as exc:
        valid = ", ".join(sorted(QSDSAN_CATEGORY_TO_UNIT_OP_TYPE))
        raise ValueError(f"Unknown QSDsan unit category {category!r}. Valid: {valid}") from exc


__all__ = [
    "QSDSAN_CATEGORY_TO_UNIT_OP_TYPE",
    "UNIT_OP_TYPE_VALUES",
    "UnitOpType",
    "WATERTAP_CATEGORY_TO_UNIT_OP_TYPE",
    "normalize_unit_op_type",
    "qsdsan_category_to_unit_op_type",
    "unit_op_type_values",
    "watertap_category_to_unit_op_type",
]

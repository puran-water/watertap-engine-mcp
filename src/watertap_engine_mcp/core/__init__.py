# WaterTAP Engine MCP - Core Module
"""Core data structures and registries for WaterTAP flowsheet building."""

from .property_registry import (
    PropertyPackageType,
    PropertyPackageSpec,
    PROPERTY_PACKAGES,
    get_property_package_spec,
    is_asm2d_property_block,
    is_asm2d_property_package_type,
    list_property_packages,
)
from .translator_registry import (
    TranslatorSpec,
    TRANSLATORS,
    get_translator,
    find_translator_chain,
    check_compatibility,
    list_translators,
)
from .unit_registry import (
    UnitCategory,
    UnitSpec,
    UNITS,
    costing_supported_units,
    unit_supports_costing_package,
    get_unit_spec,
)
from .water_state import WaterTAPState
from .session import SessionConfig, FlowsheetSession, SessionManager, UnitInstance, Connection
from .unit_registry import list_units
from .session_presets import SessionPreset, SESSION_PRESETS, get_session_preset, list_session_presets

__all__ = [
    # Property packages
    "PropertyPackageType",
    "PropertyPackageSpec",
    "PROPERTY_PACKAGES",
    "get_property_package_spec",
    "is_asm2d_property_block",
    "is_asm2d_property_package_type",
    "list_property_packages",
    # Translators
    "TranslatorSpec",
    "TRANSLATORS",
    "get_translator",
    "find_translator_chain",
    "check_compatibility",
    "list_translators",
    # Units
    "UnitCategory",
    "UnitSpec",
    "UNITS",
    "costing_supported_units",
    "unit_supports_costing_package",
    "get_unit_spec",
    "list_units",
    # State
    "WaterTAPState",
    "SessionPreset",
    "SESSION_PRESETS",
    "get_session_preset",
    "list_session_presets",
    # Session
    "SessionConfig",
    "FlowsheetSession",
    "SessionManager",
    "UnitInstance",
    "Connection",
]

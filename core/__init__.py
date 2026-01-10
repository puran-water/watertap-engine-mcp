# WaterTAP Engine MCP - Core Module
"""Core data structures and registries for WaterTAP flowsheet building."""

from .property_registry import (
    PropertyPackageType,
    PropertyPackageSpec,
    PROPERTY_PACKAGES,
    get_property_package_spec,
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
    get_unit_spec,
)
from .water_state import WaterTAPState
from .session import SessionConfig, FlowsheetSession, SessionManager, UnitInstance, Connection
from .unit_registry import list_units

__all__ = [
    # Property packages
    "PropertyPackageType",
    "PropertyPackageSpec",
    "PROPERTY_PACKAGES",
    "get_property_package_spec",
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
    "get_unit_spec",
    "list_units",
    # State
    "WaterTAPState",
    # Session
    "SessionConfig",
    "FlowsheetSession",
    "SessionManager",
    "UnitInstance",
    "Connection",
]

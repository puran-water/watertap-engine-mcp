"""Auto-Translator Subsystem for WaterTAP.

Provides automatic detection and insertion of translators between units
with different property packages. Currently only ASM↔ADM translators
are supported - other packages must use the same package throughout.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.property_registry import PropertyPackageType
from core.translator_registry import (
    get_translator,
    find_translator_chain,
    check_compatibility as registry_check_compatibility,
    TranslatorSpec,
)


@dataclass
class ConnectionResult:
    """Result of connecting two units."""
    success: bool
    direct_connection: bool  # True if no translator needed
    translator_id: Optional[str] = None
    translator_spec: Optional[TranslatorSpec] = None
    warning: Optional[str] = None
    error: Optional[str] = None


class AutoTranslator:
    """Automatic translator detection and insertion.

    Analyzes property packages of source and destination units
    and determines if a translator is needed.

    IMPORTANT: Only ASM↔ADM translators exist in WaterTAP!
    For other property package combinations, users must:
    - Use the same property package throughout, OR
    - Manually handle stream compatibility
    """

    def __init__(self):
        """Initialize auto-translator."""
        self._translator_counter = 0

    def detect_package(
        self,
        unit: Any,
        port_name: str = "outlet",
    ) -> Optional[PropertyPackageType]:
        """Detect property package from a unit port.

        Args:
            unit: Unit block
            port_name: Port to check

        Returns:
            PropertyPackageType if detectable, None otherwise
        """
        try:
            # Try to get property package from port
            port = getattr(unit, port_name, None)
            if port is None:
                return None

            # Try config.property_package
            if hasattr(unit, 'config') and hasattr(unit.config, 'property_package'):
                pkg = unit.config.property_package
                return self._identify_package_type(pkg)

            # Try to infer from state block
            if hasattr(port, 'flow_mass_phase_comp'):
                # Mass-based - could be SEAWATER, NACL, WATER
                return None  # Cannot distinguish without more info

            if hasattr(port, 'flow_mol_phase_comp'):
                return PropertyPackageType.MCAS

            if hasattr(port, 'flow_vol'):
                # Volumetric - could be ASM, ADM, or ZO
                return None

            return None

        except Exception:
            return None

    def _identify_package_type(self, pkg: Any) -> Optional[PropertyPackageType]:
        """Identify PropertyPackageType from a property package instance."""
        try:
            class_name = type(pkg).__name__
            module = type(pkg).__module__

            # Map known classes to types
            mapping = {
                ("SeawaterParameterBlock", "seawater"): PropertyPackageType.SEAWATER,
                ("NaClParameterBlock", "NaCl_prop_pack"): PropertyPackageType.NACL,
                ("NaClParameterBlock", "NaCl_T_dep"): PropertyPackageType.NACL_T_DEP,
                ("WaterParameterBlock", "water_prop_pack"): PropertyPackageType.WATER,
                ("WaterParameterBlock", "zero_order"): PropertyPackageType.ZERO_ORDER,
                ("MCASParameterBlock", ""): PropertyPackageType.MCAS,
                ("ASM1ParameterBlock", ""): PropertyPackageType.ASM1,
                ("ASM2dParameterBlock", ""): PropertyPackageType.ASM2D,
                ("ADM1ParameterBlock", ""): PropertyPackageType.ADM1,
            }

            for (cls, mod_hint), pkg_type in mapping.items():
                if class_name == cls:
                    if not mod_hint or mod_hint in module:
                        return pkg_type

            return None
        except Exception:
            return None

    def check_compatibility(
        self,
        source_pkg: PropertyPackageType,
        dest_pkg: PropertyPackageType,
    ) -> Tuple[bool, Optional[str]]:
        """Check if two property packages are compatible.

        Args:
            source_pkg: Source property package type
            dest_pkg: Destination property package type

        Returns:
            Tuple of (compatible, warning_message)
            - compatible: True if can connect (with or without translator)
            - warning_message: Warning if not ideal but possible
        """
        if source_pkg == dest_pkg:
            return True, None

        # Check for translator
        chain = find_translator_chain(source_pkg, dest_pkg)
        if chain is not None:
            return True, None

        # Check if both are biological (might have indirect path)
        bio_packages = {
            PropertyPackageType.ASM1,
            PropertyPackageType.ASM2D,
            PropertyPackageType.ASM3,
            PropertyPackageType.MODIFIED_ASM2D,
            PropertyPackageType.ADM1,
            PropertyPackageType.MODIFIED_ADM1,
        }

        if source_pkg in bio_packages and dest_pkg in bio_packages:
            return False, (
                f"No direct translator from {source_pkg.name} to {dest_pkg.name}. "
                "Check if there's an intermediate translator path."
            )

        # No translator available
        return False, (
            f"No translator exists from {source_pkg.name} to {dest_pkg.name}. "
            "Use the same property package for both units."
        )

    def get_required_translator(
        self,
        source_pkg: PropertyPackageType,
        dest_pkg: PropertyPackageType,
    ) -> Optional[TranslatorSpec]:
        """Get translator spec if one is needed.

        Args:
            source_pkg: Source property package type
            dest_pkg: Destination property package type

        Returns:
            TranslatorSpec if translator needed, None if direct connection OK
        """
        if source_pkg == dest_pkg:
            return None

        return get_translator(source_pkg, dest_pkg)

    def connect_units(
        self,
        source_pkg: PropertyPackageType,
        dest_pkg: PropertyPackageType,
        source_unit_id: str,
        dest_unit_id: str,
    ) -> ConnectionResult:
        """Determine how to connect two units.

        Args:
            source_pkg: Source unit's property package
            dest_pkg: Destination unit's property package
            source_unit_id: Source unit identifier
            dest_unit_id: Destination unit identifier

        Returns:
            ConnectionResult with connection details
        """
        # Same package - direct connection
        if source_pkg == dest_pkg:
            return ConnectionResult(
                success=True,
                direct_connection=True,
            )

        # Check for translator
        translator = get_translator(source_pkg, dest_pkg)
        if translator is not None:
            self._translator_counter += 1
            translator_id = f"translator_{source_unit_id}_{dest_unit_id}"

            return ConnectionResult(
                success=True,
                direct_connection=False,
                translator_id=translator_id,
                translator_spec=translator,
            )

        # No translator available
        compatible, warning = self.check_compatibility(source_pkg, dest_pkg)

        return ConnectionResult(
            success=False,
            direct_connection=False,
            error=warning or f"Cannot connect {source_pkg.name} to {dest_pkg.name}",
        )

    def create_translator_block(
        self,
        flowsheet: Any,
        translator_id: str,
        spec: TranslatorSpec,
        source_pkg: Any,
        dest_pkg: Any,
        source_rxn_pkg: Optional[Any] = None,
        dest_rxn_pkg: Optional[Any] = None,
    ) -> Any:
        """Create a translator block on the flowsheet.

        Args:
            flowsheet: The flowsheet block (m.fs)
            translator_id: Identifier for the translator
            spec: TranslatorSpec from registry
            source_pkg: Source property package instance
            dest_pkg: Destination property package instance
            source_rxn_pkg: Optional source reaction package
            dest_rxn_pkg: Optional destination reaction package

        Returns:
            The created translator block
        """
        try:
            # Import the translator class
            import importlib
            module = importlib.import_module(spec.module_path)
            TranslatorClass = getattr(module, spec.class_name)

            # Build config
            config = {
                "inlet_property_package": source_pkg,
                "outlet_property_package": dest_pkg,
            }

            if source_rxn_pkg is not None:
                config["inlet_reaction_package"] = source_rxn_pkg
            if dest_rxn_pkg is not None:
                config["outlet_reaction_package"] = dest_rxn_pkg

            # Create translator
            translator = TranslatorClass(**config)
            setattr(flowsheet, translator_id, translator)

            return translator

        except Exception as e:
            raise RuntimeError(f"Failed to create translator: {e}")


def check_connection_compatibility(
    source_pkg: PropertyPackageType,
    dest_pkg: PropertyPackageType,
) -> Dict[str, Any]:
    """Check if two packages can be connected.

    Args:
        source_pkg: Source property package type
        dest_pkg: Destination property package type

    Returns:
        Dict with compatibility info
    """
    auto = AutoTranslator()
    compatible, warning = auto.check_compatibility(source_pkg, dest_pkg)
    translator = auto.get_required_translator(source_pkg, dest_pkg)

    return {
        "source": source_pkg.name,
        "destination": dest_pkg.name,
        "compatible": compatible,
        "needs_translator": translator is not None,
        "translator": translator.class_name if translator else None,
        "warning": warning,
    }

"""Translator Registry for WaterTAP.

Provides metadata for property package translators that enable connections
between units with different property packages.

CRITICAL: Only ASM↔ADM translators actually exist in WaterTAP!
The following translators DO NOT EXIST:
- ZO → Seawater
- Seawater → Water
- MCAS → Seawater
- Seawater → NaCl

For non-biological flowsheets, users must use the SAME property package
throughout OR manually handle stream compatibility.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .property_registry import PropertyPackageType


@dataclass
class TranslatorSpec:
    """Specification for a WaterTAP translator block."""

    name: str
    class_name: str
    module_path: str

    source_pkg: PropertyPackageType
    dest_pkg: PropertyPackageType

    # Description of what the translator does
    description: str = ""

    # Required configuration for the translator
    config_kwargs: Dict[str, str] = field(default_factory=dict)

    # Whether this translator requires reaction packages
    requires_reaction_packages: bool = False


# Translator Registry
# Maps (source, dest) tuple to TranslatorSpec
TRANSLATORS: Dict[Tuple[PropertyPackageType, PropertyPackageType], TranslatorSpec] = {

    # ==================== ASM ↔ ADM TRANSLATORS ====================
    # These are the ONLY translators that actually exist in WaterTAP!

    (PropertyPackageType.ASM1, PropertyPackageType.ADM1): TranslatorSpec(
        name="Translator_ASM1_ADM1",
        class_name="Translator_ASM1_ADM1",
        module_path="watertap.unit_models.translators.translator_asm1_adm1",
        source_pkg=PropertyPackageType.ASM1,
        dest_pkg=PropertyPackageType.ADM1,
        description="Translates ASM1 state variables to ADM1 format for AS→AD connections",
        requires_reaction_packages=True,
        config_kwargs={
            "inlet_property_package": "ASM1 property package instance",
            "outlet_property_package": "ADM1 property package instance",
            "inlet_reaction_package": "ASM1 reaction package instance",
            "outlet_reaction_package": "ADM1 reaction package instance",
        },
    ),

    (PropertyPackageType.ADM1, PropertyPackageType.ASM1): TranslatorSpec(
        name="Translator_ADM1_ASM1",
        class_name="Translator_ADM1_ASM1",
        module_path="watertap.unit_models.translators.translator_adm1_asm1",
        source_pkg=PropertyPackageType.ADM1,
        dest_pkg=PropertyPackageType.ASM1,
        description="Translates ADM1 state variables to ASM1 format for AD→AS connections",
        requires_reaction_packages=True,
        config_kwargs={
            "inlet_property_package": "ADM1 property package instance",
            "outlet_property_package": "ASM1 property package instance",
            "inlet_reaction_package": "ADM1 reaction package instance",
            "outlet_reaction_package": "ASM1 reaction package instance",
        },
    ),

    (PropertyPackageType.ASM2D, PropertyPackageType.ADM1): TranslatorSpec(
        name="Translator_ASM2d_ADM1",
        class_name="Translator_ASM2d_ADM1",
        module_path="watertap.unit_models.translators.translator_asm2d_adm1",
        source_pkg=PropertyPackageType.ASM2D,
        dest_pkg=PropertyPackageType.ADM1,
        description="Translates ASM2d state variables to ADM1 format",
        requires_reaction_packages=True,
        config_kwargs={
            "inlet_property_package": "ASM2d property package instance",
            "outlet_property_package": "ADM1 property package instance",
            "inlet_reaction_package": "ASM2d reaction package instance",
            "outlet_reaction_package": "ADM1 reaction package instance",
        },
    ),

    (PropertyPackageType.ADM1, PropertyPackageType.ASM2D): TranslatorSpec(
        name="Translator_ADM1_ASM2d",
        class_name="Translator_ADM1_ASM2d",
        module_path="watertap.unit_models.translators.translator_adm1_asm2d",
        source_pkg=PropertyPackageType.ADM1,
        dest_pkg=PropertyPackageType.ASM2D,
        description="Translates ADM1 state variables to ASM2d format",
        requires_reaction_packages=True,
        config_kwargs={
            "inlet_property_package": "ADM1 property package instance",
            "outlet_property_package": "ASM2d property package instance",
            "inlet_reaction_package": "ADM1 reaction package instance",
            "outlet_reaction_package": "ASM2d reaction package instance",
        },
    ),

    # Modified ASM2d to ADM1
    (PropertyPackageType.MODIFIED_ASM2D, PropertyPackageType.ADM1): TranslatorSpec(
        name="Translator_ModifiedASM2d_ADM1",
        class_name="Translator_ModifiedASM2d_ADM1",
        module_path="watertap.unit_models.translators.translator_modified_asm2d_adm1",
        source_pkg=PropertyPackageType.MODIFIED_ASM2D,
        dest_pkg=PropertyPackageType.ADM1,
        description="Translates Modified ASM2d state variables to ADM1 format",
        requires_reaction_packages=True,
        config_kwargs={
            "inlet_property_package": "Modified ASM2d property package instance",
            "outlet_property_package": "ADM1 property package instance",
            "inlet_reaction_package": "Modified ASM2d reaction package instance",
            "outlet_reaction_package": "ADM1 reaction package instance",
        },
    ),

    (PropertyPackageType.ADM1, PropertyPackageType.MODIFIED_ASM2D): TranslatorSpec(
        name="Translator_ADM1_ModifiedASM2d",
        class_name="Translator_ADM1_ModifiedASM2d",
        module_path="watertap.unit_models.translators.translator_adm1_modified_asm2d",
        source_pkg=PropertyPackageType.ADM1,
        dest_pkg=PropertyPackageType.MODIFIED_ASM2D,
        description="Translates ADM1 state variables to Modified ASM2d format",
        requires_reaction_packages=True,
        config_kwargs={
            "inlet_property_package": "ADM1 property package instance",
            "outlet_property_package": "Modified ASM2d property package instance",
            "inlet_reaction_package": "ADM1 reaction package instance",
            "outlet_reaction_package": "Modified ASM2d reaction package instance",
        },
    ),

    # Modified ADM1 translators
    (PropertyPackageType.ASM2D, PropertyPackageType.MODIFIED_ADM1): TranslatorSpec(
        name="Translator_ASM2d_ModifiedADM1",
        class_name="Translator_ASM2d_ModifiedADM1",
        module_path="watertap.unit_models.translators.translator_asm2d_modified_adm1",
        source_pkg=PropertyPackageType.ASM2D,
        dest_pkg=PropertyPackageType.MODIFIED_ADM1,
        description="Translates ASM2d state variables to Modified ADM1 format",
        requires_reaction_packages=True,
    ),

    (PropertyPackageType.MODIFIED_ADM1, PropertyPackageType.ASM2D): TranslatorSpec(
        name="Translator_ModifiedADM1_ASM2d",
        class_name="Translator_ModifiedADM1_ASM2d",
        module_path="watertap.unit_models.translators.translator_modified_adm1_asm2d",
        source_pkg=PropertyPackageType.MODIFIED_ADM1,
        dest_pkg=PropertyPackageType.ASM2D,
        description="Translates Modified ADM1 state variables to ASM2d format",
        requires_reaction_packages=True,
    ),
}


def get_translator(
    source: PropertyPackageType,
    dest: PropertyPackageType
) -> Optional[TranslatorSpec]:
    """Get translator specification for a source/destination pair.

    Args:
        source: Source property package type
        dest: Destination property package type

    Returns:
        TranslatorSpec if a translator exists, None otherwise
    """
    return TRANSLATORS.get((source, dest))


def find_translator_chain(
    source: PropertyPackageType,
    dest: PropertyPackageType
) -> Optional[List[TranslatorSpec]]:
    """Find a chain of translators to connect source to destination.

    Currently only supports direct connections (single translator).
    Multi-hop translator chains are not implemented as they don't exist
    in practice for WaterTAP.

    Args:
        source: Source property package type
        dest: Destination property package type

    Returns:
        List of TranslatorSpec objects forming the chain, or None if no path
    """
    # Direct connection - same package, no translator needed
    if source == dest:
        return []

    # Try direct translator
    direct = get_translator(source, dest)
    if direct is not None:
        return [direct]

    # No multi-hop chains supported - they don't exist in WaterTAP
    return None


def list_translators(
    source: Optional[PropertyPackageType] = None,
    dest: Optional[PropertyPackageType] = None
) -> List[TranslatorSpec]:
    """List available translators with optional filtering.

    Args:
        source: Filter by source property package
        dest: Filter by destination property package

    Returns:
        List of matching TranslatorSpec objects
    """
    results = list(TRANSLATORS.values())

    if source is not None:
        results = [t for t in results if t.source_pkg == source]

    if dest is not None:
        results = [t for t in results if t.dest_pkg == dest]

    return results


def check_compatibility(
    source: PropertyPackageType,
    dest: PropertyPackageType
) -> Dict[str, any]:
    """Check if two property packages can be connected.

    Args:
        source: Source property package type
        dest: Destination property package type

    Returns:
        Dict with:
        - compatible: bool
        - requires_translator: bool
        - translator: TranslatorSpec or None
        - message: str explaining the situation
    """
    # Same package - always compatible
    if source == dest:
        return {
            "compatible": True,
            "requires_translator": False,
            "translator": None,
            "message": f"Direct connection: both units use {source.value}",
        }

    # Check for translator
    translator = get_translator(source, dest)
    if translator is not None:
        return {
            "compatible": True,
            "requires_translator": True,
            "translator": translator,
            "message": f"Translator available: {translator.name}",
        }

    # No translator - incompatible
    return {
        "compatible": False,
        "requires_translator": True,
        "translator": None,
        "message": (
            f"No translator exists for {source.value} → {dest.value}. "
            f"WaterTAP only provides ASM↔ADM translators. "
            f"Consider using the same property package for both units."
        ),
    }


def get_import_statement(translator: TranslatorSpec) -> str:
    """Generate Python import statement for a translator.

    Args:
        translator: The translator spec

    Returns:
        Python import statement string
    """
    return f"from {translator.module_path} import {translator.class_name}"

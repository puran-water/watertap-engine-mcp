"""Tests for property, translator, and unit registries."""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.property_registry import (
    PropertyPackageType,
    PROPERTY_PACKAGES,
    get_property_package_spec,
    list_property_packages,
)
from core.translator_registry import (
    TRANSLATORS,
    get_translator,
    find_translator_chain,
    list_translators,
)
from core.unit_registry import (
    UnitCategory,
    UNITS,
    get_unit_spec,
    list_units,
)


class TestPropertyRegistry:
    """Tests for property package registry."""

    def test_all_packages_have_specs(self):
        """Every PropertyPackageType should have a corresponding spec."""
        for pkg_type in PropertyPackageType:
            assert pkg_type in PROPERTY_PACKAGES, f"Missing spec for {pkg_type}"

    def test_package_count(self):
        """Should have 13 property packages."""
        # 13 packages: SEAWATER, NACL, NACL_T_DEP, WATER, MCAS, ZERO_ORDER,
        # ASM1, ASM2D, ASM3, MODIFIED_ASM2D, ADM1, MODIFIED_ADM1, ADM1_VAPOR
        assert len(PROPERTY_PACKAGES) == 13

    def test_module_paths_set(self):
        """Every package must have a module_path to avoid class-name collisions."""
        for pkg_type, spec in PROPERTY_PACKAGES.items():
            assert spec.module_path, f"{pkg_type} missing module_path"
            assert "watertap" in spec.module_path or "idaes" in spec.module_path

    def test_seawater_package(self):
        """Verify SEAWATER package spec."""
        spec = get_property_package_spec(PropertyPackageType.SEAWATER)
        assert spec is not None
        assert spec.class_name == "SeawaterParameterBlock"
        assert "H2O" in spec.required_components
        assert "TDS" in spec.required_components
        assert spec.flow_basis == "mass"
        assert "Liq" in spec.phases

    def test_nacl_collision_warning(self):
        """NaClParameterBlock exists in two modules - verify we have both."""
        nacl = get_property_package_spec(PropertyPackageType.NACL)
        nacl_t_dep = get_property_package_spec(PropertyPackageType.NACL_T_DEP)

        assert nacl is not None
        assert nacl_t_dep is not None
        # Both have same class name!
        assert nacl.class_name == "NaClParameterBlock"
        assert nacl_t_dep.class_name == "NaClParameterBlock"
        # But different module paths
        assert nacl.module_path != nacl_t_dep.module_path

    def test_water_collision_warning(self):
        """WaterParameterBlock exists in two modules - verify we have both."""
        water = get_property_package_spec(PropertyPackageType.WATER)
        zo = get_property_package_spec(PropertyPackageType.ZERO_ORDER)

        assert water is not None
        assert zo is not None
        # Both have same class name!
        assert water.class_name == "WaterParameterBlock"
        assert zo.class_name == "WaterParameterBlock"
        # But different module paths
        assert water.module_path != zo.module_path

    def test_list_property_packages(self):
        """List function should return all packages."""
        packages = list_property_packages()
        assert len(packages) == 13

    def test_mcas_requires_config(self):
        """MCAS requires solute_list and charge config."""
        spec = get_property_package_spec(PropertyPackageType.MCAS)
        assert spec.requires_config is True
        assert "solute_list" in spec.config_fields
        assert "charge" in spec.config_fields

    def test_biological_packages_require_reaction(self):
        """ASM/ADM packages require reaction packages."""
        for pkg_type in [
            PropertyPackageType.ASM1,
            PropertyPackageType.ASM2D,
            PropertyPackageType.ADM1,
        ]:
            spec = get_property_package_spec(pkg_type)
            assert spec.requires_reaction_package is True


class TestTranslatorRegistry:
    """Tests for translator registry."""

    def test_only_asm_adm_translators(self):
        """Only ASM↔ADM translators exist - no ZO/Seawater/MCAS translators."""
        for (src, dest), spec in TRANSLATORS.items():
            src_name = src.name
            dest_name = dest.name
            # Both must be biological packages
            assert "ASM" in src_name or "ADM" in src_name, f"Unexpected source: {src_name}"
            assert "ASM" in dest_name or "ADM" in dest_name, f"Unexpected dest: {dest_name}"

    def test_translator_count(self):
        """Should have 8 ASM↔ADM translators."""
        assert len(TRANSLATORS) == 8

    def test_asm1_adm1_translator(self):
        """Verify ASM1 → ADM1 translator exists."""
        spec = get_translator(PropertyPackageType.ASM1, PropertyPackageType.ADM1)
        assert spec is not None
        assert "Translator_ASM1_ADM1" in spec.class_name

    def test_adm1_asm1_translator(self):
        """Verify ADM1 → ASM1 translator exists."""
        spec = get_translator(PropertyPackageType.ADM1, PropertyPackageType.ASM1)
        assert spec is not None
        assert "Translator_ADM1_ASM1" in spec.class_name

    def test_no_seawater_translator(self):
        """Verify no SEAWATER translators exist (critical correction)."""
        # ZO → Seawater does NOT exist
        assert get_translator(PropertyPackageType.ZERO_ORDER, PropertyPackageType.SEAWATER) is None
        # Seawater → NaCl does NOT exist
        assert get_translator(PropertyPackageType.SEAWATER, PropertyPackageType.NACL) is None
        # MCAS → Seawater does NOT exist
        assert get_translator(PropertyPackageType.MCAS, PropertyPackageType.SEAWATER) is None

    def test_translator_chain_same_package(self):
        """Same package should return empty chain (direct connection)."""
        chain = find_translator_chain(PropertyPackageType.SEAWATER, PropertyPackageType.SEAWATER)
        assert chain == []

    def test_translator_chain_no_path(self):
        """Incompatible packages should return None."""
        chain = find_translator_chain(PropertyPackageType.SEAWATER, PropertyPackageType.NACL)
        assert chain is None  # No translator exists

    def test_translator_chain_biological(self):
        """ASM1 → ADM1 should return single translator."""
        chain = find_translator_chain(PropertyPackageType.ASM1, PropertyPackageType.ADM1)
        assert chain is not None
        assert len(chain) == 1

    def test_list_translators(self):
        """List function should return all translators."""
        translators = list_translators()
        assert len(translators) == 8


class TestUnitRegistry:
    """Tests for unit registry."""

    def test_unit_count(self):
        """Should have reasonable number of units."""
        assert len(UNITS) >= 15  # At minimum

    def test_ro0d_spec(self):
        """Verify ReverseOsmosis0D spec."""
        spec = get_unit_spec("ReverseOsmosis0D")
        assert spec is not None
        assert spec.category == UnitCategory.MEMBRANE
        assert len(spec.required_fixes) >= 4  # A_comp, B_comp, area, permeate.pressure

    def test_pump_spec(self):
        """Verify Pump spec."""
        spec = get_unit_spec("Pump")
        assert spec is not None
        assert spec.category == UnitCategory.PUMP
        assert len(spec.required_fixes) >= 2  # efficiency, outlet pressure

    def test_idaes_units_marked(self):
        """Mixer, Separator, Feed, Product are IDAES units."""
        for unit_name in ["Mixer", "Separator", "Feed", "Product"]:
            spec = get_unit_spec(unit_name)
            assert spec is not None
            assert spec.is_idaes_unit is True, f"{unit_name} should be marked as IDAES unit"
            assert "idaes" in spec.module_path

    def test_watertap_units_not_idaes(self):
        """RO, NF, Pump are WaterTAP units."""
        for unit_name in ["ReverseOsmosis0D", "Nanofiltration0D", "Pump"]:
            spec = get_unit_spec(unit_name)
            assert spec is not None
            assert spec.is_idaes_unit is False

    def test_zo_units_exist(self):
        """Zero-order units should exist with correct names."""
        # Correct names (not NFZO/UFZO)
        nf_zo = get_unit_spec("NanofiltrationZO")
        uf_zo = get_unit_spec("UltraFiltrationZO")
        assert nf_zo is not None
        assert uf_zo is not None

    def test_rozo_does_not_exist(self):
        """ROZO does NOT exist in WaterTAP."""
        # get_unit_spec raises KeyError for unknown units
        with pytest.raises(KeyError, match="Unknown unit type"):
            get_unit_spec("ReverseOsmosisZO")

    def test_list_units(self):
        """List function should return all units."""
        units = list_units()
        assert len(units) >= 15

    def test_typical_values_present(self):
        """Units should have typical values for common fixes."""
        spec = get_unit_spec("ReverseOsmosis0D")
        assert spec.typical_values is not None

    def test_default_scaling_present(self):
        """Units should have default scaling factors."""
        spec = get_unit_spec("ReverseOsmosis0D")
        assert spec.default_scaling is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

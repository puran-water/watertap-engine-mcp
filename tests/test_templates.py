"""Tests for flowsheet templates."""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from templates import (
    ROTrainTemplate,
    NFSofteningTemplate,
    MVCCrystallizerTemplate,
)
from templates.ro_train import ROTrainConfig
from templates.nf_softening import NFSofteningConfig
from templates.mvc_crystallizer import MVCCrystallizerConfig
from core.property_registry import PropertyPackageType


class TestROTrainTemplate:
    """Tests for RO train template."""

    def test_default_config(self):
        """Should create with default config."""
        config = ROTrainConfig()
        assert config.property_package == PropertyPackageType.SEAWATER
        assert config.membrane_type == "SWRO"
        assert config.include_erd is True

    def test_template_creation(self):
        """Template should create with default config."""
        template = ROTrainTemplate()
        assert template.config is not None
        assert template.units is not None

    def test_get_units(self):
        """Should return correct units."""
        template = ROTrainTemplate()
        units = template.get_units()
        unit_ids = [u["unit_id"] for u in units]
        assert "Feed" in unit_ids
        assert "HPPump" in unit_ids
        assert "RO" in unit_ids
        assert "Permeate" in unit_ids
        assert "Brine" in unit_ids

    def test_get_units_with_erd(self):
        """ERD units should be included when enabled."""
        config = ROTrainConfig(include_erd=True)
        template = ROTrainTemplate(config)
        units = template.get_units()
        unit_ids = [u["unit_id"] for u in units]
        assert "ERD" in unit_ids
        assert "Booster" in unit_ids

    def test_get_units_without_erd(self):
        """ERD units should be excluded when disabled."""
        config = ROTrainConfig(include_erd=False)
        template = ROTrainTemplate(config)
        units = template.get_units()
        unit_ids = [u["unit_id"] for u in units]
        assert "ERD" not in unit_ids
        assert "Booster" not in unit_ids

    def test_get_connections(self):
        """Should return connections."""
        template = ROTrainTemplate()
        connections = template.get_connections()
        assert len(connections) > 0
        # Check feed to pump connection
        feed_to_pump = any(
            c["src_unit"] == "Feed" and c["dest_unit"] == "HPPump"
            for c in connections
        )
        assert feed_to_pump

    def test_get_dof_fixes(self):
        """Should return DOF fixes."""
        template = ROTrainTemplate()
        fixes = template.get_dof_fixes()
        var_names = [f["var_name"] for f in fixes]
        # Check for typical RO fixes
        assert any("A_comp" in v for v in var_names)
        assert any("B_comp" in v for v in var_names)
        assert any("area" in v for v in var_names)

    def test_get_scaling_factors(self):
        """Should return scaling factors."""
        template = ROTrainTemplate()
        factors = template.get_scaling_factors()
        assert len(factors) > 0

    def test_get_initialization_order(self):
        """Should return init order starting with Feed."""
        template = ROTrainTemplate()
        order = template.get_initialization_order()
        assert order[0] == "Feed"
        assert "RO" in order

    def test_to_session_spec(self):
        """Should return complete session spec."""
        template = ROTrainTemplate()
        spec = template.to_session_spec()
        assert "property_package" in spec
        assert "units" in spec
        assert "connections" in spec
        assert "dof_fixes" in spec
        assert "scaling_factors" in spec
        assert "initialization_order" in spec


class TestNFSofteningTemplate:
    """Tests for NF softening template."""

    def test_default_config(self):
        """Should create with default config."""
        config = NFSofteningConfig()
        assert config.property_package == PropertyPackageType.MCAS
        assert config.feed_pressure_bar == 10.0  # NF is lower pressure

    def test_template_creation(self):
        """Template should create with default config."""
        template = NFSofteningTemplate()
        assert template.config is not None

    def test_get_units(self):
        """Should return NF-specific units."""
        template = NFSofteningTemplate()
        units = template.get_units()
        unit_types = [u["unit_type"] for u in units]
        assert "Nanofiltration0D" in unit_types

    def test_get_connections(self):
        """Should return connections."""
        template = NFSofteningTemplate()
        connections = template.get_connections()
        assert len(connections) > 0

    def test_to_session_spec(self):
        """Should return complete session spec."""
        template = NFSofteningTemplate()
        spec = template.to_session_spec()
        assert spec["property_package"] == "MCAS"


class TestMVCCrystallizerTemplate:
    """Tests for MVC crystallizer template."""

    def test_default_config(self):
        """Should create with default config."""
        config = MVCCrystallizerConfig()
        assert config.property_package == PropertyPackageType.NACL
        assert config.operating_temp_C == 70.0

    def test_template_creation(self):
        """Template should create with default config."""
        template = MVCCrystallizerTemplate()
        assert template.config is not None

    def test_get_units(self):
        """Should return MVC-specific units."""
        template = MVCCrystallizerTemplate()
        units = template.get_units()
        unit_types = [u["unit_type"] for u in units]
        assert "Evaporator" in unit_types
        assert "Compressor" in unit_types
        assert "Condenser" in unit_types
        assert "Crystallization" in unit_types

    def test_get_connections(self):
        """Should return connections including vapor path."""
        template = MVCCrystallizerTemplate()
        connections = template.get_connections()
        # Check vapor path exists
        vapor_connections = [
            c for c in connections
            if "vapor" in c["src_port"].lower() or "vapor" in c["dest_port"].lower()
        ]
        assert len(vapor_connections) > 0

    def test_get_dof_fixes(self):
        """Should include crystallizer-specific fixes."""
        template = MVCCrystallizerTemplate()
        fixes = template.get_dof_fixes()
        var_names = [f["var_name"] for f in fixes]
        assert any("crystal_growth_rate" in v for v in var_names)
        assert any("compressor" in f["unit_id"].lower() for f in fixes)

    def test_to_session_spec(self):
        """Should return complete session spec."""
        template = MVCCrystallizerTemplate()
        spec = template.to_session_spec()
        assert spec["property_package"] == "NACL"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

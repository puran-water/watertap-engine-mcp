"""Tests for utility modules (topo_sort, state_translator, auto_translator)."""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.topo_sort import (
    compute_initialization_order,
    SequentialDecompositionError,
)
from utils.state_translator import (
    StateTranslator,
    create_state_args,
    MOLECULAR_WEIGHTS,
)
from utils.auto_translator import (
    AutoTranslator,
    ConnectionResult,
    check_connection_compatibility,
)
from core.property_registry import PropertyPackageType


class TestInitializationOrder:
    """Tests for initialization order computation."""

    def test_linear_flowsheet(self):
        """Linear flowsheet should sort correctly."""
        units = {"Feed": None, "Pump": None, "RO": None, "Product": None}
        connections = [
            {"src_unit": "Feed", "src_port": "outlet", "dest_unit": "Pump", "dest_port": "inlet"},
            {"src_unit": "Pump", "src_port": "outlet", "dest_unit": "RO", "dest_port": "inlet"},
            {"src_unit": "RO", "src_port": "permeate", "dest_unit": "Product", "dest_port": "inlet"},
        ]
        order = compute_initialization_order(units, connections)
        assert order.index("Feed") < order.index("Pump")
        assert order.index("Pump") < order.index("RO")
        assert order.index("RO") < order.index("Product")

    def test_branched_flowsheet(self):
        """Branched flowsheet should sort correctly."""
        units = {"Feed": None, "RO": None, "Permeate": None, "Brine": None}
        connections = [
            {"src_unit": "Feed", "src_port": "outlet", "dest_unit": "RO", "dest_port": "inlet"},
            {"src_unit": "RO", "src_port": "permeate", "dest_unit": "Permeate", "dest_port": "inlet"},
            {"src_unit": "RO", "src_port": "retentate", "dest_unit": "Brine", "dest_port": "inlet"},
        ]
        order = compute_initialization_order(units, connections)
        assert order.index("Feed") < order.index("RO")
        assert order.index("RO") < order.index("Permeate")
        assert order.index("RO") < order.index("Brine")

    def test_cycle_detection(self):
        """Cycle without tear streams should raise error."""
        units = {"A": None, "B": None}
        connections = [
            {"src_unit": "A", "src_port": "out", "dest_unit": "B", "dest_port": "in"},
            {"src_unit": "B", "src_port": "out", "dest_unit": "A", "dest_port": "in"},
        ]
        with pytest.raises(SequentialDecompositionError, match="Cycle detected"):
            compute_initialization_order(units, connections)

    def test_tear_stream_breaks_cycle(self):
        """Tear streams should break cycles."""
        units = {"A": None, "B": None}
        connections = [
            {"src_unit": "A", "src_port": "out", "dest_unit": "B", "dest_port": "in"},
            {"src_unit": "B", "src_port": "out", "dest_unit": "A", "dest_port": "in"},
        ]
        # Specify tear to break cycle
        order = compute_initialization_order(units, connections, tear_streams=[("B", "A")])
        assert len(order) == 2


class TestStateTranslator:
    """Tests for state translator."""

    def test_molecular_weights(self):
        """Molecular weights should be defined for common species."""
        assert "H2O" in MOLECULAR_WEIGHTS
        assert "NaCl" in MOLECULAR_WEIGHTS
        assert "Na_+" in MOLECULAR_WEIGHTS
        assert "Cl_-" in MOLECULAR_WEIGHTS
        assert MOLECULAR_WEIGHTS["H2O"] == pytest.approx(18.015, rel=1e-2)

    def test_to_seawater_state(self):
        """Should convert to SEAWATER format."""
        translator = StateTranslator()
        state = translator.to_seawater_state(
            flow_vol_m3_s=0.001,
            tds_kg_m3=35.0,
            temperature_K=298.15,
            pressure_Pa=101325.0,
        )
        assert "flow_mass_phase_comp" in state
        assert ("Liq", "H2O") in state["flow_mass_phase_comp"]
        assert ("Liq", "TDS") in state["flow_mass_phase_comp"]
        assert state["temperature"] == 298.15
        assert state["pressure"] == 101325.0

    def test_to_nacl_state(self):
        """Should convert to NaCl format."""
        translator = StateTranslator()
        state = translator.to_nacl_state(
            flow_vol_m3_s=0.001,
            nacl_kg_m3=35.0,
        )
        assert ("Liq", "NaCl") in state["flow_mass_phase_comp"]

    def test_to_mcas_state(self):
        """Should convert to MCAS molar format."""
        translator = StateTranslator()
        components_mol_m3 = {"Na_+": 500, "Cl_-": 500}
        state = translator.to_mcas_state(
            flow_vol_m3_s=0.001,
            components_mol_m3=components_mol_m3,
        )
        assert "flow_mol_phase_comp" in state
        assert ("Liq", "H2O") in state["flow_mol_phase_comp"]
        assert ("Liq", "Na_+") in state["flow_mol_phase_comp"]

    def test_to_zero_order_state(self):
        """Should convert to ZO volumetric format."""
        translator = StateTranslator()
        state = translator.to_zero_order_state(flow_vol_m3_s=0.001)
        assert "flow_vol" in state
        assert state["flow_vol"] == 0.001

    def test_convert_mass_to_molar(self):
        """Should convert mass to molar correctly."""
        translator = StateTranslator()
        # 58.44 g/mol NaCl, 1 kg/s = 1000 g/s = ~17.1 mol/s
        mol_flow = translator.convert_mass_to_molar(1.0, "NaCl")
        expected = 1000 / 58.44
        assert mol_flow == pytest.approx(expected, rel=1e-2)


class TestCreateStateArgs:
    """Tests for create_state_args convenience function."""

    def test_seawater_defaults(self):
        """Should create SEAWATER state with defaults."""
        state = create_state_args(
            property_package=PropertyPackageType.SEAWATER,
            flow_vol_m3_hr=100.0,
        )
        assert "flow_mass_phase_comp" in state
        assert ("Liq", "TDS") in state["flow_mass_phase_comp"]

    def test_mcas_requires_components(self):
        """MCAS should require components_mg_L."""
        with pytest.raises(ValueError, match="MCAS requires"):
            create_state_args(
                property_package=PropertyPackageType.MCAS,
                flow_vol_m3_hr=100.0,
            )


class TestAutoTranslator:
    """Tests for auto-translator."""

    def test_connection_result_dataclass(self):
        """ConnectionResult should store connection details."""
        result = ConnectionResult(
            success=True,
            direct_connection=True,
        )
        assert result.success is True
        assert result.translator_id is None

    def test_auto_translator_creation(self):
        """AutoTranslator should create successfully."""
        auto = AutoTranslator()
        assert auto._translator_counter == 0

    def test_same_package_compatibility(self):
        """Same package should be compatible."""
        auto = AutoTranslator()
        compatible, warning = auto.check_compatibility(
            PropertyPackageType.SEAWATER,
            PropertyPackageType.SEAWATER,
        )
        assert compatible is True
        assert warning is None

    def test_biological_translator_compatibility(self):
        """ASM1 → ADM1 should be compatible (translator exists)."""
        auto = AutoTranslator()
        compatible, warning = auto.check_compatibility(
            PropertyPackageType.ASM1,
            PropertyPackageType.ADM1,
        )
        assert compatible is True

    def test_no_translator_incompatibility(self):
        """SEAWATER → NACL should be incompatible (no translator)."""
        auto = AutoTranslator()
        compatible, warning = auto.check_compatibility(
            PropertyPackageType.SEAWATER,
            PropertyPackageType.NACL,
        )
        assert compatible is False
        assert warning is not None
        assert "No translator" in warning

    def test_connect_units_same_package(self):
        """Same package should give direct connection."""
        auto = AutoTranslator()
        result = auto.connect_units(
            PropertyPackageType.SEAWATER,
            PropertyPackageType.SEAWATER,
            "RO1",
            "RO2",
        )
        assert result.success is True
        assert result.direct_connection is True
        assert result.translator_id is None

    def test_connect_units_with_translator(self):
        """Different biological packages should give translator connection."""
        auto = AutoTranslator()
        result = auto.connect_units(
            PropertyPackageType.ASM1,
            PropertyPackageType.ADM1,
            "Aeration",
            "Digester",
        )
        assert result.success is True
        assert result.direct_connection is False
        assert result.translator_id is not None
        assert result.translator_spec is not None

    def test_connect_units_incompatible(self):
        """Incompatible packages should fail."""
        auto = AutoTranslator()
        result = auto.connect_units(
            PropertyPackageType.SEAWATER,
            PropertyPackageType.MCAS,
            "RO",
            "NF",
        )
        assert result.success is False
        assert result.error is not None


class TestCheckConnectionCompatibility:
    """Tests for the top-level compatibility check function."""

    def test_returns_dict(self):
        """Should return a dict with expected keys."""
        result = check_connection_compatibility(
            PropertyPackageType.SEAWATER,
            PropertyPackageType.SEAWATER,
        )
        assert "source" in result
        assert "destination" in result
        assert "compatible" in result
        assert "needs_translator" in result

    def test_same_package(self):
        """Same package should be compatible without translator."""
        result = check_connection_compatibility(
            PropertyPackageType.SEAWATER,
            PropertyPackageType.SEAWATER,
        )
        assert result["compatible"] is True
        assert result["needs_translator"] is False

    def test_with_translator(self):
        """Biological packages should need translator."""
        result = check_connection_compatibility(
            PropertyPackageType.ASM1,
            PropertyPackageType.ADM1,
        )
        assert result["compatible"] is True
        assert result["needs_translator"] is True
        assert result["translator"] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

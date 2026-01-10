"""Tests for water state abstraction."""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.water_state import WaterTAPState
from core.property_registry import PropertyPackageType


class TestWaterTAPState:
    """Tests for WaterTAPState dataclass."""

    def test_create_basic_state(self):
        """Create basic water state."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            temperature_C=25,
            pressure_bar=1,
            components={"TDS": 35000},
        )
        assert state.flow_vol_m3_hr == 100
        assert state.temperature_C == 25
        assert state.pressure_bar == 1
        assert state.components["TDS"] == 35000

    def test_default_values(self):
        """Default values should be set."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            components={"TDS": 35000},
        )
        assert state.temperature_C == 25.0
        assert state.pressure_bar == 1.0
        assert state.concentration_units == "mg/L"
        assert state.concentration_basis == "mass"

    def test_to_seawater_state_args(self):
        """Convert to SEAWATER package state_args."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            temperature_C=25,
            pressure_bar=1,
            components={"TDS": 35000},
        )
        args = state.to_state_args(PropertyPackageType.SEAWATER)

        assert "flow_mass_phase_comp" in args
        assert "temperature" in args
        assert "pressure" in args
        # Temperature should be in Kelvin
        assert args["temperature"] == pytest.approx(298.15, rel=0.01)
        # Pressure should be in Pa (1 bar = 1e5 Pa)
        assert args["pressure"] == pytest.approx(100000, rel=0.01)

    def test_to_nacl_state_args(self):
        """Convert to NACL package state_args."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            temperature_C=25,
            pressure_bar=1,
            components={"NaCl": 100000},  # 100 g/L
        )
        args = state.to_state_args(PropertyPackageType.NACL)

        assert "flow_mass_phase_comp" in args
        assert "temperature" in args
        assert "pressure" in args

    def test_to_mcas_state_args(self):
        """Convert to MCAS package state_args."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            temperature_C=25,
            pressure_bar=1,
            components={"Na_+": 500, "Cl_-": 600, "Ca_2+": 200},
            concentration_units="mg/L",
            concentration_basis="mass",
            component_charges={"Na_+": 1, "Cl_-": -1, "Ca_2+": 2},
        )
        args = state.to_state_args(PropertyPackageType.MCAS)

        assert "flow_mol_phase_comp" in args  # MCAS uses molar basis
        assert "temperature" in args
        assert "pressure" in args

    def test_mcas_requires_charges(self):
        """MCAS conversion should require component_charges."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            components={"Na_+": 500, "Cl_-": 600},
            # Missing component_charges
        )
        with pytest.raises(ValueError, match="charge"):
            state.to_state_args(PropertyPackageType.MCAS)

    def test_flow_conversion(self):
        """Flow should convert from m³/hr to kg/s."""
        state = WaterTAPState(
            flow_vol_m3_hr=3.6,  # 3.6 m³/hr = 1 L/s = ~1 kg/s
            components={"TDS": 0},  # Pure water
        )
        args = state.to_state_args(PropertyPackageType.SEAWATER)

        # flow_mass_phase_comp should be in kg/s
        total_flow = sum(args["flow_mass_phase_comp"].values())
        # 3.6 m³/hr ≈ 1 kg/s (for water density ~1000 kg/m³)
        assert total_flow == pytest.approx(1.0, rel=0.1)

    def test_temperature_conversion(self):
        """Temperature should convert from C to K."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            temperature_C=100,
            components={"TDS": 0},
        )
        args = state.to_state_args(PropertyPackageType.SEAWATER)
        assert args["temperature"] == pytest.approx(373.15, rel=0.01)

    def test_pressure_conversion(self):
        """Pressure should convert from bar to Pa."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            pressure_bar=60,  # 60 bar
            components={"TDS": 35000},
        )
        args = state.to_state_args(PropertyPackageType.SEAWATER)
        assert args["pressure"] == pytest.approx(6e6, rel=0.01)

    def test_from_tds_factory(self):
        """Test from_tds factory method."""
        state = WaterTAPState.from_tds(
            flow_vol_m3_hr=100,
            tds_mg_L=35000,
            temperature_C=25,
            pressure_bar=1,
        )

        assert state.flow_vol_m3_hr == 100
        assert state.temperature_C == 25
        assert state.components["TDS"] == 35000

    def test_from_nacl_factory(self):
        """Test from_nacl factory method."""
        state = WaterTAPState.from_nacl(
            flow_vol_m3_hr=50,
            nacl_mg_L=50000,
            temperature_C=30,
            pressure_bar=2,
        )

        assert state.flow_vol_m3_hr == 50
        assert state.temperature_C == 30
        assert state.components["NaCl"] == 50000


class TestElectroneutrality:
    """Tests for electroneutrality in MCAS conversions."""

    def test_balanced_solution(self):
        """Balanced solution should convert without error."""
        # Na+ and Cl- in equal molar amounts
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            components={"Na_+": 2299, "Cl_-": 3545},  # ~0.1 mol/L each
            component_charges={"Na_+": 1, "Cl_-": -1},
        )
        # Should not raise
        args = state.to_state_args(PropertyPackageType.MCAS)
        assert args is not None

    def test_unbalanced_solution_warning(self):
        """Unbalanced solution should warn or adjust."""
        state = WaterTAPState(
            flow_vol_m3_hr=100,
            components={"Na_+": 2299, "Cl_-": 1000},  # Unbalanced
            component_charges={"Na_+": 1, "Cl_-": -1},
            electroneutrality_species="Cl_-",  # Adjust Cl- for balance
        )
        # Should adjust Cl- to achieve balance
        args = state.to_state_args(PropertyPackageType.MCAS)
        assert args is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

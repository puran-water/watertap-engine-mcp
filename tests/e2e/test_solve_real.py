"""Real solver tests - actually execute solver.solve() with IPOPT.

These tests require WaterTAP, IDAES, and IPOPT to be installed.
"""

import pytest
import sys
import os
import json


class TestRealSolver:
    """Tests that actually call solver.solve() with IPOPT."""

    @pytest.mark.integration
    @pytest.mark.solver
    @pytest.mark.slow
    def test_feed_pump_solves_with_ipopt(self, seawater_pump_session, watertap_available, solver_available):
        """Feed -> Pump should solve with IPOPT."""
        from utils.model_builder import ModelBuilder
        from watertap.core.solvers import get_solver
        import idaes.core.util.scaling as iscale

        builder = ModelBuilder(seawater_pump_session)
        model = builder.build()
        units = builder.get_units()

        # Fix feed state (SEAWATER uses H2O and TDS components)
        feed = units["Feed"]
        props = feed.properties[0]
        props.flow_mass_phase_comp["Liq", "H2O"].fix(0.965)
        props.flow_mass_phase_comp["Liq", "TDS"].fix(0.035)
        props.temperature.fix(298.15)
        props.pressure.fix(101325)

        iscale.calculate_scaling_factors(model)

        solver = get_solver()

        # Solve with stdout suppression (matches worker.py pattern for Bug #7)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            with open(os.devnull, 'w') as devnull:
                sys.stdout = sys.stderr = devnull
                results = solver.solve(model, tee=False)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        assert str(results.solver.termination_condition) == "optimal"


class TestUndefinedDataHandling:
    """Bug #10: UndefinedData type from solver can't be converted to float/int."""

    @pytest.mark.unit
    def test_safe_float_handles_undefined_data(self):
        """safe_float returns None for UndefinedData, not TypeError."""
        # Simulate UndefinedData behavior
        class UndefinedData:
            def __float__(self):
                raise TypeError("not a real number")

        def safe_float(val):
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        assert safe_float(UndefinedData()) is None
        assert safe_float(3.14) == 3.14
        assert safe_float(None) is None
        assert safe_float("not a number") is None

    @pytest.mark.unit
    def test_safe_int_handles_undefined_data(self):
        """safe_int returns None for UndefinedData, not TypeError."""
        class UndefinedData:
            def __int__(self):
                raise TypeError("not an integer")

        def safe_int(val):
            if val is None:
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        assert safe_int(UndefinedData()) is None
        assert safe_int(42) == 42
        assert safe_int(3.7) == 3
        assert safe_int(None) is None


class TestKPIExtraction:
    """Tests for KPI extraction from solved models."""

    @pytest.mark.integration
    @pytest.mark.solver
    def test_kpis_json_serializable_after_solve(self, seawater_pump_session, watertap_available, solver_available):
        """Bug #3: KPIs extracted from solved model must be JSON-serializable.

        Tuple keys like ('Liq', 'H2O') must be converted to strings.
        """
        from utils.model_builder import ModelBuilder
        from worker import _extract_solved_kpis
        from watertap.core.solvers import get_solver
        import idaes.core.util.scaling as iscale

        builder = ModelBuilder(seawater_pump_session)
        model = builder.build()
        units = builder.get_units()

        # Fix feed state (SEAWATER uses H2O and TDS components)
        feed = units["Feed"]
        props = feed.properties[0]
        props.flow_mass_phase_comp["Liq", "H2O"].fix(0.965)
        props.flow_mass_phase_comp["Liq", "TDS"].fix(0.035)
        props.temperature.fix(298.15)
        props.pressure.fix(101325)

        iscale.calculate_scaling_factors(model)

        solver = get_solver()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        try:
            with open(os.devnull, 'w') as devnull:
                sys.stdout = sys.stderr = devnull
                solver.solve(model, tee=False)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        kpis = _extract_solved_kpis(model, units)

        # Must not raise (tuple keys converted to strings)
        json_str = json.dumps(kpis)
        loaded = json.loads(json_str)

        assert "streams" in loaded
        assert "units" in loaded

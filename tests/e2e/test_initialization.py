"""SequentialDecomposition tests.

Bug #5: SequentialDecomposition used wrong API (get_ssc_order vs create_graph+calculation_order)
Bug #6: SequentialDecomposition defaulted to cplex MIP solver

Codex recommendation: Use behavior tests with monkeypatched SolverFactory
instead of brittle source code inspection.

CODEX AUDIT: MagicMock removed. Only patch used for CPLEX detection (valid behavior test).
Tests FAIL LOUDLY if SequentialDecomposition requests CPLEX.
"""

import pytest
from unittest.mock import patch  # Only patch, no MagicMock


class TestSequentialDecompositionHeuristic:
    """Bug #6: SequentialDecomposition must use heuristic tear selection, not MIP/cplex."""

    @pytest.mark.initialization
    @pytest.mark.integration
    def test_sequential_decomposition_uses_heuristic_not_cplex(self, seawater_pump_session, watertap_available):
        """SequentialDecomposition must use heuristic tear selection, not MIP/cplex.

        Codex recommendation: Use behavior test with monkeypatched SolverFactory
        to ensure cplex is never requested, rather than source inspection.
        """
        from utils.topo_sort import _compute_order_with_sequential_decomposition
        from utils.model_builder import ModelBuilder
        from pyomo.environ import SolverFactory

        builder = ModelBuilder(seawater_pump_session)
        model = builder.build()

        # Track if cplex is ever requested
        cplex_requested = []
        original_solver_factory = SolverFactory

        def mock_solver_factory(solver_name, *args, **kwargs):
            if 'cplex' in solver_name.lower():
                cplex_requested.append(solver_name)
                raise RuntimeError(f"TEST FAIL: cplex solver '{solver_name}' was requested!")
            return original_solver_factory(solver_name, *args, **kwargs)

        with patch('pyomo.environ.SolverFactory', side_effect=mock_solver_factory):
            # This should NOT request cplex if using heuristic method
            order = _compute_order_with_sequential_decomposition(model, tear_streams=None)

        assert isinstance(order, list)
        assert len(cplex_requested) == 0, f"CPLEX was requested: {cplex_requested}"


class TestSequentialDecompositionAPI:
    """Bug #5: Verify SD uses correct API via behavior tests."""

    @pytest.mark.initialization
    @pytest.mark.integration
    def test_sd_produces_valid_initialization_order(self, seawater_pump_session, watertap_available):
        """Verify SD returns valid unit ordering (behavior test, not source inspection).

        Codex recommendation: Test behavior on a flowsheet, not source code inspection.
        A tiny recycle flowsheet would fail with wrong API.
        """
        from utils.topo_sort import _compute_order_with_sequential_decomposition
        from utils.model_builder import ModelBuilder

        builder = ModelBuilder(seawater_pump_session)
        model = builder.build()

        order = _compute_order_with_sequential_decomposition(model, tear_streams=None)

        # Behavioral assertions
        assert isinstance(order, list)
        # Order should contain unit names from the model
        for unit_name in order:
            assert isinstance(unit_name, str)
        # Feed should come before downstream units (basic topological property)
        if "Feed" in order and "Pump1" in order:
            assert order.index("Feed") < order.index("Pump1")

    @pytest.mark.initialization
    @pytest.mark.integration
    def test_sd_does_not_raise_without_cplex(self, seawater_pump_session, watertap_available):
        """SD should complete successfully without cplex installed."""
        from utils.topo_sort import _compute_order_with_sequential_decomposition
        from utils.model_builder import ModelBuilder

        builder = ModelBuilder(seawater_pump_session)
        model = builder.build()

        # Should NOT raise any solver-related errors
        try:
            order = _compute_order_with_sequential_decomposition(model, tear_streams=None)
            assert isinstance(order, list)
        except Exception as e:
            if 'cplex' in str(e).lower() or 'solver' in str(e).lower():
                pytest.fail(f"SD failed due to solver dependency: {e}")
            raise


class TestSessionPlanningOrder:
    """Test session-level planning (no model built yet)."""

    @pytest.mark.unit
    def test_compute_order_from_connections(self):
        """Simple topological sort for session planning works without model."""
        from utils.topo_sort import compute_initialization_order

        units = {"Feed": None, "Pump1": None, "RO1": None}
        connections = [
            {"src_unit": "Feed", "src_port": "outlet", "dest_unit": "Pump1", "dest_port": "inlet"},
            {"src_unit": "Pump1", "src_port": "outlet", "dest_unit": "RO1", "dest_port": "inlet"},
        ]

        # No model provided - uses simple topo sort
        order = compute_initialization_order(units, connections, tear_streams=None, model=None)

        assert order == ["Feed", "Pump1", "RO1"]

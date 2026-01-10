"""Flowsheet Initialization for WaterTAP.

Provides tools for sequential initialization of WaterTAP flowsheets
using IDAES SequentialDecomposition for initialization order.

IMPORTANT: Uses utils.topo_sort.compute_initialization_order which delegates
to IDAES SequentialDecomposition. NO custom topological sort fallbacks.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from utils.topo_sort import compute_initialization_order, SequentialDecompositionError


class InitMethod(Enum):
    """Initialization method for a unit."""
    INITIALIZE = "initialize"           # Standard initialize()
    INITIALIZE_BUILD = "initialize_build"  # RO, NF specific
    CUSTOM = "custom"                   # Unit-specific custom init
    NONE = "none"                       # No init needed (Feed, Product)


class InitStatus(Enum):
    """Status of initialization attempt."""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    PENDING = "pending"


@dataclass
class InitializationResult:
    """Result of initializing a unit."""
    unit_id: str
    status: InitStatus
    dof_before: int = 0
    dof_after: int = 0
    solve_status: str = ""
    message: str = ""


@dataclass
class FlowsheetInitResult:
    """Result of initializing entire flowsheet."""
    success: bool
    unit_results: List[InitializationResult] = field(default_factory=list)
    init_order: List[str] = field(default_factory=list)
    message: str = ""
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    failed_unit: Optional[str] = None

    @property
    def units_initialized(self) -> List[str]:
        """List of successfully initialized units."""
        return [r.unit_id for r in self.unit_results if r.status == InitStatus.SUCCESS]


class FlowsheetInitializer:
    """Sequential initialization for WaterTAP flowsheets.

    Uses IDAES SequentialDecomposition for initialization order.
    Handles:
    - Initialization order via SequentialDecomposition
    - State propagation between connections
    - Unit-specific initialization methods
    - DOF checking before/after
    """

    def __init__(self, flowsheet: Any = None, model: Any = None):
        """Initialize the flowsheet initializer.

        Args:
            flowsheet: The flowsheet block (m.fs)
            model: The full model (for SequentialDecomposition)
        """
        self._flowsheet = flowsheet
        self._model = model

    def get_initialization_order(
        self,
        units: Dict[str, Any],
        connections: List[Dict],
        tear_streams: Optional[List[Tuple[str, str]]] = None,
    ) -> List[str]:
        """Get initialization order using IDAES SequentialDecomposition.

        Args:
            units: Dict of unit_id → unit block
            connections: List of connection dicts with source_unit, dest_unit
            tear_streams: Optional list of (src, dst) tuples to break cycles

        Returns:
            List of unit IDs in initialization order

        Raises:
            SequentialDecompositionError: If cycle detected without tear streams
        """
        return compute_initialization_order(
            units=units,
            connections=connections,
            tear_streams=tear_streams,
            model=self._model,
        )

    def propagate_state(
        self,
        source_port: Any,
        dest_port: Any,
    ) -> bool:
        """Propagate state from source port to destination port.

        Args:
            source_port: Source port object
            dest_port: Destination port object

        Returns:
            True if successful
        """
        try:
            from idaes.core.util.initialization import propagate_state
            propagate_state(arc=(source_port, dest_port))
            return True
        except ImportError:
            return self._manual_propagate_state(source_port, dest_port)
        except Exception:
            return False

    def _manual_propagate_state(
        self,
        source_port: Any,
        dest_port: Any,
    ) -> bool:
        """Manual state propagation when IDAES not available."""
        try:
            # Get state vars from source
            for var_name in dir(source_port):
                if var_name.startswith('_'):
                    continue
                src_var = getattr(source_port, var_name)
                if hasattr(src_var, 'value') and hasattr(dest_port, var_name):
                    dst_var = getattr(dest_port, var_name)
                    if hasattr(dst_var, 'set_value'):
                        dst_var.set_value(src_var.value)
            return True
        except Exception:
            return False

    def initialize_unit(
        self,
        unit: Any,
        unit_id: str,
        method: InitMethod = InitMethod.INITIALIZE,
        state_args: Optional[Dict] = None,
        solver_options: Optional[Dict] = None,
    ) -> InitializationResult:
        """Initialize a single unit.

        Args:
            unit: The unit block
            unit_id: Unit identifier
            method: Initialization method to use
            state_args: Optional state arguments
            solver_options: Optional solver options

        Returns:
            InitializationResult
        """
        from .dof_resolver import DOFResolver

        resolver = DOFResolver()
        dof_before = resolver.get_dof(unit)

        try:
            if method == InitMethod.NONE:
                return InitializationResult(
                    unit_id=unit_id,
                    status=InitStatus.SKIPPED,
                    dof_before=dof_before,
                    dof_after=dof_before,
                    message="No initialization required",
                )

            kwargs = {}
            if state_args:
                kwargs["state_args"] = state_args
            if solver_options:
                kwargs.update(solver_options)

            if method == InitMethod.INITIALIZE_BUILD:
                if hasattr(unit, "initialize_build"):
                    unit.initialize_build(**kwargs)
                else:
                    unit.initialize(**kwargs)
            elif method == InitMethod.INITIALIZE:
                unit.initialize(**kwargs)

            dof_after = resolver.get_dof(unit)

            return InitializationResult(
                unit_id=unit_id,
                status=InitStatus.SUCCESS,
                dof_before=dof_before,
                dof_after=dof_after,
                solve_status="converged",
                message="Initialization successful",
            )

        except Exception as e:
            return InitializationResult(
                unit_id=unit_id,
                status=InitStatus.FAILED,
                dof_before=dof_before,
                dof_after=-1,
                message=f"Initialization failed: {str(e)}",
            )

    def initialize_flowsheet(
        self,
        flowsheet: Any,
        units: Dict[str, Any],
        connections: List[Dict],
        unit_methods: Optional[Dict[str, InitMethod]] = None,
        state_args: Optional[Dict[str, Dict]] = None,
        tear_streams: Optional[List[Tuple[str, str]]] = None,
    ) -> FlowsheetInitResult:
        """Initialize entire flowsheet sequentially using IDAES SequentialDecomposition.

        Args:
            flowsheet: The flowsheet block
            units: Dict of unit_id → unit block
            connections: List of connection dicts
            unit_methods: Optional dict of unit_id → InitMethod
            state_args: Optional dict of unit_id → state_args
            tear_streams: Optional list of (src, dst) tuples for tear streams

        Returns:
            FlowsheetInitResult

        Raises:
            SequentialDecompositionError: If cycle detected without tear streams
        """
        init_order = self.get_initialization_order(units, connections, tear_streams)
        results = []

        for unit_id in init_order:
            if unit_id not in units:
                continue

            unit = units[unit_id]
            method = (unit_methods or {}).get(unit_id, InitMethod.INITIALIZE)
            args = (state_args or {}).get(unit_id)

            # Propagate state from upstream units
            for conn in connections:
                if conn.get("dest_unit") == unit_id:
                    src_unit_id = conn.get("source_unit")
                    if src_unit_id in units:
                        src_port_name = conn.get("source_port", "outlet")
                        dst_port_name = conn.get("dest_port", "inlet")

                        src_unit = units[src_unit_id]
                        if hasattr(src_unit, src_port_name) and hasattr(unit, dst_port_name):
                            self.propagate_state(
                                getattr(src_unit, src_port_name),
                                getattr(unit, dst_port_name),
                            )

            # Initialize the unit
            result = self.initialize_unit(unit, unit_id, method, args)
            results.append(result)

            if result.status == InitStatus.FAILED:
                return FlowsheetInitResult(
                    success=False,
                    unit_results=results,
                    init_order=init_order,
                    message=f"Initialization failed at unit '{unit_id}': {result.message}",
                    errors=[result.message],
                    failed_unit=unit_id,
                )

        return FlowsheetInitResult(
            success=True,
            unit_results=results,
            init_order=init_order,
            message="Flowsheet initialization complete",
        )


def check_solve(unit: Any) -> Tuple[bool, str]:
    """Check if a unit block is solving properly.

    Args:
        unit: Unit block to check

    Returns:
        Tuple of (success, message)
    """
    try:
        from idaes.core.solvers import get_solver
        from idaes.core.util.model_statistics import degrees_of_freedom

        dof = degrees_of_freedom(unit)
        if dof != 0:
            return False, f"DOF = {dof}, expected 0"

        solver = get_solver()
        results = solver.solve(unit, tee=False)

        from pyomo.environ import TerminationCondition
        if results.solver.termination_condition == TerminationCondition.optimal:
            return True, "Solve successful"
        else:
            return False, f"Solve returned: {results.solver.termination_condition}"

    except Exception as e:
        return False, f"Check solve failed: {str(e)}"

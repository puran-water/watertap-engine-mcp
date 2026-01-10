"""DOF Resolution for WaterTAP Flowsheets.

Provides tools for analyzing and resolving degrees of freedom in WaterTAP models.
Wraps IDAES `degrees_of_freedom` utility and provides unit-specific guidance.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Dict as DictType


class DOFStatus(Enum):
    """Status of degrees of freedom analysis."""
    READY = "ready"           # DOF = 0, ready to solve
    UNDERSPECIFIED = "underspecified"  # DOF > 0, need more fixes
    OVERSPECIFIED = "overspecified"    # DOF < 0, too many fixes
    ERROR = "error"           # Could not determine DOF


@dataclass
class VariableSuggestion:
    """Suggestion for fixing a variable."""
    var_path: str
    description: str
    typical_value: Optional[float] = None
    typical_range: Optional[Tuple[float, float]] = None
    units: str = ""
    priority: int = 1  # Lower = higher priority


@dataclass
class DOFAnalysis:
    """Result of DOF analysis for a unit or flowsheet."""
    unit_id: str
    dof: int
    status: DOFStatus
    fixed_variables: List[str] = field(default_factory=list)
    unfixed_variables: List[str] = field(default_factory=list)
    suggestions: List[VariableSuggestion] = field(default_factory=list)
    message: str = ""


@dataclass
class FlowsheetDOFAnalysis:
    """Result of DOF analysis for an entire flowsheet."""
    total_dof: int
    overall_status: DOFStatus
    unit_analyses: Dict[str, DOFAnalysis] = field(default_factory=dict)
    suggestions: List[VariableSuggestion] = field(default_factory=list)
    message: str = ""


class DOFResolver:
    """Analyzer and resolver for degrees of freedom.

    This class wraps IDAES/WaterTAP DOF utilities and provides
    suggestions based on unit registry metadata.
    """

    def __init__(self, model: Any = None):
        """Initialize DOF resolver.

        Args:
            model: Optional Pyomo model reference
        """
        self._model = model

    def get_dof(self, block: Any) -> int:
        """Get degrees of freedom for a block.

        Args:
            block: Pyomo block (unit, flowsheet, etc.)

        Returns:
            Degrees of freedom count
        """
        try:
            from idaes.core.util.model_statistics import degrees_of_freedom
            return degrees_of_freedom(block)
        except ImportError:
            # Fallback if IDAES not available
            return self._manual_dof_count(block)

    def _manual_dof_count(self, block: Any) -> int:
        """Manual DOF calculation when IDAES unavailable.

        DOF = n_variables - n_equality_constraints
        """
        try:
            from pyomo.environ import Var, Constraint

            n_vars = sum(1 for v in block.component_data_objects(
                Var, active=True, descend_into=True
            ) if not v.fixed)

            n_cons = sum(1 for c in block.component_data_objects(
                Constraint, active=True, descend_into=True
            ))

            return n_vars - n_cons
        except Exception:
            return -999  # Error indicator

    def analyze_unit(
        self,
        unit: Any,
        unit_id: str,
        unit_spec: Optional[Any] = None,
    ) -> DOFAnalysis:
        """Analyze DOF for a single unit.

        Args:
            unit: The unit block
            unit_id: Unit identifier
            unit_spec: Optional UnitSpec from registry for suggestions

        Returns:
            DOFAnalysis with status and suggestions
        """
        dof = self.get_dof(unit)

        if dof == 0:
            status = DOFStatus.READY
            message = "Unit is properly specified (DOF = 0)"
        elif dof > 0:
            status = DOFStatus.UNDERSPECIFIED
            message = f"Unit needs {dof} more fixed variable(s)"
        else:
            status = DOFStatus.OVERSPECIFIED
            message = f"Unit has {abs(dof)} too many fixed variable(s)"

        # Get fixed/unfixed variables
        fixed_vars = self._get_fixed_variables(unit, unit_id)
        unfixed_vars = self._get_unfixed_variables(unit, unit_id)

        # Generate suggestions if underspecified and spec available
        suggestions = []
        if status == DOFStatus.UNDERSPECIFIED and unit_spec is not None:
            suggestions = self._generate_suggestions(
                unit_id, unit_spec, fixed_vars, dof
            )

        return DOFAnalysis(
            unit_id=unit_id,
            dof=dof,
            status=status,
            fixed_variables=fixed_vars,
            unfixed_variables=unfixed_vars,
            suggestions=suggestions,
            message=message,
        )

    def analyze_flowsheet(
        self,
        flowsheet: Any,
        unit_specs: Optional[Dict[str, Any]] = None,
    ) -> FlowsheetDOFAnalysis:
        """Analyze DOF for all units in a flowsheet.

        Args:
            flowsheet: The flowsheet block (m.fs)
            unit_specs: Optional dict of unit_id → UnitSpec

        Returns:
            FlowsheetDOFAnalysis with overall status and per-unit analyses
        """
        unit_analyses = {}
        all_suggestions = []

        # Iterate over components that look like units
        for name in dir(flowsheet):
            if name.startswith('_'):
                continue
            obj = getattr(flowsheet, name)
            if hasattr(obj, 'inlet') or hasattr(obj, 'outlet'):
                # This looks like a unit
                spec = unit_specs.get(name) if unit_specs else None
                analysis = self.analyze_unit(obj, name, spec)
                unit_analyses[name] = analysis
                all_suggestions.extend(analysis.suggestions)

        # Get overall DOF
        total_dof = self.get_overall_dof(flowsheet)

        # Determine overall status
        if total_dof == 0:
            overall_status = DOFStatus.READY
            message = "Flowsheet is properly specified (DOF = 0)"
        elif total_dof > 0:
            overall_status = DOFStatus.UNDERSPECIFIED
            message = f"Flowsheet needs {total_dof} more fixed variable(s)"
        else:
            overall_status = DOFStatus.OVERSPECIFIED
            message = f"Flowsheet has {abs(total_dof)} too many fixed variable(s)"

        return FlowsheetDOFAnalysis(
            total_dof=total_dof,
            overall_status=overall_status,
            unit_analyses=unit_analyses,
            suggestions=all_suggestions,
            message=message,
        )

    def get_overall_dof(self, flowsheet: Any) -> int:
        """Get overall DOF for entire flowsheet.

        Args:
            flowsheet: The flowsheet block

        Returns:
            Total degrees of freedom
        """
        return self.get_dof(flowsheet)

    def _get_fixed_variables(self, block: Any, prefix: str) -> List[str]:
        """Get list of fixed variables in a block."""
        fixed = []
        try:
            from pyomo.environ import Var
            for v in block.component_data_objects(Var, active=True, descend_into=True):
                if v.fixed:
                    # Get relative path from block
                    name = str(v).replace(str(block) + ".", "")
                    fixed.append(f"{prefix}.{name}")
        except Exception:
            pass
        return fixed

    def _get_unfixed_variables(self, block: Any, prefix: str) -> List[str]:
        """Get list of unfixed variables in a block."""
        unfixed = []
        try:
            from pyomo.environ import Var
            for v in block.component_data_objects(Var, active=True, descend_into=True):
                if not v.fixed:
                    name = str(v).replace(str(block) + ".", "")
                    unfixed.append(f"{prefix}.{name}")
        except Exception:
            pass
        return unfixed

    def _generate_suggestions(
        self,
        unit_id: str,
        unit_spec: Any,
        already_fixed: List[str],
        dof_needed: int,
    ) -> List[VariableSuggestion]:
        """Generate fix suggestions based on unit spec."""
        suggestions = []

        if not hasattr(unit_spec, 'required_fixes'):
            return suggestions

        for i, var_spec in enumerate(unit_spec.required_fixes):
            var_path = f"{unit_id}.{var_spec.name}"

            # Skip if already fixed
            if any(var_path in f for f in already_fixed):
                continue

            suggestions.append(VariableSuggestion(
                var_path=var_path,
                description=var_spec.description,
                typical_value=var_spec.typical_default,
                typical_range=(var_spec.typical_min, var_spec.typical_max)
                    if var_spec.typical_min is not None else None,
                units=var_spec.units,
                priority=i + 1,
            ))

            if len(suggestions) >= dof_needed:
                break

        return suggestions


def fix_variable(block: Any, var_path: str, value: float) -> bool:
    """Fix a variable to a specific value.

    Args:
        block: The parent block (flowsheet or unit)
        var_path: Path to variable (e.g., "RO.A_comp[0,'H2O']")
        value: Value to fix to

    Returns:
        True if successful, False otherwise
    """
    try:
        # Navigate to the variable
        parts = var_path.replace("]", "").replace("[", ".").split(".")
        obj = block
        for part in parts:
            if part.startswith("'") or part.startswith('"'):
                # String index
                obj = obj[part.strip("'\"")]
            elif part.isdigit():
                obj = obj[int(part)]
            else:
                obj = getattr(obj, part)

        # Fix the variable
        obj.fix(value)
        return True
    except Exception:
        return False


def unfix_variable(block: Any, var_path: str) -> bool:
    """Unfix a variable.

    Args:
        block: The parent block
        var_path: Path to variable

    Returns:
        True if successful, False otherwise
    """
    try:
        parts = var_path.replace("]", "").replace("[", ".").split(".")
        obj = block
        for part in parts:
            if part.startswith("'") or part.startswith('"'):
                obj = obj[part.strip("'\"")]
            elif part.isdigit():
                obj = obj[int(part)]
            else:
                obj = getattr(obj, part)

        obj.unfix()
        return True
    except Exception:
        return False

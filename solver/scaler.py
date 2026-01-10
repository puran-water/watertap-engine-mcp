"""Scaling Tools for WaterTAP Flowsheets.

Provides tools for managing variable and constraint scaling in WaterTAP models.
Wraps IDAES scaling utilities and provides unit-specific defaults.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ScalingIssueType(Enum):
    """Type of scaling issue."""
    UNSCALED_VAR = "unscaled_variable"
    UNSCALED_CONSTRAINT = "unscaled_constraint"
    BADLY_SCALED_VAR = "badly_scaled_variable"
    BADLY_SCALED_CONSTRAINT = "badly_scaled_constraint"


@dataclass
class ScalingIssue:
    """A scaling issue identified in the model."""
    issue_type: ScalingIssueType
    component_name: str
    current_magnitude: Optional[float] = None
    suggested_factor: Optional[float] = None
    message: str = ""


@dataclass
class ScalingReport:
    """Report of scaling analysis."""
    total_issues: int
    issues: List[ScalingIssue] = field(default_factory=list)
    unscaled_vars: int = 0
    unscaled_constraints: int = 0
    badly_scaled_vars: int = 0
    badly_scaled_constraints: int = 0
    message: str = ""


class ScalingTools:
    """Tools for managing model scaling.

    Wraps IDAES scaling utilities and provides WaterTAP-specific defaults.
    """

    def __init__(self, model: Any = None):
        """Initialize scaling tools.

        Args:
            model: Optional Pyomo model reference
        """
        self._model = model

    def set_scaling_factor(
        self,
        var: Any,
        factor: float,
    ) -> bool:
        """Set scaling factor for a variable.

        Args:
            var: Pyomo variable or expression
            factor: Scaling factor (multiply var by this to get O(1))

        Returns:
            True if successful
        """
        try:
            import idaes.core.util.scaling as iscale
            iscale.set_scaling_factor(var, factor)
            return True
        except ImportError:
            return self._manual_set_scaling(var, factor)
        except Exception:
            return False

    def _manual_set_scaling(self, var: Any, factor: float) -> bool:
        """Set scaling using Pyomo suffix directly."""
        try:
            # Find or create scaling suffix
            model = var.model()
            if not hasattr(model, 'scaling_factor'):
                from pyomo.environ import Suffix
                model.scaling_factor = Suffix(direction=Suffix.EXPORT)

            model.scaling_factor[var] = factor
            return True
        except Exception:
            return False

    def get_scaling_factor(self, var: Any) -> Optional[float]:
        """Get current scaling factor for a variable.

        Args:
            var: Pyomo variable

        Returns:
            Current scaling factor or None if unscaled
        """
        try:
            import idaes.core.util.scaling as iscale
            return iscale.get_scaling_factor(var)
        except Exception:
            return None

    def calculate_scaling_factors(self, model: Any) -> bool:
        """Calculate scaling factors for model.

        Calls IDAES calculate_scaling_factors which recursively
        invokes unit-specific scaling methods.

        Args:
            model: Pyomo model (typically m or m.fs)

        Returns:
            True if successful
        """
        try:
            import idaes.core.util.scaling as iscale
            iscale.calculate_scaling_factors(model)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def report_scaling_issues(
        self,
        model: Any,
        threshold: float = 1e-4,
    ) -> ScalingReport:
        """Report scaling issues in the model.

        Args:
            model: Pyomo model
            threshold: Threshold for badly scaled (< threshold or > 1/threshold)

        Returns:
            ScalingReport with identified issues
        """
        try:
            import idaes.core.util.scaling as iscale

            issues = []
            unscaled_vars = 0
            unscaled_cons = 0
            badly_scaled_vars = 0
            badly_scaled_cons = 0

            # Check variables
            from pyomo.environ import Var
            for v in model.component_data_objects(Var, active=True, descend_into=True):
                sf = iscale.get_scaling_factor(v)
                if sf is None:
                    unscaled_vars += 1
                    if v.value is not None and abs(v.value) > 0:
                        suggested = 1.0 / abs(v.value)
                        issues.append(ScalingIssue(
                            issue_type=ScalingIssueType.UNSCALED_VAR,
                            component_name=str(v),
                            current_magnitude=v.value,
                            suggested_factor=suggested,
                            message=f"Unscaled variable with value {v.value:.2e}",
                        ))
                else:
                    # Check if badly scaled
                    if v.value is not None:
                        scaled_value = abs(v.value * sf)
                        if scaled_value < threshold or scaled_value > 1/threshold:
                            badly_scaled_vars += 1
                            issues.append(ScalingIssue(
                                issue_type=ScalingIssueType.BADLY_SCALED_VAR,
                                component_name=str(v),
                                current_magnitude=v.value,
                                suggested_factor=1.0/abs(v.value) if v.value != 0 else 1.0,
                                message=f"Scaled value {scaled_value:.2e} outside [{threshold:.0e}, {1/threshold:.0e}]",
                            ))

            # Check constraints (simplified - full check requires Jacobian)
            from pyomo.environ import Constraint
            for c in model.component_data_objects(Constraint, active=True, descend_into=True):
                sf = iscale.get_scaling_factor(c)
                if sf is None:
                    unscaled_cons += 1

            total = len(issues)

            return ScalingReport(
                total_issues=total,
                issues=issues[:50],  # Limit to first 50
                unscaled_vars=unscaled_vars,
                unscaled_constraints=unscaled_cons,
                badly_scaled_vars=badly_scaled_vars,
                badly_scaled_constraints=badly_scaled_cons,
                message=f"Found {unscaled_vars} unscaled vars, {unscaled_cons} unscaled constraints",
            )

        except ImportError:
            return ScalingReport(
                total_issues=-1,
                message="IDAES scaling utilities not available",
            )
        except Exception as e:
            return ScalingReport(
                total_issues=-1,
                message=f"Error analyzing scaling: {e}",
            )

    def get_scaling_report(self, model: Any) -> ScalingReport:
        """Alias for report_scaling_issues for pipeline compatibility.

        Args:
            model: Pyomo model

        Returns:
            ScalingReport
        """
        return self.report_scaling_issues(model)

    def autoscale_large_jac(self, model: Any) -> bool:
        """Auto-scale based on Jacobian analysis.

        Uses IDAES constraint_autoscale_large_jac to identify
        and fix scaling issues based on Jacobian structure.

        Args:
            model: Pyomo model

        Returns:
            True if successful
        """
        try:
            import idaes.core.util.scaling as iscale
            iscale.constraint_autoscale_large_jac(model)
            return True
        except Exception:
            return False

    def apply_default_scaling(
        self,
        model: Any,
        scaling_dict: Dict[str, float],
    ) -> int:
        """Apply default scaling factors from a dictionary.

        Args:
            model: Pyomo model
            scaling_dict: Dict mapping variable patterns to scaling factors

        Returns:
            Number of variables scaled
        """
        count = 0
        try:
            import idaes.core.util.scaling as iscale
            from pyomo.environ import Var

            for v in model.component_data_objects(Var, active=True, descend_into=True):
                v_name = str(v)
                for pattern, factor in scaling_dict.items():
                    if pattern in v_name:
                        iscale.set_scaling_factor(v, factor)
                        count += 1
                        break
        except Exception:
            pass

        return count


# Common scaling factors for WaterTAP variables
DEFAULT_SCALING_FACTORS = {
    # Membrane parameters
    "A_comp": 1e12,
    "B_comp": 1e8,

    # Pressure
    "pressure": 1e-5,

    # Temperature
    "temperature": 1e-2,

    # Membrane area
    "area": 1e-2,

    # Flux
    "flux_mass": 1e3,

    # Energy
    "work_mechanical": 1e-5,
    "heat_transfer": 1e-6,

    # Crystallizer
    "crystal_growth_rate": 1e9,
    "crystal_median_length": 1e3,

    # NF DSPMDE
    "radius_pore": 1e9,
    "membrane_thickness": 1e6,
}

"""Diagnostics for WaterTAP Flowsheets.

Provides tools for diagnosing solver failures and model issues.
Wraps IDAES DiagnosticsToolbox and provides failure pattern matching.
"""
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Tuple

class DiagnosticType(Enum):
    """Type of diagnostic check."""
    STRUCTURAL = 'structural'
    NUMERICAL = 'numerical'
    CONSTRAINT_RESIDUALS = 'constraint_residuals'
    BOUND_VIOLATIONS = 'bound_violations'
    JACOBIAN_ANALYSIS = 'jacobian_analysis'

@dataclass
class ConstraintResidual:
    """Constraint residual information."""
    constraint_name: str
    residual: float
    body_value: float
    bound: Optional[float] = None
    is_equality: bool = True

@dataclass
class BoundViolation:
    """Variable bound violation."""
    variable_name: str
    value: float
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    violation_type: str = ''

@dataclass
class DiagnosticResult:
    """Result of diagnostic analysis."""
    diagnostic_type: DiagnosticType
    issues_found: int
    details: List[Any] = field(default_factory=list)
    summary: str = ''
    suggestions: List[str] = field(default_factory=list)

class DiagnosticsRunner:
    """Runner for model diagnostics.

    Wraps IDAES DiagnosticsToolbox and provides additional
    WaterTAP-specific diagnostic patterns.
    """

    def __init__(self, model: Any=None):
        """Initialize diagnostics runner.

        Args:
            model: Pyomo model
        """
        self._model = model
        self._toolbox = None

    def _get_toolbox(self, model: Any) -> Any:
        """Get or create DiagnosticsToolbox."""
        try:
            from idaes.core.util.diagnostics_tools.diagnostics_toolbox import DiagnosticsToolbox
            return DiagnosticsToolbox(model)
        except ImportError:
            return None

    def run_structural_diagnostics(self, model: Any) -> DiagnosticResult:
        """Run structural diagnostics on model.

        Checks for:
        - Structural singularities
        - Redundant constraints
        - Variables not in constraints

        Args:
            model: Pyomo model

        Returns:
            DiagnosticResult
        """
        try:
            toolbox = self._get_toolbox(model)
            if toolbox is None:
                return DiagnosticResult(diagnostic_type=DiagnosticType.STRUCTURAL, issues_found=-1, summary='IDAES DiagnosticsToolbox not available')
            issues = []
            try:
                sing_result = toolbox.report_structural_issues()
                if sing_result:
                    issues.append(str(sing_result))
            except Exception as e:
                issues.append(f'Structural check error: {e}')
            return DiagnosticResult(diagnostic_type=DiagnosticType.STRUCTURAL, issues_found=len(issues), details=issues if issues else ['No structural issues found'], summary='Structural diagnostics complete')
        except Exception as e:
            return DiagnosticResult(diagnostic_type=DiagnosticType.STRUCTURAL, issues_found=-1, summary=f'Structural diagnostics failed: {e}')

    def run_numerical_diagnostics(self, model: Any) -> DiagnosticResult:
        """Run numerical diagnostics on model.

        Checks for:
        - Large residuals
        - Bound violations
        - Numerical singularities

        Args:
            model: Pyomo model

        Returns:
            DiagnosticResult
        """
        try:
            toolbox = self._get_toolbox(model)
            if toolbox is None:
                return DiagnosticResult(diagnostic_type=DiagnosticType.NUMERICAL, issues_found=-1, summary='IDAES DiagnosticsToolbox not available')
            issues = []
            try:
                num_result = toolbox.report_numerical_issues()
                if num_result:
                    issues.append(str(num_result))
            except Exception as e:
                issues.append(f'Numerical check error: {e}')
            return DiagnosticResult(diagnostic_type=DiagnosticType.NUMERICAL, issues_found=len(issues), details=issues if issues else ['No numerical issues found'], summary='Numerical diagnostics complete')
        except Exception as e:
            return DiagnosticResult(diagnostic_type=DiagnosticType.NUMERICAL, issues_found=-1, summary=f'Numerical diagnostics failed: {e}')

    def get_constraint_residuals(self, model: Any, threshold: float=1e-06, max_results: int=20) -> DiagnosticResult:
        """Get largest constraint residuals.

        Args:
            model: Pyomo model
            threshold: Only report residuals above this
            max_results: Maximum number to return

        Returns:
            DiagnosticResult with ConstraintResidual details
        """
        try:
            from pyomo.environ import Constraint, value
            residuals = []
            for c in model.component_data_objects(Constraint, active=True, descend_into=True):
                try:
                    body = value(c.body, exception=False)
                    if body is None:
                        continue
                    if c.equality:
                        bound = value(c.lower, exception=False)
                        if bound is not None:
                            residual = abs(body - bound)
                            if residual > threshold:
                                residuals.append(ConstraintResidual(constraint_name=str(c), residual=residual, body_value=body, bound=bound, is_equality=True))
                    else:
                        lower = value(c.lower, exception=False)
                        upper = value(c.upper, exception=False)
                        if lower is not None and body < lower:
                            residual = lower - body
                            if residual > threshold:
                                residuals.append(ConstraintResidual(constraint_name=str(c), residual=residual, body_value=body, bound=lower, is_equality=False))
                        elif upper is not None and body > upper:
                            residual = body - upper
                            if residual > threshold:
                                residuals.append(ConstraintResidual(constraint_name=str(c), residual=residual, body_value=body, bound=upper, is_equality=False))
                except Exception:
                    continue
            residuals.sort(key=lambda r: r.residual, reverse=True)
            residuals = residuals[:max_results]
            return DiagnosticResult(diagnostic_type=DiagnosticType.CONSTRAINT_RESIDUALS, issues_found=len(residuals), details=residuals, summary=f'Found {len(residuals)} constraints with residual > {threshold}')
        except Exception as e:
            return DiagnosticResult(diagnostic_type=DiagnosticType.CONSTRAINT_RESIDUALS, issues_found=-1, summary=f'Failed to get residuals: {e}')

    def get_bound_violations(self, model: Any, tolerance: float=1e-08, max_results: int=20) -> DiagnosticResult:
        """Get variables violating their bounds.

        Args:
            model: Pyomo model
            tolerance: Tolerance for bound violations
            max_results: Maximum number to return

        Returns:
            DiagnosticResult with BoundViolation details
        """
        try:
            from pyomo.environ import Var, value
            violations = []
            for v in model.component_data_objects(Var, active=True, descend_into=True):
                try:
                    val = value(v, exception=False)
                    if val is None:
                        continue
                    lb = v.lb
                    ub = v.ub
                    if lb is not None and val < lb - tolerance:
                        violations.append(BoundViolation(variable_name=str(v), value=val, lower_bound=lb, upper_bound=ub, violation_type='below_lower'))
                    elif ub is not None and val > ub + tolerance:
                        violations.append(BoundViolation(variable_name=str(v), value=val, lower_bound=lb, upper_bound=ub, violation_type='above_upper'))
                except Exception:
                    continue

            def violation_magnitude(v):
                if v.violation_type == 'below_lower':
                    return v.lower_bound - v.value
                else:
                    return v.value - v.upper_bound
            violations.sort(key=violation_magnitude, reverse=True)
            violations = violations[:max_results]
            return DiagnosticResult(diagnostic_type=DiagnosticType.BOUND_VIOLATIONS, issues_found=len(violations), details=violations, summary=f'Found {len(violations)} bound violations')
        except Exception as e:
            return DiagnosticResult(diagnostic_type=DiagnosticType.BOUND_VIOLATIONS, issues_found=-1, summary=f'Failed to check bounds: {e}')

    def get_variables_at_bounds(self, model: Any, tolerance: float=1e-08, max_results: int=50) -> DiagnosticResult:
        """Get variables sitting on or outside their bounds at the best iterate."""
        try:
            from pyomo.environ import Var, value
            at_bounds = []
            for var in model.component_data_objects(Var, active=True, descend_into=True):
                try:
                    raw_value = value(var, exception=False)
                except Exception:
                    raw_value = None
                if raw_value is None:
                    continue
                try:
                    numeric_value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                lower = var.lb
                upper = var.ub
                lower_distance = numeric_value - float(lower) if lower is not None else None
                upper_distance = float(upper) - numeric_value if upper is not None else None
                entry = None
                if lower is not None and lower_distance is not None:
                    scaled_tol = max(tolerance, abs(float(lower)) * tolerance)
                    if lower_distance < -scaled_tol:
                        entry = BoundViolation(variable_name=str(var), value=numeric_value, lower_bound=float(lower), upper_bound=float(upper) if upper is not None else None, violation_type='below_lower')
                    elif lower_distance <= scaled_tol:
                        entry = BoundViolation(variable_name=str(var), value=numeric_value, lower_bound=float(lower), upper_bound=float(upper) if upper is not None else None, violation_type='lower')
                if entry is None and upper is not None and (upper_distance is not None):
                    scaled_tol = max(tolerance, abs(float(upper)) * tolerance)
                    if upper_distance < -scaled_tol:
                        entry = BoundViolation(variable_name=str(var), value=numeric_value, lower_bound=float(lower) if lower is not None else None, upper_bound=float(upper), violation_type='above_upper')
                    elif upper_distance <= scaled_tol:
                        entry = BoundViolation(variable_name=str(var), value=numeric_value, lower_bound=float(lower) if lower is not None else None, upper_bound=float(upper), violation_type='upper')
                if entry is not None:
                    at_bounds.append(entry)

            def _rank(item: BoundViolation) -> Tuple[int, float]:
                if item.violation_type in {'below_lower', 'above_upper'}:
                    return (0, 0.0)
                if item.violation_type == 'lower' and item.lower_bound is not None:
                    return (1, abs((item.value or 0.0) - item.lower_bound))
                if item.violation_type == 'upper' and item.upper_bound is not None:
                    return (1, abs(item.upper_bound - (item.value or 0.0)))
                return (2, 0.0)
            at_bounds.sort(key=_rank)
            at_bounds = at_bounds[:max_results]
            return DiagnosticResult(diagnostic_type=DiagnosticType.BOUND_VIOLATIONS, issues_found=len(at_bounds), details=at_bounds, summary=f'Found {len(at_bounds)} variables at/over bounds')
        except Exception as e:
            return DiagnosticResult(diagnostic_type=DiagnosticType.BOUND_VIOLATIONS, issues_found=-1, summary=f'Failed to check variables at bounds: {e}')

    def diagnose_failure(self, model: Any, termination_condition: str) -> Dict[str, Any]:
        """Diagnose a solver failure.

        Args:
            model: Pyomo model that failed to solve
            termination_condition: The solver's termination condition

        Returns:
            Dict with diagnosis and suggestions
        """
        results = {'termination_condition': termination_condition, 'constraint_residuals': [], 'bound_violations': [], 'likely_causes': [], 'suggested_fixes': []}
        residuals = self.get_constraint_residuals(model)
        violations = self.get_bound_violations(model)
        results['constraint_residuals'] = [{'name': r.constraint_name, 'residual': r.residual} for r in residuals.details[:10]]
        results['bound_violations'] = [{'name': v.variable_name, 'value': v.value, 'type': v.violation_type} for v in violations.details[:10]]
        if termination_condition == 'infeasible':
            if any(('flux_mass' in str(r.constraint_name) for r in residuals.details)):
                results['likely_causes'].append('RO membrane flux constraint violated - likely insufficient feed pressure')
                results['suggested_fixes'].append('Increase feed pressure to at least 1.5x osmotic pressure')
            if any(('solubility' in str(r.constraint_name).lower() for r in residuals.details)):
                results['likely_causes'].append('Crystallizer solubility constraint violated')
                results['suggested_fixes'].append('Check that feed concentration can reach saturation at operating temperature')
        elif termination_condition == 'maxIterations':
            results['likely_causes'].append('Solver hit iteration limit - likely poor scaling or bad initialization')
            results['suggested_fixes'].extend(['Check scaling with report_scaling_issues', 'Scale small parameters (A_comp: 1e12, B_comp: 1e8)', 'Initialize units sequentially with propagate_state'])
        elif termination_condition == 'locallyInfeasible':
            results['likely_causes'].append('Local minimum is infeasible - bad initial point')
            results['suggested_fixes'].extend(['Try different initial values', 'Initialize from a known feasible solution'])
        return results

    def build_solver_infeasibility_report(self, model: Any, result: Any=None):
        """Build the structured failure artifact for an infeasible solve."""
        return build_solver_infeasibility_report(model, result)
_ASM2D_COMPONENT_RE = re.compile('\\b(?:S|X)_[A-Za-z0-9]+\\b')

def _safe_float(value_obj: Any) -> Optional[float]:
    try:
        from pyomo.environ import value
        raw_value = value(value_obj, exception=False)
    except Exception:
        raw_value = value_obj
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None

def _solver_field(result: Any, field_name: str) -> Optional[str]:
    if result is None:
        return None
    if isinstance(result, dict):
        value = result.get(field_name)
        if value is None and isinstance(result.get('details'), dict):
            value = result['details'].get(field_name)
        if value is None and isinstance(result.get('stage_result'), dict):
            value = result['stage_result'].get(field_name)
        return str(value) if value is not None else None
    solver = getattr(result, 'solver', None)
    value = getattr(solver, field_name, None)
    return str(value) if value is not None else None

def _constraint_unit_name(constraint_name: str) -> str:
    parts = constraint_name.split('.')
    if len(parts) >= 2 and parts[0] in {'fs', 'model'}:
        return parts[1].split('[', 1)[0]
    if parts:
        return parts[0].split('[', 1)[0]
    return 'unknown'

def _constraint_species(constraint_name: str) -> Optional[str]:
    matches = _ASM2D_COMPONENT_RE.findall(constraint_name)
    if matches:
        return matches[-1]
    return None

def _mass_balance_imbalances(residuals: List[ConstraintResidual], max_results: int=20):
    from puran_mcp_support.engineering.models.solver_infeasibility_report import SpeciesMassBalanceImbalance
    imbalances = []
    for residual in residuals:
        name = residual.constraint_name
        lowered = name.lower()
        if 'material_balance' not in lowered and 'material_balances' not in lowered:
            continue
        species = _constraint_species(name)
        if species is None:
            continue
        denominator = max(abs(residual.body_value or 0.0), abs(residual.bound or 0.0), 1e-12)
        imbalances.append(SpeciesMassBalanceImbalance(unit_id=_constraint_unit_name(name), species=species, constraint_name=name, residual=residual.residual, percent_imbalance=100.0 * residual.residual / denominator, body_value=residual.body_value, bound=residual.bound))
    return imbalances[:max_results]

def _variable_at_bound_payloads(bound_result: DiagnosticResult):
    from puran_mcp_support.engineering.models.solver_infeasibility_report import VariableAtBound
    payloads = []
    for item in bound_result.details:
        bound_type = item.violation_type
        if bound_type not in {'lower', 'upper', 'below_lower', 'above_upper'}:
            bound_type = 'fixed'
        distance = None
        if bound_type in {'lower', 'below_lower'} and item.lower_bound is not None:
            distance = (item.value or 0.0) - item.lower_bound
        elif bound_type in {'upper', 'above_upper'} and item.upper_bound is not None:
            distance = item.upper_bound - (item.value or 0.0)
        payloads.append(VariableAtBound(variable_name=item.variable_name, value=item.value, lower_bound=item.lower_bound, upper_bound=item.upper_bound, bound_type=bound_type, distance_to_bound=distance))
    return payloads

def _max_residual_summary(residuals: List[ConstraintResidual]):
    if not residuals:
        return None
    from puran_mcp_support.engineering.models.solver_infeasibility_report import ConstraintResidualSummary
    residual = residuals[0]
    return ConstraintResidualSummary(constraint_name=residual.constraint_name, residual=residual.residual, body_value=residual.body_value, bound=residual.bound, is_equality=residual.is_equality)

def _model_family(model: Any) -> str:
    model_text = ' '.join((type(obj).__name__ for obj in [model, getattr(model, 'fs', None)] if obj is not None))
    for attr in dir(getattr(model, 'fs', model)):
        if attr.startswith('_'):
            continue
        try:
            unit = getattr(getattr(model, 'fs', model), attr)
            package = getattr(getattr(unit, 'config', None), 'property_package', None)
        except Exception:
            continue
        if package is not None:
            model_text += ' ' + type(package).__name__
    if 'ADM1' in model_text:
        return 'ADM1'
    if 'ModifiedASM2' in model_text or 'ModifiedASM2d' in model_text:
        return 'ModifiedASM2D'
    if 'ASM2d' in model_text or 'ASM2D' in model_text:
        return 'ASM2D'
    return 'unknown'

def _cause_and_remediation(variables_at_bounds: List[Any], residuals: List[ConstraintResidual]) -> Tuple[str, List[str]]:
    variable_text = ' '.join((v.variable_name for v in variables_at_bounds)).lower()
    residual_text = ' '.join((r.constraint_name for r in residuals[:10])).lower()
    combined = f'{variable_text} {residual_text}'
    flow_bound_count = sum((1 for variable in variables_at_bounds if 'flow_vol' in variable.variable_name.lower() and variable.bound_type in {'lower', 'below_lower'}))
    if flow_bound_count >= 3:
        return ('Hydraulic recycle closure is driving process flows to their lower bounds.', ['Check MBR/clarifier hydraulic split, RAS capacity, and WAS split before changing biology.', 'For MBR seeds, provide pumped_flow_m3_d or use a permeate split based on crossflow rather than whole-plant recovery.', 'If the required WAS flow exceeds retentate hydraulic capacity, reduce SRT or increase retentate/crossflow capacity.'])
    if 'cstr_retention_time' in residual_text or ('material_balance' in residual_text and any((species in residual_text for species in ('x_tss', 'x_h', 'x_i', 'x_s', 'x_pp', 'x_pha')))):
        return ('CSTR hydraulic residence time or solids inventory closure is inconsistent with the requested MLSS/SRT sizing.', ['Increase aerobic_volume_m3 or membrane mixed-liquor volume by 30% before re-solving.', 'Relax SRT from the requested value if the calculated sludge wasting rate cannot close the solids inventory.', 'Check MBR/clarifier retentate, RAS, and WAS hydraulic capacity before changing influent fractionation.'])
    if 's_o2' in combined or 'kla' in combined:
        return ('Aerobic oxygen-transfer limitation is the dominant infeasibility signature.', ['Increase aerobic_volume_m3 by 30% or increase KLa before re-solving.', 'Check whether the influent COD/TKN load requires a lower F/M ratio at the requested MLSS.', 'If dissolved oxygen is intentionally near zero, relax the aerobic DO target during initialization only.'])
    if 'x_aut' in combined or 's_nh4' in combined:
        return ('Nitrifier inventory or nitrification kinetics are inconsistent with the requested SRT/MLSS/KLa.', ['Increase aerobic_volume_m3 by 30% to reduce F/M and stabilize X_AUT.', 'Relax SRT from 20 d toward 15 d or raise KLa if ammonia removal is binding.', 'Check influent TKN/NH4 fractionation and alkalinity before changing biology.'])
    if 'x_tss' in combined or 'split_fraction' in combined or 'sludgesplitter' in combined:
        return ('Solids inventory closure is constrained by the requested MLSS/SRT and WAS/RAS split.', ['Increase WAS split or relax target MLSS before re-solving.', 'Check membrane/clarifier solids capture and RAS hydraulic capacity.', 'Relax SRT from the requested value if the calculated sludge age forces an impossible inventory.'])
    if 's_no3' in combined or 'postanoxic' in combined or 'irsplitter' in combined:
        return ('Nitrate recycle and anoxic denitrification closure are the dominant infeasibility signature.', ['Reduce internal recycle fraction or add post-anoxic volume before re-solving.', 'Increase readily biodegradable COD fraction if the tender basis supports it.', 'Check that post-anoxic volume is not zero for MLE_POST topology.'])
    if 's_po4' in combined or 'x_pao' in combined or 'x_pp' in combined or ('x_pha' in combined):
        return ('Bio-P storage/sequestration constraints are binding at the requested operating point.', ['Relax TP removal target or increase anaerobic/anoxic selector volume where applicable.', 'Check influent VFA fractionation and PAO seed concentration.', 'Increase aerobic polishing volume if PAO sequestration residuals dominate.'])
    return ('The best-iterate residual pattern indicates an overconstrained biological design point.', ['Increase aerobic_volume_m3 by 30% to reduce F/M, then re-run the solve.', 'Relax SRT by 20-30% or lower target MLSS if solids inventory is at bound.', 'Review KLa, internal recycle, WAS split, and influent fractionation against the tender basis.'])

def build_solver_infeasibility_report(model: Any, result: Any=None):
    """Build a SolverInfeasibilityReport from the current model iterate."""
    from puran_mcp_support.engineering.models.solver_infeasibility_report import SolverInfeasibilityReport
    runner = DiagnosticsRunner(model)
    residual_result = runner.get_constraint_residuals(model, threshold=0.0, max_results=5000)
    at_bound_result = runner.get_variables_at_bounds(model, tolerance=1e-08, max_results=50)
    residuals = [residual for residual in residual_result.details if isinstance(residual, ConstraintResidual)]
    top_residuals = residuals[:50]
    variables_at_bounds = _variable_at_bound_payloads(at_bound_result)
    cause, remediation = _cause_and_remediation(variables_at_bounds, top_residuals)
    return SolverInfeasibilityReport(model_family=_model_family(model), solver_status=_solver_field(result, 'solver_status') or _solver_field(result, 'status'), termination_condition=_solver_field(result, 'termination_condition'), message=result.get('message') if isinstance(result, dict) and isinstance(result.get('message'), str) else None, species_mass_balance_imbalances=_mass_balance_imbalances(residuals), variables_at_bounds=variables_at_bounds, max_constraint_residual=_max_residual_summary(top_residuals), most_likely_cause=cause, suggested_remediation=remediation, diagnostic_basis={'constraint_residual_count': residual_result.issues_found, 'variable_at_bound_count': at_bound_result.issues_found, 'residual_summary': residual_result.summary, 'bound_summary': at_bound_result.summary})

def run_diagnostics(model: Any) -> Dict[str, Any]:
    """Run comprehensive diagnostics on a model.

    Args:
        model: Pyomo model

    Returns:
        Dict with all diagnostic results
    """
    runner = DiagnosticsRunner(model)
    return {'structural': runner.run_structural_diagnostics(model).__dict__, 'numerical': runner.run_numerical_diagnostics(model).__dict__, 'constraint_residuals': runner.get_constraint_residuals(model).__dict__, 'bound_violations': runner.get_bound_violations(model).__dict__}

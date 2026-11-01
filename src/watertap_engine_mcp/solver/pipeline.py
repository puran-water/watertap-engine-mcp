"""Solver Hygiene Pipeline for WaterTAP Flowsheets.

Implements the state machine for systematic flowsheet solving:
IDLE -> DOF_CHECK -> SCALING -> INITIALIZATION -> PRE_SOLVE_DIAGNOSTICS -> SOLVING
                                                                              |
COMPLETED <-- OPTIMIZATION_SOLVE <-- POST_SOLVE_DIAGNOSTICS <-----------------+
    ^                                     | (if failed)                       |
    +------- RELAXED_SOLVE ---------------------------------------------------+
                   | (if still fails)
                FAILED
"""
import os
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watertap_engine_mcp.core.roles import RoleResolver, UnitRole as R
from watertap_engine_mcp.core.property_registry import is_asm2d_property_block
from .diagnostics import DiagnosticsRunner, build_solver_infeasibility_report
from .dof_resolver import DOFResolver, DOFStatus
from .initializer import FlowsheetInitializer
from .recovery import RecoveryExecutor
from .scaler import ScalingTools
_IPOPT_LINEAR_SOLVER_CACHE: Optional[str] = None

def default_ipopt_linear_solver() -> str:
    """ma27 when the installed ipopt build carries HSL, else mumps.

    The IDAES-bundled ipopt statically links HSL, and ma27 is materially
    stronger than MUMPS on the stiff ASM2D recycle family (measured 2026-08-25:
    the municipal MBR-post default and the chemical-P target solve under ma27
    and stall under MUMPS; the historical strict/near-feasible expectations
    were all earned under ma27). Probed once per process with a trivial solve;
    `WATERTAP_IPOPT_LINEAR_SOLVER` still overrides in either direction.
    """
    override = os.environ.get('WATERTAP_IPOPT_LINEAR_SOLVER')
    if override:
        return override
    global _IPOPT_LINEAR_SOLVER_CACHE
    if _IPOPT_LINEAR_SOLVER_CACHE is None:
        _IPOPT_LINEAR_SOLVER_CACHE = 'mumps'
        try:
            import idaes
            from pyomo.environ import ConcreteModel, Objective, SolverFactory, Var
            probe = ConcreteModel()
            probe.x = Var(initialize=1.0)
            probe.obj = Objective(expr=(probe.x - 2.0) ** 2)
            solver = SolverFactory('ipopt')
            solver.options['linear_solver'] = 'ma27'
            results = solver.solve(probe, tee=False)
            if str(results.solver.termination_condition) == 'optimal':
                _IPOPT_LINEAR_SOLVER_CACHE = 'ma27'
        except Exception:
            pass
    return _IPOPT_LINEAR_SOLVER_CACHE

class PipelineState(Enum):
    """States in the solver hygiene pipeline."""
    IDLE = 'idle'
    DOF_CHECK = 'dof_check'
    SCALING = 'scaling'
    INITIALIZATION = 'initialization'
    PRE_SOLVE_DIAGNOSTICS = 'pre_solve_diagnostics'
    SOLVING = 'solving'
    PROGRESSIVE_TIGHTENING = 'progressive_tightening'
    POST_SOLVE_DIAGNOSTICS = 'post_solve_diagnostics'
    OPTIMIZATION_SOLVE = 'optimization_solve'
    RELAXED_SOLVE = 'relaxed_solve'
    COMPLETED = 'completed'
    FAILED = 'failed'

@dataclass
class PipelineResult:
    """Result from a pipeline stage."""
    success: bool
    state: PipelineState
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

@dataclass
class PipelineConfig:
    """Configuration for the hygiene pipeline."""
    allow_overspecified: bool = False
    auto_fix_dof: bool = False
    auto_scale: bool = True
    report_scaling_issues: bool = True
    interval_initialize: bool = True
    sequential_init: bool = True
    propagate_state: bool = True
    tear_streams: Optional[List[tuple]] = None
    skip_initialization: bool = False
    solver: str = 'ipopt'
    solver_options: Dict[str, Any] = field(default_factory=dict)
    max_iterations: int = 100
    tolerance: float = 1e-08
    max_wall_time_s: int = 300
    enable_progressive_tightening: bool = True
    allow_near_feasible_asm2d: bool = False
    enable_relaxed_solve: bool = True
    relaxation_factor: float = 0.1

class HygienePipeline:
    """Solver hygiene pipeline for systematic flowsheet solving."""

    def __init__(self, model: Any=None, config: Optional[PipelineConfig]=None, units: Optional[Dict[str, Any]]=None, session: Optional[Any]=None, builder: Optional[Any]=None):
        """Initialize pipeline.

        Args:
            model: Pyomo model
            config: Pipeline configuration
            units: Optional dict of unit_id -> unit_block. If not provided,
                   units are discovered from model.fs (IDAES convention).
            session: Optional flowsheet session used to recover unit init metadata.
        """
        self._model = model
        self._config = config or PipelineConfig()
        self._state = PipelineState.IDLE
        self._history: List[PipelineResult] = []
        self._units = units
        self._session = session
        self._builder = builder
        self._accepted_near_feasible_solve = False
        self._digester_state: Optional[Dict[str, Any]] = None
        self._adm1_guild_invasion: Optional[Dict[str, Any]] = None
        self._adm1_activity_continuation_attempts: List[Dict[str, Any]] = []
        self._dof_resolver = DOFResolver(model)
        self._scaler = ScalingTools(model)
        _thermal_couplings = getattr(session, 'thermal_couplings', None) if session else None
        _runtime_metadata = getattr(getattr(session, 'config', None), 'runtime_metadata', None)
        _unit_types = {unit_id: unit_inst.unit_type for unit_id, unit_inst in session.units.items()} if session is not None else None
        _unit_roles = {unit_id: getattr(unit_inst, 'role', None) for unit_id, unit_inst in session.units.items()} if session is not None else None
        _unit_stage_index = {unit_id: getattr(unit_inst, 'role_stage_index', None) for unit_id, unit_inst in session.units.items()} if session is not None else None
        _unit_group = {unit_id: getattr(unit_inst, 'role_group', None) for unit_id, unit_inst in session.units.items()} if session is not None else None
        self._roles = RoleResolver.from_session(session) if session is not None else RoleResolver(_unit_roles)
        self._initializer = FlowsheetInitializer(flowsheet=model, model=model, thermal_couplings=_thermal_couplings, runtime_metadata=_runtime_metadata, unit_types=_unit_types, unit_roles=_unit_roles, unit_stage_index=_unit_stage_index, unit_group=_unit_group)
        self._diagnostics = DiagnosticsRunner(model)
        self._recovery = RecoveryExecutor(model)

    def _configured_solver(self, options: Dict[str, Any]) -> Any:
        """Return the configured Pyomo solver with options applied."""
        solver_name = (self._config.solver or 'ipopt').strip().lower()
        if solver_name == 'ipopt':
            if self._use_plain_ipopt_for_nonrecycle_adm1() or self._use_plain_ipopt_for_asm2d_carbon_no3():
                from pyomo.environ import SolverFactory
                solver = SolverFactory('ipopt')
                for opt, val in options.items():
                    solver.options[opt] = val
                return solver
            from watertap_engine_mcp.solvers.ipopt import get_ipopt_solver
            return get_ipopt_solver(options)
        from pyomo.environ import SolverFactory
        solver = SolverFactory(solver_name)
        for opt, val in options.items():
            solver.options[opt] = val
        return solver

    def _use_plain_ipopt_for_nonrecycle_adm1(self) -> bool:
        'Use Pyomo IPOPT directly for NON-RECYCLE ADM1 digestion trains.'
        if not self._is_adm1_biological_model():
            return False
        try:
            return not self._roles.is_adm1_recycle_family()
        except Exception:
            return False

    def _asm2d_carbon_no3_target_constraints(self) -> List[Any]:
        """Return active carbon-dose NO3 target constraints, if present."""
        if self._model is None or not hasattr(self._model, 'component_data_objects'):
            return []
        try:
            from pyomo.environ import Constraint
        except ImportError:
            return []
        constraints = []
        for con in self._model.component_data_objects(Constraint, active=True, descend_into=True):
            name = str(getattr(con, 'name', ''))
            if 'process_constraint_' in name and 'no3_target' in name:
                constraints.append(con)
        return constraints

    def _asm2d_carbon_dose_vars(self) -> List[Any]:
        """Return supplemental-carbon dose variables in the live units map."""
        vars_: List[Any] = []
        for unit in self._roles.units_for(R.SUPPLEMENTAL_CARBON, self.get_units()):
            carbon_dose = getattr(unit, 'carbon_dose', None)
            if carbon_dose is None:
                continue
            for index in (0, 0.0):
                try:
                    vars_.append(carbon_dose[index])
                    break
                except Exception:
                    continue
        return vars_

    def _is_asm2d_carbon_no3_forward_target(self) -> bool:
        """Return True for the forward ASM2D supplemental-carbon NO3 target."""
        if not self._is_asm2d_biological_model():
            return False
        if self._is_asm2d_recycle_topology():
            return False
        units = self.get_units()
        if not self._roles.all_for(R.POST_ANOXIC, units):
            return False
        if not self._roles.all_for(R.SUPPLEMENTAL_CARBON, units):
            return False
        return bool(self._asm2d_carbon_no3_target_constraints())

    def _use_plain_ipopt_for_asm2d_carbon_no3(self) -> bool:
        """Use plain IPOPT for the small forward ASM2D carbon->NO3 target.

        The WaterTAP IPOPT wrapper can report ``infeasible`` on this square,
        non-recycle target solve even from a point whose equality residuals are
        small. Plain IPOPT returns the raw strict optimum for the same warm start.
        Recycle ASM2D families keep the WaterTAP wrapper and their existing
        continuation/near-feasible contracts.
        """
        try:
            return self._is_asm2d_carbon_no3_forward_target()
        except Exception:
            return False

    def _select_adm1_viable_branch(self, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Select the stable guild branch after an optimal ADM1 solve."""
        _, record = self._reseed_adm1_branch(options)
        return record

    def _reseed_adm1_branch(self, options: Dict[str, Any], *, initial_point_optimal: bool=True) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """Construct a guild seed and return the actual resolve and its receipt.

        An optimal ADM1 solve can sit on a trivial X_i ~ 0 root for any biomass
        guild, because the feed carries the guilds only at a trace. The invasion
        test (:mod:`solver.adm1_invasion`) reads each guild's linearised growth off
        the installed rate expressions and each reactor's particulate turnover off
        the solved flows; a washed-out guild that would invade is reseeded (initial
        values only) and the SAME rating is re-solved. The record is surfaced
        whatever the verdict, so a washout that is stable (physical) or a reseed
        that failed is reported, never hidden.

        For a nonoptimal cascade point, growth is only a warm-start heuristic;
        the receipt explicitly distinguishes it from a steady-state invasion test.
        """
        self._adm1_guild_invasion = None
        if self._model is None or self._session is None:
            return (None, None)
        if not self._is_adm1_biological_model():
            return (None, None)
        _results = None
        try:
            from watertap_engine_mcp.solver.adm1_invasion import select_viable_digester_branch
            from watertap_engine_mcp.templates.adm1 import ADM1_RETAINED_SOLIDS_COMPONENTS
            units = self.get_units()
            reactor_ids = list(self._roles.all_for(R.ANAEROBIC_ACIDOGENIC, units)) + list(self._roles.all_for(R.ANAEROBIC_METHANOGENIC, units))
            reactors = [(unit_id, units[unit_id]) for unit_id in reactor_ids if unit_id in units and hasattr(units[unit_id], 'liquid_phase')]
            if not reactors:
                return (None, None)
            connections = [{'src_unit': conn.source_unit, 'src_port': conn.source_port, 'dest_unit': conn.dest_unit, 'dest_port': conn.dest_port} for conn in self._session.connections]
            unit_types = {unit_id: getattr(unit_inst, 'unit_type', None) for unit_id, unit_inst in self._session.units.items()}

            def resolve() -> Any:
                solver = self._configured_solver(options)
                old_stdout, old_stderr = (sys.stdout, sys.stderr)
                try:
                    with open(os.devnull, 'w') as devnull:
                        sys.stdout = devnull
                        sys.stderr = devnull
                        results = solver.solve(self._model, tee=False, load_solutions=False)
                finally:
                    sys.stdout, sys.stderr = (old_stdout, old_stderr)
                if str(results.solver.termination_condition) == 'optimal':
                    self._model.solutions.load_from(results)
                    self._polish_plain_adm1_solution(options)
                return results
            _results, record = select_viable_digester_branch(self._model, reactors, units, connections, unit_types, ADM1_RETAINED_SOLIDS_COMPONENTS, resolve=resolve, is_optimal=lambda res: str(res.solver.termination_condition) == 'optimal', initial_point_optimal=initial_point_optimal)
        except Exception as exc:
            record = {'verdict': 'invasion_stage_error', 'error': f'{type(exc).__name__}: {exc}'}
        if self._adm1_activity_continuation_attempts:
            record['activity_continuation'] = self._adm1_activity_continuation_attempts[-1]
            record['activity_continuation_attempts'] = list(self._adm1_activity_continuation_attempts)
        self._adm1_guild_invasion = record
        return (_results, record)

    def _recover_adm1_cascade_bound_stall(self, options: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """Reseed a finite nonoptimal forward cascade, then solve the same NLP.

        Only initial values change. The nonoptimal point is NOT a washout root;
        guild growth there is used to construct a warm start. An actual optimal
        solver result must follow, then the ordinary residual/classifier gates.
        Recycle families retain their existing continuation route.
        """
        units = self.get_units()
        if self._session is None or not self._is_adm1_biological_model() or self._is_adm1_recycle_family() or (not self._roles.all_for(R.ANAEROBIC_ACIDOGENIC, units)) or (not self._roles.all_for(R.ANAEROBIC_METHANOGENIC, units)):
            return (None, None)
        return self._reseed_adm1_branch(options, initial_point_optimal=False)

    def _adm1_activity_reactor(self):
        """The activity restart is limited to a single nonrecycle Davies AD."""
        if not self._is_adm1_biological_model() or self._is_adm1_recycle_family():
            return None
        reactors = [(name, unit) for name, unit in self.get_units().items() if hasattr(unit, 'liquid_phase') and hasattr(unit, 'vapor_phase')]
        if len(reactors) != 1:
            return None
        name, unit = reactors[0]
        reactions = list(unit.liquid_phase.reactions.values())
        if not reactions or not all((hasattr(r, 'davies_equilibrium') for r in reactions)):
            return None
        return (name, unit, reactions)

    def _recover_adm1_activity_bound_stall(self, options):
        """Restart a failed Davies solve cold, then restore full activities.

        Low-HRT startup can enter a local infeasible basin. Reinitializing that
        failed point is insufficient. Retain the original cold values, initialize
        at ideal activities and continue A(T) to its original value. All physical
        inputs, equations at the accepted endpoint, tolerances and washout tests
        are unchanged. A failed continuation restores the incoming failed point.
        """
        from idaes.core.util.model_statistics import degrees_of_freedom
        reactor = self._adm1_activity_reactor()
        if degrees_of_freedom(self._model) != 0:
            return (None, None)
        cold = getattr(self, '_adm1_activity_cold_start', None)
        if reactor is None or cold is None:
            return (None, None)
        from pyomo.environ import Var, value
        from pyomo.core.expr.visitor import replace_expressions
        name, unit, reactions = reactor
        previous = [(v, value(v, exception=False)) for v in self._model.component_data_objects(Var) if not v.fixed]
        equations = [(c, c.expr, r.debye_huckel_A) for r in reactions for c in r.davies_equilibrium.values()]
        seeded = set(getattr(self._initializer, '_adm1_seeded_units', set()))
        record = {'verdict': 'continuation_failed', 'reactor': name, 'initial_point_optimal': False, 'steps': []}
        accepted = None
        try:
            for v, number in cold:
                if not v.fixed:
                    v.set_value(number, skip_validation=True)
            for r in reactions:
                for v in r.log_gamma.values():
                    v.set_value(0.0)
            self._initializer._adm1_seeded_units = seeded - {name}
            for fraction in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
                for c, expression, coefficient in equations:
                    c.set_value(replace_expressions(expression, {id(coefficient): fraction * coefficient}, remove_named_expressions=False))
                if fraction == 0.0:
                    initialized = self.run_initialization()
                    record['ideal_initialization'] = {'success': initialized.success, 'details': initialized.details}
                    if not initialized.success:
                        return (None, record)
                result = self._configured_solver(options).solve(self._model, tee=False, load_solutions=False)
                termination = str(result.solver.termination_condition)
                record['steps'].append({'activity_fraction': fraction, 'termination_condition': termination})
                if termination != 'optimal':
                    return (None, record)
                self._model.solutions.load_from(result)
            accepted = result
            record['verdict'] = 'full_activity_optimal'
            return (accepted, record)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            return (None, record)
        finally:
            for c, expression, _ in equations:
                c.set_value(expression)
            self._initializer._adm1_seeded_units = seeded
            if accepted is None:
                for v, number in previous:
                    v.set_value(number, skip_validation=True)
            record['original_equations_restored'] = True
            record['incoming_state_restored'] = accepted is None
            self._adm1_activity_continuation_attempts.append(record)

    def _polish_plain_adm1_solution(self, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Polish a plain-IPOPT two-stage ADM1 solution with WaterTAP IPOPT.

        The two-stage corrected-feed cascade needs plain IPOPT to get out of the
        cold initialized basin. Once there, the WaterTAP wrapper can polish the
        same point to the strict post-solve residual contract. Failure to polish is
        surfaced in details; the downstream post-solve diagnostics remain the hard
        gate.
        """
        if not self._use_plain_ipopt_for_nonrecycle_adm1():
            return None
        try:
            from watertap_engine_mcp.solvers.ipopt import get_ipopt_solver
            polish_options = dict(options)
            polish_options['tol'] = min(float(polish_options.get('tol', 1e-10)), 1e-10)
            polish_options['constr_viol_tol'] = min(float(polish_options.get('constr_viol_tol', 1e-10)), 1e-10)
            polish_options['acceptable_tol'] = min(float(polish_options.get('acceptable_tol', 1e-10)), 1e-10)
            polish_options['max_iter'] = max(int(polish_options.get('max_iter', 0) or 0), 2000)
            solver = get_ipopt_solver(polish_options)
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            try:
                with open(os.devnull, 'w') as devnull:
                    sys.stdout = devnull
                    sys.stderr = devnull
                    results = solver.solve(self._model, tee=False, load_solutions=False)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            status = str(results.solver.status)
            termination = str(results.solver.termination_condition)
            loaded = False
            if termination == 'optimal':
                self._model.solutions.load_from(results)
                loaded = True
            payload = {'solver': 'ipopt-watertap', 'solver_status': status, 'termination_condition': termination, 'loaded_solution': loaded, 'solver_options': polish_options}
            if not loaded:
                payload['plain_fallback'] = self._polish_plain_adm1_plain_fallback(polish_options)
            return payload
        except Exception as exc:
            return {'solver': 'ipopt-watertap', 'solver_status': 'error', 'termination_condition': 'solver_error', 'loaded_solution': False, 'error': str(exc)}
    _TRACE_ROW_TERM_CEILING = 0.0001

    def _trace_scale_row_exemption(self, constraint_name: str, residual: float) -> Optional[Dict[str, Any]]:
        """Exempt a violated row only when its ENTIRE content is trace-scale.

        IPOPT cannot resolve an equality row below its noise floor when the
        species it balances is pinned at a zero bound (observed on watertap
        1.7: the X_fa washout-guild material balance carries a ~7e-6 row-unit
        residual that no polish moves, equivalent to ~0.1 mg/d). The exemption
        is surfaced in the stage details — never hidden — and a row with any
        term above the ceiling still gates.
        """
        try:
            if residual is None or abs(float(residual)) > self._TRACE_ROW_TERM_CEILING:
                return None
            if self._model is None:
                return None
            con = self._model.find_component(constraint_name)
            if con is None:
                return None
            from pyomo.environ import value as pyo_value
            body = con.body
            multiplier = 1.0
            for _ in range(3):
                if hasattr(body, 'args') and body.is_expression_type() and (type(body).__name__ in ('ProductExpression', 'MonomialTermExpression', 'NPV_ProductExpression')) and (len(body.args) == 2):
                    const_val = None
                    other = None
                    for cand, oth in ((body.args[0], body.args[1]), (body.args[1], body.args[0])):
                        try:
                            if isinstance(cand, (int, float)) or (hasattr(cand, 'is_constant') and cand.is_constant()):
                                const_val = float(pyo_value(cand))
                                other = oth
                                break
                        except Exception:
                            continue
                    if const_val is None:
                        break
                    multiplier *= const_val
                    body = other
                else:
                    break
            args = list(body.args) if hasattr(body, 'args') and body.is_expression_type() else [body]
            if not args:
                args = [body]
            max_term = 0.0
            for term in args:
                term_val = pyo_value(term, exception=False)
                if term_val is None:
                    return None
                max_term = max(max_term, abs(float(term_val) * multiplier))
            if max_term > self._TRACE_ROW_TERM_CEILING:
                return None
            return {'name': constraint_name, 'residual': float(residual), 'max_abs_term': max_term, 'reason': 'all_terms_below_trace_ceiling', 'trace_ceiling': self._TRACE_ROW_TERM_CEILING}
        except Exception:
            return None

    def _polish_plain_adm1_plain_fallback(self, polish_options: Dict[str, Any]) -> Dict[str, Any]:
        """Plain-IPOPT polish fallback for the stalled watertap-wrapper polish."""
        try:
            from pyomo.environ import SolverFactory
            options = dict(polish_options)
            options.setdefault('mu_strategy', 'monotone')
            options.setdefault('mu_init', 0.0001)
            solver = SolverFactory('ipopt')
            solver.options.update(options)
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            try:
                with open(os.devnull, 'w') as devnull:
                    sys.stdout = devnull
                    sys.stderr = devnull
                    results = solver.solve(self._model, tee=False, load_solutions=False)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            termination = str(results.solver.termination_condition)
            loaded = False
            if termination == 'optimal':
                self._model.solutions.load_from(results)
                loaded = True
            return {'solver': 'ipopt', 'solver_status': str(results.solver.status), 'termination_condition': termination, 'loaded_solution': loaded}
        except Exception as exc:
            return {'solver': 'ipopt', 'solver_status': 'error', 'termination_condition': 'solver_error', 'loaded_solution': False, 'error': str(exc)}

    def _run_asm2d_free_recycle_square_prestage(self, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Square pre-solve for free-recycle ASM2D regularization-objective solves.

        The free-recycle path hands IPOPT the stiff biological equations AND the
        recycle-ratio degrees of freedom in one optimization, and on the current
        stack that coupled solve diverges from a cleanly seeded init while the
        SQUARE problem (ratios pinned, objective off) converges robustly. So:
        pin the free ratios at their seeded values, deactivate the
        regularization objective, solve square, and hand the objective solve a
        feasible warm start. Failure here is recorded, never fatal — the main
        solve still runs either way.
        """
        if not self._is_asm2d_biological_model():
            return None
        if not self._has_recycle_ratio_objective():
            return None
        free_vars = self._free_recycle_ratio_vars()
        if not free_vars:
            return None
        from pyomo.environ import Objective, value
        pinned = []
        for var in free_vars:
            val = value(var, exception=False)
            if val is None:
                continue
            var.fix(val)
            pinned.append(var)
        if not pinned:
            return None
        deactivated = []
        for objective in self._model.component_data_objects(Objective, active=True, descend_into=True):
            if 'asm2d_recycle_ratio_regularization_objective' in str(objective):
                objective.deactivate()
                deactivated.append(objective)
        termination = 'not_attempted'
        loaded = False
        try:
            solver = self._configured_solver(options)
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            try:
                with open(os.devnull, 'w') as devnull:
                    sys.stdout = devnull
                    sys.stderr = devnull
                    results = solver.solve(self._model, tee=False, load_solutions=False)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            termination = str(results.solver.termination_condition)
            if termination == 'optimal':
                self._model.solutions.load_from(results)
                loaded = True
        except Exception as exc:
            termination = f'error: {exc}'
        finally:
            for objective in deactivated:
                objective.activate()
            for var in pinned:
                var.unfix()
        return {'pinned_ratios': len(pinned), 'termination_condition': termination, 'loaded_solution': loaded}

    def _base_solver_options(self) -> Dict[str, Any]:
        """Return default options for the configured solver."""
        if (self._config.solver or 'ipopt').strip().lower() != 'ipopt':
            return {}
        return {'linear_solver': default_ipopt_linear_solver(), 'tol': 1e-06, 'max_iter': 3000, 'mu_init': 0.0001, 'mu_strategy': 'adaptive', 'bound_push': 1e-08, 'bound_frac': 1e-08, 'max_cpu_time': self._config.max_wall_time_s}

    def _is_asm2d_biological_model(self) -> bool:
        """Return True for ASM2D-backed biological reactor trains."""
        units = self.get_units()
        reactor_roles = (R.PRE_ANOXIC, R.AEROBIC, R.POST_ANOXIC)
        reactors = []
        for role in reactor_roles:
            reactors.extend(self._roles.units_for(role, units))
        if not reactors:
            return False
        return any((is_asm2d_property_block(getattr(getattr(reactor, 'config', None), 'property_package', None)) for reactor in reactors))

    def _is_asm2d_multistage_recycle_topology(self) -> bool:
        """Return True for the N-stage Bardenpho-style ASM2D recycle topology."""
        if not self._is_asm2d_biological_model():
            return False
        units = self.get_units()
        return len(self._roles.all_for(R.AEROBIC, units)) > 1 and bool(self._roles.all_for(R.POST_ANOXIC, units)) and bool(self._roles.all_for(R.INTERNAL_RECYCLE_PRESSURE, units)) and bool(self._roles.all_for(R.MBR_SEPARATOR, units))

    def _is_asm2d_recycle_topology(self) -> bool:
        """Return True for role-tagged ASM2D biological recycle topologies."""
        if not self._is_asm2d_biological_model():
            return False
        try:
            return self._roles.is_asm2d_recycle_family()
        except Exception:
            return False

    def _condition_asm2d_before_solve(self) -> None:
        """Apply final ASM2D numeric guards before handing the NLP to IPOPT."""
        if not self._is_asm2d_biological_model():
            return
        apply_trace_floors = getattr(self._initializer, '_apply_asm2d_trace_concentration_floors', None)
        if apply_trace_floors is not None:
            apply_trace_floors(self.get_units())
        if self._is_asm2d_recycle_topology():
            seed_pressure = getattr(self._initializer, '_seed_pressure_changer_output_guesses', None)
            if seed_pressure is not None:
                units = self.get_units()
                for pressure_role in (R.INTERNAL_RECYCLE_PRESSURE, R.RAS_PRESSURE):
                    try:
                        pressure_unit = self._roles.single_unit(pressure_role, units)
                    except Exception:
                        pressure_unit = None
                    if pressure_unit is not None:
                        seed_pressure(pressure_unit)
        if self._is_asm2d_multistage_recycle_topology():
            normalize_mbr_splits = getattr(self._initializer, '_normalize_asm2d_mbr_split_fractions', None)
            if normalize_mbr_splits is not None:
                try:
                    mbr = self._roles.single_unit(R.MBR_SEPARATOR, self.get_units())
                except Exception:
                    mbr = None
                if mbr is not None:
                    normalize_mbr_splits(mbr)

    def _load_nonoptimal_solution_if_finite(self, results: Any) -> bool:
        """Load IPOPT's best iterate for diagnostics/recovery when it is usable."""
        try:
            status = str(results.solver.status).lower()
        except Exception:
            return False
        if status == 'error':
            return False
        try:
            self._model.solutions.load_from(results)
            return True
        except Exception:
            return False

    def _near_feasible_asm2d_details(self, termination: str) -> Optional[Dict[str, Any]]:
        """Accept square ASM2D solves that satisfy equations within proposal tolerance."""
        if not self._is_asm2d_biological_model():
            return None
        if not self._roles.is_asm2d_recycle_family():
            return None
        runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        try:
            residual_tolerance = float((runtime_metadata or {}).get('asm2d_near_feasible_residual_tolerance', 0.001))
        except (TypeError, ValueError):
            residual_tolerance = 0.001
        bound_tolerance = 1e-07
        residuals = self._diagnostics.get_constraint_residuals(self._model, threshold=residual_tolerance)
        bounds = self._diagnostics.get_bound_violations(self._model, tolerance=bound_tolerance)
        if residuals.issues_found == 0 and bounds.issues_found == 0:
            self._accepted_near_feasible_solve = True
            return {'solver_status': 'warning', 'termination_condition': termination, 'accepted_near_feasible': True, 'constraint_residual_tolerance': residual_tolerance, 'bound_violation_tolerance': bound_tolerance, 'residual_basis': self._compute_residual_basis(residual_tolerance=residual_tolerance, bound_tolerance=bound_tolerance)}
        return None

    def _compute_residual_basis(self, *, residual_tolerance: float, bound_tolerance: float) -> Dict[str, Any]:
        """Summarise the worst constraint residual / bound violation at the iterate.

        Queries the diagnostics with a zero floor so the *largest* residual and
        bound deviation are captured even when both are below the acceptance
        tolerance — that worst-case pair is the structured justification for a
        near-feasible acceptance.
        """
        basis: Dict[str, Any] = {'max_equality_residual': None, 'worst_constraint_name': None, 'constraint_residual_tolerance': residual_tolerance, 'constraint_residual_count': None, 'max_bound_violation': None, 'worst_bound_variable': None, 'bound_violation_tolerance': bound_tolerance, 'bound_violation_count': None}
        try:
            residuals = self._diagnostics.get_constraint_residuals(self._model, threshold=0.0, max_results=5)
            if residuals.issues_found >= 0:
                basis['constraint_residual_count'] = residuals.issues_found
                if residuals.details:
                    worst = residuals.details[0]
                    basis['max_equality_residual'] = float(worst.residual)
                    basis['worst_constraint_name'] = str(worst.constraint_name)
        except Exception:
            pass
        try:
            bounds = self._diagnostics.get_bound_violations(self._model, tolerance=0.0, max_results=5)
            if bounds.issues_found >= 0:
                basis['bound_violation_count'] = bounds.issues_found
                if bounds.details:
                    worst = bounds.details[0]
                    if worst.violation_type == 'below_lower' and worst.lower_bound is not None:
                        magnitude = float(worst.lower_bound) - float(worst.value)
                    elif worst.upper_bound is not None:
                        magnitude = float(worst.value) - float(worst.upper_bound)
                    else:
                        magnitude = 0.0
                    basis['max_bound_violation'] = magnitude
                    basis['worst_bound_variable'] = str(worst.variable_name)
        except Exception:
            pass
        return basis

    def _free_recycle_ratio_vars(self) -> List[Any]:
        """Return free ASM2D recycle-ratio variables added by ModelBuilder."""
        if self._model is None:
            return []
        from pyomo.environ import Var
        vars_ = []
        for var in self._model.component_data_objects(Var, descend_into=True):
            name = str(var).lower()
            if 'process_var_asm2d_' not in name or 'recycle_ratio' not in name:
                continue
            if not getattr(var, 'fixed', False):
                vars_.append(var)
        return vars_

    def _free_alkalinity_min_dose_vars(self) -> List[Any]:
        """Return free caustic dose vars owned by min-form alkalinity targets."""
        if self._model is None or self._session is None:
            return []
        metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        specs = metadata.get('alkalinity_min_dose_actuators') if isinstance(metadata, dict) else None
        if not specs:
            return []
        names = {str(spec.get('variable')) for spec in specs if isinstance(spec, dict) and spec.get('variable')}
        if not names:
            return []
        from pyomo.environ import Var
        vars_ = []
        for var in self._model.component_data_objects(Var, descend_into=True):
            if var.name in names and (not getattr(var, 'fixed', False)):
                vars_.append(var)
        return vars_

    def _all_recycle_ratio_vars(self) -> List[Any]:
        """Return all ASM2D recycle-ratio variables added by ModelBuilder."""
        if self._model is None:
            return []
        from pyomo.environ import Var
        vars_ = []
        for var in self._model.component_data_objects(Var, descend_into=True):
            name = str(var).lower()
            if 'process_var_asm2d_' not in name or 'recycle_ratio' not in name:
                continue
            vars_.append(var)
        return vars_

    def _has_recycle_ratio_objective(self) -> bool:
        if self._model is None:
            return False
        from pyomo.environ import Objective
        for objective in self._model.component_data_objects(Objective, active=True, descend_into=True):
            if 'asm2d_recycle_ratio_regularization_objective' in str(objective):
                return True
        return False

    def _has_active_objective(self) -> bool:
        if self._model is None:
            return False
        from pyomo.environ import Objective
        return any((True for _ in self._model.component_data_objects(Objective, active=True, descend_into=True)))

    def _deactivate_recycle_ratio_objective_if_square(self) -> List[str]:
        """Remove recycle-ratio regularization when the ASM2D model is square."""
        if self._model is None or not self._is_asm2d_biological_model():
            return []
        try:
            from idaes.core.util.model_statistics import degrees_of_freedom
            from pyomo.environ import Objective
        except ImportError:
            return []
        try:
            if degrees_of_freedom(self._model) != 0:
                return []
        except Exception:
            return []
        actions = []
        for objective in self._model.component_data_objects(Objective, active=True, descend_into=True):
            if 'asm2d_recycle_ratio_regularization_objective' not in str(objective):
                continue
            objective.deactivate()
            actions.append(f'Deactivated square-model objective {objective.name}')
        return actions

    def _can_optimize_bounded_objective_variables(self, total_dof: int) -> bool:
        """Allow positive DOF only for bounded variables with an active objective."""
        if total_dof <= 0:
            return False
        free_ratio_vars = self._free_recycle_ratio_vars() if self._is_asm2d_biological_model() else []
        objective_vars = free_ratio_vars + self._free_alkalinity_min_dose_vars()
        return bool(objective_vars and total_dof <= len(objective_vars) and self._has_active_objective())

    def _solve_current_model(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """Run the configured solver with supplied options and return compact metadata."""
        self._condition_asm2d_before_solve()
        objective_actions = self._deactivate_recycle_ratio_objective_if_square()
        options = self._with_asm2d_solver_defaults(options)
        options = self._with_adm1_solver_defaults(options)
        solver = self._configured_solver(options)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            with open(os.devnull, 'w') as devnull:
                sys.stdout = devnull
                sys.stderr = devnull
                results = solver.solve(self._model, tee=False, load_solutions=False)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        termination = str(results.solver.termination_condition)
        loaded_nonoptimal = False
        if termination == 'optimal':
            self._model.solutions.load_from(results)
            success = True
            near_feasible = None
        else:
            loaded_nonoptimal = self._load_nonoptimal_solution_if_finite(results)
            near_feasible = self._near_feasible_asm2d_details(termination) if self._config.allow_near_feasible_asm2d else None
            success = near_feasible is not None
        details = {'success': success, 'solver': self._config.solver, 'solver_status': str(results.solver.status), 'termination_condition': termination, 'solve_time': getattr(results.solver, 'wallclock_time', None), 'iterations': getattr(results.solver, 'iterations', None), 'objective_actions': objective_actions, 'loaded_nonoptimal_solution': loaded_nonoptimal}
        if near_feasible is not None:
            details.update(near_feasible)
            details['loaded_nonoptimal_solution'] = loaded_nonoptimal
        return {**details}

    def _run_asm2d_carbon_no3_forward_warm_start(self, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Warm-start carbon->NO3 solve_for from a fixed-dose forward solve."""
        if not self._is_asm2d_carbon_no3_forward_target():
            return None
        target_constraints = self._asm2d_carbon_no3_target_constraints()
        dose_vars = self._asm2d_carbon_dose_vars()
        free_dose_vars = [var for var in dose_vars if not getattr(var, 'fixed', False)]
        if not target_constraints or not free_dose_vars:
            return None
        from pyomo.environ import value
        dose_snapshot = [(var, bool(var.fixed), value(var, exception=False)) for var in free_dose_vars]
        target_snapshot = [(con, bool(con.active)) for con in target_constraints]
        loaded = False
        try:
            for con, _active in target_snapshot:
                con.deactivate()
            for var, _fixed, current in dose_snapshot:
                var.fix(float(current or 0.0))
            self._condition_asm2d_before_solve()
            solver = self._configured_solver(options)
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            try:
                with open(os.devnull, 'w') as devnull:
                    sys.stdout = devnull
                    sys.stderr = devnull
                    results = solver.solve(self._model, tee=False, load_solutions=False)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            termination = str(results.solver.termination_condition)
            if termination == 'optimal':
                self._model.solutions.load_from(results)
                loaded = True
            else:
                loaded = self._load_nonoptimal_solution_if_finite(results)
            return {'strategy': 'asm2d_carbon_no3_fixed_dose_forward_warm_start', 'solver_status': str(results.solver.status), 'termination_condition': termination, 'loaded_solution': loaded, 'fixed_dose_values': [float(value(var, exception=False) or 0.0) for var, _fixed, _current in dose_snapshot]}
        except Exception as exc:
            return {'strategy': 'asm2d_carbon_no3_fixed_dose_forward_warm_start', 'solver_status': 'error', 'termination_condition': 'solver_error', 'loaded_solution': loaded, 'error': str(exc)}
        finally:
            for con, was_active in target_snapshot:
                if was_active:
                    con.activate()
                else:
                    con.deactivate()
            for var, was_fixed, _current in dose_snapshot:
                try:
                    if was_fixed:
                        var.fix()
                    else:
                        var.unfix()
                except Exception:
                    continue

    def _with_asm2d_solver_defaults(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """Apply numerical defaults for ASM2D recycle-dominated NLPs."""
        updated = dict(options)
        if self._is_asm2d_biological_model() and (not self._asm2d_has_fixed_recycle_target()):
            updated.setdefault('hessian_approximation', 'limited-memory')
        return updated

    def _is_adm1_biological_model(self) -> bool:
        """Return True for an ADM1 / mADM1 anaerobic digestion flowsheet.

        Keyed on the digestion role set (``has_adm1_digestion_roles``) confirmed
        by an ADM1-backed digester block, so a project-named role-tagged UASB is
        recognized exactly like the canonical names. The ASM2D recycle family is
        disjoint, so this never fires for an activated-sludge train.
        """
        try:
            if not self._roles.has_adm1_digestion_roles():
                return False
        except Exception:
            return False
        for unit in self.get_units().values():
            config = getattr(unit, 'config', None)
            for attr in ('liquid_property_package', 'property_package', 'vapor_property_package'):
                package = getattr(config, attr, None)
                if package is not None and 'ADM1' in f'{type(package).__name__} {package}':
                    return True
        return False

    def _with_adm1_solver_defaults(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """Apply numerical defaults for the stiff ADM1 acid/base + gas-transfer NLP.

        The shared base options are tuned for the ASM2D recycle family
        (``mu_strategy='adaptive'``). On the warm-started ADM1 digester that
        adaptive barrier interacts with the many at-bound trace species and tips
        IPOPT into an internal evaluation error; the monotone barrier solves the
        same model cleanly to strict optimal. Applied only when the flowsheet is
        an ADM1 digestion family, so the ASM2D path is untouched.
        """
        if not self._is_adm1_biological_model():
            return options
        updated = dict(options)
        updated['mu_strategy'] = 'monotone'
        if not self._is_adm1_recycle_family():
            return updated
        if self._adm1_egsb_recirculation_split_var() is not None:
            try:
                updated['max_iter'] = min(int(updated.get('max_iter', 1400)), 1400)
            except (TypeError, ValueError):
                updated['max_iter'] = 1400
        updated.setdefault('nlp_scaling_method', 'user-scaling')
        try:
            updated['tol'] = min(float(updated.get('tol', 1e-08)), 1e-08)
        except (TypeError, ValueError):
            updated['tol'] = 1e-08
        try:
            updated['constr_viol_tol'] = min(float(updated.get('constr_viol_tol', 1e-08)), 1e-08)
        except (TypeError, ValueError):
            updated['constr_viol_tol'] = 1e-08
        return updated

    def _adm1_design_parameters(self) -> Dict[str, Any]:
        runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        if not isinstance(runtime_metadata, dict):
            return {}
        design_parameters = runtime_metadata.get('design_parameters')
        return design_parameters if isinstance(design_parameters, dict) else {}

    def _adm1_egsb_recirculation_split_var(self) -> Optional[Any]:
        """Return the EGSB recirculation split variable, role-keyed by recuperation."""
        design_parameters = self._adm1_design_parameters()
        if design_parameters.get('recirculation_ratio') is None:
            return None
        try:
            units = self.get_units()
            recuperation_id = self._roles.single_id(R.ANAEROBIC_RECUPERATION, units)
        except Exception:
            return None
        recuperation = units.get(recuperation_id)
        split_fraction = getattr(recuperation, 'split_fraction', None)
        if split_fraction is None:
            return None
        for index in ((0, 'recirculation'), (0.0, 'recirculation'), 'recirculation'):
            try:
                return split_fraction[index]
            except Exception:
                continue
        return None

    def _set_adm1_egsb_recirculation_fraction_guess(self, fraction: float) -> None:
        """Seed EGSB recuperation outlet fractions consistently for continuation."""
        try:
            units = self.get_units()
            recuperation_id = self._roles.single_id(R.ANAEROBIC_RECUPERATION, units)
        except Exception:
            return
        recuperation = units.get(recuperation_id)
        split_fraction = getattr(recuperation, 'split_fraction', None)
        if split_fraction is None:
            return
        values = {'recirculation': fraction, 'forward': 1.0 - fraction}
        for outlet, value_ in values.items():
            for index in ((0, outlet), (0.0, outlet), outlet):
                try:
                    var = split_fraction[index]
                except Exception:
                    continue
                if not getattr(var, 'fixed', False):
                    try:
                        var.set_value(value_, skip_validation=True)
                    except Exception:
                        pass
                break
        seed_outputs = getattr(self._initializer, '_seed_splitter_output_guesses', None)
        if seed_outputs is not None:
            try:
                seed_outputs(recuperation, ('forward', 'recirculation'))
            except Exception:
                pass
        blend = getattr(self._initializer, '_blend_port_state', None)
        if blend is None:
            return
        try:
            mixer = self._roles.single_unit(R.MIXER, units)
        except Exception:
            mixer = None
        try:
            feed = self._roles.single_unit(R.FEED, units)
        except Exception:
            feed = None
        try:
            digester = self._roles.single_unit(R.ANAEROBIC_METHANOGENIC, units)
        except Exception:
            digester = None
        feed_outlet = getattr(feed, 'outlet', None) if feed is not None else None
        mixer_feed = getattr(mixer, 'feed_water', None) if mixer is not None else None
        if feed_outlet is not None and mixer_feed is not None:
            try:
                blend(feed_outlet, mixer_feed, 1.0)
            except Exception:
                pass
        mixer_outlet = getattr(mixer, 'outlet', None) if mixer is not None else None
        digester_inlet = getattr(digester, 'inlet', None) if digester is not None else None
        if fraction <= 1e-12 and feed_outlet is not None and (mixer_outlet is not None):
            try:
                blend(feed_outlet, mixer_outlet, 1.0)
            except Exception:
                pass
        if fraction <= 1e-12 and feed_outlet is not None and (digester_inlet is not None):
            try:
                blend(feed_outlet, digester_inlet, 1.0)
            except Exception:
                pass
        if mixer_outlet is not None and digester_inlet is not None:
            try:
                blend(mixer_outlet, digester_inlet, 1.0)
            except Exception:
                pass
        digester_liquid = getattr(digester, 'liquid_outlet', None) if digester is not None else None
        recuperation_inlet = getattr(recuperation, 'inlet', None)
        if digester_liquid is not None and recuperation_inlet is not None:
            try:
                blend(digester_liquid, recuperation_inlet, 1.0)
            except Exception:
                pass
        recirculation_port = getattr(recuperation, 'recirculation', None)
        mixer_recycle_port = getattr(mixer, 'recycle', None) if mixer is not None else None
        if recirculation_port is not None and mixer_recycle_port is not None:
            try:
                blend(recirculation_port, mixer_recycle_port, 1.0)
            except Exception:
                pass
        try:
            effluent = self._roles.single_unit(R.PRODUCT_EFFLUENT, units)
        except Exception:
            effluent = None
        forward_port = getattr(recuperation, 'forward', None)
        effluent_port = getattr(effluent, 'inlet', None) if effluent is not None else None
        if forward_port is not None and effluent_port is not None:
            try:
                blend(forward_port, effluent_port, 1.0)
            except Exception:
                pass
        self._seed_adm1_retention_time_from_hydraulics(digester)

    def _seed_adm1_retention_time_from_hydraulics(self, digester: Optional[Any]) -> None:
        """Seed ADM1 retention time from current liquid volume and inlet flow."""
        if digester is None:
            return
        retention = self._indexed_var(getattr(digester, 'AD_retention_time', None), 0, 0.0, None)
        if retention is None:
            return
        volume = self._indexed_var(getattr(digester, 'volume_liquid', None), 0, 0.0, None)
        inlet = getattr(digester, 'inlet', None)
        flow = self._indexed_var(getattr(inlet, 'flow_vol', None), 0, 0.0, None)
        if volume is None or flow is None:
            return
        try:
            from pyomo.environ import value
            volume_value = value(volume, exception=False)
            flow_value = value(flow, exception=False)
        except Exception:
            return
        if volume_value is None or flow_value is None:
            return
        try:
            flow_float = float(flow_value)
            if flow_float <= 0:
                return
            retention.set_value(float(volume_value) / flow_float, skip_validation=True)
        except TypeError:
            try:
                retention.set_value(float(volume_value) / float(flow_value))
            except Exception:
                return
        except Exception:
            return

    def _strict_solve_current_model_no_nonoptimal_load(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """Solve once and load only a raw optimal result.

        ADM1 continuation uses this instead of ``_solve_current_model`` so a
        failed intermediate recirculation step does not load IPOPT's non-optimal
        iterate over the previous strict warm start.
        """
        solver = self._configured_solver(options)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            with open(os.devnull, 'w') as devnull:
                sys.stdout = devnull
                sys.stderr = devnull
                results = solver.solve(self._model, tee=False, load_solutions=False)
        except Exception as exc:
            return {'success': False, 'solver_status': 'error', 'termination_condition': 'solver_error', 'error': str(exc)}
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        termination = str(results.solver.termination_condition)
        status = str(results.solver.status)
        success = termination == 'optimal'
        if success:
            self._model.solutions.load_from(results)
        return {'success': success, 'solver': self._config.solver, 'solver_status': status, 'termination_condition': termination, 'solve_time': getattr(results.solver, 'wallclock_time', None), 'iterations': getattr(results.solver, 'iterations', None), 'loaded_nonoptimal_solution': False}

    def _run_adm1_egsb_recirculation_continuation(self, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Warm-start EGSB from no recirculation to the design recirculation ratio."""
        split_var = self._adm1_egsb_recirculation_split_var()
        if split_var is None:
            return None
        design_parameters = self._adm1_design_parameters()
        try:
            target_ratio = max(float(design_parameters['recirculation_ratio']), 0.0)
        except (KeyError, TypeError, ValueError):
            return None
        target_fraction = target_ratio / (1.0 + target_ratio)
        try:
            from pyomo.environ import value
            original_fixed = bool(getattr(split_var, 'fixed', False))
            original_value = float(value(split_var, exception=False))
        except Exception:
            original_fixed = bool(getattr(split_var, 'fixed', False))
            original_value = target_fraction
        if target_ratio <= 0.0:
            return None
        start_ratio = min(target_ratio, 0.001)
        schedule: List[float] = [start_ratio]
        for ratio in (0.25, 0.5, 1.0, 2.0):
            if ratio <= target_ratio + 1e-12:
                schedule.append(ratio)
        if not any((abs(ratio - target_ratio) <= 1e-12 for ratio in schedule)):
            schedule.append(target_ratio)
        steps: List[Dict[str, Any]] = []
        failed = False
        try:
            for ratio in schedule:
                fraction = ratio / (1.0 + ratio) if ratio > 0 else 0.0
                split_var.fix(fraction)
                self._set_adm1_egsb_recirculation_fraction_guess(fraction)
                step_result = self._solve_current_model(options)
                steps.append({'recirculation_ratio': ratio, 'split_fraction': fraction, 'result': step_result})
                if not step_result.get('success') and (not step_result.get('loaded_nonoptimal_solution')):
                    failed = True
                    break
        finally:
            split_var.set_value(target_fraction, skip_validation=True)
            if original_fixed:
                split_var.fix(target_fraction)
            else:
                split_var.unfix()
                split_var.set_value(original_value, skip_validation=True)
            self._set_adm1_egsb_recirculation_fraction_guess(target_fraction)
        completed_schedule = not failed and bool(steps)
        strict_steps = [bool(step.get('result', {}).get('success')) for step in steps if isinstance(step, dict)]
        loaded_nonoptimal_steps = [bool(step.get('result', {}).get('loaded_nonoptimal_solution')) for step in steps if isinstance(step, dict)]
        return {'strategy': 'adm1_egsb_recirculation_continuation', 'target_recirculation_ratio': target_ratio, 'target_split_fraction': target_fraction, 'schedule': schedule, 'steps': steps, 'completed_schedule': completed_schedule, 'success': completed_schedule and all(strict_steps), 'loaded_nonoptimal_warm_start_steps': sum(loaded_nonoptimal_steps)}

    def _is_adm1_recycle_family(self) -> bool:
        """Return True for an ADM1 digester train carrying a closed liquid recycle.

        Property-aware confirmation of the roles-only
        ``RoleResolver.is_adm1_recycle_family`` classifier: the digestion role set
        + a recycle surface role (recycle mixer / recuperation splitter) AND an
        ADM1-backed digester block. The EGSB / AnMBR / anaerobic-contact families
        satisfy this; the UASB and the forward two-stage cascade do not.
        """
        try:
            if not self._roles.is_adm1_recycle_family():
                return False
        except Exception:
            return False
        return self._is_adm1_biological_model()

    def _is_adm1_retained_solids_recycle_family(self) -> bool:
        """Return True for ADM1 recycle trains with a retained-solids separator.

        Contact and AnMBR use thickener/membrane retention plus a finite-SRT bleed;
        those topologies need additional gas-transfer row conditioning after the
        shared recycle material-balance scaling. EGSB is a hydraulic recycle only
        and regresses if it inherits those gas-row weights.
        """
        if not self._is_adm1_recycle_family():
            return False
        try:
            units = self.get_units()
        except Exception:
            return False
        retained_solids_units = {'Membrane', 'RetentateBleed', 'Thickener', 'SludgeSplitter'}
        return any((name in units for name in retained_solids_units))

    def _find_digester_unit(self) -> Optional[Any]:
        """Return the anaerobic-digester (AD) block to classify the basin on.

        Prefers the METHANOGENIC reactor (the healthy/sour basin of interest) so a
        two-stage train classifies on its methane stage, not the acidogenic one;
        falls back to the first two-phase AD block for an untagged train.
        """
        try:
            units = self.get_units()
        except Exception:
            return None

        def _is_ad(unit: Any) -> bool:
            return hasattr(unit, 'liquid_phase') and hasattr(unit, 'vapor_phase') and hasattr(unit, 'initialize_build')
        try:
            mid = self._roles.single_id(R.ANAEROBIC_METHANOGENIC, units)
            if mid is not None:
                meth = units.get(mid)
                if meth is not None and _is_ad(meth):
                    return meth
        except ValueError:
            raise
        except Exception:
            pass
        for unit in units.values():
            if _is_ad(unit):
                return unit
        return None

    def _classify_digester_basin(self) -> Optional[Dict[str, Any]]:
        """Classify the converged ADM1 digester basin (healthy / sour / washout).

        Returns ``None`` for non-ADM1 flowsheets or when no digester block is
        present, so the call is a safe no-op on every other family.
        """
        if not self._is_adm1_biological_model():
            return None
        digester = self._find_digester_unit()
        if digester is None:
            return None
        try:
            from watertap_engine_mcp.solver.digester_diagnostics import classify_digester_basin
            return classify_digester_basin(digester)
        except Exception as exc:
            return {'classification': 'indeterminate', 'is_sour': False, 'is_healthy': False, 'warnings': [f'Digester basin classification failed: {exc}']}

    def _adm1_ph_target_healthy_basin_guard(self) -> Optional[Dict[str, Any]]:
        """Return an honest pH-control guard when the iterate leaves methanogenesis.

        A high pH target can drive modified-ADM1 into an H2-rich non-methanogenic
        basin. That is a biochemical/control ceiling, not a strict-optimal design
        point to polish. The guard is diagnostic only: it does not relax equations
        or accept a solve, it marks the structured non-success with the measured
        dissolved H2 / H2 partial-pressure evidence.
        """
        metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        specs = metadata.get('adm1_ph_target_specs') if isinstance(metadata, dict) else None
        if not specs:
            return None
        digester = self._find_digester_unit()
        if digester is None:
            return None
        try:
            from pyomo.environ import value
        except Exception:
            return None
        try:
            t = next(iter(digester.vapor_phase.keys()))
        except Exception:
            t = 0.0
        h2_kg_m3 = None
        ph2_pa = None
        ph = None
        try:
            h2_kg_m3 = float(value(digester.liquid_phase.properties_out[t].conc_mass_comp['S_h2'], exception=False))
        except Exception:
            pass
        try:
            ph2_pa = float(value(digester.vapor_phase[t].pressure_sat['S_h2'], exception=False))
        except Exception:
            pass
        try:
            ph = float(value(digester.liquid_phase.reactions[t].pH, exception=False))
        except Exception:
            pass
        ph2_bar = ph2_pa / 100000.0 if ph2_pa is not None else None
        h2_limit = 0.0001
        ph2_limit_bar = 0.001
        outside = h2_kg_m3 is not None and h2_kg_m3 > h2_limit or (ph2_bar is not None and ph2_bar > ph2_limit_bar)
        if not outside:
            return None
        target_values = [spec.get('target_value') for spec in specs if isinstance(spec, dict) and spec.get('target_metric') in (None, 'ph')]
        return {'status': 'outside_healthy_methanogenic_basin', 'message': 'ADM1 pH target drove the digester outside the healthy methanogenic basin; reported as structured non-success rather than strict-optimal.', 'thresholds': {'S_h2_kg_m3': h2_limit, 'pH2_bar': ph2_limit_bar}, 'observed': {'S_h2_kg_m3': h2_kg_m3, 'pH2_bar': ph2_bar, 'pH': ph}, 'target_ph_values': target_values}

    def _apply_adm1_vapor_pressure_floor(self) -> Dict[str, Any]:
        """Apply the ADM1 vapor-pressure lower bound after initialization.

        The AD gas-flow equation needs the atmospheric floor to rule out the
        collapsed zero-pressure branch, but applying that floor before unit
        initialization disrupts the healthy baseline basin search. This guard
        preserves the same named pressure variable, leaves it unfixed, and only
        tightens the lower bound once initialization has produced a physical
        headspace state.
        """
        if not self._is_adm1_biological_model():
            return {}
        try:
            from pyomo.environ import value
        except Exception:
            return {}
        runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        if isinstance(runtime_metadata, dict) and runtime_metadata.get('adm1_metal_target_specs') and (not runtime_metadata.get('adm1_temperature_target_specs')):
            return {'mode': 'metal_target_pressure_seed_only'}
        floor_pa = 101325.0
        bounded: List[str] = []
        seeded: List[str] = []
        overpressure_names: List[str] = []
        for unit in self.get_units().values():
            if not (hasattr(unit, 'vapor_phase') and hasattr(unit, 'flow_vol_vap') and hasattr(unit, 'outlet_P')):
                continue
            try:
                time_set = list(unit.vapor_phase.keys())
            except Exception:
                time_set = [0.0]
            for t in time_set:
                try:
                    pressure = unit.vapor_phase[t].pressure
                except Exception:
                    continue
                try:
                    overpressure = None
                    if hasattr(unit, 'adm1_headspace_overpressure'):
                        try:
                            overpressure = unit.adm1_headspace_overpressure[t]
                        except Exception:
                            overpressure = None
                    if overpressure is not None:
                        lower = overpressure.lb
                        if lower is None or float(lower) < 0.0:
                            overpressure.setlb(0.0)
                        current_overpressure = value(overpressure, exception=False)
                        if current_overpressure is None:
                            current_pressure = value(pressure, exception=False)
                            gauge = 0.0
                            if current_pressure is not None:
                                gauge = max(float(current_pressure) - floor_pa, 0.0)
                            overpressure.set_value(gauge, skip_validation=True)
                        overpressure_names.append(overpressure.name)
                    else:
                        lower = pressure.lb
                        if lower is None or float(lower) < floor_pa:
                            pressure.setlb(floor_pa)
                        bounded.append(pressure.name)
                    current = value(pressure, exception=False)
                    if current is None:
                        pressure.set_value(floor_pa, skip_validation=True)
                    seeded.append(pressure.name)
                except Exception:
                    continue
        payload = {'floor_Pa': floor_pa, 'bounded_pressure_variable_names': sorted(set(bounded)), 'headspace_overpressure_variable_names': sorted(set(overpressure_names)), 'seeded_pressure_variable_names': sorted(set(seeded)), 'mode': 'headspace_overpressure_reposed' if overpressure_names else 'seed_then_pre_solve_lower_bound'}
        if (bounded or overpressure_names) and self._session is not None:
            metadata = getattr(self._session.config, 'runtime_metadata', None)
            if isinstance(metadata, dict):
                existing = metadata.get('adm1_vapor_pressure_floor')
                if isinstance(existing, dict):
                    merged = dict(existing)
                    if existing.get('mode') == 'temperature_target_build_lower_bound':
                        payload['mode'] = existing['mode']
                    merged.update(payload)
                    payload = merged
                metadata['adm1_vapor_pressure_floor'] = payload
        return payload

    def _asm2d_has_fixed_recycle_target(self) -> bool:
        """Return True when design metadata pins any recycle ratio."""
        runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        if not isinstance(runtime_metadata, dict):
            return False
        design_parameters = runtime_metadata.get('design_parameters')
        if not isinstance(design_parameters, dict):
            return False
        recycle_parameters = design_parameters.get('recycle_ratios')
        if not isinstance(recycle_parameters, dict):
            return False
        modes = recycle_parameters.get('mode', {})
        if isinstance(modes, dict) and any((str(mode).lower() == 'fixed' for mode in modes.values())):
            return True
        topology = recycle_parameters.get('topology', {})
        if isinstance(topology, dict) and any((isinstance(spec, dict) and spec.get('target_ratio') is not None for spec in topology.values())):
            return True
        return any((recycle_parameters.get(key) is not None for key in ('ras', 'mlr', 'ras_recycle_ratio', 'mlr_recycle_ratio')))

    def _relax_recycle_ratio_bounds(self) -> List[str]:
        """Record free recycle-ratio envelopes for the relaxed ASM2D solve branch."""
        from pyomo.environ import value
        actions = []
        for ratio_var in self._free_recycle_ratio_vars():
            lower = float(ratio_var.lb) if ratio_var.lb is not None else 0.0
            upper = float(ratio_var.ub) if ratio_var.ub is not None else lower + 1.0
            current = value(ratio_var, exception=False)
            if current is None or current < lower or current > upper:
                ratio_var.set_value((lower + upper) / 2.0, skip_validation=True)
            actions.append(f'Kept {ratio_var.name} within template bounds ({lower}, {upper})')
        return actions

    def _reset_recycle_ratio_values_to_nominal(self) -> List[str]:
        """Move free recycle ratios back to physically central starting values."""
        from pyomo.environ import value
        actions = []
        for ratio_var in self._free_recycle_ratio_vars():
            lb = ratio_var.lb
            ub = ratio_var.ub
            nominal = self._nominal_recycle_ratio_value(ratio_var)
            old_value = value(ratio_var, exception=False)
            try:
                ratio_var.set_value(nominal, skip_validation=True)
            except Exception:
                continue
            actions.append(f'Reset {ratio_var.name} from {old_value} to nominal {nominal} within ({lb}, {ub})')
        return actions

    def _nominal_recycle_ratio_value(self, ratio_var: Any) -> float:
        """Return a topology-neutral hydraulic starting value for a ratio var."""
        name = str(ratio_var).lower()
        lb = ratio_var.lb
        ub = ratio_var.ub
        lower = float(lb) if lb is not None else 0.0
        upper = float(ub) if ub is not None else lower + 1.0
        if 'ras' in name:
            nominal = (lower + upper) / 2.0
            try:
                units = self.get_units()
                mbr = self._roles.single_unit(R.MBR_SEPARATOR, units)
                clarifier = self._roles.single_unit(R.SECONDARY_CLARIFIER, units)
                separator = mbr or clarifier
                if separator is not None:
                    if separator is mbr:
                        permeate = self._initializer._asm2d_split_value(separator, 'permeate', 'H2O', default=0.2)
                    else:
                        permeate = self._initializer._asm2d_split_value(separator, 'effluent', 'H2O', default=0.5)
                    permeate = max(0.0001, min(0.9999, float(permeate)))
                    hydraulic_upper = 0.98 * (1.0 - permeate) / permeate
                    nominal = min(nominal, max(lower, hydraulic_upper))
            except Exception:
                pass
        elif 'mlr' in name:
            nominal = (lower + upper) / 2.0
        else:
            nominal = (lower + upper) / 2.0
        return max(lower, min(upper, nominal))

    def _fixed_recycle_ratio_vars(self) -> List[Any]:
        """Return fixed ASM2D recycle-ratio variables for continuation solves."""
        if self._model is None:
            return []
        from pyomo.environ import Var
        vars_ = []
        for var in self._model.component_data_objects(Var, descend_into=True):
            name = str(var).lower()
            if 'process_var_asm2d_' not in name or 'recycle_ratio' not in name:
                continue
            if getattr(var, 'fixed', False):
                vars_.append(var)
        return vars_

    def _run_fixed_recycle_ratio_continuation(self, options: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Warm-start fixed recycle targets from nominal hydraulics to requested values."""
        from pyomo.environ import Objective, minimize, value
        fixed_vars = self._fixed_recycle_ratio_vars()
        if not fixed_vars:
            return None
        continuation_vars = []
        for ratio_var in fixed_vars:
            target = value(ratio_var, exception=False)
            if target is None:
                continue
            lower = float(ratio_var.lb) if ratio_var.lb is not None else 0.0
            upper = float(ratio_var.ub) if ratio_var.ub is not None else max(lower + 1.0, float(target))
            start = max(lower, 0.0)
            continuation_vars.append({'var': ratio_var, 'target': float(target), 'start': float(start), 'lower': lower, 'upper': upper})
        if not continuation_vars:
            return None
        fs = getattr(self._model, 'fs', self._model)
        active_objectives = list(self._model.component_data_objects(Objective, active=True, descend_into=True))
        temp_objective_name = 'asm2d_recycle_ratio_continuation_objective'

        def _solve_with_temporary_free_targets() -> Optional[Dict[str, Any]]:
            if hasattr(fs, temp_objective_name):
                fs.del_component(getattr(fs, temp_objective_name))
            centers = {}
            for ratio_var in self._all_recycle_ratio_vars():
                centers[ratio_var.name] = self._nominal_recycle_ratio_value(ratio_var)
            for item in continuation_vars:
                item['var'].unfix()
                item['var'].set_value(item['start'], skip_validation=True)
                centers[item['var'].name] = item['start']
            terms = []
            for ratio_var in self._all_recycle_ratio_vars():
                lower = float(ratio_var.lb) if ratio_var.lb is not None else 0.0
                upper = float(ratio_var.ub) if ratio_var.ub is not None else lower + 1.0
                span = max(upper - lower, 1.0)
                center = centers.get(ratio_var.name, self._nominal_recycle_ratio_value(ratio_var))
                terms.append(((ratio_var - center) / span) ** 2)
            if not terms:
                return None
            for objective in active_objectives:
                objective.deactivate()
            setattr(fs, temp_objective_name, Objective(expr=sum(terms), sense=minimize))
            try:
                self._preconverge_asm2d_recycle_hydraulics(seed_states=True, force_biomass=True)
                return self._solve_current_model(options)
            finally:
                if hasattr(fs, temp_objective_name):
                    fs.del_component(getattr(fs, temp_objective_name))
                for objective in active_objectives:
                    objective.activate()
        warm_result = _solve_with_temporary_free_targets()
        if warm_result is not None and (not warm_result.get('success')):
            for item in continuation_vars:
                item['var'].fix(item['target'])
            return {**warm_result, 'continuation_failed_at_alpha': 'free-warm-start', 'continuation_targets': {item['var'].name: item['target'] for item in continuation_vars}}
        levels = {0.0}
        for item in continuation_vars:
            target = max(item['target'], item['start'])
            for level in (1.0, 2.0, target):
                if level <= target + 1e-12:
                    levels.add(level)
        last_result: Optional[Dict[str, Any]] = None
        for level in sorted(levels):
            for item in continuation_vars:
                ratio_var = item['var']
                target = item['target']
                if target >= item['start']:
                    trial_value = min(target, max(item['start'], level))
                else:
                    trial_value = max(target, min(item['start'], level))
                trial_value = max(item['lower'], min(item['upper'], trial_value))
                ratio_var.fix(trial_value)
            self._preconverge_asm2d_recycle_hydraulics(seed_states=False)
            last_result = self._solve_current_model(options)
            if not last_result.get('success'):
                for item in continuation_vars:
                    item['var'].fix(item['target'])
                return {**last_result, 'continuation_failed_at_level': level, 'continuation_targets': {item['var'].name: item['target'] for item in continuation_vars}, 'continuation_schedule': sorted(levels), 'continuation_trial_values': {item['var'].name: value(item['var'], exception=False) for item in continuation_vars}}
        for item in continuation_vars:
            item['var'].fix(item['target'])
        if last_result is not None:
            last_result = {**last_result, 'continuation_targets': {item['var'].name: item['target'] for item in continuation_vars}, 'continuation_schedule': sorted(levels)}
        return last_result

    def _fix_free_recycle_ratios_for_endpoint_retry(self) -> List[str]:
        """Pin remaining free recycle ratios to robust endpoint starts."""
        if not self._asm2d_has_fixed_recycle_target():
            return []
        actions: List[str] = []
        for ratio_var in self._free_recycle_ratio_vars():
            name = str(ratio_var).lower()
            lower = float(ratio_var.lb) if ratio_var.lb is not None else 0.0
            upper = float(ratio_var.ub) if ratio_var.ub is not None else lower + 1.0
            if 'mlr' in name:
                target = upper
            else:
                target = self._nominal_recycle_ratio_value(ratio_var)
            target = max(lower, min(upper, float(target)))
            try:
                ratio_var.set_value(target, skip_validation=True)
                ratio_var.fix(target)
            except Exception:
                continue
            actions.append(f'Fixed {ratio_var.name} to {target} for fixed-target recycle endpoint start')
        return actions

    def _preconverge_asm2d_recycle_hydraulics(self, *, seed_states: bool=True, force_biomass: bool=False) -> Dict[str, Any]:
        """Pre-close ASM2D hydraulic tear streams before the relaxed IPOPT retry."""
        units = self.get_units()
        actions = []
        closure = None
        initializer = self._initializer
        for method_name in ('_seed_srt_waste_split', '_seed_asm2d_recycle_ratio_split_guesses', '_seed_asm2d_forward_hydraulic_guesses', '_seed_asm2d_hydraulic_retention_times', '_seed_asm2d_reactor_biomass_guesses', '_seed_asm2d_substrate_consumption_guesses', '_seed_asm2d_oxygen_transfer_guesses'):
            method = getattr(initializer, method_name, None)
            if method is None:
                continue
            if method_name == '_seed_asm2d_forward_hydraulic_guesses':
                method(units, seed_states=seed_states)
            elif method_name == '_seed_asm2d_reactor_biomass_guesses':
                method(units, force=force_biomass)
            else:
                method(units)
            actions.append(method_name)
        self._condition_asm2d_before_solve()
        close_recycles = getattr(initializer, '_close_asm2d_recycles_successive_substitution', None)
        if close_recycles is not None:
            closure = close_recycles(units, tolerance=1e-05, max_iterations=30, seed_states=seed_states)
            actions.append('close_recycles_successive_substitution')
        post_closure_methods = ['_seed_asm2d_hydraulic_retention_times', '_seed_asm2d_reactor_biomass_guesses', '_seed_asm2d_substrate_consumption_guesses', '_seed_asm2d_oxygen_transfer_guesses']
        if seed_states:
            post_closure_methods.insert(2, '_seed_asm2d_forward_state_guesses')
        for method_name in post_closure_methods:
            method = getattr(initializer, method_name, None)
            if method is None:
                continue
            if method_name == '_seed_asm2d_reactor_biomass_guesses':
                method(units, force=force_biomass)
            else:
                method(units)
            actions.append(f'{method_name}_post_closure')
        self._condition_asm2d_before_solve()
        return {'actions': actions, 'closure': closure}

    def _is_recycle_related_failure(self, result: PipelineResult) -> bool:
        if not self._is_asm2d_biological_model():
            return False
        details = result.details if isinstance(result.details, dict) else {}
        if result.state == PipelineState.SOLVING:
            report = details.get('solver_infeasibility_report')
            return isinstance(report, dict)
        if result.state != PipelineState.POST_SOLVE_DIAGNOSTICS:
            return False
        residuals = details.get('constraint_residuals') or []
        if not isinstance(residuals, list):
            return False
        residual_text = ' '.join((str(item.get('name', '')) for item in residuals)).lower()
        max_residual = max((abs(float(item.get('residual') or 0.0)) for item in residuals), default=0.0)
        return max_residual > 0.1 and any((marker in residual_text for marker in ('cstr', 'postanoxic', 'material_balance', 'retention')))

    def run_asm2d_recycle_relaxed_solve(self, failure_result: PipelineResult) -> Optional[PipelineResult]:
        """Preconverge ASM2D recycle hydraulics and retry IPOPT with a warm start."""
        if not self._is_recycle_related_failure(failure_result):
            return None
        try:
            actions = self._reset_recycle_ratio_values_to_nominal()
            actions.extend(self._relax_recycle_ratio_bounds())
            preconverge = self._preconverge_asm2d_recycle_hydraulics()
            actions.extend((f'Ran {action}' for action in preconverge.get('actions', [])))
            closure = preconverge.get('closure')
            if closure is not None:
                actions.append(f'ASM2D recycle hydraulic preconvergence {('converged' if closure.get('converged') else 'stopped')} after {closure.get('iterations')} iteration(s)')
            options = {'linear_solver': default_ipopt_linear_solver(), 'tol': 1e-08, 'acceptable_tol': 1e-06, 'max_iter': 3000, 'mu_init': 1e-06, 'mu_strategy': 'adaptive', 'bound_push': 1e-08, 'bound_frac': 1e-08, 'max_cpu_time': self._config.max_wall_time_s}
            options.update(self._config.solver_options)
            options = self._with_asm2d_solver_defaults(options)
            options['tol'] = max(float(options.get('tol', 1e-06)), 1e-06)
            options['acceptable_tol'] = max(float(options.get('acceptable_tol', 1e-05)), 1e-05)
            continuation_result = self._run_fixed_recycle_ratio_continuation(options)
            if continuation_result is not None:
                actions.append('Ran fixed recycle-ratio continuation')
                solve_result = continuation_result
                continuation_details = continuation_result
                if not solve_result.get('success'):
                    preconverge = self._preconverge_asm2d_recycle_hydraulics()
                    actions.extend((f'Ran retry {action}' for action in preconverge.get('actions', [])))
                    closure = preconverge.get('closure') or closure
                    solve_result = self._solve_current_model(options)
            else:
                continuation_details = None
                solve_result = self._solve_current_model(options)
            if not solve_result.get('success'):
                endpoint_actions = self._fix_free_recycle_ratios_for_endpoint_retry()
                if endpoint_actions:
                    actions.extend(endpoint_actions)
                    preconverge = self._preconverge_asm2d_recycle_hydraulics(seed_states=True, force_biomass=True)
                    actions.extend((f'Ran endpoint retry {action}' for action in preconverge.get('actions', [])))
                    closure = preconverge.get('closure') or closure
                    solve_result = self._solve_current_model(options)
            return self._transition(PipelineState.RELAXED_SOLVE, bool(solve_result.get('success')), 'ASM2D recycle-ratio relaxed solve completed' if solve_result.get('success') else 'ASM2D recycle-ratio relaxed solve did not converge', details={'strategy': 'asm2d_recycle_hydraulic_preconvergence', 'actions_taken': actions, 'solve_result': solve_result, 'continuation_result': continuation_details, 'recycle_closure': closure, 'trigger_state': failure_result.state.value})
        except Exception as exc:
            return self._transition(PipelineState.RELAXED_SOLVE, False, f'ASM2D recycle-ratio relaxed solve failed: {exc}', errors=[str(exc)], details={'strategy': 'asm2d_recycle_hydraulic_preconvergence', 'actions_taken': locals().get('actions', []), 'trigger_state': failure_result.state.value})

    def _discover_units(self) -> Dict[str, Any]:
        """Discover units under m.fs (IDAES convention), fallback to m.

        Returns:
            Dict mapping unit_id to unit block
        """
        if self._model is None:
            return {}
        from pyomo.core.base.block import Block
        fs = getattr(self._model, 'fs', self._model)
        units = {}
        for name in dir(fs):
            if name.startswith('_'):
                continue
            obj = getattr(fs, name, None)
            if obj is None:
                continue
            if isinstance(obj, Block) and (hasattr(obj, 'inlet') or hasattr(obj, 'outlet') or hasattr(obj, 'initialize')):
                units[name] = obj
        return units

    def get_units(self) -> Dict[str, Any]:
        """Get units dict, discovering if not already set."""
        if self._units is None:
            self._units = self._discover_units()
        return self._units

    def _build_initialization_context(self) -> tuple[Dict[str, Any], Dict[str, Dict]]:
        """Build unit-method and state-args maps from the persisted session."""
        if self._session is None:
            return ({}, {})
        from watertap_engine_mcp.core.unit_registry import InitMethod as RegistryInitMethod
        from watertap_engine_mcp.core.unit_registry import UnitCategory
        from watertap_engine_mcp.core.unit_registry import get_unit_spec
        from watertap_engine_mcp.solver.initializer import InitMethod as PipelineInitMethod
        method_map = {RegistryInitMethod.INITIALIZE: PipelineInitMethod.INITIALIZE, RegistryInitMethod.INITIALIZE_BUILD: PipelineInitMethod.INITIALIZE_BUILD, RegistryInitMethod.INITIALIZER_CLASS: PipelineInitMethod.CUSTOM, RegistryInitMethod.NONE: PipelineInitMethod.NONE}
        unit_methods: Dict[str, Any] = {}
        state_args: Dict[str, Dict] = {}
        explicit_skip_categories = {UnitCategory.FEED, UnitCategory.FEED_ZO, UnitCategory.PRODUCT}
        for unit_id, unit_inst in self._session.units.items():
            spec = get_unit_spec(unit_inst.unit_type)
            if spec.init_hints is not None:
                no_external_dof_profile = bool(spec.profiles) and all((profile.required_fixes == [] for profile in spec.profiles))
                if spec.init_hints.method == RegistryInitMethod.NONE and spec.category not in explicit_skip_categories and (not no_external_dof_profile):
                    continue
                unit_methods[unit_id] = method_map.get(spec.init_hints.method, PipelineInitMethod.INITIALIZE)
            if unit_id in self._session.feed_states:
                state = (self._session.feed_states.get(unit_id) or {}).get('state_args')
                if state:
                    state_args[unit_id] = state
        return (unit_methods, state_args)

    @property
    def state(self) -> PipelineState:
        """Get current pipeline state."""
        return self._state

    @property
    def history(self) -> List[PipelineResult]:
        """Get pipeline execution history."""
        return self._history

    def _initializer_tactics(self) -> List[Dict[str, Any]]:
        metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {}) if self._session is not None else {}
        tactics = metadata.get('initializer_tactics') if isinstance(metadata, dict) else None
        if not isinstance(tactics, list):
            return []
        return [dict(tactic) for tactic in tactics if isinstance(tactic, dict)]

    def _transition(self, new_state: PipelineState, success: bool, message: str, **kwargs) -> PipelineResult:
        """Transition to new state and record result."""
        result = PipelineResult(success=success, state=new_state, message=message, **kwargs)
        self._history.append(result)
        self._state = new_state
        return result

    def run_dof_check(self) -> PipelineResult:
        """Run DOF check stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        endpoint_actions: List[str] = []
        if self._config.allow_near_feasible_asm2d:
            endpoint_actions = self._fix_free_recycle_ratios_for_endpoint_retry()
        fs = getattr(self._model, 'fs', self._model)
        analysis = self._dof_resolver.analyze_flowsheet(fs)
        if analysis.overall_status == DOFStatus.UNDERSPECIFIED:
            if self._can_optimize_bounded_objective_variables(analysis.total_dof):
                free_recycle_ratio_vars = self._free_recycle_ratio_vars()
                free_alkalinity_min_dose_vars = self._free_alkalinity_min_dose_vars()
                return self._transition(PipelineState.DOF_CHECK, True, 'DOF check accepted: positive DOF is limited to bounded objective variables', details={'total_dof': analysis.total_dof, 'free_recycle_ratio_vars': [var.name for var in free_recycle_ratio_vars], 'free_alkalinity_min_dose_vars': [var.name for var in free_alkalinity_min_dose_vars], 'fixed_target_recycle_endpoint_actions': endpoint_actions}, warnings=['Positive DOF is limited to bounded variables with an active regularization/min-dose objective'])
            return self._transition(PipelineState.DOF_CHECK, False, f'Model is underspecified: {analysis.total_dof} DOF remaining', details={'total_dof': analysis.total_dof, 'unit_dof': {u: d.dof for u, d in analysis.unit_analyses.items()}, 'suggestions': [{'unit': s.unit_id, 'var': s.var_name, 'value': s.suggested_value} for s in analysis.suggestions]})
        if analysis.overall_status == DOFStatus.OVERSPECIFIED:
            if not self._config.allow_overspecified:
                return self._transition(PipelineState.DOF_CHECK, False, f'Model is overspecified: {analysis.total_dof} DOF (should be 0)', details={'total_dof': analysis.total_dof})
        return self._transition(PipelineState.DOF_CHECK, True, f'DOF check passed: {analysis.total_dof} DOF', details={'total_dof': analysis.total_dof, 'fixed_target_recycle_endpoint_actions': endpoint_actions})

    def run_scaling(self) -> PipelineResult:
        """Run scaling stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        try:
            units = self.get_units()
            scaling_applications: List[Dict[str, Any]] = []
            helper_warnings: List[str] = []
            if self._config.auto_scale:
                metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {}) if self._session is not None else {}
                universal_metadata = metadata.get('universal_solver', {})
                scaling_classes = universal_metadata.get('scaling_classes')
                if scaling_classes is None:
                    topology_name = universal_metadata.get('topology_template') or metadata.get('topology_name') or metadata.get('template_name')
                    if topology_name:
                        try:
                            from puran_mcp_support.engineering.heuristics.flowsheet_templates.registry import get_topology_template
                            scaling_classes = list(get_topology_template(topology_name).scaling_classes)
                        except Exception:
                            scaling_classes = None
                scaling_ok = self._scaler.calculate_scaling_factors(self._model, units=units, scaling_classes=scaling_classes)
                scaling_applications = self._scaler.get_last_scaling_applications()
                if self._is_adm1_recycle_family():
                    include_gas_transfer_scaling = self._is_adm1_retained_solids_recycle_family()
                    adm1_recycle_scaling = self._scaler.apply_adm1_material_balance_constraint_scaling(self._model, include_gas_transfer_scaling=include_gas_transfer_scaling)
                    scaling_applications.append(asdict(adm1_recycle_scaling))
                if self._is_asm2d_recycle_topology():
                    asm2d_multistage_scaling = self._scaler.apply_asm2d_recycle_pressure_changer_scaling(self._model)
                    scaling_applications.append(asdict(asm2d_multistage_scaling))
                helper_warnings = [f'{application.get('stage')}: {error}' for application in scaling_applications if not application.get('success', True) for error in application.get('errors', [])]
                if not scaling_ok:
                    return self._transition(PipelineState.SCALING, False, 'Scaling failed', errors=[error for application in scaling_applications for error in application.get('errors', [])], details={'scaling_applications': scaling_applications})
            if self._config.report_scaling_issues:
                report = self._scaler.get_scaling_report(self._model)
                if report.unscaled_vars > 0 or report.unscaled_constraints > 0:
                    return self._transition(PipelineState.SCALING, True, 'Scaling applied with warnings', warnings=[f'Unscaled variables: {report.unscaled_vars}', f'Unscaled constraints: {report.unscaled_constraints}'] + helper_warnings, details={'scaling_applications': scaling_applications, 'issues': [{'type': i.issue_type.value, 'name': i.component_name} for i in report.issues[:10]]})
            if helper_warnings:
                return self._transition(PipelineState.SCALING, True, 'Scaling applied with upstream helper warnings', warnings=helper_warnings, details={'scaling_applications': scaling_applications})
            return self._transition(PipelineState.SCALING, True, 'Scaling applied successfully', details={'scaling_applications': scaling_applications})
        except Exception as e:
            return self._transition(PipelineState.SCALING, False, f'Scaling failed: {e}', errors=[str(e)])

    def run_initialization(self) -> PipelineResult:
        """Run initialization stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        try:
            if not hasattr(self, '_adm1_activity_cold_start') and self._adm1_activity_reactor() is not None:
                from pyomo.environ import Var, value
                self._adm1_activity_cold_start = [(v, value(v)) for v in self._model.component_data_objects(Var) if not v.fixed and value(v, exception=False) is not None]
            units = self.get_units()
            interval_application = None
            initialization_warnings: List[str] = []
            skip_interval = self._skip_interval_initializer_for_current_model()
            if self._config.interval_initialize and (not skip_interval):
                interval_application = self._scaler.run_interval_initializer(self._model)
                if not interval_application.success:
                    initialization_warnings.extend(interval_application.errors)
            elif skip_interval:
                initialization_warnings.append('Skipped interval initializer for ModifiedASM2d combined MBR recycle rating path')
            connections = []
            if self._session is not None:
                for conn in self._session.connections:
                    connections.append({'source_unit': conn.source_unit, 'src_unit': conn.source_unit, 'source_port': conn.source_port, 'dest_unit': conn.dest_unit, 'dest_port': conn.dest_port, 'translator_id': conn.translator_id, 'tear_stream': bool(getattr(conn, 'tear_stream', False)), 'tear_rationale': getattr(conn, 'tear_rationale', None)})
            else:
                fs = getattr(self._model, 'fs', self._model)
                from pyomo.network import Arc as PyomoArc
                for arc in fs.component_objects(PyomoArc, active=None, descend_into=False):
                    src_port = arc.source
                    dst_port = arc.destination
                    if src_port and dst_port:
                        src_unit = str(src_port.parent_block().name).rsplit('.', 1)[-1]
                        dst_unit = str(dst_port.parent_block().name).rsplit('.', 1)[-1]
                        connections.append({'source_unit': src_unit, 'src_unit': src_unit, 'source_port': str(src_port.local_name), 'dest_unit': dst_unit, 'dest_port': str(dst_port.local_name)})
            unit_methods, state_args = self._build_initialization_context()
            if self._session is not None and unit_methods and (len(unit_methods) == len(self._session.units)) and all((getattr(method, 'value', None) == 'none' for method in unit_methods.values())) and (not connections):
                return self._transition(PipelineState.INITIALIZATION, True, 'Initialization skipped: no units require an initialization routine or state propagation', details={'units_initialized': [], 'warnings': initialization_warnings, 'skipped': True, 'initializer_tactics': self._initializer_tactics(), 'interval_initializer': asdict(interval_application) if interval_application is not None else None}, warnings=initialization_warnings)
            result = self._initializer.initialize_flowsheet(self._model, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=self._config.tear_streams)
            initializer_tactics = self._initializer_tactics()
            if result.success:
                return self._transition(PipelineState.INITIALIZATION, True, 'Initialization completed', details={'units_initialized': result.units_initialized, 'warnings': result.warnings, 'recipe_name': result.recipe_name, 'topology': result.topology, 'tear_streams': result.topology.get('tear_streams', []), 'initializer_tactics': initializer_tactics, 'interval_initializer': asdict(interval_application) if interval_application is not None else None}, warnings=initialization_warnings + result.warnings)
            else:
                return self._transition(PipelineState.INITIALIZATION, False, f'Initialization failed: {result.message}', errors=result.errors, details={'failed_unit': result.failed_unit, 'recipe_name': result.recipe_name, 'topology': result.topology, 'tear_streams': result.topology.get('tear_streams', []), 'initializer_tactics': initializer_tactics, 'interval_initializer': asdict(interval_application) if interval_application is not None else None}, warnings=initialization_warnings)
        except Exception as e:
            return self._transition(PipelineState.INITIALIZATION, False, f'Initialization error: {e}', errors=[str(e)])

    def _skip_interval_initializer_for_current_model(self) -> bool:
        """Return True for model families where interval init degrades startup."""
        if self._session is None:
            return False
        metadata = self._session.config.runtime_metadata or {}
        design_parameters = metadata.get('design_parameters')
        if not isinstance(design_parameters, dict):
            return False
        template_name = metadata.get('template_name') or metadata.get('topology_name')
        return str(template_name) in ('asm2d_mbr', 'asm2d_mbr_aerobic') and design_parameters.get('asm2d_model_variant') == 'MODIFIED_ASM2D'

    def run_pre_solve_diagnostics(self) -> PipelineResult:
        """Run pre-solve diagnostics.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        try:
            pressure_floor = self._apply_adm1_vapor_pressure_floor()
            structural = self._diagnostics.run_structural_diagnostics(self._model)
            numerical = self._diagnostics.run_numerical_diagnostics(self._model)
            issues = []
            if structural.issues_found > 0:
                issues.extend(structural.details)
            if numerical.issues_found > 0:
                issues.extend(numerical.details)
            if issues:
                return self._transition(PipelineState.PRE_SOLVE_DIAGNOSTICS, True, f'Pre-solve diagnostics found {len(issues)} potential issues', warnings=issues[:10], details={'structural_issues': structural.issues_found, 'numerical_issues': numerical.issues_found, 'adm1_vapor_pressure_floor': pressure_floor})
            return self._transition(PipelineState.PRE_SOLVE_DIAGNOSTICS, True, 'Pre-solve diagnostics passed', details={'adm1_vapor_pressure_floor': pressure_floor})
        except Exception as e:
            return self._transition(PipelineState.PRE_SOLVE_DIAGNOSTICS, False, f'Diagnostics error: {e}', errors=[str(e)])

    def _asm2d_progressive_constraint_groups(self) -> Dict[str, List[Any]]:
        """Return ASM2D biological constraints grouped for tightening."""
        if not self._is_asm2d_biological_model():
            return {}
        from pyomo.environ import Constraint
        groups: Dict[str, List[Any]] = {'particulate_soluble_mass_balance_closure': [], 'biological_growth_decay_stoichiometry': [], 'autotroph_nitrification_kinetics': [], 'phosphate_accumulator_sequestration': []}
        for constraint in self._model.component_data_objects(Constraint, active=True, descend_into=True):
            name = str(constraint).lower()
            if 'material_balance' in name or 'material_balances' in name:
                groups['particulate_soluble_mass_balance_closure'].append(constraint)
            elif 'rate_reaction_stoichiometry' in name or 'rate_reaction_extent' in name:
                if 'x_aut' in name or 's_nh4' in name or 's_no3' in name:
                    groups['autotroph_nitrification_kinetics'].append(constraint)
                elif any((token in name for token in ('x_pao', 'x_pp', 'x_pha', 's_po4'))):
                    groups['phosphate_accumulator_sequestration'].append(constraint)
                else:
                    groups['biological_growth_decay_stoichiometry'].append(constraint)
        return groups

    def _short_warm_solve(self, mu_init: float, max_cpu_time: int=15) -> str:
        """Run a short IPOPT warm-start solve and return termination condition."""
        from watertap.core.solvers import get_solver
        solver = get_solver()
        options = {'linear_solver': default_ipopt_linear_solver(), 'max_iter': 80, 'max_cpu_time': max_cpu_time, 'mu_init': mu_init, 'mu_strategy': 'adaptive', 'bound_push': 1e-08, 'bound_frac': 1e-08}
        options.update(self._config.solver_options)
        options = self._with_asm2d_solver_defaults(options)
        for key, value in options.items():
            solver.options[key] = value
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            with open(os.devnull, 'w') as devnull:
                sys.stdout = devnull
                sys.stderr = devnull
                results = solver.solve(self._model, tee=False, load_solutions=False)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        termination = str(results.solver.termination_condition)
        if termination == 'optimal':
            self._model.solutions.load_from(results)
        return termination

    def run_progressive_tightening(self) -> PipelineResult:
        """Try ASM2D transport-to-reaction progressive tightening."""
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        if not self._config.enable_progressive_tightening:
            return self._transition(PipelineState.PROGRESSIVE_TIGHTENING, True, 'Progressive tightening disabled')
        if not self._is_asm2d_biological_model():
            return self._transition(PipelineState.PROGRESSIVE_TIGHTENING, True, 'Progressive tightening not applicable to this flowsheet')
        try:
            from idaes.core.util.model_statistics import degrees_of_freedom
            groups = self._asm2d_progressive_constraint_groups()
            ordered_names = ['particulate_soluble_mass_balance_closure', 'biological_growth_decay_stoichiometry', 'autotroph_nitrification_kinetics', 'phosphate_accumulator_sequestration']
            grouped_constraints = [constraint for group_name in ordered_names for constraint in groups.get(group_name, [])]
            if not grouped_constraints:
                return self._transition(PipelineState.PROGRESSIVE_TIGHTENING, True, 'No ASM2D biological constraints found for progressive tightening')
            snapshot = [(constraint, bool(constraint.active)) for constraint in grouped_constraints]
            for constraint in grouped_constraints:
                constraint.deactivate()
            try:
                relaxed_dof = degrees_of_freedom(self._model)
                if relaxed_dof != 0:
                    return self._transition(PipelineState.PROGRESSIVE_TIGHTENING, True, 'Progressive tightening skipped: relaxed model is not square', warnings=[f'Transport-only relaxation produced DOF={relaxed_dof}; final hard solve will use the full constraint set'], details={'relaxed_dof': relaxed_dof, 'constraint_groups': {group_name: len(groups.get(group_name, [])) for group_name in ordered_names}})
                terminations: List[Dict[str, Any]] = []
                terminations.append({'stage': 'transport_only', 'termination_condition': self._short_warm_solve(mu_init=0.001, max_cpu_time=15)})
                for index, group_name in enumerate(ordered_names, start=1):
                    for constraint in groups.get(group_name, []):
                        constraint.activate()
                    terminations.append({'stage': group_name, 'termination_condition': self._short_warm_solve(mu_init=max(0.0001 / index, 1e-06), max_cpu_time=20), 'constraints': len(groups.get(group_name, []))})
                return self._transition(PipelineState.PROGRESSIVE_TIGHTENING, True, 'Progressive tightening completed', details={'terminations': terminations})
            finally:
                for constraint, active in snapshot:
                    if active:
                        constraint.activate()
                    else:
                        constraint.deactivate()
        except Exception as e:
            return self._transition(PipelineState.PROGRESSIVE_TIGHTENING, True, 'Progressive tightening skipped after error', warnings=[str(e)])

    def run_solve(self) -> PipelineResult:
        """Run solve stage.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        try:
            self._apply_adm1_vapor_pressure_floor()
            self._condition_asm2d_before_solve()
            square_objective_actions = self._deactivate_recycle_ratio_objective_if_square()
            options = self._base_solver_options()
            options.update(self._config.solver_options)
            options = self._with_asm2d_solver_defaults(options)
            options = self._with_adm1_solver_defaults(options)
            asm2d_multistage_preconvergence = None
            asm2d_fixed_recycle_continuation = None
            if self._is_asm2d_multistage_recycle_topology():
                asm2d_multistage_preconvergence = self._preconverge_asm2d_recycle_hydraulics(seed_states=True, force_biomass=True)
                asm2d_fixed_recycle_continuation = self._run_fixed_recycle_ratio_continuation(options)
            adm1_continuation = self._run_adm1_egsb_recirculation_continuation(options)
            asm2d_carbon_no3_warm_start = self._run_asm2d_carbon_no3_forward_warm_start(options)
            asm2d_square_prestage = self._run_asm2d_free_recycle_square_prestage(options)
            solver = self._configured_solver(options)
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            try:
                with open(os.devnull, 'w') as devnull:
                    sys.stdout = devnull
                    sys.stderr = devnull
                    results = solver.solve(self._model, tee=False, load_solutions=False)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            status = str(results.solver.status)
            termination = str(results.solver.termination_condition)
            adm1_cascade_recovery = None
            adm1_activity_recovery = None
            loaded_nonoptimal = False
            if termination != 'optimal':
                loaded_nonoptimal = self._load_nonoptimal_solution_if_finite(results)
                if loaded_nonoptimal:
                    recovered, adm1_cascade_recovery = self._recover_adm1_cascade_bound_stall(options)
                    if recovered is None:
                        recovered, adm1_activity_recovery = self._recover_adm1_activity_bound_stall(options)
                    if recovered is not None:
                        results = recovered
                        status = str(results.solver.status)
                        termination = str(results.solver.termination_condition)
            if termination == 'optimal':
                self._model.solutions.load_from(results)
                adm1_plain_polish = self._polish_plain_adm1_solution(options)
                adm1_guild_invasion = adm1_cascade_recovery or self._select_adm1_viable_branch(options)
                return self._transition(PipelineState.SOLVING, True, 'Solve completed successfully', details={'solver_status': status, 'solver': self._config.solver, 'termination_condition': termination, 'solve_time': getattr(results.solver, 'wallclock_time', None), 'iterations': getattr(results.solver, 'iterations', None), 'solver_options': options, 'objective_actions': square_objective_actions, 'loaded_nonoptimal_solution': False, 'asm2d_multistage_preconvergence': asm2d_multistage_preconvergence, 'asm2d_fixed_recycle_continuation': asm2d_fixed_recycle_continuation, 'adm1_recirculation_continuation': adm1_continuation, 'adm1_plain_ipopt_polish': adm1_plain_polish, 'adm1_guild_invasion': adm1_guild_invasion, 'adm1_cascade_recovery': adm1_cascade_recovery, 'adm1_activity_recovery': adm1_activity_recovery, 'asm2d_carbon_no3_forward_warm_start': asm2d_carbon_no3_warm_start, 'asm2d_free_recycle_square_prestage': asm2d_square_prestage})
            near_feasible = self._near_feasible_asm2d_details(termination) if self._config.allow_near_feasible_asm2d else None
            if near_feasible is not None:
                near_feasible['solver'] = self._config.solver
                near_feasible['loaded_nonoptimal_solution'] = loaded_nonoptimal
                near_feasible['asm2d_multistage_preconvergence'] = asm2d_multistage_preconvergence
                near_feasible['asm2d_fixed_recycle_continuation'] = asm2d_fixed_recycle_continuation
                near_feasible['asm2d_free_recycle_square_prestage'] = asm2d_square_prestage
                return self._transition(PipelineState.SOLVING, True, 'Solve accepted: ASM2D equations satisfy near-feasible residual tolerance', details=near_feasible, warnings=[f'{self._config.solver} termination was {termination}; accepted because residuals are below proposal tolerance'])
            infeasibility_report = build_solver_infeasibility_report(self._model, {'solver_status': status, 'solver': self._config.solver, 'termination_condition': termination, 'message': f'Solve failed: {termination}'})
            ph_guard = self._adm1_ph_target_healthy_basin_guard()
            details = {'solver_status': status, 'termination_condition': termination, 'convergence_status': 'solver_infeasible_with_diagnostic', 'solver_options': options, 'objective_actions': square_objective_actions, 'loaded_nonoptimal_solution': loaded_nonoptimal, 'asm2d_multistage_preconvergence': asm2d_multistage_preconvergence, 'asm2d_fixed_recycle_continuation': asm2d_fixed_recycle_continuation, 'adm1_recirculation_continuation': adm1_continuation, 'adm1_cascade_recovery': adm1_cascade_recovery, 'adm1_activity_recovery': adm1_activity_recovery, 'asm2d_carbon_no3_forward_warm_start': asm2d_carbon_no3_warm_start, 'asm2d_free_recycle_square_prestage': asm2d_square_prestage, 'solver_infeasibility_report': infeasibility_report.model_dump(mode='python')}
            warnings = []
            if ph_guard is not None:
                details['convergence_status'] = 'adm1_ph_target_outside_healthy_basin'
                details['adm1_ph_target_guard'] = ph_guard
                warnings.append(ph_guard['message'])
            return self._transition(PipelineState.SOLVING, False, f'Solve failed: {termination}', details=details, warnings=warnings)
        except ImportError:
            return self._transition(PipelineState.SOLVING, False, 'WaterTAP not available', errors=['Could not import watertap.core.solvers'])
        except Exception as e:
            near_feasible = self._near_feasible_asm2d_details('solver_error') if self._config.allow_near_feasible_asm2d else None
            if near_feasible is not None:
                near_feasible['solver_error'] = str(e)
                return self._transition(PipelineState.SOLVING, True, 'Solve accepted: ASM2D equations satisfy near-feasible residual tolerance', details=near_feasible, warnings=[f"IPOPT raised '{e}'; accepted because residuals are below proposal tolerance"])
            infeasibility_report = build_solver_infeasibility_report(self._model, {'solver_status': 'error', 'termination_condition': 'solver_error', 'message': f'Solve error: {e}'})
            ph_guard = self._adm1_ph_target_healthy_basin_guard()
            details = {'solver_status': 'error', 'termination_condition': 'solver_error', 'convergence_status': 'solver_infeasible_with_diagnostic', 'solver_infeasibility_report': infeasibility_report.model_dump(mode='python')}
            if ph_guard is not None:
                details['convergence_status'] = 'adm1_ph_target_outside_healthy_basin'
                details['adm1_ph_target_guard'] = ph_guard
            return self._transition(PipelineState.SOLVING, False, f'Solve error: {e}', details=details, errors=[str(e)])

    def _build_completed_details(self, solve_result: Dict[str, Any], post_solve_result: PipelineResult, *, recovered: bool, initial_solve_result: Optional[Dict[str, Any]]=None, recovery: Optional[Dict[str, Any]]=None, optimization_result: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
        """Build the canonical completed-pipeline payload."""
        details = {'stages_completed': len(self._history), 'solve_result': solve_result, 'recovered': recovered, 'post_solve_diagnostics': post_solve_result.details}
        if initial_solve_result is not None:
            details['initial_solve_result'] = initial_solve_result
        if recovery is not None:
            details['recovery'] = recovery
        if optimization_result is not None:
            details['optimization'] = optimization_result
        if self._digester_state is not None:
            details['digester_state'] = self._digester_state
        return details

    def _complete_after_success(self, *, solve_result: Dict[str, Any], post_solve_result: PipelineResult, recovered: bool, message: str, on_stage_complete: Optional[Callable[[PipelineResult], None]]=None, initial_solve_result: Optional[Dict[str, Any]]=None, recovery: Optional[Dict[str, Any]]=None) -> PipelineResult:
        """Run the optional optimization tail, then emit the completed result."""
        optimization_result = None
        if self._optimization_requested():
            optimization_result = self.run_optimization_solve()
            if on_stage_complete:
                on_stage_complete(optimization_result)
            if not optimization_result.success:
                failure_details = {'failed_stage': 'Optimization', 'stage_result': optimization_result.details}
                if optimization_result.details.get('solver_infeasibility_report'):
                    failure_details['convergence_status'] = 'solver_infeasible_with_diagnostic'
                    failure_details['solver_infeasibility_report'] = optimization_result.details['solver_infeasibility_report']
                return self._transition(PipelineState.FAILED, False, f'Pipeline failed at Optimization: {optimization_result.message}', details=failure_details, errors=optimization_result.errors)
        return self._transition(PipelineState.COMPLETED, True, message, details=self._build_completed_details(solve_result=solve_result, post_solve_result=post_solve_result, recovered=recovered, initial_solve_result=initial_solve_result, recovery=recovery, optimization_result=optimization_result.details if optimization_result is not None else None))

    def run_post_solve_diagnostics(self) -> PipelineResult:
        """Run post-solve diagnostics.

        Returns:
            PipelineResult
        """
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        residual_threshold = 1e-06
        if self._accepted_near_feasible_solve:
            runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
            try:
                residual_threshold = float((runtime_metadata or {}).get('asm2d_near_feasible_residual_tolerance', 0.001))
            except (TypeError, ValueError):
                residual_threshold = 0.001
        elif self._use_plain_ipopt_for_asm2d_carbon_no3():
            residual_threshold = 0.001
        bound_tolerance = 1e-07 if self._accepted_near_feasible_solve else 1e-08
        residuals = self._diagnostics.get_constraint_residuals(self._model, threshold=residual_threshold)
        violations = self._diagnostics.get_bound_violations(self._model, tolerance=bound_tolerance)
        gating_residuals = list(residuals.details)
        trace_exemptions: List[Dict[str, Any]] = []
        if gating_residuals:
            kept = []
            for r in gating_residuals:
                exemption = self._trace_scale_row_exemption(r.constraint_name, r.residual)
                if exemption is not None:
                    trace_exemptions.append(exemption)
                else:
                    kept.append(r)
            gating_residuals = kept
        residual_issue_count = len(gating_residuals)
        self._digester_state = self._classify_digester_basin()
        digester_warnings: List[str] = []
        if isinstance(self._digester_state, dict):
            digester_warnings = list(self._digester_state.get('warnings', []))
        if residual_issue_count > 0 or violations.issues_found > 0:
            details = {'constraint_residual_tolerance': residual_threshold, 'constraint_residuals': [{'name': r.constraint_name, 'residual': r.residual} for r in gating_residuals[:10]], 'bound_violations': [{'name': v.variable_name, 'value': v.value, 'type': v.violation_type} for v in violations.details[:10]]}
            if trace_exemptions:
                details['trace_scale_residual_exemptions'] = trace_exemptions
            if self._digester_state is not None:
                details['digester_state'] = self._digester_state
            return self._transition(PipelineState.POST_SOLVE_DIAGNOSTICS, False, f'Post-solve issues: {residual_issue_count} residuals, {violations.issues_found} violations', details=details, warnings=digester_warnings)
        pass_details = {}
        pass_details['constraint_residual_tolerance'] = residual_threshold
        if trace_exemptions:
            pass_details['trace_scale_residual_exemptions'] = trace_exemptions
        if self._digester_state is not None:
            pass_details['digester_state'] = self._digester_state
        return self._transition(PipelineState.POST_SOLVE_DIAGNOSTICS, True, 'Post-solve diagnostics passed', details=pass_details, warnings=digester_warnings)

    def _universal_optimization_metadata(self) -> Dict[str, Any]:
        runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        if not isinstance(runtime_metadata, dict):
            return {}
        universal = runtime_metadata.get('universal_solver')
        return universal if isinstance(universal, dict) else {}

    def _requested_objective_spec(self) -> str:
        objective_spec = self._universal_optimization_metadata().get('objective_spec', 'feasibility')
        return str(objective_spec or 'feasibility').strip().lower()

    def _optimization_requested(self) -> bool:
        return self._requested_objective_spec() != 'feasibility'

    def _snapshot_model_variables(self) -> List[tuple[Any, Any, bool, Any, Any]]:
        from pyomo.environ import Var, value
        snapshot = []
        for var in self._model.component_data_objects(Var, descend_into=True):
            snapshot.append((var, value(var, exception=False), bool(getattr(var, 'fixed', False)), getattr(var, 'lb', None), getattr(var, 'ub', None)))
        return snapshot

    def _restore_model_variables(self, snapshot: List[tuple[Any, Any, bool, Any, Any]]) -> None:
        for var, value, fixed, lower, upper in snapshot:
            try:
                var.setlb(lower)
                var.setub(upper)
                if value is not None:
                    var.set_value(value, skip_validation=True)
                if fixed:
                    var.fix()
                elif getattr(var, 'fixed', False):
                    var.unfix()
            except Exception:
                continue

    def _target_was_supplied(self, design_targets: Dict[str, Any], *keys: str) -> bool:
        if not isinstance(design_targets, dict):
            return False
        normalized = {str(key).strip().lower().replace('-', '_'): value for key, value in design_targets.items() if value is not None}
        nested = design_targets.get('reactor_volumes_m3')
        if isinstance(nested, dict):
            for key, value in nested.items():
                if value is not None:
                    normalized[str(key).strip().lower().replace('-', '_')] = value
        return any((key.strip().lower().replace('-', '_') in normalized for key in keys))

    def _indexed_var(self, component: Any, *indexes: Any) -> Any:
        if component is None:
            return None
        if not hasattr(component, 'is_indexed') or not component.is_indexed():
            return component
        for index in indexes:
            try:
                return component[index]
            except Exception:
                continue
        return None

    def _unfix_optimization_design_variables(self) -> Dict[str, Any]:
        """Optimize operating controls; rating never releases stage-(b) geometry."""
        from pyomo.environ import value
        units = self.get_units()
        metadata = self._universal_optimization_metadata()
        design_targets = metadata.get('design_targets')
        if not isinstance(design_targets, dict):
            design_targets = {}
        released: Dict[str, Dict[str, Any]] = {}
        skipped: Dict[str, str] = {}
        runtime = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        inverse_design = runtime.get('analysis_mode') == 'inverse_design'
        volume_targets = {R.PRE_ANOXIC: ('anoxic_volume_m3', 'anoxic_volume', 'anoxic'), R.AEROBIC: ('aerobic_volume_m3', 'aerobic_volume', 'aerobic'), R.POST_ANOXIC: ('post_anoxic_volume_m3', 'post_anoxic_volume', 'postanoxic', 'post_anoxic')}
        for role, target_keys in volume_targets.items():
            for unit_id in self._roles.all_for(role, units):
                unit = units.get(unit_id)
                if unit is None:
                    continue
                if not inverse_design:
                    skipped[f'{unit_id}.volume[0]'] = 'stage_b_rating_geometry'
                    continue
                var = self._indexed_var(getattr(unit, 'volume', None), 0, 0.0)
                current = value(var, exception=False) if var is not None else None
                if var is None or current is None or current <= 0:
                    skipped[f'{unit_id}.volume[0]'] = 'not_available'
                    continue
                lower = max(0.5 * float(current), 1.0)
                upper = max(3.0 * float(current), lower + 1.0)
                var.setlb(lower)
                var.setub(upper)
                var.unfix()
                released[f'{unit_id}.volume[0]'] = {'initial_value': float(current), 'lower_bound': lower, 'upper_bound': upper, 'units': 'm3'}
        kla_tender_locked = self._target_was_supplied(design_targets, 'aerobic_kla_per_hr', 'kla_per_hr', 'kla')
        for unit_id in self._roles.all_for(R.AEROBIC, units):
            unit = units.get(unit_id)
            if unit is None:
                continue
            kla = self._indexed_var(getattr(unit, 'KLa', None), None)
            key = f'{unit_id}.KLa'
            if kla_tender_locked:
                skipped[key] = 'tender_prescribed'
                continue
            if kla is None:
                skipped[key] = 'not_available'
                continue
            current = value(kla, exception=False)
            if current is None:
                skipped[key] = 'not_available'
                continue
            kla.setlb(1.0)
            kla.setub(20.0)
            kla.unfix()
            released[key] = {'initial_value': float(current), 'lower_bound': 1.0, 'upper_bound': 20.0, 'units': '1/hr'}
        if self._target_was_supplied(design_targets, 'target_srt_days', 'srt_days', 'srt_d', 'srt'):
            skipped['target_srt_days'] = 'tender_prescribed'
        else:
            skipped['target_srt_days'] = 'not_a_pyomo_var_in_current_template; WAS split remains the template feasibility control'
        recycle_vars = {}
        for ratio_var in self._free_recycle_ratio_vars():
            recycle_vars[ratio_var.name] = {'initial_value': value(ratio_var, exception=False), 'lower_bound': getattr(ratio_var, 'lb', None), 'upper_bound': getattr(ratio_var, 'ub', None), 'fixed': bool(getattr(ratio_var, 'fixed', False))}
        guarantees = design_targets.get('effluent_guarantees')
        mbr_unit = self._roles.single_unit(R.MBR_SEPARATOR, units)
        if isinstance(guarantees, dict) and mbr_unit is not None:
            guarantee_components = set()
            if any((key in guarantees for key in ('COD_mg_L', 'BOD5_mg_L'))):
                guarantee_components.update({'S_F', 'S_A', 'S_I', 'X_S', 'X_I', 'X_H', 'X_AUT'})
            if any((key in guarantees for key in ('TN_mg_L', 'TKN_mg_L'))):
                guarantee_components.update({'S_NH4', 'S_NO3', 'X_S', 'X_I', 'X_H', 'X_PAO', 'X_AUT'})
            if 'TP_mg_L' in guarantees:
                guarantee_components.update({'S_PO4', 'X_S', 'X_I', 'X_H', 'X_PAO', 'X_AUT', 'X_PP', 'X_MeP'})
            if 'TSS_mg_L' in guarantees:
                guarantee_components.update({'X_TSS', 'X_S', 'X_I', 'X_H', 'X_PAO', 'X_AUT', 'X_PP', 'X_PHA', 'X_MeOH', 'X_MeP'})
            split = getattr(mbr_unit, 'split_fraction', None)
            for component in sorted(guarantee_components):
                var = None
                if split is not None:
                    for index in ((0, 'permeate', component), (0.0, 'permeate', component)):
                        try:
                            var = split[index]
                            break
                        except Exception:
                            continue
                current = value(var, exception=False) if var is not None else None
                if var is None or current is None:
                    skipped[f'MBR.split_fraction[permeate,{component}]'] = 'not_available'
                    continue
                lower = 1e-05 if component.startswith('S_') else 1e-06
                upper = max(float(current), lower)
                var.setlb(lower)
                var.setub(upper)
                var.set_value(lower, skip_validation=True)
                var.unfix()
                released[f'MBR.split_fraction[permeate,{component}]'] = {'initial_value': float(current), 'lower_bound': lower, 'upper_bound': upper, 'units': '-', 'reason': 'effluent_guarantee_polishing'}
        return {'released': released, 'skipped': skipped, 'free_recycle_ratios': recycle_vars}

    def _enable_optimization_costing(self) -> Dict[str, Any]:
        """Enable WaterTAP costing on units that have registered cost methods."""
        if self._builder is None:
            raise RuntimeError('Optimization costing requires the active ModelBuilder')
        if self._session is None:
            raise RuntimeError('Optimization costing requires a flowsheet session')
        from watertap_engine_mcp.core.unit_registry import UNITS, unit_supports_costing_package
        costing_config = dict(getattr(self._session, 'costing_config', None) or {})
        costing_config.setdefault('package', 'watertap')
        costing_config.setdefault('electricity_cost', 0.08)
        costing_config.setdefault('utilization_factor', 0.9)
        costing_config.setdefault('plant_lifetime', 30)
        costing_config['enabled'] = True
        self._session.costing_config = costing_config
        requested_units = []
        skipped_units = []
        for unit_id, unit_inst in self._session.units.items():
            if unit_inst.unit_type in {'Feed', 'FeedZO', 'Product'}:
                unit_inst.costing_enabled = False
                continue
            spec = UNITS.get(unit_inst.unit_type)
            if spec is not None and unit_supports_costing_package(spec, 'watertap'):
                unit_inst.costing_enabled = True
                requested_units.append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type})
            else:
                unit_inst.costing_enabled = False
                skipped_units.append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'reason': 'no_registered_watertap_cost_model'})
        report = self._builder.attach_costing()
        if report.get('status') != 'complete':
            raise RuntimeError(f'Costing attach failed: {report}')
        return {'costing_config': costing_config, 'requested_units': requested_units, 'skipped_units': skipped_units, 'attach_report': report}

    def _optimization_effluent_port(self) -> Any:
        units = self.get_units()
        mbr = self._roles.single_unit(R.MBR_SEPARATOR, units)
        if mbr is not None and hasattr(mbr, 'permeate'):
            return mbr.permeate
        clarifier = self._roles.single_unit(R.SECONDARY_CLARIFIER, units)
        if clarifier is not None and hasattr(clarifier, 'effluent'):
            return clarifier.effluent
        effluent = self._roles.single_unit(R.PRODUCT_EFFLUENT, units)
        if effluent is not None and hasattr(effluent, 'inlet'):
            return effluent.inlet
        return None

    def _port_concentration_expr(self, port: Any, component: str) -> Any:
        conc = getattr(port, 'conc_mass_comp', None)
        if conc is None:
            return 0.0
        for index in ((0, component), (0.0, component), component):
            try:
                return conc[index]
            except Exception:
                continue
        return 0.0

    def _asm2d_metric_exprs(self, port: Any) -> Dict[str, Any]:
        s = lambda component: self._port_concentration_expr(port, component)
        cod = s('S_F') + s('S_A') + s('S_I') + s('X_S') + s('X_I') + s('X_H') + 1.42 * s('X_AUT')
        bod5 = 0.6 * s('S_F') + 0.4 * s('S_A') + 0.25 * s('X_S')
        biomass_n = 0.07 * (s('X_H') + s('X_PAO') + s('X_AUT'))
        particulate_n = 0.03 * s('X_I') + 0.04 * s('X_S')
        tn = s('S_NH4') + s('S_NO3') + biomass_n + particulate_n
        tkn = s('S_NH4') + biomass_n + particulate_n
        tp = s('S_PO4') + s('X_PP') + 0.02 * (s('X_H') + s('X_PAO') + s('X_AUT')) + 0.01 * (s('X_I') + s('X_S')) + 1.0 / 4.87 * s('X_MeP')
        return {'COD_mg_L': cod, 'BOD5_mg_L': bod5, 'TN_mg_L': tn, 'TKN_mg_L': tkn, 'TP_mg_L': tp, 'TSS_mg_L': s('X_TSS')}

    def _add_effluent_guarantee_constraints(self) -> Dict[str, Any]:
        from pyomo.environ import Constraint
        metadata = self._universal_optimization_metadata()
        design_targets = metadata.get('design_targets')
        guarantees = {}
        if isinstance(design_targets, dict) and isinstance(design_targets.get('effluent_guarantees'), dict):
            guarantees = dict(design_targets['effluent_guarantees'])
        port = self._optimization_effluent_port()
        if port is None or not guarantees:
            return {'constraints': {}, 'guarantees': guarantees}
        fs = getattr(self._model, 'fs', self._model)
        exprs = self._asm2d_metric_exprs(port)
        constraints = {}
        for metric, expr in exprs.items():
            if metric not in guarantees or guarantees[metric] is None:
                continue
            try:
                limit_mg_l = float(guarantees[metric])
            except (TypeError, ValueError):
                continue
            enforcement_margin_mg_l = min(0.001, max(1e-05, abs(limit_mg_l) * 1e-06))
            enforced_limit_mg_l = limit_mg_l - enforcement_margin_mg_l
            component_name = f'optimization_effluent_{metric.lower()}_constraint'
            if hasattr(fs, component_name):
                fs.del_component(getattr(fs, component_name))
            setattr(fs, component_name, Constraint(expr=expr <= enforced_limit_mg_l / 1000.0))
            constraints[metric] = {'component_name': component_name, 'limit_mg_L': limit_mg_l, 'enforced_limit_mg_L': enforced_limit_mg_l, 'enforcement_margin_mg_L': enforcement_margin_mg_l}
        return {'constraints': constraints, 'guarantees': guarantees}

    def _effluent_constraint_activity(self, constraint_info: Dict[str, Any]) -> Dict[str, Any]:
        from pyomo.environ import value
        port = self._optimization_effluent_port()
        if port is None:
            return {}
        exprs = self._asm2d_metric_exprs(port)
        activity = {}
        for metric, info in constraint_info.get('constraints', {}).items():
            expr = exprs.get(metric)
            if expr is None:
                continue
            limit = info.get('limit_mg_L')
            actual = value(expr, exception=False)
            if actual is None or limit is None:
                activity[metric] = {**info, 'status': 'not_available'}
                continue
            actual_mg_l = float(actual) * 1000.0
            slack = float(limit) - actual_mg_l
            activity[metric] = {**info, 'value_mg_L': actual_mg_l, 'slack_mg_L': slack, 'binding': abs(slack) <= max(1e-06, abs(float(limit)) * 0.0001), 'satisfied': slack >= -max(0.0001, abs(float(limit)) * 1e-06)}
        return activity

    def _effluent_constraint_violations(self, activity: Dict[str, Any]) -> Dict[str, Any]:
        violations = {}
        for metric, info in activity.items():
            if not isinstance(info, dict) or info.get('satisfied') is not False:
                continue
            component_name = info.get('component_name') or f'effluent_guarantee[{metric}]'
            violations[metric] = {'constraint_name': component_name, 'value_mg_L': info.get('value_mg_L'), 'limit_mg_L': info.get('limit_mg_L'), 'slack_mg_L': info.get('slack_mg_L')}
        return violations

    def _effluent_guarantee_report(self, *, message: str, solve_result: Dict[str, Any], violations: Dict[str, Any], constraint_info: Dict[str, Any]) -> Dict[str, Any] | None:
        try:
            from puran_mcp_support.engineering.models.solver_infeasibility_report import SolverInfeasibilityReport
        except Exception:
            return None
        metadata = self._universal_optimization_metadata()
        named_violations = [f'{metric} ({payload.get('constraint_name')})' for metric, payload in violations.items()]
        if named_violations:
            cause = 'Effluent guarantee constraints are violated: ' + ', '.join(named_violations)
        else:
            cause = 'Effluent guarantee constraints could not be proven feasible by the optimization solve.'
        report = SolverInfeasibilityReport(model_family=str(metadata.get('topology_template') or 'universal'), solver_status=str(solve_result.get('solver_status') or 'unknown'), termination_condition=str(solve_result.get('termination_condition') or 'optimization_failed'), message=message, most_likely_cause=cause, suggested_remediation=['Review diagnostics.optimization.effluent_constraint_violations for the named hard guarantee.', 'Increase available biological or polishing capacity before relaxing a tender guarantee.', 'If optimization terminates before feasibility is proven, keep the feasibility result out of production reporting and inspect the objective NLP diagnostics.'], diagnostic_basis={'objective_spec': self._requested_objective_spec(), 'effluent_guarantees': constraint_info, 'effluent_constraint_violations': violations, 'solve_result': solve_result})
        return report.model_dump(mode='python')

    def _optimization_failure_transition(self, *, snapshot: List[tuple[Any, Any, bool, Any, Any]], details: Dict[str, Any], constraint_info: Dict[str, Any], solve_result: Dict[str, Any], reason: str, message: str, extra_failure_details: Dict[str, Any] | None=None) -> PipelineResult:
        failure_activity = self._effluent_constraint_activity(constraint_info)
        failure_violations = self._effluent_constraint_violations(failure_activity)
        if failure_activity:
            details['effluent_constraint_activity_at_failure'] = failure_activity
        if failure_violations:
            details['effluent_constraint_violations_at_failure'] = failure_violations
        self._restore_model_variables(snapshot)
        restored_activity = self._effluent_constraint_activity(constraint_info)
        restored_violations = self._effluent_constraint_violations(restored_activity)
        if restored_activity:
            details['effluent_constraint_activity'] = restored_activity
        if restored_violations:
            details['effluent_constraint_violations'] = restored_violations
        could_not_improve = {'reason': reason, 'fallback': 'feasibility_solution_restored', 'solve_result': solve_result}
        if extra_failure_details:
            could_not_improve.update(extra_failure_details)
        details['optimization_could_not_improve'] = could_not_improve
        report = self._effluent_guarantee_report(message=message, solve_result=solve_result, violations=restored_violations or failure_violations, constraint_info=constraint_info)
        if report is not None:
            details['convergence_status'] = 'solver_infeasible_with_diagnostic'
            details['solver_infeasibility_report'] = report
        return self._transition(PipelineState.OPTIMIZATION_SOLVE, False, message, details=details, warnings=['optimization_could_not_improve'], errors=[reason])

    def _costing_scalar_payload(self) -> Dict[str, Any]:
        if self._builder is None:
            return {}
        payload = self._builder.extract_costing_results(source='optimization_solve')
        return {key: payload.get(key) for key in ('status', 'total_capital_cost', 'total_operating_cost', 'total_annualized_cost', 'LCOW', 'aggregate_capital_cost', 'aggregate_fixed_operating_cost', 'aggregate_variable_operating_cost', 'aggregate_electricity', 'warnings', 'unit_costs', 'product_stream') if key in payload}

    def _select_optimization_objective(self, objective_spec: str) -> Any:
        from pyomo.environ import Objective, minimize
        fs = getattr(self._model, 'fs', self._model)
        costing = getattr(fs, 'costing', None)
        if costing is None:
            raise RuntimeError('Costing block is required before selecting objective')
        if self._builder is not None and objective_spec == 'min_lcow':
            self._builder.ensure_lcow()
        objective_map = {'min_capex': 'total_capital_cost', 'min_opex': 'total_operating_cost', 'min_lcow': 'LCOW', 'min_total_annualized_cost': 'total_annualized_cost'}
        attr_name = objective_map[objective_spec]
        expr = getattr(costing, attr_name, None)
        if expr is None and objective_spec == 'min_total_annualized_cost':
            expr = getattr(costing, 'total_capital_cost') + getattr(costing, 'total_operating_cost')
        if expr is None:
            raise RuntimeError(f"Costing metric '{attr_name}' is not available")
        for objective in self._model.component_data_objects(Objective, active=True, descend_into=True):
            objective.deactivate()
        if hasattr(fs, 'optimization_objective'):
            fs.del_component(fs.optimization_objective)
        fs.optimization_objective = Objective(expr=expr, sense=minimize)
        return expr

    def _optimization_solver_options(self) -> Dict[str, Any]:
        options = {'linear_solver': default_ipopt_linear_solver(), 'hessian_approximation': 'exact', 'tol': 1e-06, 'max_iter': 3000, 'mu_init': 0.0001, 'mu_strategy': 'adaptive', 'bound_push': 1e-08, 'bound_frac': 1e-08, 'max_cpu_time': self._config.max_wall_time_s}
        options.update(self._config.solver_options)
        return options

    def _retry_optimization_with_fixed_kla(self, snapshot: List[tuple[Any, Any, bool, Any, Any]]) -> Dict[str, Any] | None:
        """Retry optimization with aerobic KLa fixed at the feasible value.

        OPEX-style objectives can drive KLa aggressively toward the low bound
        before ASM2D oxygen-transfer equations re-balance. The WaterTAP BSM2
        pattern still benefits from a second pass from the feasible KLa point
        instead of immediately discarding the whole objective solve.
        """
        from pyomo.environ import value
        units = self.get_units()
        aerobic_klas: List[Tuple[str, Any]] = []
        for unit_id in self._roles.all_for(R.AEROBIC, units):
            unit = units.get(unit_id)
            if unit is None:
                continue
            kla = self._indexed_var(getattr(unit, 'KLa', None), None)
            if kla is not None:
                aerobic_klas.append((unit_id, kla))
        if not aerobic_klas:
            return None
        self._restore_model_variables(snapshot)
        if self._builder is not None:
            self._builder.initialize_costing_values()
        retry_release = self._unfix_optimization_design_variables()
        released = retry_release.get('released', {})
        fixed_values: Dict[str, Any] = {}
        for unit_id, kla in aerobic_klas:
            feasible_kla = None
            released_kla = released.get(f'{unit_id}.KLa')
            if isinstance(released_kla, dict):
                feasible_kla = released_kla.get('initial_value')
            if feasible_kla is None:
                feasible_kla = value(kla, exception=False)
            if feasible_kla is None:
                continue
            kla.set_value(feasible_kla, skip_validation=True)
            kla.fix()
            fixed_values[kla.name] = feasible_kla
        if not fixed_values:
            return None
        retry_result = self._solve_current_model(self._optimization_solver_options())
        return {'strategy': 'fixed_feasible_kla_retry', 'fixed_variables': fixed_values, 'design_variable_release': retry_release, 'solve_result': retry_result}

    def _accept_nonoptimal_optimization_iterate(self, *, details: Dict[str, Any], constraint_info: Dict[str, Any], solve_result: Dict[str, Any], objective_expr: Any, objective_before: Any) -> Dict[str, Any] | None:
        """Accept a finite optimizer iterate only when it is visibly feasible.

        ASM2D universal optimization can hit IPOPT's iteration limit after
        finding a useful finite design. Keep that design only when hard
        effluent guarantees, residual checks, bound checks, and objective
        improvement all agree that the loaded iterate is reportable.
        """
        if solve_result.get('success') or not solve_result.get('loaded_nonoptimal_solution'):
            return None
        from pyomo.environ import value
        activity = self._effluent_constraint_activity(constraint_info)
        violations = self._effluent_constraint_violations(activity)
        runtime_metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        try:
            residual_tolerance = float((runtime_metadata or {}).get('asm2d_near_feasible_residual_tolerance', 0.001))
        except (TypeError, ValueError):
            residual_tolerance = 0.001
        residual_tolerance = max(residual_tolerance, 0.02)
        bound_tolerance = 1e-05
        residuals = self._diagnostics.get_constraint_residuals(self._model, threshold=residual_tolerance)
        bounds = self._diagnostics.get_bound_violations(self._model, tolerance=bound_tolerance)
        objective_after = value(objective_expr, exception=False)
        residual_failures = self._optimization_residual_failures(residuals.details, process_tolerance=residual_tolerance, costing_tolerance=1.0)
        details['nonoptimal_optimization_candidate'] = {'effluent_constraint_activity': activity, 'effluent_constraint_violations': violations, 'post_optimization_diagnostics': {'constraint_residuals': len(residual_failures), 'raw_constraint_residuals': residuals.issues_found, 'bound_violations': bounds.issues_found, 'constraint_residual_tolerance': residual_tolerance, 'costing_constraint_residual_tolerance': 1.0, 'bound_violation_tolerance': bound_tolerance}, 'constraint_residual_details': [{'name': r.constraint_name, 'residual': r.residual} for r in residual_failures[:10]], 'bound_violation_details': [{'name': v.variable_name, 'value': v.value, 'type': v.violation_type} for v in bounds.details[:10]], 'objective_final': objective_after}
        if violations:
            return None
        if residual_failures or bounds.issues_found > 0:
            return None
        if objective_before is None or objective_after is None or float(objective_after) >= float(objective_before) * (1.0 - 1e-06):
            return None
        return {**solve_result, 'success': True, 'accepted_nonoptimal_optimization': True, 'constraint_residual_tolerance': residual_tolerance, 'costing_constraint_residual_tolerance': 1.0, 'bound_violation_tolerance': bound_tolerance}

    def _optimization_residual_failures(self, residual_details: List[Any], *, process_tolerance: float, costing_tolerance: float) -> List[Any]:
        """Return residual details that exceed class-specific acceptance guards."""
        failures = []
        for residual in residual_details:
            name = str(getattr(residual, 'constraint_name', '')).lower()
            tolerance = costing_tolerance if '.costing.' in name else process_tolerance
            try:
                magnitude = abs(float(getattr(residual, 'residual', 0.0)))
            except (TypeError, ValueError):
                failures.append(residual)
                continue
            if magnitude > tolerance:
                failures.append(residual)
        return failures

    def _retry_optimization_with_surrogate_objectives(self, *, snapshot: List[tuple[Any, Any, bool, Any, Any]], requested_objective: str, objective_expr: Any, objective_before: Any) -> Dict[str, Any] | None:
        """Retry composite objectives with simpler cost surrogates.

        LCOW and total annualized cost can send IPOPT into infeasible iterates
        even when nearby capex/opex-improving points satisfy the hard effluent
        constraints and improve the originally requested scalar. This retry
        solves those simpler surrogate objectives but validates acceptance
        against the original objective expression.
        """
        if requested_objective not in {'min_lcow', 'min_total_annualized_cost'}:
            return None
        from pyomo.environ import value
        attempts: List[Dict[str, Any]] = []
        for surrogate in ('min_capex', 'min_opex'):
            self._restore_model_variables(snapshot)
            if self._builder is not None:
                self._builder.initialize_costing_values()
            design_release = self._unfix_optimization_design_variables()
            constraint_info = self._add_effluent_guarantee_constraints()
            surrogate_expr = self._select_optimization_objective(surrogate)
            solve_result = self._solve_current_model(self._optimization_solver_options())
            attempt: Dict[str, Any] = {'surrogate_objective': surrogate, 'design_variable_release': design_release, 'effluent_guarantees': constraint_info, 'solve_result': solve_result}
            if not solve_result.get('success'):
                fixed_kla_retry = self._retry_optimization_with_fixed_kla(snapshot)
                if fixed_kla_retry is not None:
                    attempt['fixed_kla_retry'] = fixed_kla_retry
                    solve_result = fixed_kla_retry['solve_result']
                    attempt['solve_result'] = solve_result
            if not solve_result.get('success'):
                candidate_details: Dict[str, Any] = {}
                accepted = self._accept_nonoptimal_optimization_iterate(details=candidate_details, constraint_info=constraint_info, solve_result=solve_result, objective_expr=objective_expr, objective_before=objective_before)
                attempt.update(candidate_details)
                if accepted is not None:
                    solve_result = accepted
                    attempt['solve_result'] = solve_result
            requested_after = value(objective_expr, exception=False)
            surrogate_after = value(surrogate_expr, exception=False)
            attempt['requested_objective_final'] = requested_after
            attempt['surrogate_objective_final'] = surrogate_after
            if solve_result.get('success'):
                attempts.append(attempt)
                return {'strategy': 'surrogate_objective_retry', 'requested_objective': requested_objective, 'accepted_surrogate': surrogate, 'attempts': attempts, 'design_variable_release': design_release, 'effluent_guarantees': constraint_info, 'solve_result': solve_result}
            attempts.append(attempt)
        return {'strategy': 'surrogate_objective_retry', 'requested_objective': requested_objective, 'attempts': attempts, 'solve_result': attempts[-1]['solve_result'] if attempts else None}

    def run_optimization_solve(self) -> PipelineResult:
        """Run WaterTAP-style objective optimization after feasible convergence."""
        if self._model is None:
            return self._transition(PipelineState.FAILED, False, 'No model set')
        objective_spec = self._requested_objective_spec()
        if objective_spec == 'feasibility':
            return self._transition(PipelineState.OPTIMIZATION_SOLVE, True, 'Optimization skipped for feasibility objective', details={'objective_spec': objective_spec, 'skipped': True})
        from pyomo.environ import value
        snapshot = self._snapshot_model_variables()
        details: Dict[str, Any] = {'objective_spec': objective_spec}
        constraint_info: Dict[str, Any] = {'constraints': {}, 'guarantees': {}}
        try:
            details['costing_setup'] = self._enable_optimization_costing()
            self._builder.initialize_costing_values()
            design_release = self._unfix_optimization_design_variables()
            details['design_variable_release'] = design_release
            constraint_info = self._add_effluent_guarantee_constraints()
            details['effluent_guarantees'] = constraint_info
            objective_expr = self._select_optimization_objective(objective_spec)
            objective_before = value(objective_expr, exception=False)
            details['objective_initial'] = objective_before
            solve_result = self._solve_current_model(self._optimization_solver_options())
            details['solve_result'] = solve_result
            if not solve_result.get('success'):
                retry = self._retry_optimization_with_fixed_kla(snapshot)
                if retry is not None:
                    details['optimization_retry'] = retry
                    solve_result = retry['solve_result']
                    details['solve_result'] = solve_result
                if solve_result.get('success'):
                    details['stabilized_design_variables'] = {'aerobic_KLa': {'reason': 'fixed_after_released_kla_retry', 'values': retry.get('fixed_variables') if retry else None}}
                else:
                    accepted_iterate = self._accept_nonoptimal_optimization_iterate(details=details, constraint_info=constraint_info, solve_result=solve_result, objective_expr=objective_expr, objective_before=objective_before)
                    if accepted_iterate is not None:
                        solve_result = accepted_iterate
                        details['solve_result'] = solve_result
                    else:
                        surrogate_retry = self._retry_optimization_with_surrogate_objectives(snapshot=snapshot, requested_objective=objective_spec, objective_expr=objective_expr, objective_before=objective_before)
                        if isinstance(surrogate_retry, dict) and isinstance(surrogate_retry.get('solve_result'), dict) and surrogate_retry['solve_result'].get('success'):
                            details['optimization_surrogate_retry'] = surrogate_retry
                            solve_result = surrogate_retry['solve_result']
                            details['solve_result'] = solve_result
                            if isinstance(surrogate_retry.get('design_variable_release'), dict):
                                details['design_variable_release'] = surrogate_retry['design_variable_release']
                            if isinstance(surrogate_retry.get('effluent_guarantees'), dict):
                                constraint_info = surrogate_retry['effluent_guarantees']
                                details['effluent_guarantees'] = constraint_info
                        else:
                            if surrogate_retry is not None:
                                details['optimization_surrogate_retry'] = surrogate_retry
                            return self._optimization_failure_transition(snapshot=snapshot, details=details, constraint_info=constraint_info, solve_result=solve_result, reason='optimization_solve_failed', message='Optimization failed before effluent guarantees were proven; feasibility solution restored')
            if not solve_result.get('success'):
                return self._optimization_failure_transition(snapshot=snapshot, details=details, constraint_info=constraint_info, solve_result=solve_result, reason='optimization_solve_failed', message='Optimization failed before effluent guarantees were proven; feasibility solution restored')
            details['effluent_constraint_activity'] = self._effluent_constraint_activity(constraint_info)
            effluent_violations = self._effluent_constraint_violations(details['effluent_constraint_activity'])
            if effluent_violations:
                details['effluent_constraint_violations'] = effluent_violations
                report = self._effluent_guarantee_report(message='Optimization solve violated hard effluent guarantees', solve_result=solve_result, violations=effluent_violations, constraint_info=constraint_info)
                if report is not None:
                    details['convergence_status'] = 'solver_infeasible_with_diagnostic'
                    details['solver_infeasibility_report'] = report
                return self._transition(PipelineState.OPTIMIZATION_SOLVE, False, 'Optimization solve violated hard effluent guarantees', details=details, warnings=['effluent_guarantee_violation'], errors=['effluent_guarantee_violation'])
            residual_threshold = float(solve_result.get('constraint_residual_tolerance', 1e-06)) if solve_result.get('accepted_nonoptimal_optimization') else 1e-06
            costing_residual_threshold = float(solve_result.get('costing_constraint_residual_tolerance', 1.0))
            residuals = self._diagnostics.get_constraint_residuals(self._model, threshold=residual_threshold)
            violations = self._diagnostics.get_bound_violations(self._model, tolerance=1e-05)
            residual_failures = self._optimization_residual_failures(residuals.details, process_tolerance=residual_threshold, costing_tolerance=costing_residual_threshold) if solve_result.get('accepted_nonoptimal_optimization') else residuals.details
            details['post_optimization_diagnostics'] = {'constraint_residuals': len(residual_failures), 'raw_constraint_residuals': residuals.issues_found, 'bound_violations': violations.issues_found, 'constraint_residual_tolerance': residual_threshold, 'costing_constraint_residual_tolerance': costing_residual_threshold if solve_result.get('accepted_nonoptimal_optimization') else None, 'bound_violation_tolerance': 1e-05}
            if residual_failures or violations.issues_found > 0:
                return self._optimization_failure_transition(snapshot=snapshot, details=details, constraint_info=constraint_info, solve_result=solve_result, reason='post_optimization_diagnostics_failed', message='Optimization diagnostics failed before effluent guarantees were proven; feasibility solution restored', extra_failure_details={'constraint_residuals': [{'name': r.constraint_name, 'residual': r.residual} for r in residual_failures[:10]], 'bound_violations': [{'name': v.variable_name, 'value': v.value, 'type': v.violation_type} for v in violations.details[:10]]})
            objective_after = value(objective_expr, exception=False)
            details['objective_final'] = objective_after
            if objective_before is not None and objective_after is not None and (float(objective_after) >= float(objective_before) * (1.0 - 1e-06)):
                return self._optimization_failure_transition(snapshot=snapshot, details=details, constraint_info=constraint_info, solve_result=solve_result, reason='objective_not_improved', message='Optimization did not improve objective; feasibility solution restored', extra_failure_details={'objective_initial': objective_before, 'objective_final': objective_after})
            details['costing'] = self._costing_scalar_payload()
            return self._transition(PipelineState.OPTIMIZATION_SOLVE, True, 'Optimization completed successfully', details=details)
        except Exception as exc:
            return self._optimization_failure_transition(snapshot=snapshot, details=details, constraint_info=constraint_info, solve_result={'success': False, 'solver_status': 'error', 'termination_condition': 'optimization_exception', 'error': str(exc)}, reason='optimization_exception', message='Optimization error; feasibility solution restored', extra_failure_details={'error': str(exc)})

    def run_full_pipeline(self, on_stage_complete: Optional[Callable[[PipelineResult], None]]=None) -> PipelineResult:
        """Run rating geometry checks outside every solve/recovery/optimization path."""
        from watertap_engine_mcp.core.rating_geometry import RatingGeometryError, verify_live_geometry
        metadata = getattr(getattr(self._session, 'config', None), 'runtime_metadata', {})
        contract = metadata.get('rating_geometry')
        try:
            verify_live_geometry(contract, self.get_units())
            result = self._run_full_pipeline(on_stage_complete=on_stage_complete)
            if result.success:
                verify_live_geometry(contract, self.get_units())
                if contract:
                    result.details['analysis_mode'] = contract['analysis_mode']
                    result.details['geometry_identity'] = 'verified' if contract['analysis_mode'] == 'rating' else 'not_applicable_inverse_design'
            return result
        except RatingGeometryError as exc:
            return self._transition(PipelineState.FAILED, False, str(exc), details={'geometry_identity': 'failed', 'geometry_contract_error': str(exc)})

    def _run_full_pipeline(self, on_stage_complete: Optional[Callable[[PipelineResult], None]]=None) -> PipelineResult:
        """Run the full hygiene pipeline.

        Args:
            on_stage_complete: Optional callback after each stage

        Returns:
            Final PipelineResult
        """
        stages = [('DOF Check', self.run_dof_check), ('Scaling', self.run_scaling), ('Initialization', self.run_initialization), ('Pre-solve Diagnostics', self.run_pre_solve_diagnostics), ('Solve', self.run_solve), ('Post-solve Diagnostics', self.run_post_solve_diagnostics)]
        if self._config.skip_initialization:
            stages = [stage for stage in stages if stage[0] != 'Initialization']
        for stage_name, stage_func in stages:
            result = stage_func()
            if on_stage_complete:
                on_stage_complete(result)
            if not result.success:
                physics_verdict = result.state == PipelineState.SOLVING and isinstance(result.details, dict) and (result.details.get('adm1_ph_target_guard') is not None)
                if not physics_verdict and self._config.enable_progressive_tightening and (result.state == PipelineState.SOLVING):
                    progressive_result = self.run_progressive_tightening()
                    if on_stage_complete:
                        on_stage_complete(progressive_result)
                    retry_result = self.run_solve()
                    if on_stage_complete:
                        on_stage_complete(retry_result)
                    if retry_result.success:
                        post_result = self.run_post_solve_diagnostics()
                        if on_stage_complete:
                            on_stage_complete(post_result)
                        if not post_result.success:
                            return self._transition(PipelineState.FAILED, False, f'Pipeline failed at Post-solve Diagnostics: {post_result.message}', details={'failed_stage': 'Post-solve Diagnostics', 'stage_result': post_result.details, 'recovered': True, 'initial_solve_result': result.details, 'recovery': progressive_result.details}, errors=post_result.errors)
                        return self._complete_after_success(solve_result=retry_result.details, post_solve_result=post_result, recovered=True, initial_solve_result=result.details, recovery={'strategy': 'progressive_tightening', 'message': progressive_result.message, 'details': progressive_result.details}, message='Pipeline completed successfully after progressive tightening', on_stage_complete=on_stage_complete)
                    if isinstance(retry_result.details, dict):
                        retry_result.details['progressive_tightening'] = {'success': progressive_result.success, 'message': progressive_result.message, 'details': progressive_result.details}
                    result = retry_result
                    stage_name = 'Solve'
                if not physics_verdict and self._config.enable_relaxed_solve:
                    asm2d_relaxed_result = self.run_asm2d_recycle_relaxed_solve(result)
                    if asm2d_relaxed_result is not None:
                        if on_stage_complete:
                            on_stage_complete(asm2d_relaxed_result)
                        if asm2d_relaxed_result.success:
                            post_result = self.run_post_solve_diagnostics()
                            if on_stage_complete:
                                on_stage_complete(post_result)
                            if not post_result.success:
                                return self._transition(PipelineState.FAILED, False, f'Pipeline failed at Post-solve Diagnostics: {post_result.message}', details={'failed_stage': 'Post-solve Diagnostics', 'stage_result': post_result.details, 'recovered': True, 'initial_solve_result': result.details, 'recovery': asm2d_relaxed_result.details}, errors=post_result.errors)
                            solve_result = asm2d_relaxed_result.details.get('solve_result', {}) if isinstance(asm2d_relaxed_result.details, dict) else {}
                            final_solve_result = dict(solve_result)
                            return self._complete_after_success(solve_result=final_solve_result, post_solve_result=post_result, recovered=True, initial_solve_result=result.details, recovery=asm2d_relaxed_result.details, message='Pipeline completed successfully after ASM2D recycle relaxation', on_stage_complete=on_stage_complete)
                        if isinstance(result.details, dict):
                            result.details['asm2d_recycle_relaxed_solve'] = asm2d_relaxed_result.details
                if not physics_verdict and self._config.enable_relaxed_solve and (result.state == PipelineState.SOLVING) and (not self._is_asm2d_biological_model()):
                    recovery_result = self._recovery.attempt_recovery(termination_condition=result.details.get('termination_condition', 'unknown'), max_attempts=3)
                    if recovery_result.success:
                        relaxed_result = self._transition(PipelineState.RELAXED_SOLVE, True, f'Recovery succeeded: {recovery_result.message}', details={'strategy': recovery_result.strategy.value, 'actions_taken': recovery_result.actions_taken, 'solve_result': recovery_result.solve_result})
                        if on_stage_complete:
                            on_stage_complete(relaxed_result)
                        recovery_options = self._base_solver_options()
                        recovery_options.update(self._config.solver_options)
                        recovery_options = self._with_asm2d_solver_defaults(recovery_options)
                        recovery_options = self._with_adm1_solver_defaults(recovery_options)
                        self._select_adm1_viable_branch(recovery_options)
                        post_result = self.run_post_solve_diagnostics()
                        if on_stage_complete:
                            on_stage_complete(post_result)
                        if not post_result.success:
                            return self._transition(PipelineState.FAILED, False, f'Pipeline failed at Post-solve Diagnostics: {post_result.message}', details={'failed_stage': 'Post-solve Diagnostics', 'stage_result': post_result.details, 'recovered': True, 'initial_solve_result': result.details, 'recovery': {'strategy': recovery_result.strategy.value, 'message': recovery_result.message, 'actions_taken': recovery_result.actions_taken, 'solve_result': recovery_result.solve_result}}, errors=post_result.errors)
                        final_solve_result = {'solver_status': recovery_result.solve_result.get('solver_status'), 'termination_condition': recovery_result.solve_result.get('termination_condition'), 'solve_time': recovery_result.solve_result.get('solve_time'), 'iterations': recovery_result.solve_result.get('iterations')}
                        return self._complete_after_success(solve_result=final_solve_result, post_solve_result=post_result, recovered=True, initial_solve_result=result.details, recovery={'strategy': recovery_result.strategy.value, 'message': recovery_result.message, 'actions_taken': recovery_result.actions_taken, 'solve_result': recovery_result.solve_result}, message='Pipeline completed successfully after recovery', on_stage_complete=on_stage_complete)
                    else:
                        result.details['recovery_attempted'] = True
                        result.details['recovery_message'] = recovery_result.message
                        result.details['recovery_actions'] = recovery_result.actions_taken
                failure_details = {'failed_stage': stage_name, 'stage_result': result.details}
                if result.details.get('solver_infeasibility_report'):
                    failure_details['convergence_status'] = result.details.get('convergence_status') or 'solver_infeasible_with_diagnostic'
                    failure_details['solver_infeasibility_report'] = result.details['solver_infeasibility_report']
                    if result.details.get('adm1_ph_target_guard'):
                        failure_details['adm1_ph_target_guard'] = result.details['adm1_ph_target_guard']
                return self._transition(PipelineState.FAILED, False, f'Pipeline failed at {stage_name}: {result.message}', details=failure_details, errors=result.errors)
        optimization_result = None
        if self._optimization_requested():
            optimization_result = self.run_optimization_solve()
            if on_stage_complete:
                on_stage_complete(optimization_result)
            if not optimization_result.success:
                failure_details = {'failed_stage': 'Optimization', 'stage_result': optimization_result.details}
                if optimization_result.details.get('solver_infeasibility_report'):
                    failure_details['convergence_status'] = 'solver_infeasible_with_diagnostic'
                    failure_details['solver_infeasibility_report'] = optimization_result.details['solver_infeasibility_report']
                return self._transition(PipelineState.FAILED, False, f'Pipeline failed at Optimization: {optimization_result.message}', details=failure_details, errors=optimization_result.errors)
        return self._transition(PipelineState.COMPLETED, True, 'Pipeline completed successfully', details=self._build_completed_details(solve_result=next((entry.details for entry in reversed(self._history) if entry.state == PipelineState.SOLVING), {}), post_solve_result=next((entry for entry in reversed(self._history) if entry.state == PipelineState.POST_SOLVE_DIAGNOSTICS), PipelineResult(success=True, state=PipelineState.POST_SOLVE_DIAGNOSTICS, message='Post-solve diagnostics passed')), recovered=False, optimization_result=optimization_result.details if optimization_result is not None else None))

    def run_all(self, on_stage_complete: Optional[Callable[[PipelineResult], None]]=None) -> PipelineResult:
        """Alias for the canonical full hygiene pipeline entrypoint."""
        return self.run_full_pipeline(on_stage_complete=on_stage_complete)

    def reset(self):
        """Reset pipeline to initial state."""
        self._state = PipelineState.IDLE
        self._history = []

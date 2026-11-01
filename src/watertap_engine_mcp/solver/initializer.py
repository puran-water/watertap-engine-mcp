"""Flowsheet Initialization for WaterTAP.

Provides tools for sequential initialization of WaterTAP flowsheets
using IDAES SequentialDecomposition for initialization order.

IMPORTANT: Uses utils.topo_sort.compute_initialization_order which delegates
to IDAES SequentialDecomposition and only falls back for singleton internal
self-loops that upstream helper units expose in the decomposition graph.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from watertap_engine_mcp.core.roles import RoleResolver, UnitRole as R
from watertap_engine_mcp.core.property_registry import is_asm2d_property_block
from watertap_engine_mcp.utils.topo_sort import compute_initialization_order, SequentialDecompositionError
ASM2D_TRACE_CONCENTRATION_FLOORS = {'S_O2': 1e-08, 'S_N2': 1e-08, 'S_NO3': 1e-08, 'X_MeOH': 1e-12, 'X_MeP': 1e-12, 'X_PAO': 1e-12, 'X_PHA': 1e-12, 'X_PP': 1e-12}
MODIFIED_ASM2D_COD_TO_VSS = 1.5686
ASM2D_SUBSTRATE_SEED_KG_M3 = {'S_F': 0.01, 'S_A': 0.001, 'S_PO4': 0.001}
ASM2D_BIOMASS_SEED_FRACTIONS = {'X_I': 0.35, 'X_S': 0.05, 'X_H': 0.35, 'X_PAO': 0.08, 'X_PP': 0.02, 'X_PHA': 0.02, 'X_AUT': 0.02}

class InitMethod(Enum):
    """Initialization method for a unit."""
    INITIALIZE = 'initialize'
    INITIALIZE_BUILD = 'initialize_build'
    CUSTOM = 'custom'
    NONE = 'none'

class InitStatus(Enum):
    """Status of initialization attempt."""
    SUCCESS = 'success'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    PENDING = 'pending'
    DEGRADED = 'degraded'

@dataclass
class InitializationResult:
    """Result of initializing a unit."""
    unit_id: str
    status: InitStatus
    dof_before: int = 0
    dof_after: int = 0
    solve_status: str = ''
    message: str = ''
    diagnostics: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FlowsheetInitResult:
    """Result of initializing entire flowsheet."""
    success: bool
    unit_results: List[InitializationResult] = field(default_factory=list)
    init_order: List[str] = field(default_factory=list)
    message: str = ''
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    failed_unit: Optional[str] = None
    recipe_name: str = 'generic_sequential'
    topology: Dict[str, Any] = field(default_factory=dict)
    status: Optional[InitStatus] = None

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

    def __init__(self, flowsheet: Any=None, model: Any=None, thermal_couplings: Optional[Dict[str, str]]=None, runtime_metadata: Optional[Dict[str, Any]]=None, unit_types: Optional[Dict[str, str]]=None, unit_roles: Optional[Dict[str, str]]=None, unit_stage_index: Optional[Dict[str, Optional[int]]]=None, unit_group: Optional[Dict[str, Optional[str]]]=None):
        """Initialize the flowsheet initializer.

        Args:
            flowsheet: The flowsheet block (m.fs)
            model: The full model (for SequentialDecomposition)
            thermal_couplings: Optional {evaporator_id: condenser_id} map
                for coupling-aware condenser heat guesses.  When absent,
                falls back to scanning for the first evaporator on the
                flowsheet (correct for MVC, wrong for MEE).
            unit_roles: Optional {unit_id: role} map driving role-based
                convergence priors. When absent or partial, the resolver falls
                back to the canonical template names so untagged template builds
                behave exactly as before.
            unit_stage_index: Optional {unit_id: stage_index} map giving the
                0-based position of each unit within a multi-stage role sequence
                (e.g. an N-stage aerobic train). ``None`` for every unit is the
                single-winner case and leaves 3-stage ordering unchanged.
            unit_group: Optional {unit_id: group} partition labels scoping role
                multiplicity queries (e.g. a parallel-train id).
        """
        self._flowsheet = flowsheet
        self._model = model
        self._thermal_couplings = thermal_couplings or {}
        self._runtime_metadata = runtime_metadata if runtime_metadata is not None else {}
        self._unit_types = unit_types or {}
        self._roles = RoleResolver(unit_roles, unit_stage_index=unit_stage_index, unit_group=unit_group)
        self._asm2d_recycle_connections: List[Dict[str, Any]] = []

    def _record_initializer_tactic(self, *, name: str, description: str, units: List[str]) -> None:
        """Record a named initialization tactic for solve diagnostics."""
        tactics = self._runtime_metadata.setdefault('initializer_tactics', [])
        if not isinstance(tactics, list):
            tactics = []
            self._runtime_metadata['initializer_tactics'] = tactics
        entry = {'name': name, 'description': description, 'units': sorted(set((str(unit_id) for unit_id in units)))}
        for existing in tactics:
            if isinstance(existing, dict) and existing.get('name') == entry['name'] and (existing.get('units') == entry['units']):
                return
        tactics.append(entry)

    def _unit_type(self, unit_id: str, unit: Any) -> str:
        """Return the registry unit type when available, else the live class name."""
        if unit_id in self._unit_types:
            return self._unit_types[unit_id]
        return type(unit).__name__

    def _unit_type_map(self, units: Dict[str, Any]) -> Dict[str, str]:
        """Return unit_id -> unit type for topology classification."""
        return {unit_id: self._unit_type(unit_id, unit) for unit_id, unit in units.items()}

    def _ru(self, units: Dict[str, Any], role: R) -> Optional[Any]:
        """Return the first live unit block filling ``role`` (or None).

        Kept only for old internal probes; production topology construction uses
        ``_rsingle`` for true single-winner roles and ordered-stage helpers for
        repeated reactor roles.
        """
        binding = self._roles.first(role, units)
        return units.get(binding.unit_id) if binding is not None else None

    def _rid(self, units: Dict[str, Any], role: R) -> Optional[str]:
        """Return the first present unit id filling ``role`` (or None).

        Kept only for old internal probes; callers that require a single unit
        must use ``_rsingle_id``.
        """
        binding = self._roles.first(role, units)
        return binding.unit_id if binding is not None else None

    def _rhas(self, units: Dict[str, Any], role: R) -> bool:
        """Return True when a unit fills ``role`` in the flowsheet."""
        return bool(self._roles.all_for(role, units))

    def _rsingle(self, units: Dict[str, Any], role: R) -> Optional[Any]:
        """Return the SOLE live unit block filling ``role`` (or None).

        Raises ``ValueError`` when more than one unit fills ``role`` — the
        explicit single-winner assertion for structural roles the ASM2D recycle
        topology fills exactly once (feed, mixer, splitters, separators, RAS/IR
        pressure changers, products). Use ``_ru``/``ordered`` resolution for the
        roles a train can legitimately fill more than once (the aerobic stages).
        """
        return self._roles.single_unit(role, units)

    def _rsingle_id(self, units: Dict[str, Any], role: R) -> Optional[str]:
        """Return the SOLE present unit id filling ``role`` (or None).

        Raises ``ValueError`` on multiplicity (see :meth:`_rsingle`).
        """
        return self._roles.single_id(role, units)

    def _asm2d_separator_role(self, units: Dict[str, Any]) -> Optional[R]:
        """Return the solids-separator role present (MBR preferred over clarifier)."""
        if self._rhas(units, R.MBR_SEPARATOR):
            return R.MBR_SEPARATOR
        if self._rhas(units, R.SECONDARY_CLARIFIER):
            return R.SECONDARY_CLARIFIER
        return None
    _ASM2D_REACTOR_ROLE_ORDER = (R.PRE_ANOXIC, R.AEROBIC, R.POST_ANOXIC)

    def _asm2d_reactor_ids(self, units: Dict[str, Any]) -> List[str]:
        """Return ALL present biological reactor ids in flow order (by role).

        Built from :meth:`RoleResolver.ordered_stages` so a repeated reactor role
        (an N-stage aerobic train) expands into every stage in
        ``(role_stage_index, insertion)`` order. For the canonical 3-stage
        templates each role is single-winner, so this returns
        ``[pre_anoxic, aerobic, post_anoxic]`` exactly as the prior hard-coded
        loop did.
        """
        return [binding.unit_id for binding in self._roles.ordered_stages(self._ASM2D_REACTOR_ROLE_ORDER, units)]

    def _asm2d_core_ids(self, units: Dict[str, Any]) -> Set[str]:
        """Return the resolved ids of the structural core units for SD coverage.

        Single-winner structural roles resolve via ``single_id`` (explicit
        assertion they are filled exactly once); the aerobic role contributes
        every stage so an N-stage train requires all reactors initialized before
        partial SD state is accepted.
        """
        single_roles = (R.PRE_ANOXIC, R.INTERNAL_RECYCLE_SPLITTER, R.SLUDGE_SPLITTER, R.MIXER)
        ids = {self._rsingle_id(units, role) for role in single_roles}
        ids.update(self._roles.all_for(R.AEROBIC, units))
        separator_role = self._asm2d_separator_role(units)
        if separator_role is not None:
            ids.add(self._rsingle_id(units, separator_role))
        return {unit_id for unit_id in ids if unit_id is not None}

    def _asm2d_forward_arc(self, src_id: Optional[str], dst_id: Optional[str]) -> Tuple[Optional[str], str, Optional[str], str]:
        """Return the real forward arc ``src_id -> dst_id`` from the flowsheet
        connections, falling back to the synthesized ``(outlet -> inlet)`` arc.

        Preferring the actual connection ports generalizes the forward spine to
        N-stage trains (whose inter-reactor arcs the synthesized chain does not
        enumerate). For the canonical templates the real arc IS ``outlet ->
        inlet``, so the result is byte-for-byte identical to the prior synthesis.
        """
        if src_id is not None and dst_id is not None:
            for conn in self._asm2d_recycle_connections:
                if self._connection_source_unit(conn) == src_id and self._connection_dest_unit(conn) == dst_id:
                    src_port = conn.get('source_port') or conn.get('src_port') or 'outlet'
                    dst_port = conn.get('dest_port') or conn.get('dst_port') or 'inlet'
                    return (src_id, str(src_port), dst_id, str(dst_port))
        return (src_id, 'outlet', dst_id, 'inlet')

    def _asm2d_forward_edges(self, units: Dict[str, Any]) -> Tuple[list, list, list, list]:
        """Return (upstream, middle, downstream, recycle) arc specs by role.

        Each arc is ``(src_id, src_port, dst_id, dst_port)`` with ids resolved
        from roles. Single-winner structural roles resolve via ``single_id``; the
        aerobic role expands into its ordered stages so an N-stage train chains
        ``aerobic_0 -> aerobic_1 -> …`` (the synthesized 3-stage chain is a
        special case with one aerobic stage). Inter-reactor arcs prefer the real
        flowsheet connections (``_asm2d_forward_arc``); port names are otherwise
        unit-type-determined and identical between canonical and from-scratch
        builds.
        """
        self._roles.require_explicit_stage_index(*self._ASM2D_REACTOR_ROLE_ORDER, units=units)
        feed = self._rsingle_id(units, R.FEED)
        mixer = self._rsingle_id(units, R.MIXER)
        anoxic = self._rsingle_id(units, R.PRE_ANOXIC)
        aerobic_ids = list(self._roles.all_for(R.AEROBIC, units))
        aerobic_first = aerobic_ids[0] if aerobic_ids else None
        aerobic_last = aerobic_ids[-1] if aerobic_ids else None
        ir_split = self._rsingle_id(units, R.INTERNAL_RECYCLE_SPLITTER)
        ir_press = self._rsingle_id(units, R.INTERNAL_RECYCLE_PRESSURE)
        post_anoxic = self._rsingle_id(units, R.POST_ANOXIC)
        mbr = self._rsingle_id(units, R.MBR_SEPARATOR)
        clarifier = self._rsingle_id(units, R.SECONDARY_CLARIFIER)
        separator = mbr or clarifier
        treatment_chain = [unit_id for unit_id in (self._rsingle_id(units, R.ALKALINITY), self._rsingle_id(units, R.CHEMICAL_P)) if unit_id is not None]
        sludge = self._rsingle_id(units, R.SLUDGE_SPLITTER)
        ras_press = self._rsingle_id(units, R.RAS_PRESSURE)
        effluent = self._rsingle_id(units, R.PRODUCT_EFFLUENT)
        waste = self._rsingle_id(units, R.PRODUCT_WASTE)
        upstream = [(feed, 'outlet', mixer, 'feed_water'), (mixer, 'outlet', anoxic, 'inlet'), self._asm2d_forward_arc(anoxic, aerobic_first)]
        for prev_id, next_id in zip(aerobic_ids, aerobic_ids[1:]):
            upstream.append(self._asm2d_forward_arc(prev_id, next_id))
        if ir_split is not None:
            upstream.append((aerobic_last, 'outlet', ir_split, 'inlet'))
            forward_source = ir_split
            forward_port = 'forward'
        else:
            forward_source = aerobic_last
            forward_port = 'outlet'
        if post_anoxic is not None and (not treatment_chain):
            middle: List[Tuple[Optional[str], str, Optional[str], str]] = [(forward_source, forward_port, post_anoxic, 'inlet'), (post_anoxic, 'outlet', mbr, 'inlet'), (post_anoxic, 'outlet', clarifier, 'inlet')]
        elif not treatment_chain:
            middle = [(forward_source, forward_port, mbr, 'inlet'), (forward_source, forward_port, clarifier, 'inlet')]
        else:
            middle = []
            if post_anoxic is not None:
                middle.append((forward_source, forward_port, post_anoxic, 'inlet'))
                current_unit = post_anoxic
                current_port = 'outlet'
            else:
                current_unit = forward_source
                current_port = forward_port
            for treatment_unit in treatment_chain:
                middle.append((current_unit, current_port, treatment_unit, 'inlet'))
                current_unit = treatment_unit
                current_port = 'outlet'
            middle.append((current_unit, current_port, separator, 'inlet'))
        downstream = [(mbr, 'permeate', effluent, 'inlet'), (mbr, 'retentate', sludge, 'inlet'), (clarifier, 'effluent', effluent, 'inlet'), (clarifier, 'underflow', sludge, 'inlet')]
        recycle = [(sludge, 'waste', waste, 'inlet'), (sludge, 'recycle', ras_press, 'inlet'), (ras_press, 'outlet', mixer, 'ras'), (ir_split, 'internal_recycle', ir_press, 'inlet'), (ir_press, 'outlet', mixer, 'internal_recycle')]
        return (upstream, middle, downstream, recycle)

    def _graph_has_path(self, graph: Dict[str, Set[str]], start: str, target: str, blocked_edges: Optional[Set[Tuple[str, str]]]=None) -> bool:
        """Return True if target is reachable from start in the directed graph."""
        blocked_edges = blocked_edges or set()
        stack = [start]
        seen: Set[str] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            for next_node in graph.get(node, set()):
                if (node, next_node) in blocked_edges:
                    continue
                if next_node not in seen:
                    stack.append(next_node)
        return False

    def _heuristic_tear_streams(self, connections: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
        """Pick deterministic tear-stream guesses for recycle/cyclic topologies."""
        graph: Dict[str, Set[str]] = {}
        edge_order: List[Tuple[str, str]] = []
        for conn in connections:
            src = self._connection_source_unit(conn)
            dst = self._connection_dest_unit(conn)
            if not src or not dst:
                continue
            graph.setdefault(src, set()).add(dst)
            graph.setdefault(dst, set())
            edge_order.append((src, dst))
        tears: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()
        for edge in self._explicit_tear_streams(connections):
            tears.append(edge)
            seen.add(edge)
        recycle_markers = ('recycle', 'ras', 'was', 'return', 'internal')
        marker_passes = (('source_unit', 'src_unit', 'source_port'), ('source_unit', 'src_unit', 'source_port', 'dest_unit', 'dest_port'))
        for marker_keys in marker_passes:
            for conn in connections:
                src = self._connection_source_unit(conn)
                dst = self._connection_dest_unit(conn)
                label = ' '.join((str(conn.get(key, '')) for key in marker_keys)).lower()
                edge = (src, dst)
                if src and dst and (edge not in seen) and any((marker in label for marker in recycle_markers)):
                    blocked_edges = set(tears)
                    blocked_edges.add(edge)
                    if not self._graph_has_path(graph, dst, src, blocked_edges=blocked_edges):
                        continue
                    tears.append((src, dst))
                    seen.add(edge)
        for src, dst in edge_order:
            edge = (src, dst)
            if edge in seen:
                continue
            blocked_edges = set(tears)
            blocked_edges.add(edge)
            if self._graph_has_path(graph, dst, src, blocked_edges=blocked_edges):
                tears.append(edge)
                seen.add(edge)
        return tears

    def classify_topology(self, units: Dict[str, Any], connections: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Classify a flowsheet into a server-owned initialization recipe."""
        unit_types = self._unit_type_map(units)
        type_values = set(unit_types.values())
        lower_types = {unit_id: unit_type.lower() for unit_id, unit_type in unit_types.items()}
        lower_type_values = set(lower_types.values())
        non_zo_type_values = {unit_type for unit_type in lower_type_values if not unit_type.endswith('zo')}
        explicit_tears = self._explicit_tear_streams(connections)
        template_name = self._runtime_metadata.get('template_name') or self._runtime_metadata.get('topology_name') or self._runtime_metadata.get('compiled_template_name')
        family = 'generic'
        recipe_name = 'generic_sequential'
        confidence = 0.5
        notes: List[str] = []
        if self._is_asm2d_recycle_flowsheet(units):
            self._roles.require_explicit_stage_index(*self._ASM2D_REACTOR_ROLE_ORDER, units=units)
            family = 'asm2d'
            recipe_name = 'asm2d_recycle'
            confidence = 1.0
            notes.append('ASM2D recycle topology detected from biological unit set')
        elif any(('reverseosmosis' in unit_type for unit_type in non_zo_type_values)):
            family = 'ro'
            recipe_name = 'pressure_membrane_ro'
            confidence = 0.95
            notes.append('RO membrane unit detected; using pressure-membrane initialization')
        elif any(('nanofiltration' in unit_type for unit_type in non_zo_type_values)):
            family = 'nf'
            recipe_name = 'pressure_membrane_nf'
            confidence = 0.95
            notes.append('NF membrane unit detected; using pressure-membrane initialization')
        elif any(('membranedistillation' in unit_type for unit_type in non_zo_type_values)):
            family = 'membrane_distillation'
            recipe_name = 'membrane_distillation'
            confidence = 0.85
            notes.append('Membrane distillation unit detected; using membrane-distillation SD recipe')
        elif any(('electrodialysis' in unit_type for unit_type in non_zo_type_values)):
            family = 'electrodialysis'
            recipe_name = 'electrodialysis'
            confidence = 0.85
            notes.append('Electrodialysis unit detected; using electrodialysis SD recipe')
        elif any(('ionexchange' in unit_type for unit_type in non_zo_type_values)):
            family = 'ix'
            recipe_name = 'ion_exchange'
            confidence = 0.9
            notes.append('Ion exchange unit detected; using IX initialization recipe')
        elif any(('crystallizer' in unit_type for unit_type in non_zo_type_values)):
            family = 'crystallizer'
            recipe_name = 'crystallizer'
            confidence = 0.9
            notes.append('Crystallizer unit detected; using crystallizer initialization recipe')
        elif {'Evaporator', 'Compressor', 'Condenser'}.issubset(type_values):
            family = 'mvc'
            recipe_name = 'mvc_evaporator'
            confidence = 0.95
            notes.append('Evaporator/compressor/condenser train detected; using MVC recipe')
        elif self._is_adm1_flowsheet(units):
            family = 'adm1'
            recipe_name = 'adm1_biological'
            confidence = 0.9
            notes.append('ADM1 property package detected; using ADM1 biological recipe')
        elif any((unit_type == 'gasmembranestage1d' for unit_type in lower_type_values)) and self._model_uses_natural_gas_pr(units):
            family = 'gas_membrane'
            recipe_name = 'gas_membrane_recycle_warm_start' if explicit_tears else 'gas_membrane_sequential'
            confidence = 0.95
            notes.append('NaturalGasPR gas membrane cascade detected; using gas-membrane initialization')
        elif any((unit_type.endswith('zo') for unit_type in lower_type_values)):
            family = 'zero_order'
            recipe_name = 'zero_order_database'
            confidence = 0.9
            notes.append('Zero-order WaterTAP unit detected; using database-backed SD recipe')
        elif any((unit_type in {'cstr', 'cstr_injection', 'aerationtank', 'clarifier', 'dewateringunit', 'thickener', 'secondaryclarifierasm', 'flatbottomcircularclarifier', 'mbrseparator'} for unit_type in lower_types.values())):
            family = 'biological'
            recipe_name = 'biological_sequential'
            confidence = 0.85
            notes.append('Biological process unit detected; using biological SD recipe')
        elif any((unit_type in {'genericseparation', 'genericdesalter'} for unit_type in lower_types.values())):
            family = 'generic_separation'
            recipe_name = 'generic_separation'
            confidence = 0.85
            notes.append('Generic separation/desalter unit detected; using separation SD recipe')
        elif any((marker in unit_type for unit_type in non_zo_type_values for marker in ('electrolyzer', 'electrocoagulation', 'electronp'))):
            family = 'electrochemical'
            recipe_name = 'electrochemical'
            confidence = 0.8
            notes.append('Electrochemical unit detected; using electrochemical SD recipe')
        elif any((marker in unit_type for unit_type in non_zo_type_values for marker in ('boronremoval', 'chemicaladdition', 'chlorination', 'coagulationflocculation', 'ozone', 'stoichiometricreactor', 'ultraviolet'))):
            family = 'chemical_treatment'
            recipe_name = 'chemical_treatment'
            confidence = 0.8
            notes.append('Chemical-treatment unit detected; using chemical-treatment SD recipe')
        elif any((unit_type == 'gac' for unit_type in non_zo_type_values)):
            family = 'adsorption'
            recipe_name = 'adsorption'
            confidence = 0.85
            notes.append('Adsorption unit detected; using adsorption SD recipe')
        elif any((unit_type in {'evaporator', 'compressor', 'condenser', 'steamheater0d', 'steamejector'} for unit_type in non_zo_type_values)):
            family = 'thermal'
            recipe_name = 'thermal_auxiliary'
            confidence = 0.8
            notes.append('Thermal unit detected; using thermal auxiliary SD recipe')
        elif any((unit_type in {'pump', 'pressurechanger', 'pressureexchanger', 'energyrecoverydevice'} for unit_type in non_zo_type_values)):
            family = 'hydraulic'
            recipe_name = 'hydraulic_support'
            confidence = 0.75
            notes.append('Hydraulic support unit detected; using hydraulic SD recipe')
        tear_streams = self._heuristic_tear_streams(connections)
        if explicit_tears:
            notes.append('Explicit tear stream marker(s): ' + ', '.join((f'{src}->{dst}' for src, dst in explicit_tears)))
        if tear_streams:
            notes.append('Heuristic tear stream guess(es): ' + ', '.join((f'{src}->{dst}' for src, dst in tear_streams)))
        return {'family': family, 'recipe_name': recipe_name, 'confidence': confidence, 'template_name': template_name, 'unit_types': unit_types, 'tear_streams': tear_streams, 'notes': notes}

    def get_initialization_order(self, units: Dict[str, Any], connections: List[Dict], tear_streams: Optional[List[Tuple[str, str]]]=None) -> List[str]:
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
        if len(units) <= 1 or not connections:
            return list(units.keys())
        return compute_initialization_order(units=units, connections=connections, tear_streams=tear_streams, model=self._model)

    def _model_uses_natural_gas_pr(self, units: Dict[str, Any]) -> bool:
        """Return True for NaturalGasPR gas-membrane models."""
        model = self._model if self._model is not None else self._flowsheet
        try:
            from watertap_engine_mcp.solver.scaler import _model_uses_natural_gas_pr
            return _model_uses_natural_gas_pr(model, units)
        except Exception:
            pass
        for unit in units.values():
            cfg = getattr(unit, 'config', None)
            for attr in ('property_package',):
                package = getattr(cfg, attr, None)
                if package is not None and 'GenericParameterBlock' in type(package).__name__ and hasattr(package, 'CH4'):
                    return True
            for side in ('feed_side', 'sweep_side'):
                side_cfg = getattr(cfg, side, None)
                if isinstance(side_cfg, dict):
                    package = side_cfg.get('property_package')
                else:
                    package = getattr(side_cfg, 'property_package', None)
                if package is not None and 'GenericParameterBlock' in type(package).__name__ and hasattr(package, 'CH4'):
                    return True
        return False

    def propagate_state(self, source_port: Any, dest_port: Any) -> bool:
        """Propagate state from source port to destination port.

        Args:
            source_port: Source port object
            dest_port: Destination port object

        Returns:
            True if successful
        """
        try:
            from idaes.core.util.initialization import propagate_state
            propagate_state(source=source_port, destination=dest_port)
            self._sync_adm1_vapor_pressure_sat_guess(dest_port)
            return True
        except ImportError:
            propagated = self._manual_propagate_state(source_port, dest_port)
            if propagated:
                self._sync_adm1_vapor_pressure_sat_guess(dest_port)
            return propagated
        except Exception:
            return False

    def _manual_propagate_state(self, source_port: Any, dest_port: Any) -> bool:
        """Manual state propagation when IDAES not available."""
        try:
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

    def _sync_adm1_vapor_pressure_sat_guess(self, port: Any) -> int:
        """Seed ADM1 vapor ``pressure_sat`` vars after port-state propagation.

        The ADM1 vapor property block keeps ``pressure_sat`` as an internal state
        variable constrained to the propagated concentration/temperature, but it is
        not exposed on the port. Propagation therefore updates ``flow_vol``,
        ``conc_mass_comp``, ``temperature``, and ``pressure`` while leaving
        ``pressure_sat`` at its package defaults. For downstream biogas Product
        blocks, that stale seed can dominate the first coupled solve. Keep the seed
        consistent with the same equations the property package enforces.
        """
        from math import exp, isfinite, log
        from pyomo.environ import value
        state = self._state_block_from_port(port)
        if state is None:
            return 0
        conc = getattr(state, 'conc_mass_comp', None)
        pressure_sat = getattr(state, 'pressure_sat', None)
        temperature = getattr(state, 'temperature', None)
        if conc is None or pressure_sat is None or temperature is None:
            return 0
        temp_K = value(temperature, exception=False)
        if temp_K is None:
            return 0
        try:
            temp_K = float(temp_K)
        except (TypeError, ValueError):
            return 0
        if not isfinite(temp_K) or temp_K <= 0.0:
            return 0
        gas_constant = 8.31446261815324
        molecular_weight_g_per_mol = {'S_ch4': 64.0, 'S_co2': 12.0, 'S_h2': 16.0}
        updated = 0
        for component, mw in molecular_weight_g_per_mol.items():
            try:
                conc_value = value(conc[component], exception=False)
                psat_var = pressure_sat[component]
            except Exception:
                continue
            if conc_value is None:
                continue
            try:
                psat_value = float(conc_value) * 1000.0 * gas_constant * temp_K / mw
            except (TypeError, ValueError):
                continue
            if not isfinite(psat_value) or psat_value < 0.0:
                continue
            try:
                psat_var.set_value(psat_value, skip_validation=True)
                updated += 1
            except Exception:
                continue
        try:
            water_var = pressure_sat['H2O']
        except Exception:
            water_var = None
        if water_var is not None:
            temp_ref = value(getattr(getattr(state, 'params', None), 'temperature_ref', None), exception=False)
            if temp_ref is None:
                temp_ref = 298.15
            try:
                temp_ref = float(temp_ref)
                water_psat = exp(log(0.0313) + 5290.0 * (1.0 / temp_ref - 1.0 / temp_K) + log(100000.0))
            except (TypeError, ValueError, ZeroDivisionError):
                water_psat = None
            if water_psat is not None and isfinite(water_psat) and (water_psat > 0.0):
                try:
                    water_var.set_value(water_psat, skip_validation=True)
                    updated += 1
                except Exception:
                    pass
        return updated

    def _state_block_from_port(self, port: Any) -> Optional[Any]:
        """Return the state block backing an IDAES Port, when it can be inferred."""
        for var_name in ('flow_vol', 'pressure', 'temperature', 'conc_mass_comp'):
            port_var = getattr(port, var_name, None)
            if port_var is None:
                continue
            candidates = []
            try:
                if hasattr(port_var, 'is_indexed') and port_var.is_indexed():
                    candidates.extend((port_var[index] for index in port_var.keys()))
                else:
                    candidates.append(port_var)
            except Exception:
                candidates.append(port_var)
            for candidate in candidates:
                try:
                    block = candidate.parent_block()
                except Exception:
                    block = None
                if block is not None and hasattr(block, 'conc_mass_comp'):
                    return block
                try:
                    component = candidate.parent_component()
                    block = component.parent_block()
                except Exception:
                    block = None
                if block is not None and hasattr(block, 'conc_mass_comp'):
                    return block
        return None

    def _connection_source_unit(self, conn: Dict[str, Any]) -> Optional[str]:
        """Return the normalized source unit id from a connection payload."""
        return conn.get('source_unit') or conn.get('src_unit')

    def _connection_dest_unit(self, conn: Dict[str, Any]) -> Optional[str]:
        """Return the normalized destination unit id from a connection payload."""
        return conn.get('dest_unit')

    def _connection_signature(self, conn: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], str, str]:
        """Return a stable signature for a connection payload."""
        return (self._connection_source_unit(conn), self._connection_dest_unit(conn), str(conn.get('source_port', '')), str(conn.get('dest_port', '')))

    def _explicit_tear_streams(self, connections: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
        """Return connection-level tear markers supplied by the session/compiler."""
        tears: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()
        for conn in connections:
            if not conn.get('tear_stream'):
                continue
            src = self._connection_source_unit(conn)
            dst = self._connection_dest_unit(conn)
            if not src or not dst:
                continue
            edge = (src, dst)
            if edge in seen:
                continue
            tears.append(edge)
            seen.add(edge)
        return tears

    def _expanded_arc_blocks(self, connections: List[Dict[str, Any]]) -> List[Any]:
        """Return expanded arc blocks for the provided unit-level connections."""
        fs = getattr(self._model, 'fs', self._model)
        blocks = []
        seen = set()
        for conn in connections:
            src_unit = self._connection_source_unit(conn)
            dst_unit = self._connection_dest_unit(conn)
            if not src_unit or not dst_unit:
                continue
            translator_id = conn.get('translator_id')
            block_names = []
            if translator_id:
                block_names.extend([f'arc_{src_unit}_to_{translator_id}_expanded', f'arc_{translator_id}_to_{dst_unit}_expanded'])
            else:
                block_names.append(f'arc_{src_unit}_{dst_unit}_expanded')
            for block_name in block_names:
                if block_name in seen:
                    continue
                block = getattr(fs, block_name, None)
                if block is None:
                    continue
                blocks.append(block)
                seen.add(block_name)
        return blocks

    def _snapshot_dest_port_fixity(self, unit: Any, incoming_connections: List[Dict[str, Any]]) -> List[Tuple[Any, bool]]:
        """Capture fixed/unfixed status for connected destination port variables.

        Some WaterTAP unit initializers temporarily or permanently fix connected
        inlet variables as part of their standalone initialization routine. In a
        flowsheet with upstream feed/source units, those fixed states must be
        released again before the arc constraints are reactivated.
        """
        snapshot: List[Tuple[Any, bool]] = []
        seen = set()
        for conn in incoming_connections:
            dest_port_name = conn.get('dest_port', 'inlet')
            dest_port = getattr(unit, dest_port_name, None)
            if dest_port is None:
                continue
            for port_var in getattr(dest_port, 'vars', {}).values():
                if not hasattr(port_var, 'is_indexed'):
                    continue
                if port_var.is_indexed():
                    for idx in port_var.keys():
                        datum = port_var[idx]
                        key = id(datum)
                        if key in seen or not hasattr(datum, 'fixed'):
                            continue
                        seen.add(key)
                        snapshot.append((datum, datum.fixed))
                else:
                    key = id(port_var)
                    if key in seen or not hasattr(port_var, 'fixed'):
                        continue
                    seen.add(key)
                    snapshot.append((port_var, port_var.fixed))
        return snapshot

    def _restore_port_fixity(self, snapshot: List[Tuple[Any, bool]]) -> None:
        """Restore the original fixity for a captured destination-port snapshot."""
        for datum, was_fixed in snapshot:
            try:
                if was_fixed and (not datum.fixed):
                    datum.fix()
                elif not was_fixed and datum.fixed:
                    datum.unfix()
            except Exception:
                continue

    def _normalize_state_arg_index(self, raw_index: Any, time_point: Any) -> Any:
        """Strip the leading time index from a port variable index."""
        if raw_index is None:
            return None
        if isinstance(raw_index, tuple):
            if raw_index and raw_index[0] == time_point:
                trimmed = raw_index[1:]
                if not trimmed:
                    return None
                if len(trimmed) == 1:
                    return trimmed[0]
                return trimmed
            return raw_index
        if raw_index == time_point:
            return None
        return raw_index

    def _serialize_port_state_args(self, unit: Any, port: Any) -> Optional[Dict[str, Any]]:
        """Convert a connected inlet port into property-package state_args."""
        from pyomo.environ import value
        try:
            time_point = unit.flowsheet().config.time.first()
        except Exception:
            time_point = None
        state_args: Dict[str, Any] = {}
        for var_name, port_var in getattr(port, 'vars', {}).items():
            if not hasattr(port_var, 'is_indexed'):
                continue
            if port_var.is_indexed():
                indexed_values: Dict[Any, Any] = {}
                scalar_value = None
                for raw_index in port_var.keys():
                    normalized_index = self._normalize_state_arg_index(raw_index, time_point)
                    datum = port_var[raw_index]
                    datum_value = value(datum, exception=False)
                    if datum_value is None:
                        continue
                    if normalized_index is None:
                        scalar_value = datum_value
                    else:
                        indexed_values[normalized_index] = datum_value
                if indexed_values:
                    state_args[var_name] = indexed_values
                elif scalar_value is not None:
                    state_args[var_name] = scalar_value
            else:
                datum_value = value(port_var, exception=False)
                if datum_value is not None:
                    state_args[var_name] = datum_value
        return state_args or None

    def _candidate_inlet_ports(self, unit: Any) -> List[Any]:
        """Return plausible inlet ports for deriving initialization state."""
        candidate_names = ['inlet', 'inlet_feed', 'cold_side_inlet', 'hot_side_inlet', 'inlet_diluate', 'inlet_concentrate', 'inlet_acidic', 'inlet_basic']
        seen = set()
        ports: List[Any] = []
        for name in candidate_names:
            if not hasattr(unit, name):
                continue
            port = getattr(unit, name)
            if not hasattr(port, 'vars'):
                continue
            seen.add(name)
            ports.append(port)
        for name in dir(unit):
            if name in seen or name.startswith('_') or 'inlet' not in name:
                continue
            port = getattr(unit, name, None)
            if hasattr(port, 'vars'):
                ports.append(port)
        return ports

    def _fixed_outlet_pressure_vars(self, unit: Any) -> List[Tuple[Any, float]]:
        """Collect fixed outlet-side pressure variables that can be relaxed during init."""
        from pyomo.environ import value
        inlet_names = {'inlet', 'inlet_feed', 'cold_side_inlet', 'hot_side_inlet', 'inlet_diluate', 'inlet_concentrate', 'inlet_acidic', 'inlet_basic'}
        relaxed: List[Tuple[Any, float]] = []
        for name in dir(unit):
            if name.startswith('_') or name in inlet_names:
                continue
            port = getattr(unit, name, None)
            if not hasattr(port, 'vars'):
                continue
            pressure_var = getattr(port, 'pressure', None)
            if pressure_var is None and isinstance(getattr(port, 'vars', None), dict):
                pressure_var = port.vars.get('pressure')
            if pressure_var is None:
                continue
            if pressure_var.is_indexed():
                for index in pressure_var.keys():
                    datum = pressure_var[index]
                    if datum.fixed:
                        relaxed.append((datum, value(datum)))
            elif pressure_var.fixed:
                relaxed.append((pressure_var, value(pressure_var)))
        return relaxed

    def _derive_state_args(self, unit: Any) -> Optional[Dict[str, Any]]:
        """Derive state_args from the unit's connected inlet ports."""
        for port in self._candidate_inlet_ports(unit):
            state_args = self._serialize_port_state_args(unit, port)
            if state_args:
                return state_args
        return None

    def _initialize_with_method(self, unit: Any, unit_id: str, method: InitMethod, state_args: Optional[Dict]=None, solver_options: Optional[Dict]=None) -> Optional[Dict[str, Any]]:
        """Invoke the requested unit initialization routine.

        Returns the anaerobic-digester warm-start diagnostics dict when the unit
        is an AD whose warm start was DEGRADED (a swallowed terminal check or a
        failed sanity gate); ``None`` otherwise. Every other unit path returns
        ``None`` implicitly.
        """
        if self._is_anaerobic_digester(unit):
            return self._initialize_anaerobic_digester(unit, unit_id, state_args, solver_options)
        kwargs = {}
        if state_args and self._initialization_method_accepts_state_args(unit, method):
            kwargs['state_args'] = state_args
        if self._is_asm2d_reactor_unit(unit_id, unit):
            kwargs.setdefault('solver', 'ipopt-watertap')
            optarg = {'bound_push': 1e-08}
            configured_optarg = kwargs.get('optarg')
            if isinstance(configured_optarg, dict):
                optarg.update(configured_optarg)
            kwargs['optarg'] = optarg
            try:
                import idaes.logger as idaeslog
                kwargs.setdefault('outlvl', idaeslog.ERROR)
            except Exception:
                pass
        if solver_options:
            solver_optarg = solver_options.get('optarg')
            if isinstance(solver_optarg, dict) and isinstance(kwargs.get('optarg'), dict):
                merged_optarg = dict(kwargs['optarg'])
                merged_optarg.update(solver_optarg)
                kwargs.update({k: v for k, v in solver_options.items() if k != 'optarg'})
                kwargs['optarg'] = merged_optarg
            else:
                kwargs.update(solver_options)
        mvc_condenser_temp_fixes = self._fix_mvc_condenser_outlet_temperature(unit)
        mvc_brine_spec = None
        if hasattr(unit, 'properties_brine') and hasattr(unit, 'properties_feed'):
            brine = unit.properties_brine[0]
            fractions = getattr(brine, 'mass_frac_phase_comp', {})
            endpoint = fractions['Liq', 'TDS'] if ('Liq', 'TDS') in fractions else None
            if endpoint is not None and endpoint.fixed and (0 < endpoint.value < 1):
                water = brine.flow_mass_phase_comp['Liq', 'H2O']
                salt = unit.properties_feed[0].flow_mass_phase_comp['Liq', 'TDS']
                if not water.fixed:
                    mvc_brine_spec = (endpoint, endpoint.value, water)
                    water.fix(salt.value * (1 - endpoint.value) / endpoint.value)
                    endpoint.unfix()
        try:
            if method == InitMethod.INITIALIZE_BUILD:
                if hasattr(unit, 'initialize_build'):
                    unit.initialize_build(**kwargs)
                else:
                    unit.initialize(**kwargs)
            elif method == InitMethod.INITIALIZE:
                unit.initialize(**kwargs)
            elif method == InitMethod.CUSTOM:
                if getattr(unit, 'default_initializer', None) is None:
                    raise AttributeError(f'{unit_id} does not expose a default_initializer for custom initialization')
                initializer = unit.default_initializer()
                initializer.initialize(unit)
        finally:
            if mvc_brine_spec is not None:
                endpoint, target, water = mvc_brine_spec
                water.unfix()
                endpoint.fix(target)
            for datum in mvc_condenser_temp_fixes:
                try:
                    datum.unfix()
                except Exception:
                    continue

    def _paired_mvc_evaporator(self, block: Any) -> Optional[Any]:
        """Return the evaporator thermally coupled to one MVC condenser.

        When ``thermal_couplings`` is set, look up the specific evaporator paired
        with this condenser. The fallback scan is valid ONLY when the flowsheet
        carries exactly one evaporator (single-evaporator MVC); with several
        (MEE), "the first thing dir() returns" is a wrong answer wearing a right
        one, so multiple candidates now raise instead of proceeding on a warning
        (solver-workaround audit, 2026-08-24).
        """
        flowsheet = block.flowsheet()
        for evap_id, cond_id in self._thermal_couplings.items():
            if hasattr(flowsheet, cond_id) and getattr(flowsheet, cond_id) is block:
                candidate = getattr(flowsheet, evap_id, None)
                if candidate is not None and hasattr(candidate, 'heat_transfer'):
                    return candidate
        candidates = []
        for attr_name in dir(flowsheet):
            if attr_name.startswith('_'):
                continue
            candidate = getattr(flowsheet, attr_name, None)
            if hasattr(candidate, 'connect_to_condenser') and hasattr(candidate, 'heat_transfer'):
                candidates.append(candidate)
        if len(candidates) > 1:
            from idaes.core.util.exceptions import ConfigurationError
            raise ConfigurationError('Multiple evaporators found while pairing an MVC condenser and no thermal_couplings mapping was provided — the single-evaporator fallback would silently pick an arbitrary one (wrong for MEE). Provide thermal_couplings={evaporator_id: condenser_id}.')
        return candidates[0] if candidates else None

    def _fix_mvc_condenser_outlet_temperature(self, unit: Any) -> List[Any]:
        """Transiently fix a WaterTAP MVC condenser's outlet temperature for init.

        Returns the fixed temperature var data (empty when not applicable) so the
        caller can unfix after the unit initialization returns. The target is the
        paired evaporator's brine temperature: for any compressor pressure ratio
        above 1 it lies strictly between the water package's 273.15 K temperature
        floor and the saturation temperature at the condensing pressure, so the
        outlet-spec init solve (condensed liquid, heat duty free) is always
        feasible — unlike the fixed-heat guess, whose feasibility window narrows
        with feed sensible load and compressor work.
        """
        try:
            mro = type(unit).__mro__
        except Exception:
            return []
        in_mvc_family = any(('watertap.unit_models.mvc' in (getattr(klass, '__module__', '') or '') for klass in mro))
        if not in_mvc_family or 'condenser' not in type(unit).__name__.lower():
            return []
        evaporator = self._paired_mvc_evaporator(unit)
        if evaporator is None:
            return []
        from pyomo.environ import value
        target = None
        properties_brine = getattr(evaporator, 'properties_brine', None)
        if properties_brine is not None:
            for t in properties_brine:
                try:
                    target = float(value(properties_brine[t].temperature))
                except Exception:
                    target = None
                break
        if target is None or not 200.0 < target < 500.0:
            return []
        properties_out = getattr(getattr(unit, 'control_volume', None), 'properties_out', None)
        if properties_out is None:
            return []
        fixed: List[Any] = []
        for t in properties_out:
            temperature = getattr(properties_out[t], 'temperature', None)
            if temperature is None or temperature.fixed:
                continue
            try:
                temperature.fix(target)
                fixed.append(temperature)
            except Exception:
                continue
        return fixed

    def _initialization_method_accepts_state_args(self, unit: Any, method: InitMethod) -> bool:
        """Return True when a unit initializer accepts ``state_args``.

        Generic IDAES ``Separator.initialize_build`` does not accept state_args; the
        retry path must not convert a useful initialization failure into a signature
        error. The ADM1 digester is handled by a dedicated adapter that maps
        state_args to ``liquid_state_args``.
        """
        if self._is_anaerobic_digester(unit):
            return True
        class_name = type(unit).__name__.lower()
        if 'scalarseparator' in class_name:
            return False
        target = None
        if method == InitMethod.INITIALIZE_BUILD:
            target = getattr(unit, 'initialize_build', None) or getattr(unit, 'initialize', None)
        elif method == InitMethod.INITIALIZE:
            target = getattr(unit, 'initialize', None)
        if target is None:
            return False
        try:
            import inspect
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            return False
        for parameter in signature.parameters.values():
            if parameter.kind == parameter.VAR_KEYWORD:
                return True
            if parameter.name == 'state_args':
                return True
        return False

    def _initialize_with_relaxed_outlet_pressure(self, unit: Any, unit_id: str, method: InitMethod, state_args: Optional[Dict]=None, solver_options: Optional[Dict]=None) -> int:
        """Retry initialization after temporarily unfixing outlet-side pressure specs."""
        relaxed = self._fixed_outlet_pressure_vars(unit)
        if not relaxed:
            raise RuntimeError('No fixed outlet pressure variables available to relax')
        for datum, _ in relaxed:
            datum.unfix()
        try:
            self._initialize_with_method(unit, unit_id, method, state_args, solver_options)
        finally:
            for datum, original_value in relaxed:
                datum.fix(original_value)
        return len(relaxed)

    def initialize_unit(self, unit: Any, unit_id: str, method: InitMethod=InitMethod.INITIALIZE, state_args: Optional[Dict]=None, solver_options: Optional[Dict]=None) -> InitializationResult:
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
                return InitializationResult(unit_id=unit_id, status=InitStatus.SKIPPED, dof_before=dof_before, dof_after=dof_before, message='No initialization required')
            retry_suffix = ''
            retry_state_args = state_args
            init_diagnostics: Optional[Dict[str, Any]] = None
            try:
                init_diagnostics = self._initialize_with_method(unit, unit_id, method, state_args, solver_options)
            except Exception as init_error:
                if state_args is None and self._initialization_method_accepts_state_args(unit, method):
                    retry_state_args = self._derive_state_args(unit)
                    if retry_state_args:
                        try:
                            init_diagnostics = self._initialize_with_method(unit, unit_id, method, retry_state_args, solver_options)
                            retry_suffix = ' after retrying with derived inlet state'
                        except Exception as retry_error:
                            init_error = retry_error
                    else:
                        retry_state_args = None
                if retry_suffix == '':
                    if self._is_asm2d_reactor_unit(unit_id, unit) and self._asm2d_unit_has_usable_state(unit):
                        dof_after = resolver.get_dof(unit)
                        return InitializationResult(unit_id=unit_id, status=InitStatus.DEGRADED, dof_before=dof_before, dof_after=dof_after, solve_status='near_feasible', message='ASM2D reactor standalone initialization returned locally infeasible; finite state accepted as a flowsheet IPOPT warm start')
                    if retry_state_args is None:
                        raise init_error
                    relaxed_pressure_vars = self._fixed_outlet_pressure_vars(unit)
                    if not relaxed_pressure_vars:
                        raise init_error
                    relaxed_count = self._initialize_with_relaxed_outlet_pressure(unit, unit_id, method, retry_state_args, solver_options)
                    retry_suffix = f' after retrying with {relaxed_count} relaxed outlet pressure spec{('s' if relaxed_count != 1 else '')}'
            dof_after = resolver.get_dof(unit)
            if isinstance(init_diagnostics, dict) and init_diagnostics.get('degraded'):
                failed = init_diagnostics.get('failed_gates') or []
                terminal = init_diagnostics.get('init_terminal_check', 'passed')
                reason_bits = []
                if terminal != 'passed':
                    reason_bits.append(f'terminal check {terminal}')
                if failed:
                    reason_bits.append(f'failed gates: {', '.join(failed)}')
                reason = '; '.join(reason_bits) or 'imperfect warm start'
                return InitializationResult(unit_id=unit_id, status=InitStatus.DEGRADED, dof_before=dof_before, dof_after=dof_after, solve_status='near_feasible', message=f'Initialization degraded ({reason}){retry_suffix}; warm start surfaced for the flowsheet solve, not trusted as clean', diagnostics=init_diagnostics)
            return InitializationResult(unit_id=unit_id, status=InitStatus.SUCCESS, dof_before=dof_before, dof_after=dof_after, solve_status='converged', message=f'Initialization successful{retry_suffix}')
        except Exception as e:
            return InitializationResult(unit_id=unit_id, status=InitStatus.FAILED, dof_before=dof_before, dof_after=-1, message=f'Initialization failed: {str(e)}')

    def _is_asm2d_recycle_flowsheet(self, units: Dict[str, Any]) -> bool:
        """Return True for the ASM2D MLE/MBR recycle templates."""
        common_roles = (R.FEED, R.MIXER, R.PRE_ANOXIC, R.AEROBIC, R.SLUDGE_SPLITTER)
        if not all((self._rhas(units, role) for role in common_roles)):
            return False
        separator_role = self._asm2d_separator_role(units)
        if separator_role is None:
            return False
        if not self._rhas(units, R.INTERNAL_RECYCLE_SPLITTER):
            if separator_role is not R.MBR_SEPARATOR or not self._rhas(units, R.RAS_PRESSURE):
                return False
        reactors: List[Any] = []
        for role in (R.PRE_ANOXIC, R.AEROBIC, R.POST_ANOXIC):
            reactors.extend(self._roles.units_for(role, units))
        if not reactors:
            return False
        for reactor in reactors:
            package = getattr(getattr(reactor, 'config', None), 'property_package', None)
            if package is None or not is_asm2d_property_block(package):
                return False
        return True

    @staticmethod
    def _is_anaerobic_digester(unit: Any) -> bool:
        """Return True for a WaterTAP anaerobic digester (AD) unit block.

        The AD couples a reactive liquid control volume to a single vapor state
        block and exposes the legacy ``initialize_build(liquid_state_args=...,
        vapor_state_args=...)`` signature instead of the generic
        ``initialize(state_args=...)``. Keyed on the two-phase structure (liquid
        + vapor phase blocks) rather than the unit name so a project-named
        role-tagged digester is dispatched identically to the canonical ``AD``.
        """
        if 'addata' in type(unit).__name__.lower():
            return True
        return hasattr(unit, 'liquid_phase') and hasattr(unit, 'vapor_phase') and hasattr(unit, 'initialize_build')

    def _initialize_anaerobic_digester(self, unit: Any, unit_id: str, state_args: Optional[Dict]=None, solver_options: Optional[Dict]=None) -> Optional[Dict[str, Any]]:
        """Initialize a WaterTAP anaerobic digester (AD) two-phase unit.

        The AD's ``initialize_build`` enforces ``degrees_of_freedom(self) == 0`` in
        isolation before doing anything. In an assembled flowsheet the inlet is
        only propagated (not fixed) and the liquid-outlet temperature is left to
        the global energy structure, so the unit is short by the inlet state +
        the heat-duty DOF. We transiently fix the propagated inlet state and pin
        the liquid-outlet temperature to the inlet temperature (the canonical
        digester spec that absorbs the heat duty), seed the methanogenic basin and
        a CH4-rich headspace, run the unit's own two-phase routine, then release
        every transient fix so the assembled flowsheet stays DOF=0.

        The AD's strict terminal check can flag a usable near-feasible warm start,
        so an ``InitializationError`` is caught rather than aborting the whole
        flowsheet init. But a swallowed error must NOT be reported as a clean
        success: this returns a diagnostics dict describing the terminal-check
        outcome AND the result of the warm-start sanity gates
        (:meth:`_digester_warm_start_gates`). ``None`` means a clean init that
        passed every gate; a non-empty dict with ``degraded=True`` means the
        caller must surface the imperfection (DEGRADED status) so the flowsheet
        IPOPT solve proceeds from a state that is known-imperfect, not silently
        trusted.
        """
        from pyomo.environ import value
        import idaes.logger as idaeslog
        from idaes.core.util.exceptions import InitializationError
        fixed_inlet: List[Any] = []
        fixed_outlet_temp: List[Any] = []
        inlet_temperature: Optional[float] = None
        for t in unit.liquid_phase.properties_in:
            pin = unit.liquid_phase.properties_in[t]
            for state_var in pin.define_state_vars().values():
                indexes = list(state_var.keys()) if state_var.is_indexed() else [None]
                for idx in indexes:
                    datum = state_var[idx] if idx is not None else state_var
                    if not datum.fixed:
                        datum.fix()
                        fixed_inlet.append(datum)
            try:
                inlet_temperature = value(pin.temperature)
            except Exception:
                inlet_temperature = None
        for t in unit.liquid_phase.properties_out:
            outlet_temp = unit.liquid_phase.properties_out[t].temperature
            if not outlet_temp.fixed:
                try:
                    setpoint = inlet_temperature if inlet_temperature is not None else value(outlet_temp)
                    outlet_temp.fix(setpoint)
                    fixed_outlet_temp.append(outlet_temp)
                except Exception:
                    continue
        seeded_units = getattr(self, '_adm1_seeded_units', None)
        if seeded_units is None:
            seeded_units = set()
            self._adm1_seeded_units = seeded_units
        if unit_id not in seeded_units:
            is_acidogenic = False
            try:
                is_acidogenic = self._roles.role_of(unit_id) == R.ANAEROBIC_ACIDOGENIC.value
            except Exception:
                is_acidogenic = False
            if is_acidogenic and (not self._adm1_should_use_acidogenic_seed(unit)):
                is_acidogenic = False
            self._seed_adm1_digester(unit, acidogenic=is_acidogenic)
            seeded_units.add(unit_id)
        suspended_heat_balance = self._suspend_adm1_external_heat_balance(unit)
        terminal_check = 'passed'
        terminal_error: Optional[str] = None
        try:
            ad_kwargs: Dict[str, Any] = {'outlvl': idaeslog.NOTSET}
            if state_args:
                ad_kwargs['liquid_state_args'] = state_args
            if solver_options:
                ad_kwargs['optarg'] = dict(solver_options)
            unit.initialize_build(**ad_kwargs)
        except (InitializationError, RuntimeError, ValueError) as exc:
            terminal_check = 'near_feasible'
            terminal_error = str(exc)
        finally:
            for datum in fixed_inlet:
                try:
                    datum.unfix()
                except Exception:
                    continue
            for datum in fixed_outlet_temp:
                try:
                    datum.unfix()
                except Exception:
                    continue
            self._restore_adm1_external_heat_balance(unit, suspended_heat_balance)
        gates = self._digester_warm_start_gates(unit)
        gates_passed = all((g.get('passed', False) for g in gates.values()))
        degraded = terminal_check != 'passed' or not gates_passed
        if not degraded:
            return None
        failed_gates = sorted((name for name, g in gates.items() if not g.get('passed', False)))
        return {'unit_id': unit_id, 'degraded': True, 'init_terminal_check': terminal_check, 'init_terminal_error': terminal_error, 'warm_start_gates': gates, 'warm_start_gates_passed': gates_passed, 'failed_gates': failed_gates}

    def _suspend_adm1_external_heat_balance(self, unit: Any) -> Optional[Dict[str, Any]]:
        """Temporarily restore the native adiabatic AD energy closure for init.

        When a heat-duty-controlled temperature target has swapped this AD's
        ``unit_enthalpy_balance`` for the ``adm1_external_heat_vapor_transfer``
        relation (A5-PR1), the AD's own ``initialize_build`` warm-starts poorly on
        the swapped structure. Re-activate the native ``unit_enthalpy_balance`` and
        deactivate the vapor-transfer replacement so init runs on the structure the
        AD routine is tuned for; :meth:`_restore_adm1_external_heat_balance` puts the
        swap back afterward. Returns the toggled state (or ``None`` when this AD
        carries no swap).
        """
        replacement = getattr(unit, 'adm1_external_heat_vapor_transfer', None)
        if replacement is None:
            return None
        try:
            if replacement.active:
                replacement.deactivate()
        except Exception:
            return None
        reactivated: List[Any] = []
        unit_enthalpy_balance = getattr(unit, 'unit_enthalpy_balance', None)
        if unit_enthalpy_balance is not None:
            try:
                for data in unit_enthalpy_balance.values():
                    if not data.active:
                        data.activate()
                        reactivated.append(data)
            except Exception:
                pass
        return {'replacement': replacement, 'reactivated': reactivated}

    def _adm1_outlet_temperature_target(self, unit: Any, t: Any) -> Optional[float]:
        """Return the heat-duty setpoint (K) pinning this AD's liquid-outlet temp.

        Read off the live ``adm1_temperature_target`` model constraint (the
        authoritative source for the swapped energy structure's setpoint): the
        constraint that holds ``unit.liquid_phase.properties_out[t].temperature ==
        target_param``. Returns ``None`` if no such target is found (e.g. an
        adiabatic UASB with no target), so the caller falls back to the plain
        ``enthalpy_transfer = -H_vap`` seed.
        """
        from pyomo.environ import Constraint, value
        from pyomo.core.expr.visitor import identify_variables
        try:
            from pyomo.core.expr import identify_mutable_parameters
        except Exception:
            return None
        if self._flowsheet is None:
            return None
        try:
            controlled_temp = unit.liquid_phase.properties_out[t].temperature
        except Exception:
            return None
        for con in self._flowsheet.component_data_objects(Constraint, active=True, descend_into=True):
            name = getattr(con, 'name', '') or ''
            if 'adm1_temperature_target' not in name:
                continue
            try:
                variables = list(identify_variables(con.body, include_fixed=True))
            except Exception:
                continue
            if not any((var is controlled_temp for var in variables)):
                continue
            try:
                params = list(identify_mutable_parameters(con.expr))
            except Exception:
                params = []
            for param in params:
                raw = value(param, exception=False)
                if raw is None:
                    continue
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return None

    def _restore_adm1_external_heat_balance(self, unit: Any, state: Optional[Dict[str, Any]]) -> None:
        """Re-apply the heat-duty energy swap after a native-structure AD init.

        Deactivate the natively-reactivated ``unit_enthalpy_balance`` rows,
        re-activate the ``adm1_external_heat_vapor_transfer`` relation, and seed the
        swapped energy rows from a point CONSISTENT WITH THE TEMPERATURE SETPOINT so
        the flowsheet solve starts feasible. The native-structure init above pinned
        the liquid-outlet T to the INLET T; for a setpoint above (or below) the feed
        that leaves ``heat[0]`` and the outlet enthalpy inconsistent with the
        ``adm1_temperature_target`` constraint. So for each time point:

          1. seed ``enthalpy_transfer = -H_vap`` (the vapor-transfer replacement
             ``enthalpy_transfer + H_vap == 0``);
          2. seed the controlled outlet temperature to the TARGET setpoint;
          3. seed ``heat[0] = H_out + H_vap - H_in`` (the IDAES liquid enthalpy
             balance ``H_in - H_out + heat + enthalpy_transfer = 0`` evaluated with
             the target outlet enthalpy), so ``heat[0]`` is the consistent thermal
             duty from the very first residual rather than the native adiabatic slack.

        When the AD carries no temperature target (adiabatic UASB) the setpoint
        lookup returns ``None`` and only step (1) runs -- the previous behaviour.
        """
        if not state:
            return
        from pyomo.environ import value
        for data in state.get('reactivated', []):
            try:
                data.deactivate()
            except Exception:
                continue
        replacement = state.get('replacement')
        try:
            if replacement is not None and (not replacement.active):
                replacement.activate()
        except Exception:
            pass
        try:
            liquid_in = unit.liquid_phase.properties_in
            liquid_out = unit.liquid_phase.properties_out
            enthalpy_transfer = unit.liquid_phase.enthalpy_transfer
            heat = unit.liquid_phase.heat
        except Exception:
            return
        for t in enthalpy_transfer:
            hvap = value(unit.vapor_phase[t].get_enthalpy_flow_terms('Vap'), exception=False)
            if hvap is not None:
                try:
                    enthalpy_transfer[t].set_value(-float(hvap))
                except Exception:
                    pass
            target_K = self._adm1_outlet_temperature_target(unit, t)
            if target_K is not None:
                try:
                    liquid_out[t].temperature.set_value(target_K)
                except Exception:
                    pass
            if hvap is None:
                continue
            h_in = value(liquid_in[t].get_enthalpy_flow_terms('Liq'), exception=False)
            h_out = value(liquid_out[t].get_enthalpy_flow_terms('Liq'), exception=False)
            if h_in is None or h_out is None:
                continue
            try:
                heat[t].set_value(float(h_out) + float(hvap) - float(h_in))
            except Exception:
                pass

    def _digester_warm_start_gates(self, unit: Any) -> Dict[str, Dict[str, Any]]:
        """Sanity-gate the AD warm start before the flowsheet solve proceeds.

        ADM1 has multiple steady states; a swallowed terminal check (or a bad
        seed) can leave the digester warm start in a physically implausible basin
        (NaN/Inf state, runaway VFA, a soured pH, a collapsed/negative headspace,
        or a wildly non-closed biogas). Each gate returns ``{passed, detail}``;
        the AD init aggregates them so a warm start that fails any gate is flagged
        DEGRADED rather than presented as a clean success. Thresholds are
        deliberately loose -- these catch a *broken* warm start, not a slightly
        off one (the flowsheet IPOPT solve does the real tightening).
        """
        from math import isfinite
        from pyomo.environ import Constraint, value
        gates: Dict[str, Dict[str, Any]] = {}

        def _val(obj: Any) -> Optional[float]:
            try:
                raw = value(obj, exception=False)
            except Exception:
                return None
            if raw is None:
                return None
            try:
                raw = float(raw)
            except (TypeError, ValueError):
                return None
            return raw if isfinite(raw) else None
        nonfinite: List[str] = []
        checked = 0
        for t in getattr(unit.liquid_phase, 'properties_out', {}):
            conc = getattr(unit.liquid_phase.properties_out[t], 'conc_mass_comp', None)
            if conc is not None:
                for comp in conc:
                    checked += 1
                    if _val(conc[comp]) is None:
                        nonfinite.append(f'liquid.conc_mass_comp[{comp}]')
        for t in getattr(unit, 'vapor_phase', {}):
            conc = getattr(unit.vapor_phase[t], 'conc_mass_comp', None)
            if conc is not None:
                for comp in conc:
                    checked += 1
                    if _val(conc[comp]) is None:
                        nonfinite.append(f'vapor.conc_mass_comp[{comp}]')
        gates['finite_state'] = {'passed': not nonfinite and checked > 0, 'detail': {'nonfinite_vars': nonfinite[:10], 'checked': checked}}
        worst_residual = 0.0
        worst_name: Optional[str] = None
        residual_finite = True
        for con in unit.component_data_objects(Constraint, active=True, descend_into=True):
            body = _val(getattr(con, 'body', None))
            if body is None:
                residual_finite = False
                worst_name = con.name
                break
            lo = _val(con.lower) if con.lower is not None else body
            up = _val(con.upper) if con.upper is not None else body
            if lo is None or up is None:
                continue
            residual = max(0.0, lo - body, body - up)
            if residual > worst_residual:
                worst_residual, worst_name = (residual, con.name)
        gates['active_residuals_bounded'] = {'passed': residual_finite and worst_residual < 1000000.0, 'detail': {'worst_residual': worst_residual, 'worst_constraint': worst_name}}
        ph_value: Optional[float] = None
        for t in getattr(unit.liquid_phase, 'reactions', {}):
            ph_value = _val(getattr(unit.liquid_phase.reactions[t], 'pH', None))
            break
        gates['ph_plausible'] = {'passed': ph_value is None or 3.0 < ph_value < 12.0, 'detail': {'pH': ph_value}}
        total_vfa = 0.0
        vfa_seen = False
        for t in getattr(unit.liquid_phase, 'properties_out', {}):
            conc = getattr(unit.liquid_phase.properties_out[t], 'conc_mass_comp', None)
            if conc is None:
                continue
            for comp in ('S_ac', 'S_pro', 'S_bu', 'S_va'):
                if comp in conc:
                    v = _val(conc[comp])
                    if v is not None:
                        total_vfa += max(v, 0.0)
                        vfa_seen = True
            break
        gates['vfa_not_runaway'] = {'passed': not vfa_seen or total_vfa < 50.0, 'detail': {'total_vfa_kg_cod_m3': total_vfa if vfa_seen else None}}
        head_pressure: Optional[float] = None
        for t in getattr(unit, 'vapor_phase', {}):
            head_pressure = _val(getattr(unit.vapor_phase[t], 'pressure', None))
            break
        gates['headspace_pressure_positive'] = {'passed': head_pressure is None or head_pressure > 0.0, 'detail': {'headspace_pressure_pa': head_pressure}}
        partial_sum = 0.0
        psat_seen = False
        for t in getattr(unit, 'vapor_phase', {}):
            psat = getattr(unit.vapor_phase[t], 'pressure_sat', None)
            if psat is not None:
                for j in psat:
                    v = _val(psat[j])
                    if v is not None:
                        partial_sum += v
                        psat_seen = True
            break
        closure_ok = True
        if psat_seen and head_pressure and (head_pressure > 0.0):
            ratio = partial_sum / head_pressure
            closure_ok = partial_sum > 0.0 and 0.1 < ratio < 10.0
        gates['biogas_closure_sane'] = {'passed': not psat_seen or closure_ok, 'detail': {'partial_pressure_sum_pa': partial_sum if psat_seen else None, 'headspace_pressure_pa': head_pressure}}
        return gates

    @staticmethod
    def _adm1_methanogenic_liquid_seed() -> Dict[str, float]:
        return {'S_su': 0.03, 'S_aa': 0.02, 'S_fa': 0.02, 'S_va': 0.02, 'S_bu': 0.03, 'S_pro': 0.03, 'S_ac': 0.08, 'S_ch4': 0.05, 'S_h2': 1e-07, 'X_su': 0.3, 'X_aa': 0.3, 'X_fa': 0.2, 'X_c4': 0.2, 'X_pro': 0.2, 'X_ac': 0.5, 'X_h2': 0.2}

    @classmethod
    def _adm1_methanogenic_recycle_seed(cls) -> Dict[str, float]:
        seed = dict(cls._adm1_methanogenic_liquid_seed())
        seed.update({'X_ch': 0.02, 'X_pr': 0.02, 'X_li': 0.02})
        return seed

    def _seed_adm1_digester(self, unit: Any, acidogenic: bool=False) -> None:
        """Seed an AD with a basin-appropriate headspace + dissolved-gas guess.

        Methanogenic basin (default): a CH4-rich vapor (high ``S_ch4`` partial
        pressure, low ``S_h2``) and a low dissolved ``S_h2`` in the liquid outlet,
        steering the stiff acid/base + gas-transfer digester toward the healthy
        CH4-rich steady state rather than a soured / H2-dominated one.

        Acidogenic basin (``acidogenic=True``, the upstream stage of a phase-
        separated two-stage train): a VFA-rich, lower-pH, CO2/H2-dominated state —
        elevated soluble VFAs (acetate / propionate / butyrate / valerate) + sugars,
        seeded acidogen biomass (``X_su`` / ``X_aa``), a CO2/H2-rich and CH4-lean
        headspace, and a seeded acidic ``pH`` — so the acid reactor warm-starts in
        the fermentative basin instead of converging toward methanogenesis.

        Component names are guarded so the seed is a safe no-op on any digester
        whose package lacks a given species.
        """
        from pyomo.environ import value
        if acidogenic:
            vapor_seed = {'S_ch4': 0.05, 'S_co2': 1.0, 'S_h2': 0.005}
            liquid_seed = {'S_su': 2.0, 'S_aa': 0.5, 'S_fa': 0.3, 'S_va': 0.6, 'S_bu': 1.2, 'S_pro': 1.5, 'S_ac': 3.0, 'S_ch4': 1e-06, 'S_h2': 5e-05, 'X_su': 0.6, 'X_aa': 0.4}
        else:
            vapor_seed = {'S_ch4': 1.6256, 'S_co2': 0.2, 'S_h2': 1e-05}
            liquid_seed = self._adm1_methanogenic_liquid_seed()
        for t in getattr(unit, 'vapor_phase', {}):
            vapor = unit.vapor_phase[t]
            conc = getattr(vapor, 'conc_mass_comp', None)
            if conc is None:
                continue
            for comp, guess in vapor_seed.items():
                if comp in conc:
                    try:
                        conc[comp].set_value(guess)
                    except Exception:
                        continue
            try:
                vapor.flow_vol.set_value(max(value(vapor.flow_vol), 0.001))
            except Exception:
                pass
        for t in unit.liquid_phase.properties_out:
            conc = getattr(unit.liquid_phase.properties_out[t], 'conc_mass_comp', None)
            if conc is None:
                continue
            for comp, guess in liquid_seed.items():
                if comp in conc:
                    try:
                        conc[comp].set_value(guess)
                    except Exception:
                        continue
        self._seed_adm1_hydraulic_retention_time(unit)
        if acidogenic:
            for t in getattr(unit.liquid_phase, 'reactions', {}):
                ph = getattr(unit.liquid_phase.reactions[t], 'pH', None)
                if ph is not None:
                    try:
                        ph.set_value(5.6)
                    except Exception:
                        pass
                break

    def _seed_adm1_hydraulic_retention_time(self, unit: Any) -> None:
        """Set AD HRT guesses from the current initialized inlet flow and volume."""
        from pyomo.environ import value
        hrt = getattr(unit, 'hydraulic_retention_time', None)
        volume = getattr(unit, 'volume_AD', None)
        liquid_phase = getattr(unit, 'liquid_phase', None)
        properties_in = getattr(liquid_phase, 'properties_in', None)
        if hrt is None or volume is None or properties_in is None:
            return
        for t in properties_in:
            try:
                flow_value = value(properties_in[t].flow_vol, exception=False)
                volume_value = value(volume[t], exception=False)
                hrt_var = hrt[t]
            except Exception:
                continue
            if flow_value is None or volume_value is None or flow_value <= 0:
                continue
            try:
                hrt_var.set_value(float(volume_value) / float(flow_value), skip_validation=True)
            except Exception:
                continue

    def _adm1_should_use_acidogenic_seed(self, unit: Any) -> bool:
        """Return True for genuinely short-HRT acid stages.

        The two-stage template's packaged default intentionally sizes the upstream
        acidogenic role at the methanogenic reactor scale so the cascade solves in a
        healthy, lightly staged basin. Only an explicitly short-HRT acid reactor
        should receive the sour/VFA-rich acidogenic seed; otherwise that seed steers
        the default corrected-feed cascade into a low-gas local basin.
        """
        from pyomo.environ import value
        try:
            acid_volume = value(unit.volume_liquid[0], exception=False)
        except Exception:
            acid_volume = None
        try:
            acid_volume = float(acid_volume)
        except (TypeError, ValueError):
            acid_volume = None
        design_parameters = self._runtime_metadata.get('design_parameters')
        meth_volume = None
        if isinstance(design_parameters, dict):
            raw = design_parameters.get('digester_volume_liquid_m3')
            try:
                meth_volume = float(raw)
            except (TypeError, ValueError):
                meth_volume = None
        if acid_volume is None or meth_volume is None or meth_volume <= 0.0:
            return True
        return acid_volume < 0.5 * meth_volume

    def _is_adm1_flowsheet(self, units: Dict[str, Any]) -> bool:
        """Return True when any biological unit is backed by an ADM1 package."""
        for unit_id, unit in units.items():
            unit_type = self._unit_type(unit_id, unit)
            if 'adm1' in unit_type.lower() or unit_type == 'AD':
                return True
            package = getattr(getattr(unit, 'config', None), 'property_package', None)
            if package is not None and 'ADM1' in type(package).__name__:
                return True
        return False

    def _asm2d_scalar_value(self, component: Any, *indexes: Any, default: float) -> float:
        """Read a Pyomo scalar/indexed value with fallbacks."""
        from pyomo.environ import value
        if component is None:
            return default
        if hasattr(component, 'is_indexed') and component.is_indexed():
            for index in indexes:
                try:
                    raw_value = value(component[index], exception=False)
                except Exception:
                    raw_value = None
                if raw_value is not None:
                    return float(raw_value)
            return default
        try:
            raw_value = value(component, exception=False)
        except Exception:
            raw_value = None
        return float(raw_value) if raw_value is not None else default

    def _asm2d_port_component_names(self, port: Any) -> Set[str]:
        """Return positive component ids available on an ASM2d-family port."""
        conc = getattr(port, 'conc_mass_comp', None)
        if conc is None:
            return set()
        names: Set[str] = set()
        try:
            indexes = list(conc)
        except Exception:
            indexes = []
        for index in indexes:
            if isinstance(index, tuple):
                for item in reversed(index):
                    if isinstance(item, str):
                        names.add(item)
                        break
            elif isinstance(index, str):
                names.add(index)
        return names

    def _asm2d_split_value(self, unit: Any, outlet: str, component: Optional[str]=None, *, default: float) -> float:
        """Read Separator split_fraction entries across total/component bases."""
        split_fraction = getattr(unit, 'split_fraction', None)
        indexes = []
        if component is not None:
            indexes.extend([(0, outlet, component), (0.0, outlet, component)])
        indexes.extend([(0, outlet), (0.0, outlet), outlet])
        return self._asm2d_scalar_value(split_fraction, *indexes, default=default)

    def _asm2d_recycle_ratio_value(self, ratio_name: str) -> Optional[float]:
        """Read an ASM2D recycle-ratio value from model Vars or template metadata."""
        ratio_token = ratio_name.lower()
        if self._model is not None:
            from pyomo.environ import Var, value
            for var in self._model.component_data_objects(Var, descend_into=True):
                name = str(var).lower()
                if 'process_var_asm2d_' not in name or ratio_token not in name:
                    continue
                raw_value = value(var, exception=False)
                if raw_value is None:
                    return None
                try:
                    return float(raw_value)
                except (TypeError, ValueError):
                    return None
        design_parameters = self._runtime_metadata.get('design_parameters')
        if not isinstance(design_parameters, dict):
            return None
        recycle_parameters = design_parameters.get('recycle_ratios')
        if not isinstance(recycle_parameters, dict):
            return None
        key = 'ras' if 'ras' in ratio_token else 'mlr' if 'mlr' in ratio_token else None
        if key is None:
            return None
        raw_value = recycle_parameters.get(key)
        if raw_value is None:
            topology = recycle_parameters.get('topology')
            if isinstance(topology, dict):
                entry = topology.get(ratio_name) or topology.get(f'{key}_recycle_ratio')
                if isinstance(entry, dict):
                    raw_value = entry.get('target_ratio')
        if raw_value is None:
            return None
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return None

    def _asm2d_recycle_ratio_var(self, ratio_name: str) -> Optional[Any]:
        """Return the model Var used for a free ASM2D recycle ratio, if present."""
        if self._model is None:
            return None
        from pyomo.environ import Var
        ratio_token = ratio_name.lower()
        for var in self._model.component_data_objects(Var, descend_into=True):
            name = str(var).lower()
            if 'process_var_asm2d_' not in name or ratio_token not in name:
                continue
            return var
        return None

    def _seed_asm2d_recycle_ratio_split_guesses(self, units: Dict[str, Any]) -> None:
        """Translate bounded RAS/MLR ratio guesses into splitter initial values."""
        ras_ratio = self._asm2d_recycle_ratio_value('ras_recycle_ratio')
        mlr_ratio = self._asm2d_recycle_ratio_value('mlr_recycle_ratio')
        if mlr_ratio is not None and self._rhas(units, R.INTERNAL_RECYCLE_SPLITTER):
            total_mixed_ratio = 1.0 + max(ras_ratio or 0.0, 0.0) + max(mlr_ratio, 0.0)
            if total_mixed_ratio > 0:
                internal_fraction = max(0.0001, min(0.999, mlr_ratio / total_mixed_ratio))
                splitter = self._rsingle(units, R.INTERNAL_RECYCLE_SPLITTER)
                try:
                    splitter.split_fraction[0, 'internal_recycle'].set_value(internal_fraction, skip_validation=True)
                    splitter.split_fraction[0, 'forward'].set_value(1.0 - internal_fraction, skip_validation=True)
                except Exception:
                    pass
        if ras_ratio is not None and self._rhas(units, R.SLUDGE_SPLITTER):
            splitter = self._rsingle(units, R.SLUDGE_SPLITTER)
            separator_role = self._asm2d_separator_role(units)
            separator = self._rsingle(units, separator_role) if separator_role else None
            retentate_per_pass = 0.8
            try:
                if separator_role is R.MBR_SEPARATOR:
                    permeate = self._asm2d_split_value(separator, 'permeate', 'H2O', default=0.2)
                    retentate_per_pass = 1.0 - permeate
                elif separator is not None:
                    effluent = self._asm2d_split_value(separator, 'effluent', 'H2O', default=0.5)
                    retentate_per_pass = 1.0 - effluent
            except Exception:
                pass
            retentate_per_pass = max(0.0001, min(0.9999, retentate_per_pass))
            retentate_feed_ratio = retentate_per_pass * (1.0 + max(ras_ratio, 0.0))
            recycle_fraction = max(0.0001, min(0.999, ras_ratio / max(retentate_feed_ratio, 1e-06)))
            try:
                splitter.split_fraction[0, 'recycle'].set_value(recycle_fraction, skip_validation=True)
                splitter.split_fraction[0, 'waste'].set_value(1.0 - recycle_fraction, skip_validation=True)
            except Exception:
                pass

    def _set_port_var_guess(self, component: Any, index: Any, value_guess: float) -> None:
        try:
            var = component[index] if index is not None else component
        except Exception:
            return
        if getattr(var, 'fixed', False):
            return
        try:
            var.set_value(value_guess, skip_validation=True)
        except Exception:
            return

    def _set_port_flow_guess(self, port: Any, flow_guess: float) -> None:
        flow_vol = getattr(port, 'flow_vol', None)
        if flow_vol is None:
            return
        self._set_port_var_guess(flow_vol, 0, flow_guess)

    def _seed_splitter_output_guesses(self, splitter: Any, outlet_names: Tuple[str, ...]) -> None:
        """Seed total-split separator outlet states from inlet and split fractions."""
        from pyomo.environ import value
        inlet = getattr(splitter, 'inlet', None)
        if inlet is None:
            return
        inlet_vars = getattr(inlet, 'vars', {})
        try:
            inlet_flow = value(inlet.flow_vol[0], exception=False)
        except Exception:
            inlet_flow = None
        if inlet_flow is None:
            return
        for outlet_name in outlet_names:
            outlet = getattr(splitter, outlet_name, None)
            if outlet is None:
                continue
            split = self._asm2d_split_value(splitter, outlet_name, default=0.5)
            split = max(0.0, min(1.0, split))
            outlet_vars = getattr(outlet, 'vars', {})
            for var_name, outlet_var in outlet_vars.items():
                inlet_var = inlet_vars.get(var_name)
                if inlet_var is None:
                    continue
                if var_name == 'flow_vol':
                    self._set_port_var_guess(outlet_var, 0, float(inlet_flow) * split)
                    continue
                if not hasattr(outlet_var, 'is_indexed'):
                    continue
                indexes = list(outlet_var.keys()) if outlet_var.is_indexed() else [None]
                for index in indexes:
                    try:
                        source = inlet_var[index] if index is not None else inlet_var
                        source_value = value(source, exception=False)
                    except Exception:
                        source_value = None
                    if source_value is None:
                        continue
                    self._set_port_var_guess(outlet_var, index, float(source_value))

    def _normalize_asm2d_mbr_split_fractions(self, mbr: Any) -> int:
        """Keep free MBR retentate component splits on the physical complement."""
        from pyomo.environ import value
        split_fraction = getattr(mbr, 'split_fraction', None)
        if split_fraction is None:
            return 0
        components = set()
        try:
            indexes = list(split_fraction.keys())
        except Exception:
            indexes = []
        for index in indexes:
            if not isinstance(index, tuple) or len(index) < 3:
                continue
            if str(index[1]) in {'permeate', 'retentate'}:
                components.add(index[-1])
        normalized = 0
        for component in components:
            permeate_var = None
            retentate_var = None
            for split_index in ((0, 'permeate', component), (0.0, 'permeate', component)):
                try:
                    permeate_var = split_fraction[split_index]
                    break
                except Exception:
                    continue
            for split_index in ((0, 'retentate', component), (0.0, 'retentate', component)):
                try:
                    retentate_var = split_fraction[split_index]
                    break
                except Exception:
                    continue
            if permeate_var is None or retentate_var is None:
                continue
            permeate_value = value(permeate_var, exception=False)
            if permeate_value is None:
                continue
            permeate_value = max(0.0, min(1.0, float(permeate_value)))
            for var in (permeate_var, retentate_var):
                if getattr(var, 'fixed', False):
                    continue
                try:
                    var.setlb(0.0)
                    var.setub(1.0)
                except Exception:
                    pass
            if getattr(retentate_var, 'fixed', False):
                continue
            try:
                retentate_var.set_value(1.0 - permeate_value, skip_validation=True)
                normalized += 1
            except Exception:
                continue
        return normalized

    def _seed_mbr_output_guesses(self, mbr: Any) -> None:
        """Seed MBR outlet guesses from the configured component split."""
        from pyomo.environ import value
        inlet = getattr(mbr, 'inlet', None)
        if inlet is None:
            return
        try:
            inlet_flow = value(inlet.flow_vol[0], exception=False)
        except Exception:
            inlet_flow = None
        if inlet_flow is None:
            return
        permeate_split = self._asm2d_split_value(mbr, 'permeate', 'H2O', default=0.2)
        permeate_split = max(0.0001, min(0.9999, permeate_split))
        guesses = {'permeate': float(inlet_flow) * permeate_split, 'retentate': float(inlet_flow) * (1.0 - permeate_split)}
        split_fraction = getattr(mbr, 'split_fraction', None)
        self._normalize_asm2d_mbr_split_fractions(mbr)
        for outlet_name, flow_guess in guesses.items():
            outlet = getattr(mbr, outlet_name, None)
            if outlet is None or not hasattr(outlet, 'flow_vol'):
                continue
            self._set_port_var_guess(outlet.flow_vol, 0, flow_guess)
            outlet_fraction = max(flow_guess / max(float(inlet_flow), 1e-12), 1e-12)
            inlet_concentrations = getattr(inlet, 'conc_mass_comp', None)
            outlet_concentrations = getattr(outlet, 'conc_mass_comp', None)
            if inlet_concentrations is None or outlet_concentrations is None:
                continue
            for index in list(outlet_concentrations.keys()):
                component = index[-1] if isinstance(index, tuple) else index
                try:
                    source_value = value(inlet_concentrations[0, component], exception=False)
                except Exception:
                    source_value = None
                if source_value is None:
                    continue
                if outlet_name == 'permeate':
                    component_split = self._asm2d_split_value(mbr, 'permeate', component, default=permeate_split)
                else:
                    permeate_component_split = self._asm2d_split_value(mbr, 'permeate', component, default=permeate_split)
                    component_split = 1.0 - permeate_component_split
                    if split_fraction is not None:
                        for split_index in ((0, 'retentate', component), (0.0, 'retentate', component)):
                            try:
                                split_var = split_fraction[split_index]
                            except Exception:
                                continue
                            if not getattr(split_var, 'fixed', False):
                                try:
                                    split_var.setlb(0.0)
                                    split_var.setub(1.0)
                                    split_var.set_value(max(0.0, min(1.0, component_split)), skip_validation=True)
                                except Exception:
                                    pass
                            break
                concentration_guess = max(float(source_value) * component_split / outlet_fraction, 1e-12)
                self._set_port_var_guess(outlet_concentrations, index, concentration_guess)

    def _seed_pressure_changer_output_guesses(self, unit: Any) -> None:
        """Seed pump/pressure-changer outlet state from inlet state guesses."""
        from pyomo.environ import value
        inlet = getattr(unit, 'inlet', None)
        outlet = getattr(unit, 'outlet', None)
        if inlet is None or outlet is None:
            return
        inlet_vars = getattr(inlet, 'vars', {})
        outlet_vars = getattr(outlet, 'vars', {})
        for var_name, outlet_var in outlet_vars.items():
            if var_name == 'pressure':
                continue
            inlet_var = inlet_vars.get(var_name)
            if inlet_var is None or not hasattr(outlet_var, 'is_indexed'):
                continue
            indexes = list(outlet_var.keys()) if outlet_var.is_indexed() else [None]
            for index in indexes:
                try:
                    source = inlet_var[index] if index is not None else inlet_var
                    source_value = value(source, exception=False)
                except Exception:
                    source_value = None
                if source_value is None:
                    continue
                self._set_port_var_guess(outlet_var, index, float(source_value))

    def _seed_asm2d_separator_output_guesses(self, units: Dict[str, Any]) -> None:
        """Refresh hydraulic outlet guesses after splitter fraction updates."""
        splitter = self._rsingle(units, R.INTERNAL_RECYCLE_SPLITTER)
        if splitter is not None:
            self._seed_splitter_output_guesses(splitter, ('forward', 'internal_recycle'))
        mbr = self._rsingle(units, R.MBR_SEPARATOR)
        if mbr is not None:
            self._seed_mbr_output_guesses(mbr)
        splitter = self._rsingle(units, R.SLUDGE_SPLITTER)
        if splitter is not None:
            self._seed_splitter_output_guesses(splitter, ('waste', 'recycle'))
        for pressure_role in (R.INTERNAL_RECYCLE_PRESSURE, R.RAS_PRESSURE):
            pressure_unit = self._rsingle(units, pressure_role)
            if pressure_unit is not None:
                self._seed_pressure_changer_output_guesses(pressure_unit)

    def _seed_asm2d_forward_state_guesses(self, units: Dict[str, Any]) -> None:
        """Propagate ASM2D arc states after local seeding (role-resolved arcs)."""

        def _port(unit_id: str, port_name: str) -> Any:
            unit = units.get(unit_id)
            return getattr(unit, port_name, None) if unit is not None else None
        upstream_edges, middle_edges, downstream_edges, recycle_edges = self._asm2d_forward_edges(units)
        for edge_group in (upstream_edges, middle_edges, downstream_edges, recycle_edges):
            for src_id, src_port_name, dst_id, dst_port_name in edge_group:
                source = _port(src_id, src_port_name)
                dest = _port(dst_id, dst_port_name)
                if source is not None and dest is not None:
                    self._blend_port_state(source, dest, 1.0)
            self._seed_asm2d_separator_output_guesses(units)

    def _seed_asm2d_forward_hydraulic_guesses(self, units: Dict[str, Any], *, seed_states: bool=True) -> None:
        """Propagate volumetric flow guesses along the ASM2D MLE recycle path.

        The pre-separator mixed-liquor spine carries the full ``mixed_flow``
        through the pre-anoxic zone and EVERY aerobic stage (a 3-stage template
        has one aerobic stage; an N-stage train has several), so the core flow
        guesses loop over the ordered aerobic stages rather than a single
        named reactor.
        """
        from pyomo.environ import value
        feed = self._rsingle(units, R.FEED)
        mixer = self._rsingle(units, R.MIXER)
        anoxic = self._rsingle(units, R.PRE_ANOXIC)
        aerobic_units = list(self._roles.units_for(R.AEROBIC, units))
        aerobic_last = aerobic_units[-1] if aerobic_units else None
        splitter = self._rsingle(units, R.INTERNAL_RECYCLE_SPLITTER)
        if not all((feed, mixer, anoxic)) or not aerobic_units:
            self._seed_asm2d_separator_output_guesses(units)
            return
        try:
            feed_flow = value(feed.outlet.flow_vol[0], exception=False)
        except Exception:
            feed_flow = None
        if feed_flow is None:
            self._seed_asm2d_separator_output_guesses(units)
            return

        def _port_flow(unit_id: Optional[str], port_name: str, default: float=0.0) -> float:
            unit = units.get(unit_id) if unit_id is not None else None
            port = getattr(unit, port_name, None) if unit is not None else None
            flow_vol = getattr(port, 'flow_vol', None)
            if flow_vol is None:
                return default
            try:
                raw = value(flow_vol[0], exception=False)
            except Exception:
                raw = None
            return float(raw) if raw is not None else default
        ras_ratio = self._asm2d_recycle_ratio_value('ras_recycle_ratio')
        mlr_ratio = self._asm2d_recycle_ratio_value('mlr_recycle_ratio')
        if ras_ratio is not None:
            recycle_flow = max(float(ras_ratio), 0.0) * float(feed_flow)
        else:
            recycle_flow = _port_flow(self._rsingle_id(units, R.RAS_PRESSURE), 'outlet')
        if mlr_ratio is not None:
            internal_flow = max(float(mlr_ratio), 0.0) * float(feed_flow)
        else:
            internal_flow = _port_flow(self._rsingle_id(units, R.INTERNAL_RECYCLE_PRESSURE), 'outlet')
        mixed_flow = max(float(feed_flow) + recycle_flow + internal_flow, 1e-12)
        core_ports = [getattr(mixer, 'outlet', None), getattr(anoxic, 'inlet', None), getattr(anoxic, 'outlet', None)]
        for reactor in aerobic_units:
            core_ports.append(getattr(reactor, 'inlet', None))
            core_ports.append(getattr(reactor, 'outlet', None))
        if splitter is not None:
            core_ports.append(getattr(splitter, 'inlet', None))
        for port in core_ports:
            if port is not None:
                self._set_port_flow_guess(port, mixed_flow)
        if splitter is not None:
            self._seed_splitter_output_guesses(splitter, ('forward', 'internal_recycle'))
            forward = getattr(splitter, 'forward', None)
            forward_flow = _port_flow(self._rsingle_id(units, R.INTERNAL_RECYCLE_SPLITTER), 'forward')
        else:
            forward = getattr(aerobic_last, 'outlet', None)
            forward_flow = mixed_flow
        post_anoxic = self._rsingle(units, R.POST_ANOXIC)
        separator_role = self._asm2d_separator_role(units)
        separator = self._rsingle(units, separator_role) if separator_role else None
        if post_anoxic is not None:
            for port in (getattr(post_anoxic, 'inlet', None), getattr(post_anoxic, 'outlet', None)):
                if port is not None:
                    self._set_port_flow_guess(port, forward_flow)
        treatment_chain = [self._rsingle(units, R.ALKALINITY), self._rsingle(units, R.CHEMICAL_P)]
        for treatment_unit in treatment_chain:
            if treatment_unit is None:
                continue
            for port in (getattr(treatment_unit, 'inlet', None), getattr(treatment_unit, 'outlet', None)):
                if port is not None:
                    self._set_port_flow_guess(port, forward_flow)
        if separator is not None and hasattr(separator, 'inlet'):
            self._set_port_flow_guess(separator.inlet, forward_flow)
        if separator_role is R.MBR_SEPARATOR and separator is not None:
            self._seed_mbr_output_guesses(separator)
            solids_source = getattr(separator, 'retentate', None)
        elif separator_role is R.SECONDARY_CLARIFIER and separator is not None:
            self._seed_splitter_output_guesses(separator, ('effluent', 'underflow'))
            solids_source = getattr(separator, 'underflow', None)
        else:
            solids_source = None
        sludge_splitter = self._rsingle(units, R.SLUDGE_SPLITTER)
        if sludge_splitter is not None and solids_source is not None:
            try:
                solids_flow = value(solids_source.flow_vol[0], exception=False)
            except Exception:
                solids_flow = None
            if solids_flow is not None:
                self._set_port_flow_guess(sludge_splitter.inlet, float(solids_flow))
        if sludge_splitter is not None:
            self._seed_splitter_output_guesses(sludge_splitter, ('waste', 'recycle'))
        ir_pressure = self._rsingle(units, R.INTERNAL_RECYCLE_PRESSURE)
        if ir_pressure is not None and forward is not None:
            internal = getattr(splitter, 'internal_recycle', None)
            if internal is not None:
                self._blend_port_state(internal, ir_pressure.inlet, 1.0)
            self._seed_pressure_changer_output_guesses(ir_pressure)
        ras_pressure = self._rsingle(units, R.RAS_PRESSURE)
        if ras_pressure is not None and sludge_splitter is not None:
            recycle = getattr(sludge_splitter, 'recycle', None)
            if recycle is not None:
                self._blend_port_state(recycle, ras_pressure.inlet, 1.0)
            self._seed_pressure_changer_output_guesses(ras_pressure)
        self._seed_asm2d_hydraulic_retention_times(units)
        if seed_states:
            self._seed_asm2d_forward_state_guesses(units)

    def _seed_asm2d_hydraulic_retention_times(self, units: Dict[str, Any]) -> None:
        """Set CSTR HRT guesses from current flow and fixed reactor volume."""
        from pyomo.environ import value
        for reactor_id in self._asm2d_reactor_ids(units):
            reactor = units.get(reactor_id)
            if reactor is None:
                continue
            hydraulic_retention_time = getattr(reactor, 'hydraulic_retention_time', None)
            volume = getattr(reactor, 'volume', None)
            inlet = getattr(reactor, 'inlet', None)
            flow_vol = getattr(inlet, 'flow_vol', None)
            if hydraulic_retention_time is None or volume is None or flow_vol is None:
                continue
            try:
                volume_value = value(volume[0], exception=False)
                flow_value = value(flow_vol[0], exception=False)
                hrt_var = hydraulic_retention_time[0]
            except Exception:
                continue
            if volume_value is None or flow_value is None or flow_value <= 0:
                continue
            try:
                hrt_var.set_value(float(volume_value) / float(flow_value), skip_validation=True)
            except Exception:
                continue

    def _asm2d_target_mlss_kg_m3(self, units: Dict[str, Any]) -> float:
        """Return the template target MLSS in kg/m3, with topology fallback."""
        design_parameters = self._runtime_metadata.get('design_parameters')
        if not isinstance(design_parameters, dict):
            design_parameters = {}
        target_mlss = design_parameters.get('target_mlss_mg_l')
        try:
            if target_mlss is not None:
                return max(float(target_mlss) / 1000.0, 0.5)
        except (TypeError, ValueError):
            pass
        recycle_parameters = design_parameters.get('recycle_ratios')
        recycle_modes = recycle_parameters.get('mode', {}) if isinstance(recycle_parameters, dict) else {}
        fixed_recycle_target = any((str(mode).lower() == 'fixed' for mode in recycle_modes.values()))
        strength = str(self._runtime_metadata.get('feed_strength_class') or design_parameters.get('feed_strength_class') or '').lower()
        if self._rhas(units, R.MBR_SEPARATOR):
            if fixed_recycle_target and ('industrial' in strength or 'high_strength' in strength):
                return 12.0
            return 10.0
        return 3.5

    def _asm2d_tear_guesses(self, units: Dict[str, Any]) -> Dict[str, Dict[Any, float]]:
        """Build upstream-style tear guesses for the mixed-liquor recycle stream."""
        feed = self._rsingle(units, R.FEED)
        feed_outlet = getattr(feed, 'outlet', None)
        feed_flow = self._asm2d_scalar_value(getattr(feed_outlet, 'flow_vol', None), 0, 0.0, default=350.0 / 86400.0)
        temperature = self._asm2d_scalar_value(getattr(feed_outlet, 'temperature', None), 0, 0.0, default=298.15)
        pressure = self._asm2d_scalar_value(getattr(feed_outlet, 'pressure', None), 0, 0.0, default=101325.0)
        alkalinity = self._asm2d_scalar_value(getattr(feed_outlet, 'alkalinity', None), 0, 0.0, default=7.0)
        if self._rhas(units, R.INTERNAL_RECYCLE_SPLITTER):
            internal_recycle = self._asm2d_split_value(self._rsingle(units, R.INTERNAL_RECYCLE_SPLITTER), 'internal_recycle', default=0.6)
        else:
            internal_recycle = 0.0
        waste_fraction = self._asm2d_split_value(self._rsingle(units, R.SLUDGE_SPLITTER), 'waste', default=0.02)
        ras_fraction = max(0.0, 1.0 - waste_fraction)
        forward_fraction = max(0.0, 1.0 - internal_recycle)
        if self._rhas(units, R.MBR_SEPARATOR):
            permeate = self._asm2d_split_value(self._rsingle(units, R.MBR_SEPARATOR), 'permeate', 'S_A', default=0.99)
            solids_return_flow_fraction = max(0.0, 1.0 - permeate)
        else:
            effluent = self._asm2d_split_value(self._rsingle(units, R.SECONDARY_CLARIFIER), 'effluent', 'S_A', default=0.49986)
            solids_return_flow_fraction = max(0.0, 1.0 - effluent)
        denominator = 1.0 - internal_recycle - ras_fraction * forward_fraction * solids_return_flow_fraction
        if denominator <= 0.05:
            denominator = 0.2
        mixed_flow = min(max(feed_flow / denominator, feed_flow), feed_flow * 25.0)
        mlss = self._asm2d_target_mlss_kg_m3(units)
        conc = {(0, 'S_O2'): 0.001, (0, 'S_F'): 0.03, (0, 'S_A'): 0.02, (0, 'S_NH4'): 0.016, (0, 'S_NO3'): 0.01, (0, 'S_PO4'): 0.0036, (0, 'S_I'): 0.03, (0, 'S_N2'): 0.001, (0, 'X_I'): 0.35 * mlss, (0, 'X_S'): 0.05, (0, 'X_H'): 0.35 * mlss, (0, 'X_PAO'): 0.08 * mlss, (0, 'X_PP'): 0.02 * mlss, (0, 'X_PHA'): 0.02 * mlss, (0, 'X_AUT'): 0.02 * mlss, (0, 'X_MeOH'): 1e-09, (0, 'X_MeP'): 1e-09, (0, 'X_TSS'): mlss}
        valid_components = self._asm2d_port_component_names(feed_outlet)
        if valid_components:
            if 'X_TSS' not in valid_components and valid_components.intersection(ASM2D_BIOMASS_SEED_FRACTIONS):
                total_fraction = sum(ASM2D_BIOMASS_SEED_FRACTIONS.values())
                particulate_cod = mlss * MODIFIED_ASM2D_COD_TO_VSS
                for component, fraction in ASM2D_BIOMASS_SEED_FRACTIONS.items():
                    if component in valid_components:
                        conc[0, component] = particulate_cod * fraction / total_fraction
            if 'S_IC' in valid_components:
                s_ic = self._asm2d_scalar_value(getattr(feed_outlet, 'conc_mass_comp', None), (0, 'S_IC'), (0.0, 'S_IC'), default=alkalinity * 0.012)
                conc[0, 'S_IC'] = max(s_ic, 1e-09)
            if 'S_K' in valid_components:
                conc[0, 'S_K'] = 0.015
            if 'S_Mg' in valid_components:
                conc[0, 'S_Mg'] = 0.015
            conc = {index: value for index, value in conc.items() if index[1] in valid_components}
        state_args = {'flow_vol': {0: mixed_flow}, 'conc_mass_comp': conc, 'temperature': {0: temperature}, 'pressure': {0: pressure}}
        if not valid_components or 'S_ALK' in valid_components:
            state_args['alkalinity'] = {0: alkalinity}
        return state_args

    def _clip_tiny_negative_nonnegative_vars(self, tolerance: float=1e-06) -> int:
        """Clip tiny negative nonnegative variables caused by tear extrapolation."""
        if self._model is None:
            return 0
        from pyomo.environ import Var, value
        clipped = 0
        for var in self._model.component_data_objects(Var, descend_into=True):
            try:
                lb = var.lb
                raw_value = value(var, exception=False)
            except Exception:
                continue
            if lb is None or lb < 0 or raw_value is None:
                continue
            if raw_value < lb and raw_value >= lb - tolerance:
                replacement = lb if lb > 0 else 1e-12
                try:
                    var.set_value(replacement, skip_validation=True)
                    clipped += 1
                except Exception:
                    continue
        return clipped

    def _component_from_concentration_index(self, index: Any) -> Optional[str]:
        if isinstance(index, tuple):
            for item in reversed(index):
                if isinstance(item, str):
                    return item
            return None
        if isinstance(index, str):
            return index
        return None

    def _apply_asm2d_trace_concentration_floors(self, units: Optional[Dict[str, Any]]=None) -> int:
        """Keep trace ASM2D species away from singular zero kinetics.

        RECYCLE-FAMILY conditioning only (Wave 5c toggle experiments,
        2026-08-25). On the stiff recycle trains the floors are part of the
        conditioning that lands the near-feasible contract. On the
        non-recycle forward paths they are actively harmful: the raised
        lower bounds made the carbon->NO3 solve_for square system locally
        infeasible (every Anoxic ``cstr_performance_eqn`` row violated by a
        consistent ~1e-6 raw, x1000 row scaling — the "22% over the 1e-3
        gate" miss; wrapper and exact-Hessian polishes both declared local
        infeasibility), and a value-floor-only variant sent the same solve
        to outright solver infeasibility. With the floors skipped the
        non-recycle solve is strict-optimal, so the family gate below
        scopes the floors to the recycle topology that earned them.
        """
        if self._model is None:
            return 0
        try:
            if not self._roles.is_asm2d_recycle_family():
                return 0
        except Exception:
            pass
        from pyomo.environ import Var, value
        changed = 0
        for var in self._model.component_data_objects(Var, descend_into=True):
            parent = var.parent_component()
            parent_name = getattr(parent, 'local_name', getattr(parent, 'name', ''))
            if parent_name != 'conc_mass_comp':
                continue
            component = self._component_from_concentration_index(var.index())
            floor = ASM2D_TRACE_CONCENTRATION_FLOORS.get(component or '')
            if floor is None:
                continue
            try:
                if var.lb is None or float(var.lb) < floor:
                    var.setlb(floor)
                    changed += 1
            except Exception:
                continue
            current = value(var, exception=False)
            if current is None or current < floor:
                try:
                    var.set_value(floor, skip_validation=True)
                    changed += 1
                except Exception:
                    continue
        return changed

    def _condition_asm2d_reactor_inlet_for_initialization(self, unit: Any) -> None:
        """Keep ASM2D reactor initializer guesses in its robust operating range."""
        unit_name = str(getattr(unit, 'local_name', getattr(unit, 'name', ''))).lower()
        if 'anoxic' not in unit_name:
            return
        inlet = getattr(unit, 'inlet', None)
        if inlet is None or not hasattr(inlet, 'conc_mass_comp'):
            return
        try:
            oxygen = inlet.conc_mass_comp[0, 'S_O2']
        except Exception:
            return
        try:
            from pyomo.environ import value
            oxygen_value = value(oxygen, exception=False)
        except Exception:
            oxygen_value = None
        if oxygen_value is not None and oxygen_value > 0.002:
            try:
                oxygen.set_value(0.001)
            except Exception:
                pass

    def _asm2d_unit_has_usable_state(self, unit: Any) -> bool:
        """Return True when a failed ASM2D unit init still produced finite states."""
        import math
        ports = [getattr(unit, port_name, None) for port_name in ('outlet', 'inlet')]
        ports = [port for port in ports if port is not None and hasattr(port, 'flow_vol')]
        if not ports:
            return False
        from pyomo.environ import value
        for port in ports:
            try:
                flow_vol = value(port.flow_vol[0], exception=False)
            except Exception:
                flow_vol = None
            if flow_vol is None or not math.isfinite(float(flow_vol)) or flow_vol <= 0:
                continue
            concentrations = getattr(port, 'conc_mass_comp', None)
            if concentrations is None:
                return True
            checked = 0
            finite = True
            for key in list(concentrations.keys()):
                component = key[-1] if isinstance(key, tuple) else key
                try:
                    raw_value = value(concentrations[0, component], exception=False)
                except Exception:
                    raw_value = None
                if raw_value is None:
                    continue
                numeric_value = float(raw_value)
                if not math.isfinite(numeric_value) or numeric_value < -1e-06:
                    finite = False
                    break
                checked += 1
            if finite and checked > 0:
                return True
        return False

    def _is_reactor_unit_id(self, unit_id: str) -> bool:
        """Return True when ``unit_id`` is a biological reactor (by role or name)."""
        role = self._roles.role_of(unit_id)
        reactor_roles = {R.PRE_ANOXIC.value, R.AEROBIC.value, R.POST_ANOXIC.value}
        if role is not None:
            return role in reactor_roles
        return unit_id in {'Anoxic', 'Aerobic', 'PostAnoxic'}

    def _is_asm2d_reactor_unit(self, unit_id: str, unit: Any) -> bool:
        """Return True for ASM2D-backed biological reactors."""
        if not self._is_reactor_unit_id(unit_id):
            return False
        package = getattr(getattr(unit, 'config', None), 'property_package', None)
        return package is not None and is_asm2d_property_block(package)

    def _can_accept_locally_infeasible_asm2d_unit(self, unit_id: str, unit: Any) -> bool:
        """Return True for ASM2D reactors whose finite state can seed IPOPT.

        Still earning its keep on watertap 1.7 + ma27 (Wave 5c toggle
        experiment, 2026-08-25): refusing this acceptance fails the
        industrial pinned-ratio MBR-post inits outright — the finite
        locally-infeasible reactor state is what seeds the flowsheet solve
        that then lands under the near-feasible residual contract.
        """
        return self._is_asm2d_reactor_unit(unit_id, unit) and self._asm2d_unit_has_usable_state(unit)

    def _variable_fixity_snapshot(self) -> Dict[int, bool]:
        """Capture fixed status for model variables before SD initialization."""
        if self._model is None:
            return {}
        from pyomo.environ import Var
        return {id(var): bool(var.fixed) for var in self._model.component_data_objects(Var, descend_into=True)}

    def _restore_newly_fixed_variables(self, snapshot: Dict[int, bool]) -> None:
        """Unfix variables that SD left fixed but were free beforehand."""
        if self._model is None:
            return
        from pyomo.environ import Var
        for var in self._model.component_data_objects(Var, descend_into=True):
            was_fixed = snapshot.get(id(var))
            if was_fixed is False and var.fixed:
                try:
                    var.unfix()
                except Exception:
                    continue

    def _srt_constraints(self) -> List[Any]:
        """Return flowsheet-level SRT constraints added by templates."""
        if self._model is None:
            return []
        from pyomo.environ import Constraint
        fs = getattr(self._model, 'fs', self._model)
        constraints = []
        for constraint in fs.component_objects(Constraint, active=None, descend_into=False):
            local_name = getattr(constraint, 'local_name', '')
            if local_name.startswith('process_constraint_') and 'srt' in local_name:
                constraints.append(constraint)
        return constraints

    def _srt_target_specs(self) -> List[Dict[str, Any]]:
        specs = self._runtime_metadata.get('srt_target_specs')
        return [s for s in specs if isinstance(s, dict)] if isinstance(specs, list) else []

    def _primary_srt_target_spec(self) -> Optional[Dict[str, Any]]:
        specs = self._srt_target_specs()
        return specs[0] if specs else None

    def _srt_seed_value(self) -> float:
        spec = self._primary_srt_target_spec() or {}
        for source in (spec.get('waste_split_seed'), (self._runtime_metadata.get('design_parameters') or {}).get('srt_waste_split_seed') if isinstance(self._runtime_metadata.get('design_parameters'), dict) else None):
            try:
                value_ = float(source)
            except (TypeError, ValueError):
                continue
            if 0.0 < value_ < 0.95:
                return value_
        return 0.02

    def _seed_adm1_srt_hydraulic_point(self, units: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Seed retained-solids ADM1 recycle hydraulics at the finite-SRT point."""
        spec = self._primary_srt_target_spec()
        if spec is None or not self._is_adm1_flowsheet(units):
            return None
        from pyomo.environ import value
        splitter, waste_port = self._srt_splitter_and_port(units)
        if splitter is None:
            return None
        retained_streams = spec.get('retained_streams') or []
        if not retained_streams:
            return None
        retained_spec = retained_streams[0]
        if not isinstance(retained_spec, dict):
            return None
        separator = units.get(str(retained_spec.get('unit')))
        retained_port_name = str(retained_spec.get('port'))
        if separator is None or not retained_port_name:
            return None
        clarified_port_name = {'retentate': 'permeate', 'underflow': 'overflow'}.get(retained_port_name)
        if clarified_port_name is None:
            return None
        try:
            waste_fraction = float(spec.get('waste_split_seed', self._srt_seed_value()))
        except (TypeError, ValueError):
            waste_fraction = self._srt_seed_value()
        waste_fraction = max(0.0001, min(0.95, waste_fraction))

        def _split_fraction(outlet: str, component: str, default: float) -> float:
            split_fraction = getattr(separator, 'split_fraction', None)
            if split_fraction is None:
                return default
            for index in ((0, outlet, component), (0.0, outlet, component)):
                try:
                    raw = value(split_fraction[index], exception=False)
                except Exception:
                    raw = None
                if raw is not None:
                    return float(raw)
            return default
        soluble_clarified = _split_fraction(clarified_port_name, 'H2O', 0.25)
        particulate_clarified = _split_fraction(clarified_port_name, 'X_I', 0.15)
        retained_soluble = max(0.0, min(1.0, 1.0 - soluble_clarified))
        retained_particulate = max(0.0, min(1.0, 1.0 - particulate_clarified))
        denominator = soluble_clarified + retained_soluble * waste_fraction
        if denominator <= 0.0:
            return None
        feed = self._rsingle(units, R.FEED)
        if feed is None:
            return None
        try:
            feed_flow = value(feed.outlet.flow_vol[0], exception=False)
        except Exception:
            feed_flow = None
        if feed_flow is None or feed_flow <= 0:
            return None
        feed_flow = float(feed_flow)
        digester_flow = feed_flow / denominator
        retained_flow = retained_soluble * digester_flow
        waste_flow = waste_fraction * retained_flow
        recycle_flow = (1.0 - waste_fraction) * retained_flow
        particulate_purge_flow = waste_fraction * retained_particulate * digester_flow
        try:
            splitter.split_fraction[0, waste_port].set_value(waste_fraction, skip_validation=True)
        except Exception:
            pass
        try:
            splitter.split_fraction[0, 'recycle'].set_value(1.0 - waste_fraction, skip_validation=True)
        except Exception:
            pass
        mixer = self._rsingle(units, R.MIXER)
        digester = self._rsingle(units, R.ANAEROBIC_METHANOGENIC)
        retained_port = getattr(separator, retained_port_name, None)
        waste_out = getattr(splitter, waste_port, None)
        recycle_out = getattr(splitter, 'recycle', None)
        flow_guesses = ((getattr(mixer, 'feed_water', None) if mixer is not None else None, feed_flow), (getattr(mixer, 'recycle', None) if mixer is not None else None, recycle_flow), (getattr(mixer, 'outlet', None) if mixer is not None else None, digester_flow), (getattr(digester, 'inlet', None) if digester is not None else None, digester_flow), (getattr(separator, 'inlet', None), digester_flow), (retained_port, retained_flow), (getattr(splitter, 'inlet', None), retained_flow), (waste_out, waste_flow), (recycle_out, recycle_flow))
        for port, flow in flow_guesses:
            if port is not None:
                self._set_port_flow_guess(port, flow)
        return {'waste_split': waste_fraction, 'feed_flow': feed_flow, 'digester_flow': digester_flow, 'retained_flow': retained_flow, 'waste_flow': waste_flow, 'recycle_flow': recycle_flow, 'particulate_purge_flow': particulate_purge_flow}

    def _srt_splitter_and_port(self, units: Dict[str, Any]) -> Tuple[Optional[Any], str]:
        spec = self._primary_srt_target_spec() or {}
        waste_unit = spec.get('waste_unit')
        waste_port = str(spec.get('waste_port', 'waste'))
        splitter = units.get(str(waste_unit)) if waste_unit else None
        if splitter is None:
            splitter = self._rsingle(units, R.SLUDGE_SPLITTER)
        return (splitter, waste_port)

    def _relax_srt_for_initialization(self, units: Dict[str, Any]) -> Callable[[], None]:
        """Temporarily replace the SRT equation with a fixed WAS split."""
        from pyomo.environ import value
        constraints = self._srt_constraints()
        constraint_states = [(constraint, bool(constraint.active)) for constraint in constraints]
        for constraint, active in constraint_states:
            if active:
                constraint.deactivate()
        waste_var = None
        waste_was_fixed = None
        waste_value = None
        splitter, waste_port = self._srt_splitter_and_port(units)
        if splitter is not None:
            try:
                waste_var = splitter.split_fraction[0, waste_port]
            except Exception:
                waste_var = None
        if waste_var is not None:
            waste_was_fixed = bool(waste_var.fixed)
            waste_value = value(waste_var, exception=False)
            if not waste_was_fixed:
                waste_value = self._srt_seed_value()
            elif waste_value is None or waste_value <= 0 or waste_value >= 0.95:
                waste_value = self._srt_seed_value()
            if not waste_was_fixed:
                waste_var.fix(waste_value)
            try:
                splitter.split_fraction[0, 'recycle'].set_value(1.0 - waste_value)
            except Exception:
                pass

        def restore() -> None:
            for constraint, active in constraint_states:
                if active:
                    constraint.activate()
                else:
                    constraint.deactivate()
            if waste_var is None:
                return
            try:
                if waste_was_fixed:
                    waste_var.fix(waste_value)
                else:
                    waste_var.unfix()
            except Exception:
                pass
        return restore

    def _fix_aerobic_do_for_initialization(self, units: Dict[str, Any]) -> Callable[[], None]:
        """Use upstream ASM2D's robust startup pattern for aerated CSTRs.

        CSTR_Injection blocks are easier to initialize with outlet dissolved
        oxygen fixed and KLa free. After the recycle is closed, proposal
        templates restore the design KLa specification and release outlet DO.
        """
        from pyomo.environ import value
        snapshots = []
        affected_units: List[str] = []
        for unit_id, unit in units.items():
            kla = getattr(unit, 'KLa', None)
            outlet = getattr(unit, 'outlet', None)
            if kla is None or outlet is None or (not hasattr(outlet, 'conc_mass_comp')):
                continue
            try:
                oxygen = outlet.conc_mass_comp[0, 'S_O2']
            except Exception:
                continue
            try:
                kla_was_fixed = bool(kla.fixed)
                kla_value = value(kla, exception=False)
                oxygen_was_fixed = bool(oxygen.fixed)
                oxygen_value = value(oxygen, exception=False)
            except Exception:
                continue
            snapshots.append((kla, kla_was_fixed, kla_value, oxygen, oxygen_was_fixed, oxygen_value))
            affected_units.append(str(unit_id))
            try:
                if kla.fixed:
                    kla.unfix()
                oxygen.fix(self._do_target_setpoint_kg_m3(str(unit_id)))
            except Exception:
                continue
        if affected_units:
            self._record_initializer_tactic(name='asm2d_do_to_kla_startup', description='Temporarily fixes aerobic outlet dissolved oxygen and frees KLa during ASM2D recycle initialization', units=affected_units)

        def restore() -> None:
            for kla, kla_was_fixed, kla_value, oxygen, oxygen_was_fixed, oxygen_value in snapshots:
                try:
                    if kla_was_fixed:
                        kla.fix(kla_value)
                    elif kla.fixed:
                        kla.unfix()
                except Exception:
                    pass
                try:
                    if oxygen_was_fixed:
                        oxygen.fix(oxygen_value)
                    elif oxygen.fixed:
                        oxygen.unfix()
                except Exception:
                    pass
        return restore

    def _chemical_p_target_constraints(self) -> List[Any]:
        """Return flowsheet-level effluent-TP target constraints added by the builder."""
        if self._model is None:
            return []
        from pyomo.environ import Constraint
        fs = getattr(self._model, 'fs', self._model)
        constraints = []
        for constraint in fs.component_objects(Constraint, active=None, descend_into=False):
            local_name = getattr(constraint, 'local_name', '')
            if local_name.startswith('process_constraint_') and 'chemical_p_target' in local_name:
                constraints.append(constraint)
        return constraints

    def _do_target_constraints(self) -> List[Any]:
        """Return flowsheet-level DO target constraints added by the builder."""
        if self._model is None:
            return []
        from pyomo.environ import Constraint
        fs = getattr(self._model, 'fs', self._model)
        constraints = []
        for constraint in fs.component_objects(Constraint, active=None, descend_into=False):
            local_name = getattr(constraint, 'local_name', '')
            if local_name.startswith('process_constraint_') and 'do_target' in local_name:
                constraints.append(constraint)
        return constraints

    def _relax_do_target_for_initialization(self) -> Callable[[], None]:
        """Deactivate persistent DO targets during sequential initialization."""
        constraints = self._do_target_constraints()
        constraint_states = [(constraint, bool(constraint.active)) for constraint in constraints]
        for constraint, active in constraint_states:
            if active:
                constraint.deactivate()

        def restore() -> None:
            for constraint, active in constraint_states:
                if active:
                    constraint.activate()
                else:
                    constraint.deactivate()
        return restore

    def _do_target_setpoint_kg_m3(self, unit_id: str) -> float:
        specs = self._runtime_metadata.get('do_target_specs')
        if isinstance(specs, list):
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                if str(spec.get('target_unit')) != str(unit_id):
                    continue
                try:
                    return float(spec.get('target_kg_m3'))
                except (TypeError, ValueError):
                    try:
                        return float(spec.get('target_mg_l')) / 1000.0
                    except (TypeError, ValueError):
                        pass
        return 0.002

    def _relax_chemical_p_target_for_initialization(self, units: Dict[str, Any]) -> Callable[[], None]:
        """Temporarily replace the effluent-TP target with a fixed metal dose.

        Mirrors ``_relax_srt_for_initialization``: the effluent total-P target
        couples the dose to the recycle-closed permeate state, which sequential
        initialization cannot yet evaluate. Deactivate the target and fix the
        dose at its seed so the chemical-P block initializes as a clean forward
        transform; restore (reactivate target + free dose) for the final IPOPT
        solve. A no-op when no chemical_p_target constraint is present (the
        fixed-dose path is unchanged).
        """
        from pyomo.environ import value
        constraints = self._chemical_p_target_constraints()
        if not constraints:
            return lambda: None
        constraint_states = [(constraint, bool(constraint.active)) for constraint in constraints]
        for constraint, active in constraint_states:
            if active:
                constraint.deactivate()
        dose_snapshots = []
        for unit_id, unit in units.items():
            metal_dose = getattr(unit, 'metal_dose', None)
            if metal_dose is None:
                continue
            try:
                dose_var = metal_dose[0]
            except Exception:
                continue
            was_fixed = bool(dose_var.fixed)
            seed = self._chemical_p_dose_seed_value(unit_id, unit, dose_var, prefer_settled=False)
            dose_snapshots.append((dose_var, was_fixed, seed))
            if not was_fixed:
                try:
                    dose_var.fix(seed)
                except Exception:
                    pass

        def restore() -> None:
            for constraint, active in constraint_states:
                if active:
                    constraint.activate()
                else:
                    constraint.deactivate()
            for dose_var, was_fixed, seed in dose_snapshots:
                try:
                    if was_fixed:
                        dose_var.fix(seed)
                    else:
                        dose_var.unfix()
                except Exception:
                    pass
        return restore

    def _alkalinity_target_constraints(self) -> List[Any]:
        """Return flowsheet-level alkalinity target constraints added by the builder."""
        if self._model is None:
            return []
        from pyomo.environ import Constraint
        fs = getattr(self._model, 'fs', self._model)
        constraints = []
        for constraint in fs.component_objects(Constraint, active=None, descend_into=False):
            local_name = getattr(constraint, 'local_name', '')
            if local_name.startswith('process_constraint_') and 'alkalinity_target' in local_name:
                constraints.append(constraint)
        return constraints

    def _relax_alkalinity_target_for_initialization(self, units: Dict[str, Any]) -> Callable[[], None]:
        """Temporarily replace the alkalinity target with a fixed caustic dose."""
        from pyomo.environ import value
        constraints = self._alkalinity_target_constraints()
        if not constraints:
            return lambda: None
        constraint_states = [(constraint, bool(constraint.active)) for constraint in constraints]
        for constraint, active in constraint_states:
            if active:
                constraint.deactivate()
        dose_snapshots = []
        for unit_id, unit in units.items():
            caustic_dose = getattr(unit, 'caustic_dose', None)
            if caustic_dose is None:
                continue
            try:
                dose_var = caustic_dose[0]
            except Exception:
                continue
            was_fixed = bool(dose_var.fixed)
            seed = self._alkalinity_dose_seed_value(unit_id, dose_var)
            dose_snapshots.append((dose_var, was_fixed, seed))
            if not was_fixed:
                try:
                    dose_var.fix(seed)
                except Exception:
                    pass

        def restore() -> None:
            for constraint, active in constraint_states:
                if active:
                    constraint.activate()
                else:
                    constraint.deactivate()
            for dose_var, was_fixed, seed in dose_snapshots:
                try:
                    if was_fixed:
                        dose_var.fix(seed)
                    else:
                        dose_var.unfix()
                except Exception:
                    pass
        return restore

    def _alkalinity_target_specs(self) -> List[Dict[str, Any]]:
        specs = self._runtime_metadata.get('alkalinity_target_specs')
        return [s for s in specs if isinstance(s, dict)] if isinstance(specs, list) else []

    def _alkalinity_spec_for_unit(self, unit_id: str) -> Optional[Dict[str, Any]]:
        catch_all: Optional[Dict[str, Any]] = None
        for spec in self._alkalinity_target_specs():
            dose_unit = spec.get('dose_unit')
            if dose_unit and str(dose_unit) == str(unit_id):
                return spec
            if not dose_unit and catch_all is None:
                catch_all = spec
        return catch_all

    def _alkalinity_dose_seed_value(self, unit_id: str, dose_var: Any) -> float:
        from pyomo.environ import value
        lb = dose_var.lb
        ub = dose_var.ub
        dose_lower = float(lb) if lb is not None else 0.0
        dose_upper = float(ub) if ub is not None else 50.0
        spec = self._alkalinity_spec_for_unit(unit_id)
        if spec is not None and spec.get('dose_seed') is not None:
            try:
                return min(max(float(spec['dose_seed']), dose_lower), dose_upper)
            except (TypeError, ValueError):
                pass
        current = value(dose_var, exception=False)
        if current is not None and dose_lower <= current <= dose_upper:
            return float(current)
        return min(max(0.0, dose_lower), dose_upper)

    def _chemical_p_target_specs(self) -> List[Dict[str, Any]]:
        specs = self._runtime_metadata.get('chemical_p_target_specs')
        return [s for s in specs if isinstance(s, dict)] if isinstance(specs, list) else []

    def _chemical_p_spec_for_unit(self, unit_id: str) -> Optional[Dict[str, Any]]:
        """Return the chemical_p_target seed spec governing a dosing unit.

        An exact ``dose_unit`` match wins; a spec with no ``dose_unit`` applies
        to every metal-dose unit (the role-agnostic single-dosing-unit case).
        """
        catch_all: Optional[Dict[str, Any]] = None
        for spec in self._chemical_p_target_specs():
            dose_unit = spec.get('dose_unit')
            if dose_unit and str(dose_unit) == str(unit_id):
                return spec
            if not dose_unit and catch_all is None:
                catch_all = spec
        return catch_all

    def _chemical_p_inlet_soluble_p_mg_l(self, unit: Any) -> Optional[float]:
        """Settled chemical-P inlet soluble ortho-P (S_PO4), mg/L, or None."""
        from pyomo.environ import value
        getters = []
        properties_in = getattr(unit, 'properties_in', None)
        if properties_in is not None:
            getters.append(lambda: properties_in[0].conc_mass_comp['S_PO4'])
        inlet = getattr(unit, 'inlet', None)
        if inlet is not None:
            getters.append(lambda: inlet.conc_mass_comp[0, 'S_PO4'])
        for getter in getters:
            try:
                conc = value(getter(), exception=False)
            except Exception:
                continue
            if conc is not None and conc >= 0:
                return float(conc) * 1000.0
        return None

    def _chemical_p_dose_seed_value(self, unit_id: str, unit: Any, dose_var: Any, *, prefer_settled: bool) -> float:
        """Water-chemistry equivalence seed for a freed metal_dose variable.

        Priority: explicit ``dose_seed`` (opt-out) > the engine equivalence dose
        from the best post-bio soluble-P estimate (the settled chem-P inlet
        S_PO4 once available, else the spec hint) > a stoichiometric floor. The
        engine equivalence comes from ``engineering_utils.dosing`` — the single
        source shared with MCP clients — and is clipped to the dose bounds.
        """
        from pyomo.environ import value
        lb = dose_var.lb
        ub = dose_var.ub
        dose_lower = float(lb) if lb is not None else 0.0
        dose_upper = float(ub) if ub is not None else 0.5
        floor = min(max(0.0075, dose_lower), dose_upper)
        spec = self._chemical_p_spec_for_unit(unit_id)
        if spec is None:
            current = value(dose_var, exception=False)
            if current is not None and dose_lower <= current <= dose_upper:
                return float(current)
            return floor
        explicit = spec.get('dose_seed')
        if explicit is not None:
            try:
                return min(max(float(explicit), dose_lower), dose_upper)
            except (TypeError, ValueError):
                pass
        target = float(spec.get('target_mg_l', 0.8))
        settled = self._chemical_p_inlet_soluble_p_mg_l(unit)
        hint = spec.get('seed_post_bio_soluble_p_mg_l')
        if prefer_settled:
            post_bio = settled if settled is not None else hint
        else:
            post_bio = hint if hint is not None else settled
        if post_bio is None:
            return floor
        try:
            from puran_mcp_support.engineering.dosing import engine_metal_dose_seed
        except Exception:
            delta = max(float(post_bio) - target, 0.0) / 1000.0
            return min(max(3.45 * delta, dose_lower), dose_upper)
        return engine_metal_dose_seed(float(post_bio), target, dose_lower=dose_lower, dose_upper=dose_upper)

    def _reseed_chemical_p_target_dose(self, units: Dict[str, Any]) -> None:
        """Reseed each freed metal_dose from the settled chem-P inlet S_PO4.

        Called after the recycle has been closed at the cold-start (fixed) dose:
        the chem-P inlet soluble P now reflects the recycle-coupled
        post-biological state, so the engine equivalence dose computed from it
        lands the freed-dose final solve right at the precipitation knee. A no-op
        for units whose spec carries an explicit ``dose_seed`` (opt-out) and when
        no chemical_p_target spec is present.
        """
        if not self._chemical_p_target_specs():
            return
        for unit_id, unit in units.items():
            metal_dose = getattr(unit, 'metal_dose', None)
            if metal_dose is None:
                continue
            try:
                dose_var = metal_dose[0]
            except Exception:
                continue
            spec = self._chemical_p_spec_for_unit(unit_id)
            if spec is None or spec.get('dose_seed') is not None:
                continue
            seed = self._chemical_p_dose_seed_value(unit_id, unit, dose_var, prefer_settled=True)
            try:
                dose_var.set_value(seed)
            except Exception:
                pass

    def _seed_srt_waste_split(self, units: Dict[str, Any]) -> None:
        """Set the free WAS split near the algebraic SRT target before IPOPT."""
        design_parameters = self._runtime_metadata.get('design_parameters')
        if not isinstance(design_parameters, dict):
            return
        target_days = design_parameters.get('target_srt_days')
        spec = self._primary_srt_target_spec()
        if spec is not None:
            target_days = spec.get('target_days', target_days)
        if target_days is None:
            return
        try:
            target_seconds = float(target_days) * 24.0 * 3600.0
        except (TypeError, ValueError):
            return
        if target_seconds <= 0:
            return
        from pyomo.environ import value
        if spec is not None and self._is_adm1_flowsheet(units):
            components = tuple((str(c) for c in spec.get('biomass_components') or ()))
            reactor_ids = tuple((str(r) for r in spec.get('reactor_units') or ()))
            if components and reactor_ids:
                if self._seed_adm1_srt_waste_split(units, reactor_ids=reactor_ids, biomass_components=components, target_seconds=target_seconds):
                    return
        feed = self._rsingle(units, R.FEED)
        valid_components = self._asm2d_port_component_names(getattr(feed, 'outlet', None))
        if valid_components and 'X_TSS' not in valid_components:
            biomass_components = tuple((component for component in ASM2D_BIOMASS_SEED_FRACTIONS if component in valid_components))
        else:
            biomass_components = ('X_AUT', 'X_H', 'X_PAO')
        inventory = 0.0
        for reactor_id in self._asm2d_reactor_ids(units):
            reactor = units.get(reactor_id)
            if reactor is None:
                continue
            try:
                volume = value(reactor.volume[0], exception=False)
                concentration = sum((value(reactor.outlet.conc_mass_comp[0, component], exception=False) or 0.0 for component in biomass_components))
            except Exception:
                continue
            if volume is not None:
                inventory += volume * concentration
        splitter = self._rsingle(units, R.SLUDGE_SPLITTER)
        if splitter is None or inventory <= 0:
            return
        try:
            sludge_flow = value(splitter.inlet.flow_vol[0], exception=False)
            sludge_concentration = sum((value(splitter.inlet.conc_mass_comp[0, component], exception=False) or 0.0 for component in biomass_components))
            waste_var = splitter.split_fraction[0, 'waste']
        except Exception:
            return
        denominator = target_seconds * (sludge_flow or 0.0) * sludge_concentration
        if denominator <= 0:
            return
        split_guess = max(1e-05, min(0.95, inventory / denominator))
        try:
            waste_var.set_value(split_guess)
        except Exception:
            pass
        self._seed_srt_balanced_ras_ratio(units, split_guess)
        try:
            splitter.split_fraction[0, 'recycle'].set_value(1.0 - split_guess)
        except Exception:
            pass

    def _seed_adm1_srt_waste_split(self, units: Dict[str, Any], *, reactor_ids: Tuple[str, ...], biomass_components: Tuple[str, ...], target_seconds: float) -> bool:
        """Seed an ADM1 SRT-closed bleed split from the retained-solids inventory."""
        from pyomo.environ import value

        def reactor_volume(unit: Any) -> Any:
            for attr in ('volume', 'volume_liquid'):
                indexed = getattr(unit, attr, None)
                if indexed is None:
                    continue
                try:
                    return indexed[0]
                except Exception:
                    try:
                        return indexed[0.0]
                    except Exception:
                        continue
            return None

        def reactor_concentration(unit: Any, component: str) -> Optional[float]:
            try:
                liquid_phase = getattr(unit, 'liquid_phase', None)
                if liquid_phase is not None:
                    state = liquid_phase.properties_out[0]
                    return value(state.conc_mass_comp[component], exception=False)
            except Exception:
                pass
            outlet = getattr(unit, 'liquid_outlet', None) or getattr(unit, 'outlet', None)
            if outlet is None:
                return None
            try:
                return value(outlet.conc_mass_comp[0, component], exception=False)
            except Exception:
                return None
        inventory = 0.0
        for reactor_id in reactor_ids:
            reactor = units.get(reactor_id)
            if reactor is None:
                continue
            volume = reactor_volume(reactor)
            volume_value = value(volume, exception=False) if volume is not None else None
            if volume_value is None:
                continue
            concentration = 0.0
            for component in biomass_components:
                concentration += reactor_concentration(reactor, component) or 0.0
            inventory += float(volume_value) * concentration
        splitter, waste_port = self._srt_splitter_and_port(units)
        if splitter is None or inventory <= 0:
            return False
        try:
            inlet = splitter.inlet
            sludge_flow = value(inlet.flow_vol[0], exception=False)
            sludge_concentration = sum((value(inlet.conc_mass_comp[0, component], exception=False) or 0.0 for component in biomass_components))
            waste_var = splitter.split_fraction[0, waste_port]
        except Exception:
            return False
        denominator = target_seconds * (sludge_flow or 0.0) * sludge_concentration
        if denominator <= 0:
            split_guess = self._srt_seed_value()
        else:
            split_guess = max(1e-05, min(0.95, inventory / denominator))
        try:
            waste_var.set_value(split_guess, skip_validation=True)
        except TypeError:
            try:
                waste_var.set_value(split_guess)
            except Exception:
                return False
        except Exception:
            return False
        try:
            splitter.split_fraction[0, 'recycle'].set_value(1.0 - split_guess, skip_validation=True)
        except TypeError:
            try:
                splitter.split_fraction[0, 'recycle'].set_value(1.0 - split_guess)
            except Exception:
                pass
        except Exception:
            pass
        return True

    def _seed_srt_balanced_ras_ratio(self, units: Dict[str, Any], waste_fraction: float) -> None:
        """Align a free RAS ratio guess with the SRT-required WAS split."""
        ratio_var = self._asm2d_recycle_ratio_var('ras_recycle_ratio')
        if ratio_var is None or getattr(ratio_var, 'fixed', False):
            return
        separator_role = self._asm2d_separator_role(units)
        separator = self._rsingle(units, separator_role) if separator_role else None
        if separator is None:
            return
        try:
            waste_fraction = max(1e-06, min(0.95, float(waste_fraction)))
        except (TypeError, ValueError):
            return
        if separator_role is R.MBR_SEPARATOR:
            reject_fraction = 1.0 - self._asm2d_split_value(separator, 'permeate', 'H2O', default=0.2)
        else:
            reject_fraction = 1.0 - self._asm2d_split_value(separator, 'effluent', 'H2O', default=0.5)
        reject_fraction = max(1e-06, min(0.999999, float(reject_fraction)))
        recycle_fraction = 1.0 - waste_fraction
        retained_recycle = recycle_fraction * reject_fraction
        if retained_recycle <= 0.0 or retained_recycle >= 0.999999:
            return
        ratio_guess = retained_recycle / (1.0 - retained_recycle)
        lower = float(ratio_var.lb) if ratio_var.lb is not None else 0.0
        upper = float(ratio_var.ub) if ratio_var.ub is not None else max(lower, ratio_guess)
        ratio_guess = max(lower, min(upper, ratio_guess))
        try:
            ratio_var.set_value(ratio_guess, skip_validation=True)
        except Exception:
            pass

    def _seed_asm2d_reactor_biomass_guesses(self, units: Dict[str, Any], *, force: bool=False) -> None:
        """Seed reactor biomass states at the design MLSS before final IPOPT."""
        import math
        from pyomo.environ import value
        design_parameters = self._runtime_metadata.get('design_parameters')
        if not isinstance(design_parameters, dict):
            design_parameters = {}
        target_mlss = design_parameters.get('target_mlss_mg_l')
        recycle_parameters = design_parameters.get('recycle_ratios')
        recycle_modes = recycle_parameters.get('mode', {}) if isinstance(recycle_parameters, dict) else {}
        fixed_recycle_target = any((str(mode).lower() == 'fixed' for mode in recycle_modes.values()))
        force_target = force or target_mlss is not None or fixed_recycle_target
        mlss = self._asm2d_target_mlss_kg_m3(units)
        default_guesses = {component: fraction * mlss for component, fraction in ASM2D_BIOMASS_SEED_FRACTIONS.items()}
        default_guesses['X_TSS'] = mlss
        for reactor_id in self._asm2d_reactor_ids(units):
            reactor = units.get(reactor_id)
            if reactor is None:
                continue
            for port_name in ('outlet', 'inlet'):
                port = getattr(reactor, port_name, None)
                concentrations = getattr(port, 'conc_mass_comp', None)
                if concentrations is None:
                    continue
                valid_components = self._asm2d_port_component_names(port)
                guesses = dict(default_guesses)
                if 'X_TSS' not in valid_components and valid_components.intersection(ASM2D_BIOMASS_SEED_FRACTIONS):
                    total_fraction = sum(ASM2D_BIOMASS_SEED_FRACTIONS.values())
                    particulate_cod = mlss * MODIFIED_ASM2D_COD_TO_VSS
                    guesses = {component: particulate_cod * fraction / total_fraction for component, fraction in ASM2D_BIOMASS_SEED_FRACTIONS.items()}
                for component, guess in guesses.items():
                    try:
                        current = value(concentrations[0, component], exception=False)
                    except Exception:
                        continue
                    if not force_target:
                        try:
                            current_float = float(current)
                        except (TypeError, ValueError):
                            current_float = None
                        if current_float is not None and math.isfinite(current_float) and (current_float > 1e-10):
                            continue
                    try:
                        concentrations[0, component].set_value(guess, skip_validation=True)
                    except Exception:
                        continue

    def _seed_asm2d_substrate_consumption_guesses(self, units: Dict[str, Any]) -> None:
        """Pin reactor soluble-substrate guesses onto the productive PAO branch.

        Force-writes the ``ASM2D_SUBSTRATE_SEED_KG_M3`` post-consumption
        targets (S_F / S_A / S_PO4) onto every biological reactor port so the
        init handoff sits in the consumed-substrate basin regardless of which
        kinetic branch the sequential subsolves wandered into. Call it wherever
        ``_seed_asm2d_reactor_biomass_guesses`` is called — the two together
        define the seeded phenotype.
        """
        for reactor_id in self._asm2d_reactor_ids(units):
            reactor = units.get(reactor_id)
            if reactor is None:
                continue
            for port_name in ('outlet', 'inlet'):
                port = getattr(reactor, port_name, None)
                concentrations = getattr(port, 'conc_mass_comp', None)
                if concentrations is None:
                    continue
                valid_components = self._asm2d_port_component_names(port)
                for component, guess in ASM2D_SUBSTRATE_SEED_KG_M3.items():
                    if component not in valid_components:
                        continue
                    try:
                        concentrations[0, component].set_value(guess, skip_validation=True)
                    except Exception:
                        continue

    def _seed_asm2d_oxygen_transfer_guesses(self, units: Dict[str, Any]) -> None:
        """Seed aeration injection terms to match the current KLa and DO guess."""
        from pyomo.environ import value, units as pyunits
        for unit in units.values():
            kla = getattr(unit, 'KLa', None)
            volume = getattr(unit, 'volume', None)
            outlet = getattr(unit, 'outlet', None)
            injection = getattr(unit, 'injection', None)
            if kla is None or volume is None or outlet is None or (injection is None) or (not hasattr(outlet, 'conc_mass_comp')):
                continue
            oxygen_var = None
            try:
                oxygen_var = outlet.conc_mass_comp[0, 'S_O2']
            except Exception:
                try:
                    oxygen_var = outlet.conc_mass_comp[0, 'S_O']
                except Exception:
                    oxygen_var = None
            if oxygen_var is None:
                continue
            try:
                oxygen_value = value(oxygen_var, exception=False)
            except Exception:
                oxygen_value = None
            if oxygen_value is None or oxygen_value <= 0 or oxygen_value >= 0.008:
                oxygen_value = 0.002
                if not getattr(oxygen_var, 'fixed', False):
                    try:
                        oxygen_var.set_value(oxygen_value, skip_validation=True)
                    except Exception:
                        pass
            try:
                oxygen_component = 'S_O2'
                oxygen_injection = injection[0, 'Liq', oxygen_component]
            except Exception:
                try:
                    oxygen_component = 'S_O'
                    oxygen_injection = injection[0, 'Liq', oxygen_component]
                except Exception:
                    continue
            try:
                s_o_eq = getattr(unit, 'S_O_eq', 0.008)
                transfer_expr = kla * volume[0] * (s_o_eq - oxygen_var)
                target_units = pyunits.get_units(oxygen_injection)
                if target_units is None:
                    target_units = pyunits.kg / pyunits.s
                transfer_value = value(pyunits.convert(transfer_expr, to_units=target_units), exception=False)
            except Exception:
                transfer_value = None
            if transfer_value is None:
                continue
            try:
                oxygen_injection.set_value(max(float(transfer_value), 1e-12), skip_validation=True)
            except Exception:
                continue

    def _solve_asm2d_closing_recycle(self, warnings: List[str]) -> None:
        """Run a relaxed closing-recycle solve when the model is square."""
        if not self._runtime_metadata.get('asm2d_enable_relaxed_recycle_solve', False):
            warnings.append('Relaxed ASM2D recycle solve skipped; using successive-substitution tear closure')
            return
        try:
            from idaes.core.util.model_statistics import degrees_of_freedom
            from pyomo.environ import TerminationCondition
            from watertap.core.solvers import get_solver
        except ImportError:
            return
        try:
            if degrees_of_freedom(self._model) != 0:
                return
        except Exception:
            return
        solver = get_solver()
        solver.options['bound_push'] = 1e-08
        solver.options['max_iter'] = 300
        configured_options = self._runtime_metadata.get('asm2d_initialization_solver_options')
        if isinstance(configured_options, dict):
            for key, option_value in configured_options.items():
                solver.options[key] = option_value
        try:
            results = solver.solve(self._model, tee=False, load_solutions=False)
        except Exception as exc:
            warnings.append(f'Relaxed ASM2D recycle solve failed during initialization: {exc}')
            return
        termination = getattr(results.solver, 'termination_condition', None)
        if termination != TerminationCondition.optimal:
            warnings.append(f'Relaxed ASM2D recycle solve returned {termination}')
            return
        try:
            self._model.solutions.load_from(results)
        except Exception as exc:
            warnings.append(f'Relaxed ASM2D recycle solve solution load failed: {exc}')

    def _blend_port_state(self, source_port: Any, dest_port: Any, damping: float) -> float:
        """Blend values from a source port into a destination port."""
        from pyomo.environ import value
        max_delta = 0.0
        source_vars = getattr(source_port, 'vars', {})
        dest_vars = getattr(dest_port, 'vars', {})
        for var_name, dest_var in dest_vars.items():
            source_var = source_vars.get(var_name)
            if source_var is None or not hasattr(dest_var, 'is_indexed'):
                continue
            if dest_var.is_indexed():
                indexes = list(dest_var.keys())
            else:
                indexes = [None]
            for index in indexes:
                try:
                    dest_data = dest_var[index] if index is not None else dest_var
                    source_data = source_var[index] if index is not None else source_var
                except Exception:
                    continue
                if getattr(dest_data, 'fixed', False):
                    continue
                old_value = value(dest_data, exception=False)
                source_value = value(source_data, exception=False)
                if old_value is None or source_value is None:
                    continue
                try:
                    old_value = float(old_value)
                    source_value = float(source_value)
                except (TypeError, ValueError):
                    continue
                new_value = old_value + damping * (source_value - old_value)
                try:
                    dest_data.set_value(new_value, skip_validation=True)
                except Exception:
                    continue
                denominator = max(abs(old_value), abs(source_value), 1e-12)
                max_delta = max(max_delta, abs(source_value - old_value) / denominator)
        return max_delta

    def _asm2d_hydraulic_signature(self, units: Dict[str, Any]) -> Dict[str, float]:
        """Capture key ASM2D volumetric-flow guesses for recycle closure."""
        from pyomo.environ import value
        role_ports = ((R.FEED, ('outlet',)), (R.MIXER, ('outlet',)), (R.PRE_ANOXIC, ('inlet', 'outlet')), (R.AEROBIC, ('inlet', 'outlet')), (R.POST_ANOXIC, ('inlet', 'outlet')), (R.INTERNAL_RECYCLE_SPLITTER, ('inlet', 'forward', 'internal_recycle')), (R.INTERNAL_RECYCLE_PRESSURE, ('inlet', 'outlet')), (R.MBR_SEPARATOR, ('inlet', 'permeate', 'retentate')), (R.SECONDARY_CLARIFIER, ('inlet', 'effluent', 'underflow')), (R.SLUDGE_SPLITTER, ('inlet', 'waste', 'recycle')), (R.RAS_PRESSURE, ('inlet', 'outlet')), (R.PRODUCT_EFFLUENT, ('inlet',)))
        ports = []
        for role, port_names in role_ports:
            for unit_id in self._roles.all_for(role, units):
                for port_name in port_names:
                    ports.append((unit_id, port_name))
        signature: Dict[str, float] = {}
        for unit_id, port_name in ports:
            unit = units.get(unit_id)
            port = getattr(unit, port_name, None) if unit is not None else None
            flow_vol = getattr(port, 'flow_vol', None)
            if flow_vol is None:
                continue
            try:
                raw_value = value(flow_vol[0], exception=False)
            except Exception:
                raw_value = None
            if raw_value is None:
                continue
            try:
                signature[f'{unit_id}.{port_name}.flow_vol'] = float(raw_value)
            except (TypeError, ValueError):
                continue
        return signature

    def _hydraulic_signature_delta(self, before: Dict[str, float], after: Dict[str, float]) -> float:
        """Return the largest relative change between two hydraulic signatures."""
        max_delta = 0.0
        for key, old_value in before.items():
            if key not in after:
                continue
            new_value = after[key]
            denominator = max(abs(old_value), abs(new_value), 1e-12)
            max_delta = max(max_delta, abs(new_value - old_value) / denominator)
        return max_delta

    def _close_asm2d_recycles_successive_substitution(self, units: Dict[str, Any], *, tolerance: float=0.0001, max_iterations: int=9, seed_states: bool=True) -> Dict[str, Any]:
        """Close ASM2D IR/RAS tear streams by damped successive substitution."""
        ir_split = self._rsingle_id(units, R.INTERNAL_RECYCLE_SPLITTER)
        ir_press = self._rsingle_id(units, R.INTERNAL_RECYCLE_PRESSURE)
        sludge = self._rsingle_id(units, R.SLUDGE_SPLITTER)
        ras_press = self._rsingle_id(units, R.RAS_PRESSURE)
        mixer = self._rsingle_id(units, R.MIXER)
        candidate_edges = [(ir_split, 'internal_recycle', ir_press, 'inlet'), (ir_press, 'outlet', mixer, 'internal_recycle'), (ir_split, 'internal_recycle', mixer, 'internal_recycle'), (sludge, 'recycle', ras_press, 'inlet'), (ras_press, 'outlet', mixer, 'ras'), (sludge, 'recycle', mixer, 'ras')]
        pressure_ids = {unit_id for unit_id in (ir_press, ras_press) if unit_id is not None}
        edges = []
        seen_destinations = set()
        for src_id, src_port_name, dst_id, dst_port_name in candidate_edges:
            src = units.get(src_id)
            dst = units.get(dst_id)
            if src is None or dst is None:
                continue
            if not hasattr(src, src_port_name) or not hasattr(dst, dst_port_name):
                continue
            key = (dst_id, dst_port_name)
            if key in seen_destinations:
                continue
            seen_destinations.add(key)
            edges.append((src_id, src_port_name, getattr(src, src_port_name), dst_id, dst_port_name, getattr(dst, dst_port_name)))
        history = []
        converged = not edges
        for iteration in range(1, max_iterations + 1):
            if iteration <= 3:
                damping = 0.3
            elif iteration <= 6:
                damping = 0.7
            else:
                damping = 1.0
            max_delta = 0.0
            hydraulic_before = self._asm2d_hydraulic_signature(units)
            self._seed_asm2d_forward_hydraulic_guesses(units, seed_states=seed_states)
            hydraulic_after_seed = self._asm2d_hydraulic_signature(units)
            max_delta = max(max_delta, self._hydraulic_signature_delta(hydraulic_before, hydraulic_after_seed))
            for _src_id, _src_port, source_port, _dst_id, _dst_port, dest_port in edges:
                max_delta = max(max_delta, self._blend_port_state(source_port, dest_port, damping))
                if _dst_id in pressure_ids and _dst_port == 'inlet':
                    pressure_unit = units.get(_dst_id)
                    if pressure_unit is not None:
                        self._seed_pressure_changer_output_guesses(pressure_unit)
            self._clip_tiny_negative_nonnegative_vars()
            self._apply_asm2d_trace_concentration_floors(units)
            hydraulic_after_blend = self._asm2d_hydraulic_signature(units)
            max_delta = max(max_delta, self._hydraulic_signature_delta(hydraulic_after_seed, hydraulic_after_blend))
            history.append({'iteration': iteration, 'damping': damping, 'max_relative_delta': max_delta})
            if max_delta <= tolerance:
                converged = True
                break
        return {'converged': converged, 'iterations': len(history), 'tolerance': tolerance, 'edges': [f'{src_id}.{src_port}->{dst_id}.{dst_port}' for src_id, src_port, _source, dst_id, dst_port, _dest in edges], 'history': history}

    def _run_asm2d_sequential_decomposition(self, units: Dict[str, Any], unit_methods: Dict[str, InitMethod], state_args: Dict[str, Dict], *, tear_method: str, iteration_limit: int) -> Tuple[List[InitializationResult], List[str]]:
        """Run WaterTAP-style SD initialization for ASM2D recycle flowsheets."""
        from pyomo.network import SequentialDecomposition
        warnings: List[str] = []
        results: List[InitializationResult] = []
        seen_unit_ids = set()
        unit_ids_by_block = {id(block): unit_id for unit_id, block in units.items()}
        seq = SequentialDecomposition()
        seq.options.select_tear_method = 'heuristic'
        seq.options.tear_method = tear_method
        seq.options.iterLim = iteration_limit
        sd_tolerance = self._runtime_metadata.get('asm2d_sd_almost_equal_tolerance')
        if sd_tolerance is not None:
            try:
                seq.options.almost_equal_tol = float(sd_tolerance)
            except (TypeError, ValueError):
                pass
        reactor_ids = self._asm2d_reactor_ids(units)
        first_reactor = units.get(reactor_ids[0]) if reactor_ids else None
        if first_reactor is not None and hasattr(first_reactor, 'inlet'):
            seq.set_guesses_for(first_reactor.inlet, self._asm2d_tear_guesses(units))
        try:
            import idaes.logger as idaeslog
            outlvl = idaeslog.ERROR
        except Exception:
            outlvl = None
        optarg = {'bound_push': 1e-08}
        configured_optarg = self._runtime_metadata.get('asm2d_initialization_solver_options')
        if isinstance(configured_optarg, dict):
            optarg.update(configured_optarg)
        solver_options = {'optarg': optarg, 'solver': 'ipopt-watertap'}
        if outlvl is not None:
            solver_options['outlvl'] = outlvl

        def initialize_block(unit: Any) -> None:
            self._clip_tiny_negative_nonnegative_vars()
            self._condition_asm2d_reactor_inlet_for_initialization(unit)
            unit_id = unit_ids_by_block.get(id(unit), getattr(unit, 'local_name', str(unit)))
            method = unit_methods.get(unit_id, InitMethod.INITIALIZE)
            if method == InitMethod.NONE:
                if unit_id not in seen_unit_ids:
                    results.append(InitializationResult(unit_id=unit_id, status=InitStatus.SKIPPED, message='No initialization required'))
                    seen_unit_ids.add(unit_id)
                return
            args = state_args.get(unit_id)
            retry_suffix = ''
            retry_args = args
            try:
                self._initialize_with_method(unit, unit_id, method, args, solver_options)
            except Exception as init_error:
                if retry_args is None:
                    retry_args = self._derive_state_args(unit)
                    if retry_args:
                        try:
                            self._initialize_with_method(unit, unit_id, method, retry_args, solver_options)
                            retry_suffix = ' after retrying with derived inlet state'
                        except Exception as retry_error:
                            init_error = retry_error
                    else:
                        retry_args = None
                if retry_suffix == '':
                    if retry_args is None:
                        if self._can_accept_locally_infeasible_asm2d_unit(unit_id, unit):
                            warnings.append(f'{unit_id} standalone initialization returned infeasible; using its finite state as an IPOPT initial guess')
                            retry_suffix = ' with finite locally infeasible state accepted'
                        else:
                            raise init_error
                    relaxed_pressure_vars = self._fixed_outlet_pressure_vars(unit)
                    if retry_suffix == '':
                        if not relaxed_pressure_vars:
                            if self._can_accept_locally_infeasible_asm2d_unit(unit_id, unit):
                                warnings.append(f'{unit_id} standalone initialization returned infeasible; using its finite state as an IPOPT initial guess')
                                retry_suffix = ' with finite locally infeasible state accepted'
                            else:
                                raise init_error
                        else:
                            try:
                                relaxed_count = self._initialize_with_relaxed_outlet_pressure(unit, unit_id, method, retry_args, solver_options)
                                retry_suffix = f' after retrying with {relaxed_count} relaxed outlet pressure spec{('s' if relaxed_count != 1 else '')}'
                            except Exception as relaxed_error:
                                if self._can_accept_locally_infeasible_asm2d_unit(unit_id, unit):
                                    warnings.append(f'{unit_id} standalone initialization returned infeasible; using its finite state as an IPOPT initial guess')
                                    retry_suffix = ' with finite locally infeasible state accepted'
                                else:
                                    raise relaxed_error
            self._clip_tiny_negative_nonnegative_vars()
            if unit_id not in seen_unit_ids:
                results.append(InitializationResult(unit_id=unit_id, status=InitStatus.SUCCESS, solve_status='converged', message=f'Initialized by ASM2D SequentialDecomposition ({tear_method}){retry_suffix}'))
                seen_unit_ids.add(unit_id)
        try:
            seq.run(self._model, initialize_block)
        except Exception as exc:
            setattr(exc, '_partial_unit_results', list(results))
            raise
        clipped = self._clip_tiny_negative_nonnegative_vars()
        trace_floors = self._apply_asm2d_trace_concentration_floors(units)
        if clipped:
            warnings.append(f'Clipped {clipped} tiny negative ASM2D state initial guesses')
        if trace_floors:
            warnings.append(f'Applied {trace_floors} ASM2D trace concentration floor(s)')
        return (results, warnings)

    def _initialize_asm2d_recycle_flowsheet(self, units: Dict[str, Any], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], connections: Optional[List[Dict[str, Any]]]=None) -> FlowsheetInitResult:
        """Initialize ASM2D recycle templates using the upstream WaterTAP pattern."""
        self._asm2d_recycle_connections = list(connections or [])
        warnings: List[str] = []
        restore_srt = self._relax_srt_for_initialization(units)
        restore_do_target = self._relax_do_target_for_initialization()
        restore_chem_p = self._relax_chemical_p_target_for_initialization(units)
        restore_alkalinity = self._relax_alkalinity_target_for_initialization(units)
        original_fixity_snapshot = self._variable_fixity_snapshot()
        restore_aerobic_do = self._fix_aerobic_do_for_initialization(units)
        initialization_fixity_snapshot = self._variable_fixity_snapshot()
        self._apply_asm2d_trace_concentration_floors(units)
        self._seed_asm2d_recycle_ratio_split_guesses(units)
        self._seed_asm2d_reactor_biomass_guesses(units, force=True)
        self._seed_asm2d_substrate_consumption_guesses(units)
        completed_successfully = False
        try:
            last_error: Optional[Exception] = None
            unit_results: List[InitializationResult] = []
            for tear_method, iteration_limit in (('Wegstein', 1), ('Direct', 3)):
                try:
                    unit_results, sd_warnings = self._run_asm2d_sequential_decomposition(units, unit_methods or {}, state_args or {}, tear_method=tear_method, iteration_limit=iteration_limit)
                    warnings.extend(sd_warnings)
                    if tear_method != 'Wegstein':
                        warnings.append('ASM2D SequentialDecomposition used Direct tear iteration')
                    break
                except Exception as exc:
                    last_error = exc
                    partial_results = getattr(exc, '_partial_unit_results', None)
                    if partial_results:
                        unit_results = partial_results
                    clipped = self._clip_tiny_negative_nonnegative_vars()
                    self._apply_asm2d_trace_concentration_floors(units)
                    warnings.append(f'ASM2D SequentialDecomposition {tear_method} failed: {exc}')
                    if clipped:
                        warnings.append(f'Clipped {clipped} tiny negative state guesses before retry')
                    self._restore_newly_fixed_variables(initialization_fixity_snapshot)
                    initialized_ids = {str(result.unit_id).rsplit('.', 1)[-1] for result in unit_results}
                    core_ids = self._asm2d_core_ids(units)
                    if core_ids.issubset(initialized_ids):
                        warnings.append(f'Using partial ASM2D {tear_method} state after one recycle pass; final IPOPT solve will close the recycle equations')
                        break
            else:
                initialized_ids = {str(result.unit_id).rsplit('.', 1)[-1] for result in unit_results}
                core_ids = self._asm2d_core_ids(units)
                if not core_ids.issubset(initialized_ids):
                    return FlowsheetInitResult(success=False, unit_results=unit_results, init_order=list(units), message=f'ASM2D recycle initialization failed: {last_error}', warnings=warnings, errors=[str(last_error)], recipe_name='asm2d_recycle')
                warnings.append('Proceeding with partial ASM2D SequentialDecomposition state; final IPOPT solve will close the recycle equations')
            self._restore_newly_fixed_variables(initialization_fixity_snapshot)
            self._seed_asm2d_recycle_ratio_split_guesses(units)
            self._seed_asm2d_hydraulic_retention_times(units)
            self._seed_asm2d_reactor_biomass_guesses(units)
            self._seed_asm2d_substrate_consumption_guesses(units)
            self._apply_asm2d_trace_concentration_floors(units)
            recycle_closure = self._close_asm2d_recycles_successive_substitution(units)
            self._seed_asm2d_hydraulic_retention_times(units)
            if recycle_closure['edges']:
                warnings.append(f'ASM2D recycle successive-substitution {('converged' if recycle_closure['converged'] else 'stopped')} after {recycle_closure['iterations']} iteration(s); edges={', '.join(recycle_closure['edges'])}')
            self._solve_asm2d_closing_recycle(warnings)
            restore_aerobic_do()
            self._seed_asm2d_reactor_biomass_guesses(units)
            self._seed_asm2d_substrate_consumption_guesses(units)
            self._seed_asm2d_oxygen_transfer_guesses(units)
            self._seed_srt_waste_split(units)
            self._seed_asm2d_recycle_ratio_split_guesses(units)
            recycle_closure = self._close_asm2d_recycles_successive_substitution(units)
            self._seed_asm2d_forward_state_guesses(units)
            self._seed_asm2d_hydraulic_retention_times(units)
            self._seed_asm2d_oxygen_transfer_guesses(units)
            self._apply_asm2d_trace_concentration_floors(units)
            if recycle_closure['edges']:
                warnings.append(f'ASM2D recycle final successive-substitution {('converged' if recycle_closure['converged'] else 'stopped')} after {recycle_closure['iterations']} iteration(s)')
            self._solve_asm2d_closing_recycle(warnings)
            completed_successfully = True
            return FlowsheetInitResult(success=True, unit_results=unit_results, init_order=[result.unit_id for result in unit_results], message='ASM2D recycle initialization complete', warnings=warnings, recipe_name='asm2d_recycle')
        finally:
            restore_aerobic_do()
            restore_srt()
            if completed_successfully:
                self._reseed_chemical_p_target_dose(units)
            restore_chem_p()
            restore_alkalinity()
            restore_do_target()
            if completed_successfully:
                final_recycle_closure = {'edges': [], 'converged': True, 'iterations': 0}
                for _ in range(2):
                    self._seed_srt_waste_split(units)
                    self._seed_asm2d_recycle_ratio_split_guesses(units)
                    final_recycle_closure = self._close_asm2d_recycles_successive_substitution(units)
                self._seed_asm2d_forward_state_guesses(units)
                self._apply_asm2d_trace_concentration_floors(units)
                if final_recycle_closure['edges'] and (not final_recycle_closure['converged']):
                    warnings.append(f'ASM2D recycle post-restore successive-substitution stopped after {final_recycle_closure['iterations']} iteration(s)')
                self._seed_asm2d_hydraulic_retention_times(units)
                self._seed_asm2d_oxygen_transfer_guesses(units)
                self._apply_asm2d_trace_concentration_floors(units)
            else:
                self._seed_asm2d_reactor_biomass_guesses(units)
                self._seed_srt_waste_split(units)
                self._seed_asm2d_oxygen_transfer_guesses(units)
                self._apply_asm2d_trace_concentration_floors(units)
            self._restore_newly_fixed_variables(original_fixity_snapshot)

    def _initialize_sequential_playbook(self, *, recipe_name: str, topology: Dict[str, Any], units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], tear_streams: Optional[List[Tuple[str, str]]]=None, warnings: Optional[List[str]]=None) -> FlowsheetInitResult:
        """Run a topology-classified SequentialDecomposition playbook."""
        effective_tears = tear_streams
        if effective_tears is None:
            effective_tears = topology.get('tear_streams') or None
        init_order = self.get_initialization_order(units, connections, effective_tears)
        results: List[InitializationResult] = []
        playbook_warnings = list(warnings or [])
        for unit_id in init_order:
            if unit_id not in units:
                continue
            unit = units[unit_id]
            method = (unit_methods or {}).get(unit_id, InitMethod.INITIALIZE)
            args = (state_args or {}).get(unit_id)
            incoming_connections = [conn for conn in connections if self._connection_dest_unit(conn) == unit_id]
            outgoing_connections = [conn for conn in connections if self._connection_source_unit(conn) == unit_id]
            arc_blocks = []
            inlet_fixity_snapshot: List[Tuple[Any, bool]] = []
            if method == InitMethod.INITIALIZE_BUILD:
                arc_blocks = self._expanded_arc_blocks(incoming_connections + outgoing_connections)
                inlet_fixity_snapshot = self._snapshot_dest_port_fixity(unit, incoming_connections)
                for block in arc_blocks:
                    block.deactivate()
            for conn in incoming_connections:
                src_unit_id = self._connection_source_unit(conn)
                if src_unit_id in units:
                    src_port_name = conn.get('source_port', 'outlet')
                    dst_port_name = conn.get('dest_port', 'inlet')
                    src_unit = units[src_unit_id]
                    if hasattr(src_unit, src_port_name) and hasattr(unit, dst_port_name):
                        self.propagate_state(getattr(src_unit, src_port_name), getattr(unit, dst_port_name))
            result = self.initialize_unit(unit, unit_id, method, args)
            results.append(result)
            if result.status == InitStatus.DEGRADED:
                playbook_warnings.append(f'{unit_id} initialization DEGRADED: {result.message}')
            if result.status != InitStatus.FAILED:
                if inlet_fixity_snapshot:
                    self._restore_port_fixity(inlet_fixity_snapshot)
                for conn in outgoing_connections:
                    dst_unit_id = self._connection_dest_unit(conn)
                    if dst_unit_id not in units:
                        continue
                    src_port_name = conn.get('source_port', 'outlet')
                    dst_port_name = conn.get('dest_port', 'inlet')
                    dst_unit = units[dst_unit_id]
                    if hasattr(unit, src_port_name) and hasattr(dst_unit, dst_port_name):
                        self.propagate_state(getattr(unit, src_port_name), getattr(dst_unit, dst_port_name))
            for block in arc_blocks:
                block.activate()
            if result.status == InitStatus.FAILED:
                return FlowsheetInitResult(success=False, unit_results=results, init_order=init_order, message=f"Initialization failed at unit '{unit_id}': {result.message}", warnings=playbook_warnings, errors=[result.message], failed_unit=unit_id, recipe_name=recipe_name, topology=topology)
        return FlowsheetInitResult(success=True, unit_results=results, init_order=init_order, message=f'{recipe_name} initialization complete', warnings=playbook_warnings, recipe_name=recipe_name, topology=topology)

    def _initialize_pressure_membrane_flowsheet(self, *, topology: Dict[str, Any], units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], tear_streams: Optional[List[Tuple[str, str]]]) -> FlowsheetInitResult:
        """Initialize RO/NF pressure-membrane trains from the topology graph."""
        return self._initialize_sequential_playbook(recipe_name=topology['recipe_name'], topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=tear_streams)

    def _initialize_mvc_flowsheet(self, *, topology: Dict[str, Any], units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], tear_streams: Optional[List[Tuple[str, str]]]) -> FlowsheetInitResult:
        """Initialize MVC thermal trains with explicit condenser heat guesses."""
        warnings = []
        if not self._thermal_couplings:
            warnings.append('MVC recipe did not receive explicit thermal_couplings; falling back to flowsheet scan')
        return self._initialize_sequential_playbook(recipe_name=topology['recipe_name'], topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=tear_streams, warnings=warnings)

    def _initialize_first_class_unit_family(self, *, topology: Dict[str, Any], units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], tear_streams: Optional[List[Tuple[str, str]]]) -> FlowsheetInitResult:
        """Initialize non-template first-class families through SD dispatch."""
        return self._initialize_sequential_playbook(recipe_name=topology['recipe_name'], topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=tear_streams)

    def _adm1_tear_connections(self, connections: List[Dict[str, Any]], tear_streams: Optional[List[Tuple[str, str]]]) -> List[Dict[str, Any]]:
        """Return the ADM1 liquid recycle connections to tear for warm-start.

        Prefers explicit ``tear_stream`` markers (the recycle family templates mark
        the recirculation / membrane-retentate / sludge-return edge); falls back to
        the heuristic graph-cycle tear edges.
        """
        explicit = [conn for conn in connections if conn.get('tear_stream')]
        if explicit:
            return explicit
        tear_edges = set(tear_streams or [])
        return [conn for conn in connections if (self._connection_source_unit(conn), self._connection_dest_unit(conn)) in tear_edges]

    def _seed_adm1_active_tear_composition(self, port: Any) -> int:
        """Seed an ADM1 recycle tear with active methanogenic biomass.

        The recycle tear is an initial guess only.  Its flow is handled elsewhere;
        this method prevents the first recycle pass from blending the fresh feed's
        near-zero biomass into the digester inlet and steering IPOPT toward the
        partial-washout basin.
        """
        conc = getattr(port, 'conc_mass_comp', None)
        if conc is None or not hasattr(conc, 'is_indexed'):
            return 0
        seeds = self._adm1_methanogenic_recycle_seed()
        try:
            indexes = list(conc.keys()) if conc.is_indexed() else [None]
        except Exception:
            return 0
        seeded = 0
        for index in indexes:
            if index is None:
                component = None
            elif isinstance(index, tuple):
                component = index[-1]
            else:
                component = index
            if component not in seeds:
                continue
            try:
                var_data = conc[index] if index is not None else conc
            except Exception:
                continue
            if getattr(var_data, 'fixed', False):
                continue
            try:
                var_data.set_value(seeds[component], skip_validation=True)
                seeded += 1
            except TypeError:
                try:
                    var_data.set_value(seeds[component])
                    seeded += 1
                except Exception:
                    continue
            except Exception:
                continue
        return seeded

    def _seed_adm1_tear_initial_guesses(self, *, units: Dict[str, Any], connections: List[Dict[str, Any]], tear_connections: List[Dict[str, Any]]) -> int:
        """Seed each torn recycle-mixer inlet from its fresh-feed inlet.

        Before the first forward pass the recycle stream is unknown; seeding the
        torn mixer inlet with the fresh-feed hydraulics gives the digester a
        sensible blended inlet to initialize from. ADM1 biomass/VFA composition is
        then overwritten with the mature methanogenic seed so the recycle tear
        carries active biomass instead of the fresh feed's near-zero inoculum. The
        damped successive substitution then drives the torn inlet to the converged
        recycle.
        """
        seeded = 0
        tear_signatures = {self._connection_signature(conn) for conn in tear_connections}
        for tear_conn in tear_connections:
            dest_unit_id = self._connection_dest_unit(tear_conn)
            dest_unit = units.get(dest_unit_id)
            if dest_unit is None:
                continue
            dest_port = getattr(dest_unit, tear_conn.get('dest_port', 'inlet'), None)
            if dest_port is None:
                continue
            self._unfix_port_state_data(dest_port)
            fresh_source = None
            for conn in connections:
                if self._connection_signature(conn) in tear_signatures:
                    continue
                if self._connection_dest_unit(conn) != dest_unit_id:
                    continue
                src_unit = units.get(self._connection_source_unit(conn))
                if src_unit is None:
                    continue
                source_port = getattr(src_unit, conn.get('source_port', 'outlet'), None)
                if source_port is not None:
                    fresh_source = source_port
                    break
            if fresh_source is not None:
                self._blend_port_state(fresh_source, dest_port, 1.0)
                self._seed_adm1_active_tear_composition(dest_port)
                self._scale_liquid_port_flow_guess(dest_port, 0.05)
            seeded += 1
        return seeded

    def _scale_liquid_port_flow_guess(self, port: Any, factor: float) -> None:
        """Scale a liquid port's ``flow_vol`` guess (recycle tear seeding)."""
        from pyomo.environ import value
        flow_vol = getattr(port, 'flow_vol', None)
        if flow_vol is None or not hasattr(flow_vol, 'is_indexed'):
            return
        try:
            indexes = list(flow_vol.keys()) if flow_vol.is_indexed() else [None]
        except Exception:
            return
        for index in indexes:
            try:
                var_data = flow_vol[index] if index is not None else flow_vol
                raw = value(var_data, exception=False)
            except Exception:
                continue
            if raw is None:
                continue
            try:
                var_data.set_value(max(float(raw) * factor, 1e-09), skip_validation=True)
            except Exception:
                continue

    def _is_adm1_retained_solids_recycle(self, units: Dict[str, Any]) -> bool:
        """Return True for AnMBR/contact-style ADM1 retained-solids recycles."""
        if self._primary_srt_target_spec() is None:
            return False
        try:
            return self._rsingle(units, R.ANAEROBIC_THICKENER) is not None
        except Exception:
            return False

    def _clear_adm1_retained_solids_scaling_suffixes(self) -> int:
        """Drop scaling suffix values that destabilize retained-solids ADM1 NLPs.

        The retained-solids recycle trains solve cleanly from the active basin
        without exported user-scaling weights. Keeping the model equations and
        final strict-optimal gate unchanged, clear only the scaling suffix values
        after initialization so IPOPT's final solve can use its internal scaling.
        """
        if self._model is None or not hasattr(self._model, 'component_data_objects'):
            return 0
        try:
            from pyomo.environ import Suffix
        except Exception:
            return 0
        cleared = 0
        for suffix in self._model.component_data_objects(Suffix, descend_into=True):
            if getattr(suffix, 'local_name', '') != 'scaling_factor':
                continue
            try:
                cleared += len(list(suffix.items()))
                suffix.clear()
            except Exception:
                continue
        return cleared

    def _seed_adm1_retained_solids_active_path(self, units: Dict[str, Any]) -> int:
        """Seed retained-solids ADM1 recycle-path concentrations at the active root."""
        seeded = 0
        try:
            mixer = self._rsingle(units, R.MIXER)
        except Exception:
            mixer = None
        try:
            digester = self._rsingle(units, R.ANAEROBIC_METHANOGENIC)
        except Exception:
            digester = None
        try:
            separator = self._rsingle(units, R.ANAEROBIC_THICKENER)
        except Exception:
            separator = None
        try:
            recuperation = self._rsingle(units, R.ANAEROBIC_RECUPERATION)
        except Exception:
            recuperation = None
        port_specs = ((mixer, ('recycle', 'outlet')), (digester, ('inlet',)), (separator, ('inlet', 'retentate', 'underflow')), (recuperation, ('inlet', 'recycle')))
        for unit, port_names in port_specs:
            if unit is None:
                continue
            for port_name in port_names:
                port = getattr(unit, port_name, None)
                if port is not None:
                    seeded += self._seed_adm1_active_tear_composition(port)
        if digester is not None:
            liquid_seed = self._adm1_methanogenic_recycle_seed()
            for t in getattr(digester.liquid_phase, 'properties_out', {}):
                conc = getattr(digester.liquid_phase.properties_out[t], 'conc_mass_comp', None)
                if conc is None:
                    continue
                for comp, guess in liquid_seed.items():
                    if comp in conc:
                        try:
                            conc[comp].set_value(guess, skip_validation=True)
                            seeded += 1
                        except TypeError:
                            try:
                                conc[comp].set_value(guess)
                                seeded += 1
                            except Exception:
                                continue
                        except Exception:
                            continue
        return seeded

    def _initialize_adm1_recycle_flowsheet(self, *, topology: Dict[str, Any], units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], tear_streams: Optional[List[Tuple[str, str]]]) -> FlowsheetInitResult:
        """Warm-start an ADM1 liquid-recycle digester train by damped successive
        substitution (roadmap A4 PR2).

        The structural analogue of the ASM2D / gas-membrane recycle closures, for
        the EGSB recirculation, AnMBR membrane-retentate, and anaerobic-contact
        sludge-return loops: open the explicit recycle tear, initialize the acyclic
        cascade (feed -> mixer -> digester -> separator(s) -> products) once, then
        close the torn recycle inlet by damped successive substitution (ramping the
        blend factor 0.3 -> 0.7 -> 1.0) until the recycle state stops moving. The
        digester inits through its own two-phase routine inside the cascade; the
        modified-ADM1 liquid state (conc + flow + temperature + pressure + the
        strong-ion cations/anions) is blended generically through the port vars, so
        the vapor S_h2 / reaction pH / trace ions stay solver-determined.
        """
        tear_connections = self._adm1_tear_connections(connections, tear_streams)
        if not tear_connections:
            return self._initialize_first_class_unit_family(topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=tear_streams)
        tear_signatures = {self._connection_signature(conn) for conn in tear_connections}
        active_connections = [conn for conn in connections if self._connection_signature(conn) not in tear_signatures]
        init_order = self.get_initialization_order(units, connections, tear_streams=tear_streams)
        tear_arc_blocks = self._expanded_arc_blocks(tear_connections)
        for block in tear_arc_blocks:
            block.deactivate()
        restore_srt = self._relax_srt_for_initialization(units)
        srt_hydraulic_seed = self._seed_adm1_srt_hydraulic_point(units)
        self._record_initializer_tactic(name='adm1_recycle_warm_start', description='Opened the explicit ADM1 liquid recycle tear, initialized the acyclic digester cascade, and closed the tear by damped successive substitution', units=list(units))
        warnings: List[str] = []
        seeded = self._seed_adm1_tear_initial_guesses(units=units, connections=connections, tear_connections=tear_connections)
        if seeded:
            warnings.append(f'Seeded {seeded} ADM1 recycle tear inlet guess(es)')
        history: List[Dict[str, Any]] = []
        last_results: Dict[str, InitializationResult] = {}
        converged = False
        max_iterations = int(self._runtime_metadata.get('adm1_recycle_warm_start_iterations', 12))
        tolerance = float(self._runtime_metadata.get('adm1_recycle_warm_start_tolerance', 0.0001))
        try:
            for iteration in range(1, max_iterations + 1):
                if iteration <= 3:
                    damping = 0.3
                elif iteration <= 6:
                    damping = 0.7
                else:
                    damping = 1.0
                max_delta = 0.0
                for unit_id in init_order:
                    unit = units.get(unit_id)
                    if unit is None:
                        continue
                    incoming_connections = [conn for conn in active_connections if self._connection_dest_unit(conn) == unit_id]
                    outgoing_connections = [conn for conn in connections if self._connection_source_unit(conn) == unit_id]
                    for conn in incoming_connections:
                        src_unit = units.get(self._connection_source_unit(conn))
                        if src_unit is None:
                            continue
                        source_port = getattr(src_unit, conn.get('source_port', 'outlet'), None)
                        dest_port = getattr(unit, conn.get('dest_port', 'inlet'), None)
                        if source_port is not None and dest_port is not None:
                            self.propagate_state(source_port, dest_port)
                    method = (unit_methods or {}).get(unit_id, InitMethod.INITIALIZE)
                    args = (state_args or {}).get(unit_id)
                    result = self.initialize_unit(unit, unit_id, method, args)
                    last_results[unit_id] = result
                    if result.status == InitStatus.FAILED:
                        return FlowsheetInitResult(success=False, status=InitStatus.FAILED, unit_results=list(last_results.values()), init_order=init_order, message=f"ADM1 recycle warm-start failed at unit '{unit_id}': {result.message}", warnings=warnings, errors=[result.message], failed_unit=unit_id, recipe_name='adm1_recycle_warm_start', topology=topology)
                    for conn in outgoing_connections:
                        dst_unit = units.get(self._connection_dest_unit(conn))
                        if dst_unit is None:
                            continue
                        source_port = getattr(unit, conn.get('source_port', 'outlet'), None)
                        dest_port = getattr(dst_unit, conn.get('dest_port', 'inlet'), None)
                        if source_port is None or dest_port is None:
                            continue
                        if self._connection_signature(conn) in tear_signatures:
                            self._unfix_port_state_data(dest_port)
                            max_delta = max(max_delta, self._blend_port_state(source_port, dest_port, damping))
                        else:
                            self.propagate_state(source_port, dest_port)
                self._clip_tiny_negative_nonnegative_vars()
                history.append({'iteration': iteration, 'damping': damping, 'max_relative_delta': max_delta})
                if max_delta <= tolerance:
                    converged = True
                    break
            for conn in tear_connections:
                src_unit = units.get(self._connection_source_unit(conn))
                dst_unit = units.get(self._connection_dest_unit(conn))
                if src_unit is None or dst_unit is None:
                    continue
                source_port = getattr(src_unit, conn.get('source_port', 'outlet'), None)
                dest_port = getattr(dst_unit, conn.get('dest_port', 'inlet'), None)
                if source_port is not None and dest_port is not None:
                    self._unfix_port_state_data(dest_port)
                    self._blend_port_state(source_port, dest_port, 1.0)
            self._clip_tiny_negative_nonnegative_vars()
        finally:
            for block in tear_arc_blocks:
                block.activate()
            restore_srt()
        self._seed_srt_waste_split(units)
        restored_srt_hydraulic_seed = self._seed_adm1_srt_hydraulic_point(units)
        if restored_srt_hydraulic_seed is not None:
            srt_hydraulic_seed = restored_srt_hydraulic_seed
        active_path_seeded = 0
        scaling_suffix_values_cleared = 0
        if self._is_adm1_retained_solids_recycle(units):
            active_path_seeded = self._seed_adm1_retained_solids_active_path(units)
            scaling_suffix_values_cleared = self._clear_adm1_retained_solids_scaling_suffixes()
            if scaling_suffix_values_cleared:
                self._record_initializer_tactic(name='adm1_retained_solids_internal_scaling', description='Cleared exported user-scaling suffix values for retained-solids ADM1 recycle final solve; equations and strict optimal gate are unchanged', units=list(units))
        tear_descriptions = [f'{self._connection_source_unit(conn)}.{conn.get('source_port')}->{self._connection_dest_unit(conn)}.{conn.get('dest_port')}' for conn in tear_connections]
        final_tear_residual = float(history[-1]['max_relative_delta']) if history else None
        warnings.append(f'ADM1 recycle warm-start {('converged' if converged else 'stopped')} after {len(history)} iteration(s) at residual {(final_tear_residual if final_tear_residual is not None else 'n/a')} (tolerance {tolerance}); tears={', '.join(tear_descriptions)}')
        ordered_results = [last_results[unit_id] for unit_id in init_order if unit_id in last_results]
        init_status = InitStatus.SUCCESS if converged else InitStatus.DEGRADED
        if not converged:
            warnings.append(f'ADM1 recycle warm-start DEGRADED: the recycle tear did not close to tolerance {tolerance} within {len(history)} iteration(s) (final residual {final_tear_residual}); the downstream strict solve will report its own feasibility honestly.')
        return FlowsheetInitResult(success=True, status=init_status, unit_results=ordered_results, init_order=init_order, message='ADM1 recycle warm-start complete', warnings=warnings, recipe_name='adm1_recycle_warm_start', topology={**topology, 'adm1_recycle_warm_start': {'converged': converged, 'iterations': len(history), 'tolerance': tolerance, 'final_tear_residual': final_tear_residual, 'tears': tear_descriptions, 'srt_hydraulic_seed': srt_hydraulic_seed, 'active_path_seeded_values': active_path_seeded, 'scaling_suffix_values_cleared': scaling_suffix_values_cleared, 'history': history}})

    def _gas_tear_connections(self, connections: List[Dict[str, Any]], tear_streams: Optional[List[Tuple[str, str]]]) -> List[Dict[str, Any]]:
        """Return the gas recycle connections that should be torn for warm-start."""
        explicit = [conn for conn in connections if conn.get('tear_stream')]
        if explicit:
            return explicit
        tear_edges = set(tear_streams or [])
        return [conn for conn in connections if (self._connection_source_unit(conn), self._connection_dest_unit(conn)) in tear_edges]

    def _port_state_data(self, port: Any) -> List[Any]:
        """Return scalar VarData objects exposed by an IDAES port."""
        data = []
        seen = set()
        for port_var in getattr(port, 'vars', {}).values():
            if not hasattr(port_var, 'is_indexed'):
                continue
            try:
                items = [port_var[index] for index in port_var.keys()] if port_var.is_indexed() else [port_var]
            except Exception:
                continue
            for item in items:
                marker = id(item)
                if marker in seen:
                    continue
                seen.add(marker)
                data.append(item)
        return data

    def _unfix_port_state_data(self, port: Any) -> int:
        """Release any inlet state variables fixed by a previous unit initializer."""
        count = 0
        for var_data in self._port_state_data(port):
            if not getattr(var_data, 'fixed', False):
                continue
            try:
                var_data.unfix()
                count += 1
            except Exception:
                continue
        return count

    def _normalize_gas_port_mole_fractions(self, port: Any) -> None:
        """Keep FTPx mole-fraction guesses feasible after damping/propagation."""
        from pyomo.environ import value
        mole_frac = getattr(port, 'mole_frac_comp', None)
        if mole_frac is None or not hasattr(mole_frac, 'is_indexed'):
            return
        groups: Dict[Any, List[Tuple[Any, str]]] = {}
        try:
            indexes = list(mole_frac.keys()) if mole_frac.is_indexed() else [None]
        except Exception:
            return
        for index in indexes:
            if index is None:
                continue
            if isinstance(index, tuple) and len(index) >= 2:
                time_index = index[0]
                component = str(index[-1])
            else:
                time_index = None
                component = str(index)
            groups.setdefault(time_index, []).append((index, component))
        for entries in groups.values():
            raw: Dict[Any, float] = {}
            for index, _component in entries:
                try:
                    raw_value = value(mole_frac[index], exception=False)
                except Exception:
                    raw_value = None
                raw[index] = max(0.0, float(raw_value)) if raw_value is not None else 0.0
            total = sum(raw.values())
            if total <= 0.0:
                total = float(len(entries) or 1)
                raw = {index: 1.0 for index, _component in entries}
            for index, _component in entries:
                target = raw[index] / total
                try:
                    mole_frac[index].set_value(target, skip_validation=True)
                except Exception:
                    continue

    def _scale_gas_port_flow_guess(self, port: Any, factor: float) -> None:
        """Scale a gas port's molar-flow guess after copying a fresh-feed state."""
        from pyomo.environ import value
        flow_mol = getattr(port, 'flow_mol', None)
        if flow_mol is None or not hasattr(flow_mol, 'is_indexed'):
            return
        try:
            indexes = list(flow_mol.keys()) if flow_mol.is_indexed() else [None]
        except Exception:
            return
        for index in indexes:
            try:
                var_data = flow_mol[index] if index is not None else flow_mol
                raw_value = value(var_data, exception=False)
            except Exception:
                continue
            if raw_value is None:
                continue
            try:
                var_data.set_value(max(float(raw_value) * factor, 1e-08), skip_validation=True)
            except Exception:
                continue

    def _seed_gas_tear_initial_guesses(self, *, units: Dict[str, Any], connections: List[Dict[str, Any]], tear_connections: List[Dict[str, Any]]) -> int:
        """Seed torn mixer inlets from a fresh-feed inlet before the first pass."""
        seeded = 0
        tear_signatures = {self._connection_signature(conn) for conn in tear_connections}
        for tear_conn in tear_connections:
            dest_unit_id = self._connection_dest_unit(tear_conn)
            dest_unit = units.get(dest_unit_id)
            if dest_unit is None:
                continue
            dest_port = getattr(dest_unit, tear_conn.get('dest_port', 'inlet'), None)
            if dest_port is None:
                continue
            self._unfix_port_state_data(dest_port)
            fresh_source = None
            for conn in connections:
                if self._connection_signature(conn) in tear_signatures:
                    continue
                if self._connection_dest_unit(conn) != dest_unit_id:
                    continue
                src_unit_id = self._connection_source_unit(conn)
                src_unit = units.get(src_unit_id)
                if src_unit is None:
                    continue
                source_port = getattr(src_unit, conn.get('source_port', 'outlet'), None)
                if source_port is not None:
                    fresh_source = source_port
                    break
            if fresh_source is not None:
                self._blend_port_state(fresh_source, dest_port, 1.0)
                self._scale_gas_port_flow_guess(dest_port, 0.01)
            self._normalize_gas_port_mole_fractions(dest_port)
            seeded += 1
        return seeded

    def _initialize_gas_membrane_recycle_flowsheet(self, *, topology: Dict[str, Any], units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]], state_args: Optional[Dict[str, Dict]], tear_streams: Optional[List[Tuple[str, str]]]) -> FlowsheetInitResult:
        """Warm-start NaturalGasPR membrane recycles by successive substitution."""
        tear_connections = self._gas_tear_connections(connections, tear_streams)
        if not tear_connections:
            return self._initialize_sequential_playbook(recipe_name='gas_membrane_sequential', topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=tear_streams)
        if not self._model_uses_natural_gas_pr(units):
            return self._initialize_sequential_playbook(recipe_name='generic_sequential', topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=tear_streams)
        tear_signatures = {self._connection_signature(conn) for conn in tear_connections}
        active_connections = [conn for conn in connections if self._connection_signature(conn) not in tear_signatures]
        init_order = self.get_initialization_order(units, connections, tear_streams=tear_streams)
        tear_arc_blocks = self._expanded_arc_blocks(tear_connections)
        for block in tear_arc_blocks:
            block.deactivate()
        self._record_initializer_tactic(name='gas_membrane_recycle_warm_start', description='Opened explicit gas recycle tear, initialized the acyclic cascade, and closed the tear by successive substitution', units=list(units))
        warnings: List[str] = []
        seeded_tears = self._seed_gas_tear_initial_guesses(units=units, connections=connections, tear_connections=tear_connections)
        if seeded_tears:
            warnings.append(f'Seeded {seeded_tears} gas recycle tear inlet guess(es)')
        history = []
        last_results: Dict[str, InitializationResult] = {}
        converged = False
        max_iterations = int(self._runtime_metadata.get('gas_recycle_warm_start_iterations', 15))
        tolerance = float(self._runtime_metadata.get('gas_recycle_warm_start_tolerance', 0.0005))
        try:
            for iteration in range(1, max_iterations + 1):
                if iteration <= 3:
                    damping = 0.35
                elif iteration <= 8:
                    damping = 0.7
                else:
                    damping = 1.0
                max_delta = 0.0
                for unit_id in init_order:
                    unit = units.get(unit_id)
                    if unit is None:
                        continue
                    incoming_connections = [conn for conn in active_connections if self._connection_dest_unit(conn) == unit_id]
                    outgoing_connections = [conn for conn in connections if self._connection_source_unit(conn) == unit_id]
                    for conn in incoming_connections:
                        src_unit_id = self._connection_source_unit(conn)
                        src_unit = units.get(src_unit_id)
                        if src_unit is None:
                            continue
                        source_port = getattr(src_unit, conn.get('source_port', 'outlet'), None)
                        dest_port = getattr(unit, conn.get('dest_port', 'inlet'), None)
                        if source_port is not None and dest_port is not None:
                            self.propagate_state(source_port, dest_port)
                            self._normalize_gas_port_mole_fractions(dest_port)
                    method = (unit_methods or {}).get(unit_id, InitMethod.INITIALIZE)
                    args = (state_args or {}).get(unit_id)
                    result = self.initialize_unit(unit, unit_id, method, args)
                    last_results[unit_id] = result
                    if result.status == InitStatus.FAILED:
                        return FlowsheetInitResult(success=False, unit_results=list(last_results.values()), init_order=init_order, message=f"Gas membrane warm-start failed at unit '{unit_id}': {result.message}", warnings=warnings, errors=[result.message], failed_unit=unit_id, recipe_name='gas_membrane_recycle_warm_start', topology=topology)
                    for conn in outgoing_connections:
                        dst_unit_id = self._connection_dest_unit(conn)
                        dst_unit = units.get(dst_unit_id)
                        if dst_unit is None:
                            continue
                        source_port = getattr(unit, conn.get('source_port', 'outlet'), None)
                        dest_port = getattr(dst_unit, conn.get('dest_port', 'inlet'), None)
                        if source_port is None or dest_port is None:
                            continue
                        if self._connection_signature(conn) in tear_signatures:
                            self._unfix_port_state_data(dest_port)
                            max_delta = max(max_delta, self._blend_port_state(source_port, dest_port, damping))
                            self._normalize_gas_port_mole_fractions(dest_port)
                        else:
                            self.propagate_state(source_port, dest_port)
                            self._normalize_gas_port_mole_fractions(dest_port)
                history.append({'iteration': iteration, 'damping': damping, 'max_relative_delta': max_delta})
                if max_delta <= tolerance:
                    converged = True
                    break
            for conn in tear_connections:
                src_unit = units.get(self._connection_source_unit(conn))
                dst_unit = units.get(self._connection_dest_unit(conn))
                if src_unit is None or dst_unit is None:
                    continue
                source_port = getattr(src_unit, conn.get('source_port', 'outlet'), None)
                dest_port = getattr(dst_unit, conn.get('dest_port', 'inlet'), None)
                if source_port is not None and dest_port is not None:
                    self._unfix_port_state_data(dest_port)
                    self._blend_port_state(source_port, dest_port, 1.0)
                    self._normalize_gas_port_mole_fractions(dest_port)
        finally:
            for block in tear_arc_blocks:
                block.activate()
        for unit_id, unit in units.items():
            if not getattr(unit, '_gas_saturated_capacity_requested', False):
                continue
            if not hasattr(unit, 'activate_saturated_capacity'):
                continue
            unit.activate_saturated_capacity(free_discharge=True)
            warnings.append(f"Activated saturated-slide capacity closure on compressor '{unit_id}' (suction held at field setpoint, discharge freed, inlet capacity pinned to displacement)")
        tear_descriptions = [f'{self._connection_source_unit(conn)}.{conn.get('source_port')}->{self._connection_dest_unit(conn)}.{conn.get('dest_port')}' for conn in tear_connections]
        warnings.append(f'Gas membrane recycle warm-start {('converged' if converged else 'stopped')} after {len(history)} iteration(s); tears={', '.join(tear_descriptions)}')
        ordered_results = [last_results[unit_id] for unit_id in init_order if unit_id in last_results]
        return FlowsheetInitResult(success=True, status=InitStatus.SUCCESS if converged else InitStatus.DEGRADED, unit_results=ordered_results, init_order=init_order, message='Gas membrane recycle warm-start complete', warnings=warnings, recipe_name='gas_membrane_recycle_warm_start', topology={**topology, 'gas_recycle_warm_start': {'converged': converged, 'iterations': len(history), 'tolerance': tolerance, 'tears': tear_descriptions, 'history': history}})

    def initialize_flowsheet(self, flowsheet: Any, units: Dict[str, Any], connections: List[Dict], unit_methods: Optional[Dict[str, InitMethod]]=None, state_args: Optional[Dict[str, Dict]]=None, tear_streams: Optional[List[Tuple[str, str]]]=None) -> FlowsheetInitResult:
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
        topology = self.classify_topology(units, connections)
        effective_tears = tear_streams
        if effective_tears is None:
            effective_tears = topology.get('tear_streams') or None
        if topology['recipe_name'] == 'asm2d_recycle':
            result = self._initialize_asm2d_recycle_flowsheet(units, unit_methods, state_args, connections=connections)
            result.topology = topology
            result.recipe_name = topology['recipe_name']
            return result
        if topology['family'] in {'ro', 'nf'}:
            return self._initialize_pressure_membrane_flowsheet(topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=effective_tears)
        if topology['family'] == 'mvc':
            return self._initialize_mvc_flowsheet(topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=effective_tears)
        if topology['family'] == 'adm1' and (effective_tears or any((conn.get('tear_stream') for conn in connections))):
            return self._initialize_adm1_recycle_flowsheet(topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=effective_tears)
        if topology['family'] in {'ix', 'adm1', 'crystallizer'}:
            return self._initialize_first_class_unit_family(topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=effective_tears)
        if topology['family'] == 'gas_membrane' and effective_tears:
            return self._initialize_gas_membrane_recycle_flowsheet(topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=effective_tears)
        return self._initialize_sequential_playbook(recipe_name=topology['recipe_name'], topology=topology, units=units, connections=connections, unit_methods=unit_methods, state_args=state_args, tear_streams=effective_tears)

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
            return (False, f'DOF = {dof}, expected 0')
        solver = get_solver()
        results = solver.solve(unit, tee=False)
        from pyomo.environ import TerminationCondition
        if results.solver.termination_condition == TerminationCondition.optimal:
            return (True, 'Solve successful')
        else:
            return (False, f'Solve returned: {results.solver.termination_condition}')
    except Exception as e:
        return (False, f'Check solve failed: {str(e)}')

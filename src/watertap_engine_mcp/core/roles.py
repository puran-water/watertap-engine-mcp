"""Unit role metadata for role-based solver convergence.

The ASM2D biological recycle convergence priors (initialization seeding, tear
closure, SRT/MLSS conditioning, recycle-ratio guesses) historically keyed on
the canonical template unit names (``Anoxic``/``Aerobic``/``IRSplitter``/...).
That coupling blocked from-scratch flowsheets that name their units anything
else from inheriting the priors and converging.

This module introduces a ``role`` concept that decouples the convergence
machinery from unit names. Each structural unit can carry a ``role`` string
(see :class:`UnitRole`). The :class:`RoleResolver` maps roles to the live unit
ids present in a flowsheet purely from the explicit role tags — there is NO
name inference. A unit with no role is ``neutral``: it gets no special priors,
which is the correct default.

Roles are the only mechanism. The canonical ASM2D templates tag every unit
with a role (``templates/asm2d.py`` ``FIELD_ROLES``), and ``build_from_topology``
/ ``create_unit`` carry explicit role tags, so a template-seeded build and a
from-scratch role-tagged build inherit identical priors and converge to the
same steady state (the validation oracle).
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from puran_mcp_support.engineering.simulation_role_graph import ADM1_OPTIONAL_SURFACE_ROLE_VALUES, ADM1_RECYCLE_REQUIRED_ROLE_VALUE, ADM1_SINGLE_STAGE_REQUIRED_ROLE_VALUES, ADM1_TWO_STAGE_REQUIRED_ROLE_VALUES, ASM2D_MBR_RETENTATE_RECYCLE_REQUIRED_ROLE_VALUES, ASM2D_MLE_RECYCLE_REQUIRED_ROLE_VALUES, ASM2D_RECYCLE_SEPARATOR_ROLE_VALUES, designed_graph_family as _designed_graph_family, role_set_forms_adm1_digestion_family, role_set_forms_adm1_recycle_family, role_set_forms_asm2d_recycle_family

class UnitRole(str, Enum):
    """Structural role a unit plays in a biological recycle flowsheet.

    Roles are the stable identity the convergence machinery keys on instead of
    unit names. ``NEUTRAL`` is the explicit default for units that play no
    special structural role (the priors simply ignore them).
    """
    FEED = 'feed'
    MIXER = 'mixer'
    PRE_ANOXIC = 'pre_anoxic'
    AEROBIC = 'aerobic'
    POST_ANOXIC = 'post_anoxic'
    INTERNAL_RECYCLE_SPLITTER = 'internal_recycle_splitter'
    INTERNAL_RECYCLE_PRESSURE = 'internal_recycle_pressure'
    SECONDARY_CLARIFIER = 'secondary_clarifier'
    MBR_SEPARATOR = 'mbr_separator'
    SLUDGE_SPLITTER = 'sludge_splitter'
    RAS_PRESSURE = 'ras_pressure'
    ALKALINITY = 'alkalinity'
    CHEMICAL_P = 'chemical_p'
    SUPPLEMENTAL_CARBON = 'supplemental_carbon'
    ANAEROBIC_ACIDOGENIC = 'anaerobic_acidogenic'
    ANAEROBIC_METHANOGENIC = 'anaerobic_methanogenic'
    ANAEROBIC_GAS_PHASE = 'anaerobic_gas_phase'
    ANAEROBIC_RECUPERATION = 'anaerobic_recuperation'
    ANAEROBIC_THICKENER = 'anaerobic_thickener'
    PRODUCT_EFFLUENT = 'product_effluent'
    PRODUCT_WASTE = 'product_waste'
    PRODUCT = 'product'
    NEUTRAL = 'neutral'
VALID_ROLES = frozenset((role.value for role in UnitRole))
ASM2D_MLE_RECYCLE_REQUIRED_ROLES = tuple((UnitRole(value) for value in ASM2D_MLE_RECYCLE_REQUIRED_ROLE_VALUES))
ASM2D_MBR_RETENTATE_RECYCLE_REQUIRED_ROLES = tuple((UnitRole(value) for value in ASM2D_MBR_RETENTATE_RECYCLE_REQUIRED_ROLE_VALUES))
ASM2D_RECYCLE_SEPARATOR_ROLES = tuple((UnitRole(value) for value in ASM2D_RECYCLE_SEPARATOR_ROLE_VALUES))
ADM1_TWO_STAGE_REQUIRED_ROLES = tuple((UnitRole(value) for value in ADM1_TWO_STAGE_REQUIRED_ROLE_VALUES))
ADM1_SINGLE_STAGE_REQUIRED_ROLES = tuple((UnitRole(value) for value in ADM1_SINGLE_STAGE_REQUIRED_ROLE_VALUES))
ADM1_OPTIONAL_SURFACE_ROLES = tuple((UnitRole(value) for value in ADM1_OPTIONAL_SURFACE_ROLE_VALUES))
ADM1_RECYCLE_REQUIRED_ROLE = UnitRole(ADM1_RECYCLE_REQUIRED_ROLE_VALUE)

def normalize_role(value: Any) -> Optional[str]:
    """Validate and normalize a role value to its canonical string form.

    Accepts a :class:`UnitRole`, its string value, or ``None``. Raises
    ``ValueError`` for any other (unknown) string so that bad role tags fail
    loudly at the create/build boundary rather than silently degrading the
    convergence priors.
    """
    if value is None:
        return None
    if isinstance(value, UnitRole):
        return value.value
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in VALID_ROLES:
        raise ValueError(f"Unknown unit role '{value}'. Valid roles: {sorted(VALID_ROLES)}")
    return text

def _role_value(role: Any) -> str:
    if isinstance(role, UnitRole):
        return role.value
    return str(role)

@dataclass(frozen=True)
class RoleBinding:
    """A single unit's binding to a structural role.

    The resolver keeps an ordered list of bindings per role so a role can be
    filled by more than one unit (e.g. an N-stage aerobic train where several
    reactors share the ``aerobic`` role at different ``stage_index`` positions).

    Fields:
      * ``unit_id`` — the live unit id filling the role.
      * ``role`` — the canonical role string (``UnitRole`` value).
      * ``stage_index`` — optional 0-based position of this unit within a
        multi-stage role sequence. ``None`` (the default for every existing
        single-winner role) sorts as stage ``0``.
      * ``insertion_index`` — the order the unit was added to the resolver.
        Breaks stage ties stably so the first-added unit wins, preserving the
        historical "first tag wins" single-winner semantics.
      * ``group`` — optional partition label (e.g. a parallel-train id) used to
        scope multiplicity queries; ``None`` participates in every query.
    """
    unit_id: str
    role: str
    stage_index: Optional[int]
    insertion_index: int
    group: Optional[str] = None

def _binding_sort_key(binding: 'RoleBinding') -> Tuple[int, int]:
    """Sort bindings by ``(stage_index or 0, insertion_index)``.

    Untagged-stage bindings collapse to stage ``0`` and then fall back to
    insertion order, so a role with no explicit staging resolves first-added
    first — byte-for-byte the historical ``setdefault`` "first tag wins".
    """
    return (binding.stage_index if binding.stage_index is not None else 0, binding.insertion_index)

class RoleStageIndexError(ValueError):
    """A multiply-filled role lacks an explicit ``role_stage_index``.

    Raised by :meth:`RoleResolver.require_explicit_stage_index` when a role is
    filled by more than one unit but at least one of those bindings omits its
    global ``role_stage_index``. A repeated reactor role defines an N-stage train
    whose hydraulic order IS the global stage index; without it the train order
    is ambiguous and seeding it from unit-list (insertion) order would synthesize
    the wrong inter-stage arcs. Subclasses ``ValueError`` so existing
    ``except ValueError`` boundaries still catch it.
    """

    def __init__(self, *, role: str, missing_unit_ids: Sequence[str], all_unit_ids: Sequence[str]):
        self.role = role
        self.missing_unit_ids = list(missing_unit_ids)
        self.all_unit_ids = list(all_unit_ids)
        super().__init__(f"Role '{role}' is filled by {len(self.all_unit_ids)} units ({', '.join(self.all_unit_ids)}) but {len(self.missing_unit_ids)} lack an explicit role_stage_index ({', '.join(self.missing_unit_ids)}). A repeated role defines an N-stage train whose hydraulic order is the global role_stage_index; assign a distinct role_stage_index to every binding of this role so the train order is unambiguous.")

class RoleResolver:
    """Resolve unit roles to live unit ids present in a flowsheet.

    Construction takes a ``unit_id -> role`` mapping (e.g. read off the session
    units). Resolution returns the id of the unit explicitly tagged with the
    requested role, or ``None`` when no unit carries it. There is no name
    inference: an untagged unit is ``neutral`` and gets no special priors.
    """

    def __init__(self, unit_roles: Optional[Mapping[str, Any]]=None, *, unit_stage_index: Optional[Mapping[str, Optional[int]]]=None, unit_group: Optional[Mapping[str, Optional[str]]]=None):
        self._by_id: Dict[str, str] = {}
        self._by_role: Dict[str, List[RoleBinding]] = {}
        stage_map = unit_stage_index or {}
        group_map = unit_group or {}
        for insertion_index, (unit_id, role) in enumerate((unit_roles or {}).items()):
            role_value = _role_value(role) if role else None
            if not role_value:
                continue
            self._by_id[unit_id] = role_value
            binding = RoleBinding(unit_id=unit_id, role=role_value, stage_index=stage_map.get(unit_id), insertion_index=insertion_index, group=group_map.get(unit_id))
            self._by_role.setdefault(role_value, []).append(binding)
        for bindings in self._by_role.values():
            bindings.sort(key=_binding_sort_key)

    @classmethod
    def from_session(cls, session: Any) -> 'RoleResolver':
        """Build a resolver from a FlowsheetSession's unit role tags."""
        if session is None:
            return cls({})
        units = getattr(session, 'units', {}) or {}
        roles: Dict[str, Any] = {}
        stage_index: Dict[str, Optional[int]] = {}
        group: Dict[str, Optional[str]] = {}
        for unit_id, unit_inst in units.items():
            roles[unit_id] = getattr(unit_inst, 'role', None)
            stage_index[unit_id] = getattr(unit_inst, 'role_stage_index', None)
            group[unit_id] = getattr(unit_inst, 'role_group', None)
        return cls(roles, unit_stage_index=stage_index, unit_group=group)

    def role_of(self, unit_id: str) -> Optional[str]:
        """Return the role tagged on a unit id, if any."""
        return self._by_id.get(unit_id)

    def _bindings(self, role: Any, units: Optional[Mapping[str, Any]]=None, group: Optional[str]=None) -> List[RoleBinding]:
        """Return the ordered bindings for ``role`` after group/units filters."""
        role_value = _role_value(role)
        bindings = self._by_role.get(role_value, ())
        result: List[RoleBinding] = []
        for binding in bindings:
            if group is not None and binding.group != group:
                continue
            if units is not None and binding.unit_id not in units:
                continue
            result.append(binding)
        return result

    def id_for(self, role: Any, units: Optional[Mapping[str, Any]]=None) -> Optional[str]:
        """Return the unit id explicitly tagged with ``role``, or ``None``.

        Returns the FIRST binding's unit id (lowest ``(stage_index, insertion)``)
        — historically the single "first tag wins" winner. With ``units``
        provided, only ids actually present in the flowsheet are considered (so
        an absent or untagged role yields ``None`` and callers can skip it). No
        canonical-name inference is performed.
        """
        bindings = self._bindings(role, units)
        return bindings[0].unit_id if bindings else None

    def unit(self, role: Any, units: Mapping[str, Any]) -> Optional[Any]:
        """Return the live unit block filling ``role``, or ``None``."""
        unit_id = self.id_for(role, units)
        return units.get(unit_id) if unit_id is not None else None

    def has(self, role: Any, units: Mapping[str, Any]) -> bool:
        """Return ``True`` when a unit fills ``role`` in the flowsheet."""
        return self.unit(role, units) is not None

    def first(self, role: Any, units: Optional[Mapping[str, Any]]=None, group: Optional[str]=None) -> Optional['RoleBinding']:
        """Return the first :class:`RoleBinding` for ``role``, or ``None``.

        Same winner as :meth:`id_for` but returns the full binding (carrying
        ``stage_index``/``group``) rather than just the unit id.
        """
        bindings = self._bindings(role, units, group)
        return bindings[0] if bindings else None

    def all_for(self, role: Any, units: Optional[Mapping[str, Any]]=None, group: Optional[str]=None) -> Tuple[str, ...]:
        """Return every unit id filling ``role`` in ``(stage, insertion)`` order."""
        return tuple((binding.unit_id for binding in self._bindings(role, units, group)))

    def units_for(self, role: Any, units: Mapping[str, Any], group: Optional[str]=None) -> Tuple[Any, ...]:
        """Return every live unit block filling ``role`` in order."""
        return tuple((units[binding.unit_id] for binding in self._bindings(role, units, group)))

    def at(self, role: Any, stage_index: int, units: Optional[Mapping[str, Any]]=None, group: Optional[str]=None) -> Optional[str]:
        """Return the unit id filling ``role`` at ``stage_index``, or ``None``.

        An untagged-stage binding (``stage_index is None``) collapses to stage
        ``0`` — the same convention the binding sort uses — so ``at(role, 0)``
        resolves a single-winner role with no explicit staging. When several
        bindings share the same effective stage the first (lowest insertion)
        wins.
        """
        for binding in self._bindings(role, units, group):
            effective = binding.stage_index if binding.stage_index is not None else 0
            if effective == stage_index:
                return binding.unit_id
        return None

    def single(self, role: Any, units: Optional[Mapping[str, Any]]=None, group: Optional[str]=None) -> Optional['RoleBinding']:
        """Return the sole :class:`RoleBinding` for ``role``, or ``None``.

        Raises ``ValueError`` when more than one unit fills ``role`` (after the
        optional ``group``/``units`` filters) — the explicit assertion a caller
        makes when it requires a role to be single-winner.
        """
        bindings = self._bindings(role, units, group)
        if not bindings:
            return None
        if len(bindings) > 1:
            ids = ', '.join((binding.unit_id for binding in bindings))
            raise ValueError(f"Role '{_role_value(role)}' expected a single binding but is filled by {len(bindings)} units: {ids}")
        return bindings[0]

    def single_id(self, role: Any, units: Optional[Mapping[str, Any]]=None, group: Optional[str]=None) -> Optional[str]:
        """Return the sole unit id filling ``role``, or ``None``.

        Convenience wrapper over :meth:`single` returning just the unit id. Like
        :meth:`single` it raises ``ValueError`` when more than one unit fills the
        role — the explicit "I require this role to be single-winner" assertion a
        caller makes instead of silently taking the first via :meth:`id_for`.
        """
        binding = self.single(role, units, group)
        return binding.unit_id if binding is not None else None

    def single_unit(self, role: Any, units: Mapping[str, Any], group: Optional[str]=None) -> Optional[Any]:
        """Return the sole live unit block filling ``role``, or ``None``.

        Raises ``ValueError`` when more than one unit fills ``role`` (after the
        optional ``group`` filter), making single-winner assumptions explicit.
        """
        unit_id = self.single_id(role, units, group)
        return units.get(unit_id) if unit_id is not None else None

    def ordered_stages(self, stage_roles: Iterable[Any], units: Optional[Mapping[str, Any]]=None, default_role_order: Optional[Sequence[Any]]=None) -> List['RoleBinding']:
        """Return bindings for ``stage_roles`` flattened into GLOBAL train order.

        ``role_stage_index`` is the GLOBAL position of a reactor in the train, so
        the bindings from ALL requested roles are pooled and sorted together by
        ``(role_stage_index, role-priority, insertion_index)`` — NOT role-primary
        (all of one role, then all of the next). This is what lets a true
        alternating train (e.g. a Bardenpho ``anoxic → aerobic → anoxic → aerobic
        → aerobic`` spine, whose anoxic and aerobic stages interleave in global
        order) come back in actual hydraulic order rather than collapsing into
        ``[all anoxic, all aerobic]``.

        Tie-breaks, in order:

        * ``role_stage_index`` — the global train position. An untagged-stage
          binding (``None``) collapses to stage ``0``.
        * role priority — the role's position in ``default_role_order`` when it is
          supplied, else its position in ``stage_roles``. This is what orders the
          3-stage canonical templates (every reactor role single-winner with no
          explicit stage index, so every binding collapses to stage ``0`` and the
          role priority resolves ``pre_anoxic → aerobic → post_anoxic``
          byte-for-byte as before).
        * ``insertion_index`` — stable final tie-break.
        """
        roles = [_role_value(role) for role in stage_roles]
        priority_source = [_role_value(role) for role in default_role_order] if default_role_order is not None else roles
        role_priority = {role: position for position, role in enumerate(priority_source)}
        unranked_priority = len(role_priority)
        collected: List[RoleBinding] = []
        for role in roles:
            collected.extend(self._bindings(role, units))
        collected.sort(key=lambda binding: (binding.stage_index if binding.stage_index is not None else 0, role_priority.get(binding.role, unranked_priority), binding.insertion_index))
        return collected

    def require_explicit_stage_index(self, *roles: Any, units: Optional[Mapping[str, Any]]=None) -> None:
        """Raise when a multiply-filled role lacks an explicit ``role_stage_index``.

        A role filled by more than one unit defines an N-stage train whose
        hydraulic order is the GLOBAL ``role_stage_index`` (see
        :meth:`ordered_stages`). If any of those bindings omits the index there is
        no reliable train order — silently falling back to insertion order would
        synthesize the wrong inter-stage arcs / N-stage seeding for a topology
        whose unit-list order differs from its hydraulic order. This turns that
        ambiguity into a loud, structured :class:`RoleStageIndexError` instead.

        Single-binding roles are unaffected (one unit needs no ordering), so the
        canonical single-winner templates never trip this.
        """
        for role in roles:
            bindings = self._bindings(role, units)
            if len(bindings) <= 1:
                continue
            missing = [b.unit_id for b in bindings if b.stage_index is None]
            if missing:
                raise RoleStageIndexError(role=_role_value(role), missing_unit_ids=missing, all_unit_ids=[b.unit_id for b in bindings])

    def is_asm2d_recycle_family(self) -> bool:
        """Return ``True`` when the role tags constitute an ASM2D biological
        recycle flowsheet, independent of unit/topology names.

        This is the structural, name-free classifier the near-feasible-acceptance
        policy keys on at solve-submission time. It mirrors the role set in
        ``solver.initializer._is_asm2d_recycle_flowsheet``. The ASM2D
        property-package confirmation is enforced later in the solve pipeline
        (``HygienePipeline._is_asm2d_biological_model``) before any near-feasible
        iterate is actually accepted, so a mis-tagged non-ASM2D train can never
        slip a near-feasible solve through on the role tags alone.
        """
        return role_set_forms_asm2d_recycle_family(self._by_role)

    def has_adm1_digestion_roles(self) -> bool:
        """Return ``True`` when the ROLE TAGS form an ADM1/mADM1 anaerobic
        digestion train, independent of unit/topology names.

        ROLES-ONLY classification: this inspects the digestion role set and says
        nothing about the property package backing those units. Package-aware
        routing (confirming the units are actually ADM1/mADM1-backed) is roadmap
        task A4 and deliberately NOT performed here — the honest name reflects
        that. It carries no ADM1 convergence priors and has no
        near-feasible-acceptance analog.

        A phase-separated two-stage train (acidogenic stage feeding a
        methanogenic stage) and a single combined-reactor digester both classify;
        gas-phase / recuperation / thickener surface roles are optional. The
        ASM2D recycle family (``is_asm2d_recycle_family``) is disjoint from this
        — an aerobic recycle train carries none of the anaerobic roles.
        """
        return role_set_forms_adm1_digestion_family(self._by_role)

    def is_adm1_recycle_family(self) -> bool:
        """Return ``True`` when the ROLE TAGS form an ADM1 digester train carrying a
        closed liquid recycle (EGSB recirculation / AnMBR membrane-retentate /
        anaerobic-contact sludge-return), independent of unit/topology names.

        ROLES-ONLY classification, complementary to the PR1 non-recycle path: it
        requires the anaerobic digestion role set (``has_adm1_digestion_roles``)
        PLUS a genuine recycle signal — the ``ANAEROBIC_RECUPERATION`` splitter that
        returns a liquid stream to the head of the train. A bare recycle ``MIXER``
        is deliberately NOT sufficient: a mixer can equally blend two FRESH feeds
        with no return stream, so keying "recycle" on a mixer alone would
        misclassify a non-recycle feed-blend digester train. The UASB (single
        methanogenic reactor) and the forward acidogenic->methanogenic two-stage
        cascade carry no recuperation splitter, so they classify as ADM1 digestion
        families but NOT recycle families.

        This roles-only classifier asserts the recuperation/return SURFACE role; the
        package-aware confirmation (that the digester block is actually ADM1/mADM1-
        backed) and the structural confirmation that an actual graph CYCLE / tear is
        present are enforced downstream in the solve pipeline / initializer (the
        explicit tear detection at the init routing boundary), exactly as the ASM2D
        recycle classifier defers its property confirmation to the pipeline. So a
        family is never driven down the recycle init path on the role tags alone — a
        real returned-liquid recuperation role here AND an explicit tear there.
        """
        return role_set_forms_adm1_recycle_family(self._by_role)

    def designed_graph_family(self) -> Optional[str]:
        """Return which COMPLETE designed-graph biological family the role tags form.

        ``"asm2d"`` (:data:`DESIGNED_GRAPH_FAMILY_ASM2D`) for a complete ASM2D
        biological recycle family, ``"adm1"`` (:data:`DESIGNED_GRAPH_FAMILY_ADM1`)
        for a complete ADM1/mADM1 anaerobic digestion family (UASB / two-stage /
        recycle), or ``None`` when neither — the family discriminator the
        designed-graph build keys on to select the family config + default provider
        (``ASM2DBiologicalConfig`` + ``Asm2dPresetProvider`` vs the ADM1 config +
        ``Adm1PresetProvider``). Delegates to the shared leaf rule
        (:func:`engineering_utils.simulation_role_graph.designed_graph_family`) so it
        can never drift from ``validate_role_graph`` / the Stage-7b dispatch.
        """
        return _designed_graph_family(self._by_role)

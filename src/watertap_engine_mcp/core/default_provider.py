"""Role-keyed default/preset provider interface.

A "template" historically baked its solver defaults — DOF fixes, scaling
factors, feed states, process constraints, recycle-ratio declarations,
initialization order, solver options, near-feasible policy, expected ranges —
directly into ``to_session_spec()`` keyed on the canonical template *unit names*.
That coupling meant only a template could emit those defaults; a from-graph
build (a role-tagged BFD / ``simulation_seed`` topology with project-specific
names) could not inherit them.

This module externalizes that authority into a :class:`DefaultProvider`. A
provider is handed a :class:`RoleResolvedTopology` — the *structural graph*
(units, types, role tags, stage indices) wrapped around an A1
:class:`core.roles.RoleResolver` — and yields a :class:`DefaultBundle`. Every
rule the provider applies resolves against **roles** (via the resolver
multiplicity API: ``single``/``first``/``all_for``/``at``/``ordered_stages``),
**role-index** (the stage position of a unit within a repeated role), **unit
type**, and **family pattern** (which structural roles are present) — never on a
hardcoded unit name. The SAME provider therefore defaults a template-built graph
and a from-graph build identically: the bundle depends only on the roles present
(plus the preset config), so it is byte-for-byte equal modulo the unit names the
two topology sources happen to use.

A *preset* is then just a binding of a family's provider to a specific config
(volumes, recycle targets, bounds); the registered "templates" become preset IDs
over their family's provider rather than the authority for the defaults.

Downstream roadmap consumers:
  * A4 implements ``Adm1PresetProvider(DefaultProvider)`` for the digestion
    families (the anaerobic roles already exist on :class:`core.roles.UnitRole`).
  * A-build compiles a role-tagged seed graph straight into a
    :class:`RoleResolvedTopology` and runs the family provider over it, with no
    template instance in the loop.
"""
from __future__ import annotations
import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence
from watertap_engine_mcp.core.roles import RoleBinding, RoleResolver

@dataclass
class DefaultBundle:
    """The role-resolved default bundle a provider merges onto a graph.

    These are exactly the fields ``to_session_spec()`` historically inlined.
    Each list/dict carries concrete *unit ids* (resolved from the graph's role
    tags), so the bundle composes directly with the topology (units +
    connections) into a session spec.
    """
    dof_fixes: List[Dict[str, Any]] = field(default_factory=list)
    scaling_factors: List[Dict[str, Any]] = field(default_factory=list)
    feed_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    process_constraints: List[Dict[str, Any]] = field(default_factory=list)
    design_parameters: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    solver_options: Dict[str, Any] = field(default_factory=dict)
    initialization_order: List[str] = field(default_factory=list)
    recycle_ratio_declarations: Dict[str, Dict[str, Any]] = field(default_factory=dict)

@dataclass
class RoleResolvedTopology:
    """A structural graph wrapped around an A1 :class:`RoleResolver`.

    This is the provider's view of a topology, independent of where the graph
    came from (a template emitter or a from-graph BFD/seed build). It carries
    the role resolver, the per-unit type map, and the units in topology order,
    and exposes the resolver multiplicity API filtered to the live units.
    """
    resolver: RoleResolver
    unit_types: Dict[str, str]
    ordered_unit_ids: List[str]

    @classmethod
    def from_unit_specs(cls, unit_specs: Sequence[Mapping[str, Any]]) -> 'RoleResolvedTopology':
        """Build from a list of ``{unit_id, unit_type, role, ...}`` dicts.

        This is the template path: ``get_units()`` already emits role-tagged
        unit specs, so the provider keys on the same roles the template stamped.
        """
        roles: Dict[str, Any] = {}
        stage_index: Dict[str, Optional[int]] = {}
        group: Dict[str, Optional[str]] = {}
        unit_types: Dict[str, str] = {}
        ordered: List[str] = []
        for spec in unit_specs:
            unit_id = spec['unit_id']
            ordered.append(unit_id)
            unit_types[unit_id] = spec.get('unit_type')
            roles[unit_id] = spec.get('role')
            stage_index[unit_id] = spec.get('role_stage_index')
            group[unit_id] = spec.get('role_group')
        resolver = RoleResolver(roles, unit_stage_index=stage_index, unit_group=group)
        return cls(resolver=resolver, unit_types=unit_types, ordered_unit_ids=ordered)

    @classmethod
    def from_session(cls, session: Any) -> 'RoleResolvedTopology':
        """Build from a live ``FlowsheetSession`` (the from-graph path).

        Reads the role tags / stage indices / types off the session units, so a
        graph built via ``build_from_topology`` defaults through the SAME
        provider as the named-preset path.
        """
        units = getattr(session, 'units', {}) or {}
        specs = [{'unit_id': unit_id, 'unit_type': getattr(unit, 'unit_type', None), 'role': getattr(unit, 'role', None), 'role_stage_index': getattr(unit, 'role_stage_index', None), 'role_group': getattr(unit, 'role_group', None)} for unit_id, unit in units.items()]
        return cls.from_unit_specs(specs)

    def single_id(self, role: Any) -> Optional[str]:
        """The sole unit id filling ``role`` (raises if multiply-filled)."""
        return self.resolver.single_id(role, self.unit_types)

    def first_id(self, role: Any) -> Optional[str]:
        """The first (lowest stage/insertion) unit id filling ``role``."""
        binding = self.resolver.first(role, self.unit_types)
        return binding.unit_id if binding is not None else None

    def all_ids(self, role: Any) -> tuple:
        """Every unit id filling ``role`` in ``(stage, insertion)`` order."""
        return self.resolver.all_for(role, self.unit_types)

    def at(self, role: Any, stage_index: int) -> Optional[str]:
        """The unit id filling ``role`` at ``stage_index``."""
        return self.resolver.at(role, stage_index, self.unit_types)

    def has_role(self, role: Any) -> bool:
        """Whether any live unit fills ``role`` (the family-pattern probe)."""
        return bool(self.resolver.all_for(role, self.unit_types))

    def ordered_stage_ids(self, stage_roles: Sequence[Any], default_role_order: Optional[Sequence[Any]]=None) -> List[str]:
        """Unit ids for ``stage_roles`` flattened into GLOBAL train order."""
        return [binding.unit_id for binding in self.resolver.ordered_stages(stage_roles, self.unit_types, default_role_order=default_role_order)]

    def ordered_stage_bindings(self, stage_roles: Sequence[Any], default_role_order: Optional[Sequence[Any]]=None) -> List[RoleBinding]:
        """Bindings for ``stage_roles`` in GLOBAL train order (with metadata)."""
        return self.resolver.ordered_stages(stage_roles, self.unit_types, default_role_order=default_role_order)

    def unit_type_of(self, unit_id: str) -> Optional[str]:
        """The unit type registered for ``unit_id``."""
        return self.unit_types.get(unit_id)

    def role_of(self, unit_id: str) -> Optional[str]:
        """The structural role tagged on ``unit_id``."""
        return self.resolver.role_of(unit_id)

class DefaultProvider(abc.ABC):
    """Yields a role-resolved :class:`DefaultBundle` for a topology.

    Concrete providers (one per process family) carry the preset config and a
    structural *family profile* (design bounds and the like that are not
    recoverable from roles), and resolve every rule against the graph's roles.
    """

    @abc.abstractmethod
    def provide(self, topology: RoleResolvedTopology) -> DefaultBundle:
        """Return the default bundle merged onto ``topology``'s role graph."""
        raise NotImplementedError

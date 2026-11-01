"""ASM-family alkalinity/caustic dose block.

This deterministic single-inlet/single-outlet unit is the caustic analogue of
``ChemicalPhosphorusRemovalASM2D``. A fixed ``caustic_dose`` adds supplemental
alkalinity in the property package's native alkalinity coordinate while all
other state variables pass through. Under an ``alkalinity_target`` process
constraint the dose is freed and the downstream alkalinity setpoint is enforced
either as an exact equality target or as a minimum residual reserve with a
minimize-dose objective.

For WaterTAP ASM2d the native alkalinity state is ``alkalinity`` (nominally
mol/m^3 in the model's convention). Modified packages that expose inorganic
carbon as a concentration component can instead use ``S_IC`` or ``S_ALK`` as
the additive target, but no pH equilibrium is embedded here; pH-to-alkalinity
mapping belongs outside the NLP.
"""

from __future__ import annotations

from pyomo.common.config import ConfigBlock, ConfigValue
from pyomo.environ import Constraint, NonNegativeReals, Var, units as pyunits, value

from idaes.core import UnitModelBlockData, declare_process_block_class, useDefault
from idaes.core.solvers import get_solver
from idaes.core.util.config import is_physical_parameter_block
from idaes.core.util.exceptions import ConfigurationError
import idaes.logger as idaeslog


_ALKALINITY_COMPONENT_CANDIDATES = ("S_IC", "S_ALK")


def alkalinity_expr(port_or_state, component: str | None = None):
    """Return a Pyomo expression for the package-native alkalinity coordinate."""

    if hasattr(port_or_state, "alkalinity"):
        return getattr(port_or_state, "alkalinity")
    conc = getattr(port_or_state, "conc_mass_comp", None)
    if conc is None:
        raise AttributeError("state/port exposes neither alkalinity nor conc_mass_comp")
    candidates = (component,) if component else _ALKALINITY_COMPONENT_CANDIDATES
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return conc[candidate]
        except Exception:
            try:
                return conc[0, candidate]
            except Exception:
                continue
    raise AttributeError(
        "state/port has no alkalinity, S_IC, or S_ALK coordinate"
    )


@declare_process_block_class("AlkalinityDoseASM2D")
class AlkalinityDoseASM2DData(UnitModelBlockData):
    """Deterministic NaOH-equivalent alkalinity addition for ASM streams."""

    CONFIG = UnitModelBlockData.CONFIG()

    CONFIG.declare(
        "property_package",
        ConfigValue(
            default=useDefault,
            domain=is_physical_parameter_block,
            description="ASM-family property package",
        ),
    )
    CONFIG.declare(
        "property_package_args",
        ConfigBlock(
            implicit=True,
            description="Arguments passed to the property package state blocks",
        ),
    )
    CONFIG.declare(
        "alkalinity_component",
        ConfigValue(
            default=None,
            domain=str,
            description=(
                "Optional concentration component to manipulate when the package "
                "does not expose a native alkalinity state"
            ),
        ),
    )

    def build(self):
        super().build()

        prop = self.config.property_package
        component = self._alkalinity_component()
        time = self.flowsheet().time

        in_args = dict(self.config.property_package_args)
        in_args["defined_state"] = True
        self.properties_in = prop.build_state_block(time, **in_args)

        out_args = dict(self.config.property_package_args)
        out_args["defined_state"] = False
        self.properties_out = prop.build_state_block(time, **out_args)

        self.add_port("inlet", self.properties_in, doc="Inlet stream")
        self.add_port("outlet", self.properties_out, doc="Caustic-dosed outlet stream")

        if component is None and not hasattr(self.properties_in[time.first()], "alkalinity"):
            raise ConfigurationError(
                "AlkalinityDoseASM2D requires a native alkalinity state or an "
                "S_IC/S_ALK component in the property package"
            )

        self.caustic_dose = Var(
            time,
            initialize=0.0,
            domain=NonNegativeReals,
            units=pyunits.kmol / pyunits.m**3,
            doc="NaOH-equivalent alkalinity dose in the package-native basis",
        )

        @self.Constraint(time, doc="Volumetric flow continuity")
        def flow_balance(blk, t):
            return blk.properties_out[t].flow_vol == blk.properties_in[t].flow_vol

        @self.Constraint(time, doc="Temperature continuity")
        def temperature_balance(blk, t):
            return blk.properties_out[t].temperature == blk.properties_in[t].temperature

        @self.Constraint(time, doc="Pressure continuity")
        def pressure_balance(blk, t):
            return blk.properties_out[t].pressure == blk.properties_in[t].pressure

        if hasattr(self.properties_in[time.first()], "alkalinity"):

            @self.Constraint(time, doc="Supplemental alkalinity addition")
            def alkalinity_balance(blk, t):
                return (
                    blk.properties_out[t].alkalinity
                    == blk.properties_in[t].alkalinity + blk.caustic_dose[t]
                )

            transformed = set()
        else:
            transformed = {component}

            @self.Constraint(time, doc="Supplemental inorganic-carbon/alkalinity addition")
            def alkalinity_component_balance(blk, t):
                return (
                    blk.properties_out[t].conc_mass_comp[component]
                    == blk.properties_in[t].conc_mass_comp[component]
                    + blk.caustic_dose[t]
                )

        passthrough = [c for c in prop.solute_set if c not in transformed]

        @self.Constraint(time, passthrough, doc="Conserved-component passthrough")
        def component_passthrough(blk, t, j):
            return (
                blk.properties_out[t].conc_mass_comp[j]
                == blk.properties_in[t].conc_mass_comp[j]
            )

    def _alkalinity_component(self) -> str | None:
        configured = self.config.alkalinity_component
        if configured:
            return str(configured)
        prop = self.config.property_package
        solutes = set(str(c) for c in getattr(prop, "solute_set", []))
        for component in _ALKALINITY_COMPONENT_CANDIDATES:
            if component in solutes:
                return component
        return None

    def initialize_build(
        self,
        state_args=None,
        outlvl=idaeslog.NOTSET,
        solver=None,
        optarg=None,
    ):
        """Triangular initialization from inlet state and fixed caustic dose."""

        init_log = idaeslog.getInitLogger(self.name, outlvl, tag="unit")
        solve_log = idaeslog.getSolveLogger(self.name, outlvl, tag="unit")

        self.properties_in.initialize(
            outlvl=outlvl,
            optarg=optarg,
            solver=solver,
            state_args=state_args,
        )

        component = self._alkalinity_component()
        for t in self.flowsheet().time:
            in_blk = self.properties_in[t]
            out_blk = self.properties_out[t]
            try:
                dose = value(self.caustic_dose[t])
            except Exception:
                dose = 0.0
            dose = float(dose or 0.0)

            for attr in ("flow_vol", "temperature", "pressure"):
                src = getattr(in_blk, attr, None)
                dst = getattr(out_blk, attr, None)
                if src is None or dst is None:
                    continue
                try:
                    dst.set_value(value(src), skip_validation=True)
                except Exception:
                    continue

            if hasattr(in_blk, "alkalinity") and hasattr(out_blk, "alkalinity"):
                try:
                    out_blk.alkalinity.set_value(
                        max(value(in_blk.alkalinity) + dose, 0.0),
                        skip_validation=True,
                    )
                except Exception:
                    pass

            for j in in_blk.conc_mass_comp.keys():
                try:
                    base = float(value(in_blk.conc_mass_comp[j]))
                except Exception:
                    continue
                if component is not None and j == component:
                    base = max(base + dose, 0.0)
                try:
                    out_blk.conc_mass_comp[j].set_value(base, skip_validation=True)
                except Exception:
                    continue

        results = None
        try:
            opt = get_solver(solver, optarg)
        except Exception:
            opt = get_solver()
        try:
            with idaeslog.solver_log(solve_log, idaeslog.DEBUG) as slc:
                results = opt.solve(self, tee=slc.tee)
            init_log.info(
                f"AlkalinityDoseASM2D initialization {idaeslog.condition(results)}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            init_log.info(f"AlkalinityDoseASM2D polish solve skipped: {exc}")
        return results


__all__ = ["AlkalinityDoseASM2D", "alkalinity_expr"]

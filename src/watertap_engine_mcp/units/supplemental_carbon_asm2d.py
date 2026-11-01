"""ASM-family supplemental external-carbon (rbCOD) dose block.

This deterministic single-inlet/single-outlet unit is the carbon analogue of
``AlkalinityDoseASM2D`` and ``ChemicalPhosphorusRemovalASM2D``. A fixed
``carbon_dose`` adds external readily-biodegradable COD to the stream as the
ASM2D fermentation-product/acetate component ``S_A`` (the rbCOD electron donor
heterotrophic denitrifiers consume) while flow, pressure, temperature,
alkalinity, and every other component pass through unchanged.

Supplemental carbon (methanol / acetate / glycerol / proprietary MicroC) is the
standard external electron donor dosed ahead of — or directly into — a
post-anoxic zone to drive effluent NO3-N down when the influent rbCOD:N ratio is
donor-limited and endogenous denitrification cannot reach the tender nitrogen
limit. It is modeled here as an external COD input added to ``S_A``: the ASM2D
heterotroph denitrification kinetics in the downstream anoxic reactor consume
that ``S_A`` to reduce ``S_NO3``. Under an ``no3_target`` process constraint the
dose is freed and the solver sizes it so the effluent (or anoxic-zone) ``S_NO3``
meets the target; the ~2.86 g COD / g NO3-N denitrification ratio is used only
as a cold-start seed (see ``DENITRIFICATION_COD_PER_NO3_N``) — the ASM2D
kinetics determine the final dose.

``S_A`` is a COD-basis concentration component in both ASM2D and mASM2d, so the
dose carries the same kg-COD/m^3 basis. No kinetics / residence time live in
this block (it is a single-pass deterministic transform), which keeps the design
square and IPOPT-friendly: fix the dose and the outlet state is fully
determined; free it under an ``no3_target`` constraint and the swap stays
DOF-neutral.
"""

from __future__ import annotations

from pyomo.common.config import ConfigBlock, ConfigValue
from pyomo.environ import NonNegativeReals, Var, units as pyunits, value

from idaes.core import UnitModelBlockData, declare_process_block_class, useDefault
from idaes.core.solvers import get_solver
from idaes.core.util.config import is_physical_parameter_block
from idaes.core.util.exceptions import ConfigurationError
import idaes.logger as idaeslog


# Stoichiometric denitrification COD demand (g COD consumed per g NO3-N reduced
# to N2). The classic ~2.86 figure is the electron-equivalent ratio used as the
# cold-start SEED for the freed dose under an ``no3_target`` constraint — NOT a
# model coefficient. The ASM2D heterotroph kinetics (yield, anoxic correction,
# competition with aerobic respiration) set the actual dose at solve time.
DENITRIFICATION_COD_PER_NO3_N = 2.86

# Default and fallback rbCOD electron-donor component. ``S_A`` (volatile-acid /
# acetate-equivalent readily-biodegradable COD) is present in both ASM2D and
# mASM2d; ``S_F`` (fermentable rbCOD) is the documented alternative when a
# package or design routes the supplemental donor through the fermentable pool.
_CARBON_COMPONENT_DEFAULT = "S_A"
_CARBON_COMPONENT_CANDIDATES = ("S_A", "S_F")


@declare_process_block_class("SupplementalCarbonDoseASM2D")
class SupplementalCarbonDoseASM2DData(UnitModelBlockData):
    """Deterministic external rbCOD (``S_A``) addition for ASM streams."""

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
        "carbon_component",
        ConfigValue(
            default=None,
            domain=str,
            description=(
                "Concentration component the supplemental rbCOD is added to "
                "(default S_A; S_F is the documented fermentable-pool alternative)"
            ),
        ),
    )

    def build(self):
        super().build()

        prop = self.config.property_package
        component = self._carbon_component()
        if component not in prop.solute_set:
            raise ConfigurationError(
                "SupplementalCarbonDoseASM2D requires an ASM-family property "
                f"package carrying the carbon component {component!r}; got "
                f"components {list(prop.solute_set)}"
            )

        time = self.flowsheet().time

        in_args = dict(self.config.property_package_args)
        in_args["defined_state"] = True
        self.properties_in = prop.build_state_block(time, **in_args)

        out_args = dict(self.config.property_package_args)
        out_args["defined_state"] = False
        self.properties_out = prop.build_state_block(time, **out_args)

        self.add_port("inlet", self.properties_in, doc="Inlet stream")
        self.add_port(
            "outlet", self.properties_out, doc="rbCOD-dosed outlet stream"
        )

        # Design input: external readily-biodegradable COD dosed per m^3 of
        # throughput, added to S_A (kg COD/m^3, the S_A basis). Fixing this makes
        # the unit square (the outlet state is determined); freeing it under an
        # no3_target constraint keeps the swap DOF-neutral.
        self.carbon_dose = Var(
            time,
            initialize=0.0,
            domain=NonNegativeReals,
            units=pyunits.kg / pyunits.m**3,
            doc="External rbCOD (S_A) dose, kg COD/m^3 feed",
        )

        @self.Constraint(time, doc="Volumetric flow continuity")
        def flow_balance(blk, t):
            return blk.properties_out[t].flow_vol == blk.properties_in[t].flow_vol

        @self.Constraint(time, doc="Temperature continuity")
        def temperature_balance(blk, t):
            return (
                blk.properties_out[t].temperature
                == blk.properties_in[t].temperature
            )

        @self.Constraint(time, doc="Pressure continuity")
        def pressure_balance(blk, t):
            return blk.properties_out[t].pressure == blk.properties_in[t].pressure

        if hasattr(self.properties_in[time.first()], "alkalinity"):

            @self.Constraint(time, doc="Alkalinity continuity")
            def alkalinity_balance(blk, t):
                return (
                    blk.properties_out[t].alkalinity
                    == blk.properties_in[t].alkalinity
                )

        @self.Constraint(time, doc="Supplemental rbCOD addition to S_A")
        def carbon_balance(blk, t):
            return (
                blk.properties_out[t].conc_mass_comp[component]
                == blk.properties_in[t].conc_mass_comp[component]
                + blk.carbon_dose[t]
            )

        passthrough = [c for c in prop.solute_set if c != component]

        @self.Constraint(time, passthrough, doc="Conserved-component passthrough")
        def component_passthrough(blk, t, j):
            return (
                blk.properties_out[t].conc_mass_comp[j]
                == blk.properties_in[t].conc_mass_comp[j]
            )

    def _carbon_component(self) -> str:
        configured = self.config.carbon_component
        if configured:
            return str(configured)
        prop = self.config.property_package
        solutes = set(str(c) for c in getattr(prop, "solute_set", []))
        for candidate in _CARBON_COMPONENT_CANDIDATES:
            if candidate in solutes:
                return candidate
        return _CARBON_COMPONENT_DEFAULT

    def initialize_build(
        self,
        state_args=None,
        outlvl=idaeslog.NOTSET,
        solver=None,
        optarg=None,
    ):
        """Triangular initialization: seed inlet, then compute the outlet.

        The unit is an explicit forward transform, so the outlet state is set
        directly from the (initialized) inlet and the fixed dose. No recycle.
        """

        init_log = idaeslog.getInitLogger(self.name, outlvl, tag="unit")
        solve_log = idaeslog.getSolveLogger(self.name, outlvl, tag="unit")

        if state_args is not None:
            self.properties_in.initialize(
                outlvl=outlvl,
                optarg=optarg,
                solver=solver,
                state_args=state_args,
        )

        component = self._carbon_component()
        dose_snapshot = []
        inlet_snapshot = []
        for t in self.flowsheet().time:
            in_blk = self.properties_in[t]
            out_blk = self.properties_out[t]
            dose_var = self.carbon_dose[t]
            try:
                dose = value(dose_var)
            except Exception:
                dose = 0.0
            dose = float(dose or 0.0)
            dose_snapshot.append((dose_var, bool(dose_var.fixed), dose))

            for attr in ("flow_vol", "temperature", "pressure", "alkalinity"):
                src = getattr(in_blk, attr, None)
                dst = getattr(out_blk, attr, None)
                if src is None or dst is None:
                    continue
                try:
                    inlet_snapshot.append((src, bool(src.fixed), value(src)))
                except Exception:
                    pass
                try:
                    dst.set_value(value(src), skip_validation=True)
                except Exception:
                    continue

            for j in in_blk.conc_mass_comp.keys():
                try:
                    inlet_var = in_blk.conc_mass_comp[j]
                    base = float(value(inlet_var))
                    inlet_snapshot.append((inlet_var, bool(inlet_var.fixed), base))
                except Exception:
                    continue
                if j == component:
                    base = max(base + dose, 0.0)
                try:
                    out_blk.conc_mass_comp[j].set_value(base, skip_validation=True)
                except Exception:
                    continue

        # Best-effort polish solve. The outlet state was set exactly from the
        # inlet and dose above, so the unit is already consistent; if the
        # requested solver is unavailable, fall back to the default solver and
        # never hard-fail (the seeded state is correct either way).
        results = None
        try:
            opt = get_solver(solver, optarg)
        except Exception:
            opt = get_solver()
        try:
            for inlet_var, was_fixed, inlet_value in inlet_snapshot:
                if not was_fixed:
                    inlet_var.fix(inlet_value)
            for dose_var, was_fixed, dose in dose_snapshot:
                if not was_fixed:
                    dose_var.fix(dose)
            with idaeslog.solver_log(solve_log, idaeslog.DEBUG) as slc:
                results = opt.solve(self, tee=slc.tee)
            init_log.info(
                "SupplementalCarbonDoseASM2D initialization "
                f"{idaeslog.condition(results)}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            init_log.info(
                f"SupplementalCarbonDoseASM2D polish solve skipped: {exc}"
            )
        finally:
            for dose_var, was_fixed, dose in dose_snapshot:
                try:
                    if was_fixed:
                        dose_var.fix(dose)
                    else:
                        dose_var.unfix()
                except Exception:
                    continue
            for inlet_var, was_fixed, inlet_value in inlet_snapshot:
                try:
                    inlet_var.set_value(inlet_value, skip_validation=True)
                    if was_fixed:
                        inlet_var.fix(inlet_value)
                    else:
                        inlet_var.unfix()
                except Exception:
                    continue
        return results


__all__ = [
    "SupplementalCarbonDoseASM2D",
    "DENITRIFICATION_COD_PER_NO3_N",
]

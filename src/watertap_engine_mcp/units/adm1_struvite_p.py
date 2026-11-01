'mADM1-compatible struvite (Mg) soluble-phosphorus removal block.'

from __future__ import annotations

from pyomo.common.config import ConfigBlock, ConfigValue
from pyomo.environ import (
    NonNegativeReals,
    Param,
    Var,
    sqrt,
    units as pyunits,
    value,
)

from idaes.core import UnitModelBlockData, declare_process_block_class, useDefault
from idaes.core.solvers import get_solver
from idaes.core.util.config import is_physical_parameter_block
from idaes.core.util.exceptions import ConfigurationError
import idaes.logger as idaeslog


# Struvite stoichiometry: kg magnesium reagent (Mg basis) dosed per kg P
# precipitated. Struvite MgNH4PO4 is 1 mol Mg : 1 mol P, so the mass ratio is the
# atomic-weight ratio Mg(24.305) / P(30.974). The reagent is an external input,
# not the mADM1 S_Mg state component.
STRUVITE_MG_PER_P = 24.305 / 30.974  # ~= 0.7847 kg Mg / kg P

_S_IP = "S_IP"


@declare_process_block_class("ADM1StruvitePRemoval")
class ADM1StruvitePRemovalData(UnitModelBlockData):
    """Deterministic struvite (Mg) soluble-phosphorus precipitation for mADM1.

    Single inlet, single outlet. A fixed ``mg_dose`` (external Mg reagent,
    kg/m^3 of throughput — not the mADM1 S_Mg component) removes soluble
    inorganic phosphorus (``S_IP``) by the struvite Mg:P ratio, capped by the
    available inlet S_IP. The precipitated struvite leaves the liquid as an
    untracked solid; all other components, flow, temperature, pressure, and the
    strong-ion ``cations`` / ``anions`` coordinates pass through.
    """

    CONFIG = UnitModelBlockData.CONFIG()

    CONFIG.declare(
        "property_package",
        ConfigValue(
            default=useDefault,
            domain=is_physical_parameter_block,
            description="Modified-ADM1 liquid property package (must carry S_IP)",
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
        "precipitation_smoothing_epsilon",
        ConfigValue(
            default=1e-6,
            domain=float,
            description=(
                "Smooth-min epsilon, kg P/m^3, for the dose-limited vs "
                "available-S_IP precipitation cap"
            ),
        ),
    )

    def build(self):
        super().build()

        prop = self.config.property_package
        if _S_IP not in prop.solute_set:
            raise ConfigurationError(
                "ADM1StruvitePRemoval requires a modified-ADM1 property package "
                f"carrying the inorganic-P state {_S_IP!r}; got components "
                f"{list(prop.solute_set)}"
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
            "outlet", self.properties_out, doc="Struvite-treated outlet stream"
        )

        # Design input: external magnesium reagent dosed per m^3 (NOT the mADM1
        # S_Mg state component). Fixing this makes the unit square.
        self.mg_dose = Var(
            time,
            initialize=0.0,
            domain=NonNegativeReals,
            units=pyunits.kg / pyunits.m**3,
            doc="External magnesium reagent dose driving struvite precipitation, kg/m^3 feed",
        )

        # Soluble inorganic P precipitated (kg P/m^3) implied by the dose +
        # struvite stoichiometry, capped by the available inlet S_IP.
        self.phosphorus_removed = Var(
            time,
            initialize=0.0,
            domain=NonNegativeReals,
            units=pyunits.kg / pyunits.m**3,
            doc="Soluble inorganic P precipitated as struvite, kg P/m^3 feed",
        )
        self.precipitation_smoothing_epsilon = Param(
            initialize=float(self.config.precipitation_smoothing_epsilon),
            mutable=True,
            units=pyunits.kg / pyunits.m**3,
            doc="Smooth-min epsilon for capping precipitation by available inlet S_IP",
        )

        @self.Constraint(time, doc="P removed from dose and available soluble P")
        def phosphorus_removed_eq(blk, t):
            dose_limited = blk.mg_dose[t] / STRUVITE_MG_PER_P
            available = blk.properties_in[t].conc_mass_comp[_S_IP]
            eps = blk.precipitation_smoothing_epsilon
            smooth_min = 0.5 * (
                dose_limited
                + available
                - sqrt((dose_limited - available) ** 2 + eps**2)
            )
            # The smooth-min expression is infinitesimally negative when
            # dose_limited is exactly zero. Rectify that numerical artifact
            # smoothly so the no-dose case remains a feasible pass-through.
            nonnegative_smooth_min = 0.5 * (
                smooth_min + sqrt(smooth_min**2 + (eps * 1e-6) ** 2)
            )
            return blk.phosphorus_removed[t] == nonnegative_smooth_min

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

        if hasattr(self.properties_in[time.first()], "cations"):

            @self.Constraint(time, doc="Cation strong-ion continuity")
            def cations_balance(blk, t):
                return blk.properties_out[t].cations == blk.properties_in[t].cations

            @self.Constraint(time, doc="Anion strong-ion continuity")
            def anions_balance(blk, t):
                return blk.properties_out[t].anions == blk.properties_in[t].anions

        @self.Constraint(time, doc="Soluble inorganic P consumed by struvite precipitation")
        def s_ip_balance(blk, t):
            return (
                blk.properties_out[t].conc_mass_comp[_S_IP]
                == blk.properties_in[t].conc_mass_comp[_S_IP]
                - blk.phosphorus_removed[t]
            )

        passthrough = [c for c in prop.solute_set if c != _S_IP]

        @self.Constraint(time, passthrough, doc="Conserved-component passthrough")
        def component_passthrough(blk, t, j):
            return (
                blk.properties_out[t].conc_mass_comp[j]
                == blk.properties_in[t].conc_mass_comp[j]
            )

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

        self.properties_in.initialize(
            outlvl=outlvl,
            optarg=optarg,
            solver=solver,
            state_args=state_args,
        )

        for t in self.flowsheet().time:
            in_blk = self.properties_in[t]
            out_blk = self.properties_out[t]
            try:
                dose = float(value(self.mg_dose[t]) or 0.0)
            except Exception:
                dose = 0.0
            try:
                available = float(value(in_blk.conc_mass_comp[_S_IP]))
            except Exception:
                available = 0.0
            delta_p = min(dose / STRUVITE_MG_PER_P, max(available, 0.0))
            try:
                self.phosphorus_removed[t].set_value(max(delta_p, 0.0))
            except Exception:
                pass

            for attr in ("flow_vol", "temperature", "pressure", "cations", "anions"):
                src = getattr(in_blk, attr, None)
                dst = getattr(out_blk, attr, None)
                if src is None or dst is None:
                    continue
                try:
                    dst.set_value(value(src), skip_validation=True)
                except Exception:
                    continue

            for j in in_blk.conc_mass_comp.keys():
                try:
                    base = float(value(in_blk.conc_mass_comp[j]))
                except Exception:
                    continue
                if j == _S_IP:
                    base = max(base - delta_p, 1e-9)
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
            with idaeslog.solver_log(solve_log, idaeslog.DEBUG) as slc:
                results = opt.solve(self, tee=slc.tee)
            init_log.info(
                f"ADM1StruvitePRemoval initialization {idaeslog.condition(results)}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            init_log.info(f"ADM1StruvitePRemoval polish solve skipped: {exc}")
        return results


__all__ = ["ADM1StruvitePRemoval", "STRUVITE_MG_PER_P"]

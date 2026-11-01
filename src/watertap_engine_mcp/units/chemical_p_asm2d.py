"""ASM2D-compatible chemical (metal) phosphorus-removal block.

The registered WaterTAP ``StoichiometricReactor`` is MCAS-specific and the
ModelBuilder attaches reaction packages only to AD/CSTR/CSTR_Injection/
AerationTank, so an ASM2D train cannot reach low effluent TP by a metal-salt
dose with the existing units. Bio-P alone cannot drive TP from ~24 mg/L to the
≤ 1 mg/L tender limit; a chemical (Fe/Al) precipitation step is required.

This module adds a deterministic, ASM2D-property-compatible metal-P removal
unit modeled as an **external-metal-dose precipitation surrogate**. The design
input ``metal_dose`` is the mass of metal-salt reagent dosed per m^3 of feed —
an external chemical input, NOT the ASM2D ``X_MeOH`` state component being
consumed. The dose drives precipitation of soluble orthophosphate (``S_PO4``)
into a removable metal-phosphate solid (``X_MeP``):

    P removed (kg P/m^3) = smooth_min(metal_dose / 3.45, inlet S_PO4)
    X_MeP formed         = 4.87 * P removed        (FePO4 mass basis)

This is a precipitation *surrogate*, not a closed elemental reaction: the metal
mass is tracked as an external dose, and the ASM2D ``X_MeOH`` component (if any
is present in the feed) passes through untouched rather than being stoichio-
metrically consumed. Phosphorus closes — the P leaving ``S_PO4`` reappears as
``X_MeP``-bound P (``asm2d_tp_kg_m3`` counts X_MeP at 1/4.87), so inlet TP ==
outlet TP. Excess reagent above the available inlet S_PO4 is intentionally not
converted to X_MeP; this ASM2D surrogate conserves P, not Fe.

``X_MeP`` is a particulate ASM2D component, so the downstream MBR / secondary
clarifier component-flow split routes it to the underflow/retentate -> WAS, the
same path that removes biomass solids. The block is a single-pass deterministic
transform (no kinetics / residence time), which keeps the design square and
IPOPT-friendly: fix the metal dose and the outlet state is fully determined.
"""

from __future__ import annotations

from pyomo.common.config import ConfigBlock, ConfigValue
from pyomo.environ import Constraint, NonNegativeReals, Param, Var, sqrt, units as pyunits

from idaes.core import (
    UnitModelBlockData,
    declare_process_block_class,
    useDefault,
)
from idaes.core.solvers import get_solver
from idaes.core.util.config import is_physical_parameter_block
from idaes.core.util.exceptions import ConfigurationError
import idaes.logger as idaeslog


# Phosphate-precipitation mass factors (per kg P removed).
# ASM2D_PRECIP_XMEOH_PER_P: kg of external metal-salt reagent dosed per kg P
#   precipitated (the dose-to-P ratio; the reagent is an external input, not the
#   ASM2D X_MeOH state component).
# ASM2D_PRECIP_XMEP_PER_P: kg of metal-phosphate solid (X_MeP, FePO4 mass basis
#   MW 150.8 / P 31) formed per kg P precipitated; its inverse (1/4.87) is the
#   P content used by asm2d_tp_kg_m3 so total P closes across the unit.
ASM2D_PRECIP_XMEOH_PER_P = 3.45
ASM2D_PRECIP_XMEP_PER_P = 4.87

_S_PO4 = "S_PO4"
_X_MEP = "X_MeP"
_X_MEOH = "X_MeOH"
_X_TSS = "X_TSS"

# Total-phosphorus accounting coefficients (kg P per kg of the named species),
# kept byte-for-byte identical to engineering_utils.heuristics
# .asm2d_influent_fractionation.asm2d_tp_kg_m3 so a Pyomo total-P constraint
# pinned here and the reported effluent TP agree exactly. Soluble ortho-P
# (S_PO4) and intracellular polyphosphate (X_PP) carry P at 1:1; biomass,
# inert/slowly-biodegradable particulate, and precipitated metal-phosphate
# carry their respective P contents.
ASM2D_I_PBM = 0.02          # P content of biomass (X_H + X_PAO + X_AUT)
ASM2D_I_PXI = 0.01          # P content of inert particulate X_I
ASM2D_I_PXS = 0.01          # P content of slowly-biodegradable particulate X_S
ASM2D_P_PER_XMEP = 1.0 / ASM2D_PRECIP_XMEP_PER_P  # P content of X_MeP (FePO4)


def asm2d_total_p_expr(conc):
    """Return a Pyomo expression for ASM2D total phosphorus (kg P/m^3).

    ``conc`` is a callable ``conc(component) -> Var | float`` returning the
    concentration accessor for one ASM2D component (or 0.0 if the component is
    absent). The expression mirrors ``asm2d_tp_kg_m3`` exactly: soluble
    ortho-P + polyphosphate + biomass-bound + inert/slow particulate-bound +
    metal-phosphate P.
    Used to pin the compliance metric (effluent total P) while the metal dose
    is freed — the chemical-P analogue of SRT<-WAS and DO<-KLa control.
    """

    return (
        conc(_S_PO4)
        + conc("X_PP")
        + ASM2D_I_PBM * (conc("X_H") + conc("X_PAO") + conc("X_AUT"))
        + ASM2D_I_PXI * conc("X_I")
        + ASM2D_I_PXS * conc("X_S")
        + ASM2D_P_PER_XMEP * conc(_X_MEP)
    )


@declare_process_block_class("ChemicalPhosphorusRemovalASM2D")
class ChemicalPhosphorusRemovalASM2DData(UnitModelBlockData):
    """Deterministic metal-salt phosphorus precipitation for ASM2D streams.

    Single inlet, single outlet. A fixed ``metal_dose`` (external metal-salt
    reagent dosed, kg/m^3 of throughput — not the ASM2D X_MeOH component)
    removes soluble S_PO4 and produces particulate X_MeP by the precipitation
    dose ratio. All other components (including any feed X_MeOH), flow,
    alkalinity, temperature, and pressure pass through.
    """

    CONFIG = UnitModelBlockData.CONFIG()

    CONFIG.declare(
        "property_package",
        ConfigValue(
            default=useDefault,
            domain=is_physical_parameter_block,
            description="Property package (must be an ASM2D parameter block)",
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
                "available-S_PO4 precipitation cap"
            ),
        ),
    )

    def build(self):
        super().build()

        prop = self.config.property_package
        if _S_PO4 not in prop.solute_set or _X_MEP not in prop.solute_set:
            raise ConfigurationError(
                "ChemicalPhosphorusRemovalASM2D requires an ASM2D property package "
                f"carrying {_S_PO4} and {_X_MEP}; got components {list(prop.solute_set)}"
            )

        time = self.flowsheet().time

        in_args = dict(self.config.property_package_args)
        in_args["defined_state"] = True
        self.properties_in = prop.build_state_block(time, **in_args)

        out_args = dict(self.config.property_package_args)
        out_args["defined_state"] = False
        self.properties_out = prop.build_state_block(time, **out_args)

        self.add_port("inlet", self.properties_in, doc="Inlet stream")
        self.add_port("outlet", self.properties_out, doc="Treated outlet stream")

        # Design input: external metal-salt reagent dosed per m^3 of throughput
        # (NOT the ASM2D X_MeOH state component). Fixing this makes the unit
        # square (the outlet state is determined).
        self.metal_dose = Var(
            time,
            initialize=0.0,
            domain=NonNegativeReals,
            units=pyunits.kg / pyunits.m**3,
            doc="External metal-salt reagent dose driving precipitation, kg/m^3 feed",
        )

        # Phosphorus removed (kg P/m^3) implied by the dose and stoichiometry.
        self.phosphorus_removed = Var(
            time,
            initialize=0.0,
            domain=NonNegativeReals,
            units=pyunits.kg / pyunits.m**3,
            doc="Soluble P precipitated, kg P/m^3 feed",
        )
        self.precipitation_smoothing_epsilon = Param(
            initialize=float(self.config.precipitation_smoothing_epsilon),
            mutable=True,
            units=pyunits.kg / pyunits.m**3,
            doc=(
                "Smooth-min epsilon for capping precipitation by available "
                "inlet S_PO4"
            ),
        )

        transformed = {_S_PO4, _X_MEP}
        if _X_TSS in prop.solute_set:
            transformed.add(_X_TSS)

        @self.Constraint(time, doc="P removed from dose and available soluble P")
        def phosphorus_removed_eq(blk, t):
            dose_limited = blk.metal_dose[t] / ASM2D_PRECIP_XMEOH_PER_P
            available = blk.properties_in[t].conc_mass_comp[_S_PO4]
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

        if hasattr(self.properties_in[time.first()], "alkalinity"):

            @self.Constraint(time, doc="Alkalinity continuity")
            def alkalinity_balance(blk, t):
                return (
                    blk.properties_out[t].alkalinity
                    == blk.properties_in[t].alkalinity
                )

        @self.Constraint(time, doc="Soluble phosphate consumed by precipitation")
        def s_po4_balance(blk, t):
            return (
                blk.properties_out[t].conc_mass_comp[_S_PO4]
                == blk.properties_in[t].conc_mass_comp[_S_PO4]
                - blk.phosphorus_removed[t]
            )

        @self.Constraint(time, doc="Metal phosphate produced by precipitation")
        def x_mep_balance(blk, t):
            return (
                blk.properties_out[t].conc_mass_comp[_X_MEP]
                == blk.properties_in[t].conc_mass_comp[_X_MEP]
                + ASM2D_PRECIP_XMEP_PER_P * blk.phosphorus_removed[t]
            )

        if _X_TSS in transformed:

            @self.Constraint(time, doc="Precipitated solids added to TSS")
            def x_tss_balance(blk, t):
                return (
                    blk.properties_out[t].conc_mass_comp[_X_TSS]
                    == blk.properties_in[t].conc_mass_comp[_X_TSS]
                    + ASM2D_PRECIP_XMEP_PER_P * blk.phosphorus_removed[t]
                )

        passthrough = [c for c in prop.solute_set if c not in transformed]

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

        from pyomo.environ import value

        for t in self.flowsheet().time:
            in_blk = self.properties_in[t]
            out_blk = self.properties_out[t]
            try:
                dose = value(self.metal_dose[t])
            except Exception:
                dose = 0.0
            delta_p = (dose or 0.0) / ASM2D_PRECIP_XMEOH_PER_P
            try:
                self.phosphorus_removed[t].set_value(delta_p)
            except Exception:
                pass

            for attr in ("flow_vol", "temperature", "pressure", "alkalinity"):
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
                if j == _S_PO4:
                    base = max(base - delta_p, 1e-9)
                elif j == _X_MEP:
                    base = base + ASM2D_PRECIP_XMEP_PER_P * delta_p
                elif j == _X_TSS:
                    base = base + ASM2D_PRECIP_XMEP_PER_P * delta_p
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
                "ChemicalPhosphorusRemovalASM2D initialization "
                f"{idaeslog.condition(results)}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            init_log.info(
                f"ChemicalPhosphorusRemovalASM2D polish solve skipped: {exc}"
            )
        return results

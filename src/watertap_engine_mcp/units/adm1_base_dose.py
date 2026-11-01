"""mADM1-family base (caustic / strong-cation) dose block.

This deterministic single-inlet/single-outlet unit is the ADM1 analogue of
``AlkalinityDoseASM2D`` — but it does NOT reuse the ASM-family alkalinity
coordinate. Modified-ADM1 has no native ``alkalinity`` state and does not carry
inorganic carbon as a manipulable alkalinity surrogate the way ASM2d does;
instead it solves pH from electroneutrality. The mADM1 liquid state exposes a
strong-ion difference through the scalar ``cations`` / ``anions`` Vars
(kmol/m^3), and the modified-ADM1 reaction block closes the acid/base balance
``S_H_cons`` (``cations + NH4 + Mg + K + S_H - HCO3 - VFAs - OH - anions == 0``),
solving ``S_H`` / ``pH`` from that strong-ion difference. Adding strong base
(NaOH-equivalent) therefore raises the cation pool, which raises alkalinity and
pH; adding strong acid would raise the anion pool.

This block models a caustic (strong-base) dose as a strong-cation addition:

    out.cations == in.cations + caustic_dose

while flow, temperature, pressure, the ``anions`` strong-ion coordinate, and
every ``conc_mass_comp`` species (the COD/N/P/trace-ion components) pass through
unchanged. The dose is in the package-native ``cations`` basis (kmol/m^3). No
acid/base equilibrium lives in this block — the pH response is solved DOWNSTREAM
by the digester reaction block's ``S_H_cons`` electroneutrality constraint
against the dosed strong-ion difference. Placed AHEAD of an ADM1 digester, a
freed ``caustic_dose`` under an ``adm1_ph_target`` process constraint lets the
solver size the base addition so the digester's reaction-block pH (or a
strong-ion alkalinity surrogate) meets the setpoint. Fixing the dose makes the
unit square (the outlet state is fully determined); freeing it under the target
constraint keeps the swap DOF-neutral.
"""

from __future__ import annotations

from pyomo.common.config import ConfigBlock, ConfigValue
from pyomo.environ import Reals, Var, units as pyunits, value

from idaes.core import UnitModelBlockData, declare_process_block_class, useDefault
from idaes.core.solvers import get_solver
from idaes.core.util.config import is_physical_parameter_block
from idaes.core.util.exceptions import ConfigurationError
import idaes.logger as idaeslog


@declare_process_block_class("ADM1BaseDose")
class ADM1BaseDoseData(UnitModelBlockData):
    """Deterministic strong-base (caustic) strong-cation addition for mADM1 streams.

    A fixed ``caustic_dose`` (kmol/m^3, NaOH-equivalent) is added to the mADM1
    ``cations`` strong-ion coordinate; everything else passes through. The pH
    consequence is solved downstream by the digester reaction electroneutrality
    (``S_H_cons``) — this unit carries NO acid/base equilibrium of its own.
    """

    CONFIG = UnitModelBlockData.CONFIG()

    CONFIG.declare(
        "property_package",
        ConfigValue(
            default=useDefault,
            domain=is_physical_parameter_block,
            description="Modified-ADM1 liquid property package",
        ),
    )
    CONFIG.declare(
        "property_package_args",
        ConfigBlock(
            implicit=True,
            description="Arguments passed to the property package state blocks",
        ),
    )

    def build(self):
        super().build()

        prop = self.config.property_package
        time = self.flowsheet().time

        in_args = dict(self.config.property_package_args)
        in_args["defined_state"] = True
        self.properties_in = prop.build_state_block(time, **in_args)

        out_args = dict(self.config.property_package_args)
        out_args["defined_state"] = False
        self.properties_out = prop.build_state_block(time, **out_args)

        self.add_port("inlet", self.properties_in, doc="Inlet stream")
        self.add_port("outlet", self.properties_out, doc="Base-dosed outlet stream")

        in_state = self.properties_in[time.first()]
        if not hasattr(in_state, "cations") or not hasattr(in_state, "anions"):
            raise ConfigurationError(
                "ADM1BaseDose requires a modified-ADM1 liquid property package "
                "exposing the strong-ion 'cations'/'anions' state vars; got a "
                f"state block without them ({type(in_state).__name__})"
            )

        # Design input: NaOH-equivalent strong-base dose added to the cation pool
        # (kmol/m^3, the cations basis). Domain is Reals (not NonNegative): a
        # negative dose represents a strong-ACID addition (raising the anion pool
        # is equivalent to lowering net cations), so a pH target BELOW the natural
        # digester pH is reachable. Fixing this makes the unit square; freeing it
        # under an adm1_ph_target constraint keeps the swap DOF-neutral.
        self.caustic_dose = Var(
            time,
            initialize=0.0,
            domain=Reals,
            units=pyunits.kmol / pyunits.m**3,
            doc="NaOH-equivalent strong-base (cation) dose, kmol/m^3 feed",
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

        @self.Constraint(time, doc="Strong-base addition to the cation pool")
        def cations_balance(blk, t):
            return (
                blk.properties_out[t].cations
                == blk.properties_in[t].cations + blk.caustic_dose[t]
            )

        @self.Constraint(time, doc="Anion strong-ion continuity")
        def anions_balance(blk, t):
            return blk.properties_out[t].anions == blk.properties_in[t].anions

        @self.Constraint(
            time, prop.solute_set, doc="Conserved-component passthrough"
        )
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
                dose = float(value(self.caustic_dose[t]) or 0.0)
            except Exception:
                dose = 0.0

            for attr in ("flow_vol", "temperature", "pressure", "anions"):
                src = getattr(in_blk, attr, None)
                dst = getattr(out_blk, attr, None)
                if src is None or dst is None:
                    continue
                try:
                    dst.set_value(value(src), skip_validation=True)
                except Exception:
                    continue

            if hasattr(in_blk, "cations") and hasattr(out_blk, "cations"):
                try:
                    out_blk.cations.set_value(
                        max(value(in_blk.cations) + dose, 0.0),
                        skip_validation=True,
                    )
                except Exception:
                    pass

            for j in in_blk.conc_mass_comp.keys():
                try:
                    out_blk.conc_mass_comp[j].set_value(
                        float(value(in_blk.conc_mass_comp[j])), skip_validation=True
                    )
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
                f"ADM1BaseDose initialization {idaeslog.condition(results)}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            init_log.info(f"ADM1BaseDose polish solve skipped: {exc}")
        return results


__all__ = ["ADM1BaseDose"]

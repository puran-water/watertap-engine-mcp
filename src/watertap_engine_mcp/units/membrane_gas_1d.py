'Gas-separation membrane unit for biogas upgrading (CO2/CH4).'

from pyomo.common.config import ConfigValue
from pyomo.environ import Param
from pyomo.environ import units as pyunits

from idaes.core import MaterialFlowBasis, declare_process_block_class
from idaes.models_extra.co2_capture_and_utilization.unit_models.membrane_1d import (
    Membrane1DData,
)

# Literal-GPU reference molar density: 0 C, 1.013 bar ideal gas.
STP_DENS_MOL = 101325.0 / (8.314462618 * 273.15)  # ~44.62 mol/m3


@declare_process_block_class("GasMembraneStage1D")
class GasMembraneStage1DData(Membrane1DData):
    """1-D CO2/CH4 gas-separation membrane with a fixed flux density reference."""

    CONFIG = Membrane1DData.CONFIG()
    CONFIG.declare(
        "reference_dens_mol",
        ConfigValue(
            default=STP_DENS_MOL,
            domain=float,
            description=(
                "Fixed molar density [mol/m3] used in the flux law in place of the "
                "local gas density, so 'permeance' is an intrinsic, pressure-"
                "independent literal vendor GPU."
            ),
        ),
    )

    def build(self):
        super().build()
        self._apply_fixed_density_flux()

    def initialize(self, state_args=None, outlvl=None, solver=None, optarg=None, **kwargs):
        """Square-block initialization for the flowsheet-level sequential initializer.

        The no-sweep stage carries an extra design spec (the permeate / sweep-side
        outlet pressure, already fixed by the builder), so it is DoF=0 only once the
        feed-side inlet is pinned. The standard sequential initializer pins inlets via
        propagation but does not fix them, so we fix the feed-side inlet at its current
        (propagated) values, run IDAES BlockTriangularizationInitializer, then release
        the inlet for the flowsheet connection arc to take over.
        """
        from pyomo.environ import value
        from idaes.core.initialization import BlockTriangularizationInitializer

        # Guard against the sequential initializer's relax-outlet-pressure retry path,
        # which unfixes the permeate (sweep-side) pressure design spec between attempts.
        # Re-fix it so this initialize() is idempotent and stays DoF=0.
        for t in self.flowsheet().time:
            p = self.sweep_side_outlet.pressure[t]
            if not p.fixed:
                p.fix(value(p))

        comps = list(self.mscontactor.feed_side.component_list)
        # Renormalize the propagated feed composition before fixing: clamp tiny
        # negatives (FTPx mole fractions can solve slightly < 0) and rescale to sum
        # to 1, so fixing all components is feasible (DoF=0) and inside NonNegativeReals.
        raw = {}
        for c in comps:
            v = value(self.feed_side_inlet.mole_frac_comp[0, c])
            raw[c] = max(0.0, v) if v is not None else 0.0
        total = sum(raw.values()) or 1.0
        for c in comps:
            self.feed_side_inlet.mole_frac_comp[0, c].set_value(raw[c] / total)

        feed_vars = [
            self.feed_side_inlet.flow_mol[0],
            self.feed_side_inlet.temperature[0],
            self.feed_side_inlet.pressure[0],
        ]
        feed_vars += [self.feed_side_inlet.mole_frac_comp[0, c] for c in comps]
        newly_fixed = [v for v in feed_vars if not v.fixed]
        for v in newly_fixed:
            v.fix()
        try:
            BlockTriangularizationInitializer(constraint_tolerance=1e-3).initialize(self)
        finally:
            for v in newly_fixed:
                v.unfix()

    def _apply_fixed_density_flux(self):
        # Deactivate the stock local-density flux and replace with a fixed reference.
        self.permeability_calculation.deactivate()

        self.reference_dens_mol = Param(
            initialize=self.config.reference_dens_mol,
            units=pyunits.mol / pyunits.m**3,
            mutable=True,
            doc="Fixed reference molar density for the transmembrane flux law",
        )

        feed_side_units = (
            self.config.feed_side.property_package.get_metadata().derived_units
        )
        p_units = feed_side_units.PRESSURE
        crossover = list(
            set(self.mscontactor.feed_side.component_list)
            & set(self.mscontactor.sweep_side.component_list)
        )

        @self.Constraint(
            self.flowsheet().time,
            self.mscontactor.elements,
            crossover,
            doc="permeability calculation (fixed reference density)",
        )
        def permeability_calculation_fixed_density(b, t, s, m):
            basis = b.mscontactor.feed_side[t, s].get_material_flow_basis()
            if basis is MaterialFlowBasis.molar:
                mb_units = feed_side_units.FLOW_MOLE
            else:
                raise TypeError(
                    "GasMembraneStage1D requires a molar material flow basis "
                    "(natural_gas_PR); got "
                    f"{basis}"
                )
            return b.mscontactor.material_transfer_term[
                t, s, "feed_side", "sweep_side", m
            ] == -pyunits.convert(
                (
                    b.reference_dens_mol
                    * b.gpu_factor
                    * b.permeance[t, s, m]
                    * b.cell_area
                    * (
                        b.mscontactor.feed_side[t, s].pressure
                        * b.mscontactor.feed_side[t, s].mole_frac_comp[m]
                        - pyunits.convert(
                            b.mscontactor.sweep_side[t, s].pressure, to_units=p_units
                        )
                        * b.mscontactor.sweep_side[t, s].mole_frac_comp[m]
                    )
                ),
                to_units=mb_units,
            )

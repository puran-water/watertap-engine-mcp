'House mADM1 SULFUR/IRON digester: the shared S/Fe kernel compiled into WaterTAP (phase 3a).'

from __future__ import annotations

import contextlib
import math
from typing import Any, Mapping, Optional

import idaes.core.util.scaling as iscale
import idaes.logger as idaeslog
import pyomo.environ as pyo
from idaes.core import Solute, StateBlockData, declare_process_block_class
from pyomo.common.config import Bool, ConfigValue
from idaes.core.util.constants import Constants
from idaes.core.util.exceptions import ConfigurationError
from watertap.property_models.unit_specific.anaerobic_digestion.adm1_properties_vapor import (
    ADM1_vaporParameterData,
    ADM1_vaporStateBlockData,
    _ADM1_vaporStateBlock,
)
from watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_properties import (
    ModifiedADM1ParameterData,
    ModifiedADM1StateBlockData,
    _ModifiedADM1StateBlock,
)
from watertap.unit_models.anaerobic_digester import ADData, ADScaler

from watertap_engine_mcp.chemistry.modified_adm1_kernel import CARRIER_BASIS, KernelADM1ReactionBlockData, KernelADM1ReactionParameterData, KernelReactionScaler, _KernelReactionBlock, kernel_parameter_symbols
from watertap_engine_mcp.chemistry.pyomo_compiler import compile_pyomo
from puran_mcp_support.chemistry.evaluator import evaluate
from puran_mcp_support.chemistry.madm1_speciation import ION_PAIRS
from puran_mcp_support.chemistry.madm1_biokinetics import madm1_parameter_values, madm1_stoichiometric_matrix
from puran_mcp_support.schema.madm1_iron_reduction import MADM1_IRON_REDUCTION_EXTENSION, validate_iron_reduction_extension
from puran_mcp_support.schema.madm1_iron_minerals import FORMULA_MASS, IRON_PER_FORMULA, MADM1_IRON_MINERAL_EXTENSION, MAGNESIUM_PER_FORMULA, SULFUR_PER_FORMULA
from puran_mcp_support.schema.madm1_organic_sulfur import MADM1_ORGANIC_SULFUR_EXTENSION, validate_organic_sulfur_extension
from puran_mcp_support.schema.madm1_sfe_kinetics import MADM1_FES_PRECIPITATION, MADM1_H2S_TRANSFER, MADM1_SFE_COMPONENTS, MADM1_SFE_KERNEL, MADM1_SFE_PARAMETERS

SFE_KERNEL = MADM1_SFE_KERNEL
SFE_KERNEL_SOURCE = 'puran_mcp_support.schema.madm1_sfe_kinetics.MADM1_SFE_KERNEL'
#: WaterTAP reaction id -> kernel process, R1..R56 (the twin's process indices 0..55, in order).
SFE_REACTION_PROCESS = {f"R{p.index + 1}": p for p in SFE_KERNEL.processes}
#: The mineral extension (vivianite, siderite, mackinawite, struvite), compiled into every S/Fe build: the S/Fe
#: digester's mineral step.
IRON_MINERALS = MADM1_IRON_MINERAL_EXTENSION
IRON_MINERALS_SOURCE = 'puran_mcp_support.schema.madm1_iron_minerals.MADM1_IRON_MINERAL_EXTENSION'
IRON_MINERAL_COMPONENTS = IRON_MINERALS.component_ids
#: Every state the S/Fe packages add to WaterTAP's ModifiedADM1: the S/Fe kernel's and the iron minerals'.
SFE_BLOCK_COMPONENTS = MADM1_SFE_COMPONENTS + IRON_MINERAL_COMPONENTS
#: WaterTAP reaction id -> mineral precipitation, after the kernel's R1..R56: FeS (R57, the twin's 66), vivianite (R58,
#: 68), siderite (R59, 69), mackinawite (R60, 67), struvite (R61, 62). Each rate is its definition's own law.
MINERAL_REACTION_PROCESS = {f"R{len(SFE_KERNEL.processes) + 1 + i}": p
                            for i, p in enumerate((MADM1_FES_PRECIPITATION,) + IRON_MINERALS.processes)}
#: The FeS precipitation's WaterTAP reaction id (the twin's process 66).
FES_REACTION = next(r for r, p in MINERAL_REACTION_PROCESS.items() if p is MADM1_FES_PRECIPITATION)

#: minerals: one reaction per HFO host (R62-R68), each its definition's own law. The twin compiles none of it yet.
IRON_REDUCTION = MADM1_IRON_REDUCTION_EXTENSION
IRON_REDUCTION_SOURCE = 'puran_mcp_support.schema.madm1_iron_reduction.MADM1_IRON_REDUCTION_EXTENSION'
IRON_REDUCTION_REACTION_PROCESS = {
    f"R{len(SFE_KERNEL.processes) + len(MINERAL_REACTION_PROCESS) + 1 + i}": p for i, p in enumerate(IRON_REDUCTION.processes)}
#: The reactions this block adds to the library's R1..R25, in order.
SFE_REACTIONS = (tuple(f"R{p.index + 1}" for p in SFE_KERNEL.processes[25:]) + tuple(MINERAL_REACTION_PROCESS)
                 + tuple(IRON_REDUCTION_REACTION_PROCESS))
#: WaterTAP reaction id -> the twin's process index, for every reaction this block carries that the twin compiles (the
#: iron reduction's are not: `iron_reduction_identity`).
TWIN_PROCESS_INDEX = {**{r: p.index for r, p in SFE_REACTION_PROCESS.items()},
                      **{r: p.index for r, p in MINERAL_REACTION_PROCESS.items()}}
#: The FeS law's smoothing epsilon as WaterTAP binds it (``mineral_smoothing``: the maxes and sign of the twin's
#: crystallisation law, 0.5 (x + sqrt(x^2 + eps^2))). max(SI, 0) >= eps/2 keeps d SI^(1/2) / d SI <= 0.5 (eps/2)^-0.5
#: (7e3 at 1e-8) where SI -> 0; the drive D = SI^(1/2) - 1 then sits at most (eps/2)^0.5 = 7e-5 above -1 (the
#: dissolution rate low by 1.4e-4 relative there), and the growth term's max moves the rate by k X (eps/2)^2 ~ 6e-14 X/d.
FES_MINERAL_SMOOTHING = 1e-8
#: Nominal magnitudes (kg/m3/s) the reaction-rate scaling reads for the added reactions (the library's ``rates``).
SFE_NOMINAL_RATE = 1e-8
SFE_COMPONENT_DOCS = {
    "S_SO4": "Sulfate (kg S)", "S_IS": "Total dissolved sulfide H2S + HS- (kg S)", "S_S0": "Elemental sulfur (kg S)",
    "X_hSRB": "Hydrogenotrophic sulfate reducers (kg COD)", "X_aSRB": "Acetotrophic sulfate reducers (kg COD)",
    "X_pSRB": "Propionate-degrading sulfate reducers (kg COD)",
    "X_c4SRB": "Butyrate/valerate-degrading sulfate reducers (kg COD)",
    "S_Fe3": "Ferric iron (kg Fe)", "S_Fe2": "Ferrous iron (kg Fe)",
    "X_HFO_H": "High-activity hydrous ferric oxide (kg Fe)", "X_HFO_L": "Low-activity HFO (kg Fe)",
    "X_HFO_old": "Aged HFO (kg Fe)", "X_HFO_HP": "High-activity HFO with bound P (kg Fe)",
    "X_HFO_LP": "Low-activity HFO with bound P (kg Fe)", "X_HFO_HP_old": "Aged HFO-HP (kg Fe)",
    "X_HFO_LP_old": "Aged HFO-LP (kg Fe)", "X_FeS": "Iron sulfide, amorphous (kg FeS)",
    "X_Fe3PO42": "Vivianite, dry formula basis (kg Fe3(PO4)2)", "X_Siderite": "Siderite (kg FeCO3)",
    "X_Mackinawite": "Iron sulfide, mackinawite (kg FeS)", "X_struv": "Struvite, anhydrous formula basis (kg MgNH4PO4)",
}
SRB_BIOMASS = ("X_hSRB", "X_aSRB", "X_pSRB", "X_c4SRB")
#: The opt-in organic-sulfur extension, compiled ONLY into the organic-sulfur variant (module docstring).
ORGANIC_S = MADM1_ORGANIC_SULFUR_EXTENSION
ORGANIC_S_SOURCE = 'puran_mcp_support.schema.madm1_organic_sulfur.MADM1_ORGANIC_SULFUR_EXTENSION'
#: Its components (X_Sorg), after the S/Fe set.
ORGANIC_S_COMPONENTS = ORGANIC_S.component_ids
#: WaterTAP reaction id -> extension process: after the iron reduction (R69 onward), in the extension's order (twin 75
#: onward).
ORGANIC_S_REACTION_PROCESS = {f"R{int(SFE_REACTIONS[-1][1:]) + 1 + i}": p for i, p in enumerate(ORGANIC_S.processes)}
ORGANIC_S_REACTIONS = tuple(ORGANIC_S_REACTION_PROCESS)
#: WaterTAP reaction id -> the twin's process index for every reaction the organic-sulfur variant carries.
ORGANIC_S_TWIN_PROCESS_INDEX = {**TWIN_PROCESS_INDEX, **{r: p.index for r, p in ORGANIC_S_REACTION_PROCESS.items()}}
ORGANIC_S_COMPONENT_DOCS = {
    "X_Sorg": ("Particulate organic sulfur (kg S; no C/N/P/K/Mg, retained like X_pr); counted with the organic solids "
               "in VSS at its own S mass"),
}


def iron_minerals_identity() -> dict[str, str]:
    """The iron-mineral extension's identity, stamped beside the S/Fe kernel's on every S/Fe build."""
    return {"iron_mineral_extension_source": IRON_MINERALS_SOURCE,
            "iron_mineral_extension_id": IRON_MINERALS.extension_id,
            "iron_mineral_extension_sha256": IRON_MINERALS.sha256}


def iron_reduction_identity() -> dict[str, Any]:
    """The iron-reduction extension's identity, stamped beside the S/Fe kernel's on every S/Fe build: its reactions by
    process id (the definition's indices are not the twin's: the twin compiles none of them yet)."""
    return {"iron_reduction_extension_source": IRON_REDUCTION_SOURCE,
            "iron_reduction_extension_id": IRON_REDUCTION.extension_id,
            "iron_reduction_extension_sha256": IRON_REDUCTION.sha256,
            "iron_reduction_reactions": {r: p.process_id for r, p in IRON_REDUCTION_REACTION_PROCESS.items()}}


def _block_content(cid: str):
    """A S/Fe package state's element contents: the S/Fe kernel's, or the iron-mineral extension's."""
    return IRON_MINERALS.content(cid) if cid in IRON_MINERAL_COMPONENTS else SFE_KERNEL.content(cid)


def organic_sulfur_identity() -> dict[str, str]:
    """The extension's identity, stamped beside the S/Fe kernel's wherever the organic-sulfur variant answers."""
    return {"organic_sulfur_extension_source": ORGANIC_S_SOURCE,
            "organic_sulfur_extension_id": ORGANIC_S.extension_id,
            "organic_sulfur_extension_sha256": ORGANIC_S.sha256}
#: The S/Fe kernel's own molar masses for the carriers' elements (QSDsan's, which its contents are stated at).
_KERNEL_MW = {"C": 12.011, "N": 14.0067, "P": 30.973761}


# --------------------------------------------------------------------------------------------- liquid property
@declare_process_block_class("SFEModifiedADM1StateBlock", block_class=_ModifiedADM1StateBlock)
class SFEModifiedADM1StateBlockData(ModifiedADM1StateBlockData):
    """ModifiedADM1 state over the S/Fe component list; SRB biomass counts in VSS/ISS as biomass does."""

    def _VSS(self):
        super()._VSS()
        lhs, rhs = self.eq_VSS.expr.args
        self.eq_VSS.set_value(lhs == rhs + sum(self.conc_mass_comp[j] for j in SRB_BIOMASS)
                              / self.params.CODtoVSS_XBM)

    def _ISS(self):
        super()._ISS()
        lhs, rhs = self.eq_ISS.expr.args
        self.eq_ISS.set_value(lhs == rhs + self.params.f_ISS_BM * sum(self.conc_mass_comp[j] for j in SRB_BIOMASS)
                              / self.params.CODtoVSS_XBM)


@declare_process_block_class("SFEModifiedADM1ParameterBlock")
class SFEModifiedADM1ParameterData(ModifiedADM1ParameterData):
    """WaterTAP's ModifiedADM1 property package plus the 17 S/Fe components of the shared S/Fe kernel and the two
    iron minerals of its extension (``SFE_BLOCK_COMPONENTS``)."""

    def build(self):
        super().build()
        self._state_block_class = SFEModifiedADM1StateBlock
        for cid in SFE_BLOCK_COMPONENTS:
            setattr(self, cid, Solute(doc=SFE_COMPONENT_DOCS[cid]))


@declare_process_block_class("SFEOrganicSModifiedADM1StateBlock", block_class=_ModifiedADM1StateBlock)
class SFEOrganicSModifiedADM1StateBlockData(SFEModifiedADM1StateBlockData):
    """The S/Fe state with particulate organic sulfur: X_Sorg (kg S) counts in VSS with the organic solids, at its own
    mass (it carries no COD, so no COD-to-VSS factor applies)."""

    def _VSS(self):
        super()._VSS()
        lhs, rhs = self.eq_VSS.expr.args
        self.eq_VSS.set_value(lhs == rhs + sum(self.conc_mass_comp[j] for j in ORGANIC_S_COMPONENTS))


@declare_process_block_class("SFEOrganicSModifiedADM1ParameterBlock")
class SFEOrganicSModifiedADM1ParameterData(SFEModifiedADM1ParameterData):
    """The S/Fe property package plus the organic-sulfur extension's component (X_Sorg, kg S)."""

    def build(self):
        super().build()
        self._state_block_class = SFEOrganicSModifiedADM1StateBlock
        for cid in ORGANIC_S_COMPONENTS:
            setattr(self, cid, Solute(doc=ORGANIC_S_COMPONENT_DOCS[cid]))


# ---------------------------------------------------------------------------------------------- vapor property
@declare_process_block_class("SFEADM1VaporStateBlock", block_class=_ADM1_vaporStateBlock)
class SFEADM1VaporStateBlockData(ADM1_vaporStateBlockData):
    """The ADM1 headspace state with H2S. The library's build raises for an unknown vapor component, so this build is
    the library's with the S_h2s branch added (every other line as upstream WaterTAP 1.8.0 writes it). The component
    tables are class attributes so a variant headspace (the micro-aeration O2/N2) extends them without a second build;
    the S/Fe values are exactly the ones this build always used."""

    #: initial gas concentrations (kg/m3 on each component's basis) and partial pressures (Pa)
    _INITIAL_CONC = {"S_ch4": 1.6256, "S_co2": 0.01415 * 12, "S_h2": 1e-5, "S_h2s": 1e-9}
    _INITIAL_PRESSURE = {"S_ch4": 65077, "S_co2": 36255, "S_h2": 1.639, "S_h2s": 1e-4, "H2O": 5570}
    #: kg per kmol of each component's measured basis (COD for H2 and CH4, C for CO2, S for H2S)
    _MOLAR_MASS = {"S_h2": 16, "S_ch4": 64, "S_co2": 12, "S_h2s": float(MADM1_SFE_KERNEL.parameter_values()["MW_S"])}
    #: components that sit at ~0 on a feed without them and are equality-defined: unbounded (see the build)
    _UNBOUNDED = ("S_h2s",)

    def build(self):
        StateBlockData.build(self)
        self.flow_vol = pyo.Var(initialize=1, domain=pyo.NonNegativeReals, doc="Total volumetric flowrate",
                                units=pyo.units.m**3 / pyo.units.s)
        self.pressure = pyo.Var(domain=pyo.NonNegativeReals, initialize=101325.0, bounds=(1e3, 1e6), doc="Pressure",
                                units=pyo.units.Pa)
        self.temperature = pyo.Var(domain=pyo.NonNegativeReals, initialize=298.15, bounds=(273.15, 323.15),
                                   doc="Temperature", units=pyo.units.K)
        # H2S starts near zero: a sulfur-free feed leaves it there, and a 5,000 ppm initial headspace drives sulfide
        # INTO the liquor on the first Newton step of a sulfur-free solve (the Terminus design digester did not start)
        comp = dict(self._INITIAL_CONC)
        self.conc_mass_comp = pyo.Var(self.params.solute_set, domain=pyo.NonNegativeReals, initialize=comp,
                                      doc="Component mass concentrations", units=pyo.units.kg / pyo.units.m**3)
        init = dict(self._INITIAL_PRESSURE)
        self.pressure_sat = pyo.Var(self.params.component_list, domain=pyo.NonNegativeReals, initialize=init,
                                    doc="Component pressure", units=pyo.units.Pa)
        molar_mass = dict(self._MOLAR_MASS)

        def pressure_sat_rule(b, j):
            if j == "H2O":
                return pyo.log(b.pressure_sat[j] / pyo.units.Pa) == (
                    pyo.log(0.0313) + 5290 * pyo.units.K * ((1 / b.params.temperature_ref) - (1 / b.temperature))
                    + pyo.log(1e5))
            if j in molar_mass:
                return b.pressure_sat[j] == pyo.units.convert(
                    b.conc_mass_comp[j] * (1000 * pyo.units.g / pyo.units.kg) * Constants.gas_constant
                    * b.temperature / (molar_mass[j] * pyo.units.g / pyo.units.mole), to_units=pyo.units.Pa)
            raise ConfigurationError(f"vapor component {j!r} is not implemented")

        self._pressure_sat = pyo.Constraint(self.params.component_list, rule=pressure_sat_rule,
                                            doc="Saturation pressure for components")
        # The headspace H2S is equality-defined (its transfer law and the take-off balance) and sits at ~0 on a
        # sulfur-free feed: with its nonnegativity bounds the interior point stalled against the barrier there (the
        # Terminus design digester converged to local infeasibility with the H2S coupling on, and solved with it off;
        # lane madm1-sfe-phase3a evidence terminus/dis_all*.log). Unbounded, it takes its value from those equations.
        for j in self._UNBOUNDED:
            for var in (self.conc_mass_comp[j], self.pressure_sat[j]):
                var.domain = pyo.Reals
                var.setlb(None)

        def material_flow_expression(b, j):
            return b.flow_vol * b.params.dens_mass if j == "H2O" else b.flow_vol * b.conc_mass_comp[j]

        self.material_flow_expression = pyo.Expression(self.component_list, rule=material_flow_expression,
                                                       doc="Material flow terms")
        self.enthalpy_flow_expression = pyo.Expression(
            rule=lambda b: b.flow_vol * b.params.dens_mass * b.params.cp_mass * (b.temperature - b.params.temperature_ref),
            doc="Enthalpy flow term")
        self.material_density_expression = pyo.Expression(
            self.component_list, rule=lambda b, j: b.params.dens_mass if j == "H2O" else b.conc_mass_comp[j],
            doc="Material density terms")
        self.energy_density_expression = pyo.Expression(
            rule=lambda b: b.params.dens_mass * b.params.cp_mass * (b.temperature - b.params.temperature_ref),
            doc="Energy density term")

    def calculate_scaling_factors(self):
        super().calculate_scaling_factors()
        iscale.set_scaling_factor(self.conc_mass_comp["S_h2s"], 1e3)
        iscale.set_scaling_factor(self.pressure_sat["S_h2s"], 1e-2)
        iscale.set_scaling_factor(self.material_flow_expression["S_h2s"],
                                  iscale.get_scaling_factor(self.flow_vol, default=1e2) * 1e3)
        iscale.set_scaling_factor(self.material_density_expression["S_h2s"], 1e3)


@declare_process_block_class("SFEADM1VaporParameterBlock")
class SFEADM1VaporParameterData(ADM1_vaporParameterData):
    """The ADM1 headspace property package plus hydrogen sulfide (kg S/m3)."""

    def build(self):
        super().build()
        self._state_block_class = SFEADM1VaporStateBlock
        self.S_h2s = Solute(doc="Hydrogen sulfide gas (kg S)")


# ------------------------------------------------------------------------------------------------- reactions
MW_S = float(MADM1_SFE_KERNEL.parameter_values()["MW_S"])
MW_FE = float(MADM1_SFE_KERNEL.parameter_values()["MW_Fe"])
#: kg Fe per kg of each iron mineral (its formula on the extension's molar masses).
IRON_MINERAL_FE_SHARE = {cid: IRON_PER_FORMULA[cid] * MW_FE / FORMULA_MASS[cid] for cid in IRON_MINERAL_COMPONENTS}
#: kg S per kg of each sulfide mineral (mackinawite), for the sulfur closure.
IRON_MINERAL_S_SHARE = {cid: n * MW_S / FORMULA_MASS[cid] for cid, n in SULFUR_PER_FORMULA.items()}
#: kg Mg per kg of each magnesium mineral (struvite), for the magnesium closure.
MINERAL_MG_SHARE = {cid: _block_content(cid).content("Mg") for cid in MAGNESIUM_PER_FORMULA}
SULFIDE_VARIABLE_SCALING = {"pK_a_h2s": 1.0}
SULFIDE_CONSTRAINT_SCALING = {"H2S_acid_base_equilibrium": 1.0}


def sfe_design_parameter_values(store: Any = None) -> tuple[dict[str, float], dict[str, Any]]:
    """(values, identity) of the digester's S/Fe design set, read through custody."""
    from puran_mcp_support.engineering.heuristics.anaerobic.sfe_kinetics import madm1_sfe_design_parameters

    values = madm1_sfe_design_parameters(store)
    identity = values.pop("_identity")
    return values, identity


@contextlib.contextmanager
def _library_reactions_only(params):
    """Hold the S/Fe reactions out of the shared ``rate_reaction_idx`` while the library chain builds R1..R25
    (module docstring: WaterTAP 1.8.0's rate rule raises for anything else), and put them back, in order."""
    added = [r for r in params.sfe_reaction_idx]
    for r in added:
        params.rate_reaction_idx.remove(r)
    try:
        yield
    finally:
        for r in added:
            params.rate_reaction_idx.add(r)


class SFEReactionScaler(KernelReactionScaler):
    def variable_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().variable_scaling_routine(model, overwrite, submodel_scalers)
        for name, factor in SULFIDE_VARIABLE_SCALING.items():
            self.set_variable_scaling_factor(getattr(model, name), factor, overwrite=overwrite)

    def constraint_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().constraint_scaling_routine(model, overwrite, submodel_scalers)
        for name, factor in SULFIDE_CONSTRAINT_SCALING.items():
            self.set_constraint_scaling_factor(getattr(model, name), factor, overwrite=overwrite)


def _log10_ka(constant_id: str, temperature):
    """log10 Ka at the block's temperature: the ONE shared definition (process_model_schema.speciation, ruling
    HOUSE-MADM1-ACID-CONSTANTS-EVALUATED-LITERATURE-20261008) rendered through its own law (``log10_ka_law``), never
    restated here."""
    from puran_mcp_support.schema.speciation import ACID_DISSOCIATION_CONSTANTS

    return ACID_DISSOCIATION_CONSTANTS[constant_id].log10_ka_law(temperature / pyo.units.K, pyo.log10)


def _h2s_log10_ka(temperature):
    """log10 Ka1(H2S) at the block's temperature (shared "sulfide_1")."""
    return _log10_ka("sulfide_1", temperature)


class _SFEReactionBlock(_KernelReactionBlock):
    default_scaler = SFEReactionScaler


def _sfe_symbols(block) -> dict[str, Any]:
    """Every symbol the S/Fe kernel's rate laws read, on this block (dimensionless numbers in the kernel's units)."""
    mass = pyo.units.kg / pyo.units.m**3
    molar = pyo.units.kmol / pyo.units.m**3
    params = block.params
    symbols: dict[str, Any] = {cid: block.conc_mass_comp_ref[cid] / mass
                               for cid in SFE_KERNEL.component_ids + IRON_MINERAL_COMPONENTS}
    symbols.update(kernel_parameter_symbols(params))
    symbols.update({name: params.sfe_parameter[name] for name in params.sfe_parameter_names})
    symbols.update({name: params.iron_mineral_parameter[name] for name in params.iron_mineral_parameter_names})
    symbols.update({name: params.iron_reduction_parameter[name] for name in params.iron_reduction_parameter_names})
    symbols["H_ion"] = 10 ** (-block.pH)                       # Davies block: pH = -log10(a_H)
    symbols["NH3_M"] = block.conc_mol_nh3 / molar
    symbols["Na_M"] = block.sodium_mol_l / molar
    symbols["H2S_free_kg_S_m3"] = block.conc_mol_h2s / molar * params.sfe_parameter["MW_S"]
    symbols["competition_floor_kg_m3"] = params.kernel_numerical_floor["competition_floor_kg_m3"]
    symbols["pao_ratio_floor_kg_m3"] = params.kernel_numerical_floor["pao_ratio_floor_kg_m3"]
    symbols["a_Fe2"] = block.activity_fe2
    symbols["a_HS"] = block.activity_hs
    symbols["Ksp_FeS"] = block.Ksp_FeS
    symbols["a_PO4"] = block.activity_po4
    symbols["a_CO3"] = block.activity_co3
    symbols["Ksp_vivianite"] = block.Ksp_vivianite
    symbols["Ksp_siderite"] = block.Ksp_siderite
    symbols["Ksp_mackinawite"] = block.Ksp_mackinawite
    symbols["a_Mg"] = block.activity_mg
    symbols["a_NH4"] = block.activity_nh4
    symbols["Ksp_struvite"] = block.Ksp_struvite
    symbols["mineral_smoothing"] = params.sfe_mineral_smoothing
    return symbols


@declare_process_block_class("SFEKernelADM1ReactionBlock", block_class=_SFEReactionBlock)
class SFEKernelADM1ReactionBlockData(KernelADM1ReactionBlockData):
    def _builds_ion_pairs_here(self) -> bool:
        return False          # ALL the pairs, iron included, are built in _sulfide_and_iron

    def _extra_ligand_conc(self) -> dict:
        return {"HS": self.conc_mol_hs}

    def _rxn_rate(self):
        with _library_reactions_only(self.params):
            super()._rxn_rate()
        self.rates.update({r: SFE_NOMINAL_RATE for r in SFE_REACTIONS})
        self._sulfide_and_iron()
        mass = pyo.units.kg / pyo.units.m**3
        symbols = _sfe_symbols(self)
        for reaction, process in {**SFE_REACTION_PROCESS, **IRON_REDUCTION_REACTION_PROCESS}.items():
            inhibition = (self.I[reaction] == (1 if process.inhibition is None
                                               else compile_pyomo(process.inhibition, symbols)))
            base = compile_pyomo(process.rate_base, symbols)
            rate = self.reaction_rate[reaction] == pyo.units.convert(
                base * self.I[reaction] * mass / pyo.units.day, to_units=mass / pyo.units.s)
            self._write(reaction, inhibition, rate)
        for reaction, process in MINERAL_REACTION_PROCESS.items():
            mineral = compile_pyomo(process.rate, symbols)
            self._write(reaction, self.I[reaction] == 1,
                        self.reaction_rate[reaction] == pyo.units.convert(mineral * mass / pyo.units.day,
                                                                          to_units=mass / pyo.units.s))

    def _write(self, reaction, inhibition, rate) -> None:
        if reaction in self.I_fun:
            self.I_fun[reaction].set_value(inhibition)
            self.rate_expression[reaction].set_value(rate)
            return
        # an added reaction's rate is DEFINED by its rate expression, with no bound of its own: FeS dissolution is a
        # negative precipitation rate, and an S/Fe pathway with no substrate (no HFO, no iron) sits at ~0, where a
        # nonnegativity bound would hold an equality-defined variable against an active barrier (IPOPT stalls there)
        var = self.reaction_rate[reaction]
        var.domain = pyo.Reals
        var.setlb(None)
        var.set_value(SFE_NOMINAL_RATE)
        self.I[reaction].set_value(1.0)
        self.I_fun[reaction] = inhibition
        self.rate_expression[reaction] = rate

    def _sulfide_and_iron(self) -> None:
        molar = pyo.units.kmol / pyo.units.m**3
        molal = pyo.units.mol / pyo.units.kg
        mw = pyo.units.kg / pyo.units.kmol
        params = self.params
        kp, sp = params.kernel_parameter, params.sfe_parameter
        conc = self.conc_mass_comp_ref
        self.conc_mol_so4 = pyo.Expression(expr=conc["S_SO4"] / (sp["MW_S"] * mw))
        self.conc_mol_fe2 = pyo.Expression(expr=conc["S_Fe2"] / (sp["MW_Fe"] * mw))
        self.conc_mol_fe3 = pyo.Expression(expr=conc["S_Fe3"] / (sp["MW_Fe"] * mw))
        # started ON the shared law at the block's temperature as built (a fixed 7.0 start put the base S/Fe liquor
        # of tests/test_adm1_sfe_block.py on a locally infeasible IPOPT path once the law moved to Millero's curve)
        self.pK_a_h2s = pyo.Var(initialize=-pyo.value(_h2s_log10_ka(self.temperature)), units=pyo.units.dimensionless)
        correction = 1 / params.temperature_ref - 1 / self.temperature
        self.H2S_acid_base_equilibrium = pyo.Constraint(expr=(
            self.pK_a_h2s == -_h2s_log10_ka(self.temperature)))
        # HS- / H2S = Ka / (a_H g1). The pair is written as EXPRESSIONS of the sulfide total (the free-H2S share
        # 1 / (1 + Ka / (a_H g1))), not as two bounded Vars tied by a balance and a ratio: on a sulfur-free feed the
        # total sits at the 1e-9 floor and two nonnegative Vars of ~1e-11 kmol/m3 held the interior point against
        # its barrier (the Terminus design digester stalled at its dilute start, lane madm1-sfe-phase3a evidence
        # terminus/). Defined at EXACTLY zero sulfide.
        self.sulfide_hs_ratio = pyo.Expression(expr=10 ** (self.pH - self.pK_a_h2s - self.log_gamma[1]))
        # the ion pairs (iron included) are declared here so the free sulfide can net out what Fe(HS)n hold
        # (HOUSE-MADM1-SPECIATION-ION-PAIRS-FIRST-20261008); their constraints follow once sulfate and iron exist
        self._declare_ion_pairs(ION_PAIRS)
        held_sulfide = sum(n * self.conc_mol_pair[p.name] for p in ION_PAIRS for key, n in p.reactants if key == "HS")
        self.conc_mol_h2s = pyo.Expression(expr=(conc["S_IS"] / (sp["MW_S"] * mw) - held_sulfide)
                                           / (1 + self.sulfide_hs_ratio))
        self.conc_mol_hs = pyo.Expression(expr=self.conc_mol_h2s * self.sulfide_hs_ratio)
        lhs, rhs = self.ionic_strength_balance.expr.args
        self.ionic_strength_balance.set_value(lhs == rhs + 0.5 * self.molar_to_molal * (
            4 * self.conc_mol_so4 + self.conc_mol_hs + 4 * self.conc_mol_fe2 + 9 * self.conc_mol_fe3))
        balance = self.S_H_cons.expr.args[0]
        self.S_H_cons.set_value(balance + 2 * self.conc_mol_fe2 + 3 * self.conc_mol_fe3
                                - 2 * self.conc_mol_so4 - self.conc_mol_hs == 0)
        # the pairs: sulfate total = the carried lump's (zero when S_SO4 is the state) + S_SO4; Fe2 total = S_Fe2
        self._ion_pair_constraints(ION_PAIRS, totals={"Ca": self.calcium_mol_l, "Mg": self.conc_mol_Mg,
                                                      "SO4": self.sulfate_mol_l + self.conc_mol_so4,
                                                      "Fe2": self.conc_mol_fe2})
        # FeS reads the FREE Fe2+ (what Fe(HS)n, FeHPO4, FeHCO3+ ... leave)
        self.activity_fe2 = pyo.Expression(expr=self.gamma[2] * self.conc_mol_free["Fe2"] * self.molar_to_molal / molal)
        self.activity_hs = pyo.Expression(expr=self.gamma[1] * self.conc_mol_hs * self.molar_to_molal / molal)
        self.Ksp_FeS = pyo.Expression(expr=10 ** sp["logK_FeS"] * pyo.exp(
            sp["dH_FeS"] * pyo.units.J / pyo.units.mol / Constants.gas_constant * correction))
        self.saturation_index_FeS = pyo.Expression(expr=self.activity_fe2 * self.activity_hs
                                                   / (10 ** (-self.pH) * self.Ksp_FeS))
        # the iron minerals (module docstring): PO4-3 from the FREE HPO4-2 by the shared third dissociation
        # (a_PO4 = a_HPO4 Ka3 / a_H), CO3-2 the free carbonate; Ksp(T) by van't Hoff on the shared kernel's constants
        mp = params.iron_mineral_parameter
        self.activity_po4 = pyo.Expression(expr=self.gamma[2] * self.conc_mol_hpo4 * self.molar_to_molal / molal
                                           * 10 ** (_log10_ka("phosphoric_3", self.temperature) + self.pH))
        self.activity_co3 = pyo.Expression(expr=self.gamma[2] * self.conc_mol_co3 * self.molar_to_molal / molal)
        # struvite: the FREE Mg+2 (what MgHPO4, MgHCO3+, MgSO4 ... leave) and the free NH4+ (net of NH4SO4-)
        self.activity_mg = pyo.Expression(expr=self.gamma[2] * self.conc_mol_free["Mg"] * self.molar_to_molal / molal)
        self.activity_nh4 = pyo.Expression(expr=self.gamma[1] * self.conc_mol_nh4 * self.molar_to_molal / molal)
        for phase in ("vivianite", "siderite", "mackinawite", "struvite"):
            setattr(self, f"Ksp_{phase}", pyo.Expression(expr=10 ** mp[f"logK_{phase}"] * pyo.exp(
                mp[f"dH_{phase}"] * pyo.units.J / pyo.units.mol / Constants.gas_constant * correction)))
        self.saturation_index_vivianite = pyo.Expression(expr=self.activity_fe2 ** 3 * self.activity_po4 ** 2
                                                         / self.Ksp_vivianite)
        self.saturation_index_siderite = pyo.Expression(expr=self.activity_fe2 * self.activity_co3 / self.Ksp_siderite)
        # mackinawite: FeS's ion product on the shared kernel's mackinawite constant
        self.saturation_index_mackinawite = pyo.Expression(expr=self.activity_fe2 * self.activity_hs
                                                           / (10 ** (-self.pH) * self.Ksp_mackinawite))
        self.saturation_index_struvite = pyo.Expression(expr=self.activity_mg * self.activity_nh4 * self.activity_po4
                                                        / self.Ksp_struvite)

    def calculate_scaling_factors(self):
        super().calculate_scaling_factors()
        for name, factor in SULFIDE_VARIABLE_SCALING.items():
            iscale.set_scaling_factor(getattr(self, name), factor)
        for name, factor in SULFIDE_CONSTRAINT_SCALING.items():
            iscale.constraint_scaling_transform(getattr(self, name), factor, overwrite=True)

    def activity_report(self):
        report = super().activity_report()
        report.update({
            "model": "davies_molal_acid_base_sulfide_iron_v1",
            "explicit_ions": {"SO4-2": float(pyo.value(self.conc_mol_so4)), "HS-": float(pyo.value(self.conc_mol_hs)),
                              "Fe+2": float(pyo.value(self.conc_mol_fe2)), "Fe+3": float(pyo.value(self.conc_mol_fe3))},
            "free_h2s_kg_S_m3": float(pyo.value(self.conc_mol_h2s)) * MW_S,
            "pK_a_h2s": float(pyo.value(self.pK_a_h2s)),
            "saturation_index_FeS": float(pyo.value(self.saturation_index_FeS)),
            "saturation_index_vivianite": float(pyo.value(self.saturation_index_vivianite)),
            "saturation_index_siderite": float(pyo.value(self.saturation_index_siderite)),
            "saturation_index_mackinawite": float(pyo.value(self.saturation_index_mackinawite)),
            "saturation_index_struvite": float(pyo.value(self.saturation_index_struvite)),
            "mineral_equilibria": ("FeS (amorphous), mackinawite, vivianite, siderite and struvite kinetic precipitation; "
                                   "the Ca/K/Al phases and the other Mg phases are twin-only"),
            "trivalent_scope": "Fe+3 explicit (gamma_3)",
        })
        return report


@declare_process_block_class("SFEKernelADM1ReactionParameterBlock")
class SFEKernelADM1ReactionParameterData(KernelADM1ReactionParameterData):
    """The kernel reaction block extended with the S/Fe kernel's processes and FeS (module docstring)."""

    def build(self):
        super().build()
        self._reaction_block_class = SFEKernelADM1ReactionBlock
        components = list(self.config.property_package.component_list)
        missing = [c for c in SFE_BLOCK_COMPONENTS if c not in components]
        if missing:
            raise ConfigurationError(f"the S/Fe reaction block needs the S/Fe property package (missing {missing})")
        values, identity = sfe_design_parameter_values()
        names = tuple(p.name for p in MADM1_SFE_PARAMETERS)
        absent = sorted(set(names) - set(values))
        if absent:
            raise ValueError(f"the digester S/Fe design parameter set lacks {absent}")
        self.sfe_parameter_names = names
        self.sfe_parameter = pyo.Param(names, mutable=True, within=pyo.Reals, units=pyo.units.dimensionless,
                                       initialize={k: values[k] for k in names},
                                       doc="shared mADM1 S/Fe kernel parameters (the kernel's own units)")
        mineral_values = IRON_MINERALS.parameter_values()
        self.iron_mineral_parameter_names = tuple(mineral_values)
        self.iron_mineral_parameter = pyo.Param(self.iron_mineral_parameter_names, mutable=True, within=pyo.Reals,
                                                units=pyo.units.dimensionless, initialize=mineral_values,
                                                doc="the iron-mineral extension's parameters (its own units)")
        validate_iron_reduction_extension(IRON_REDUCTION)    # defined against THIS S/Fe kernel
        reduction_values = IRON_REDUCTION.parameter_values()
        self.iron_reduction_parameter_names = tuple(reduction_values)
        self.iron_reduction_parameter = pyo.Param(self.iron_reduction_parameter_names, mutable=True, within=pyo.Reals,
                                                  units=pyo.units.dimensionless, initialize=reduction_values,
                                                  doc="the iron-reduction extension's parameters (its own units)")
        self.sfe_mineral_smoothing = pyo.Param(initialize=FES_MINERAL_SMOOTHING, mutable=True,
                                               within=pyo.PositiveReals, units=pyo.units.dimensionless,
                                               doc="FeS crystallisation-law smoothing (the kernel's mineral_smoothing)")
        carriers = dict(SFE_KERNEL.conserved_carriers)
        # the S/Fe components' element contents on WaterTAP's own molar basis: the kernel's MASS content divided by the
        # library molar mass, so the carrier closure (x the library molar mass) books exactly the kernel's mass
        self.sfe_content = {}
        for element, carrier in carriers.items():
            group, mw = CARRIER_BASIS[element]
            name = f"sfe_{group}"
            initial = {cid: _block_content(cid).content(element) / mw for cid in SFE_BLOCK_COMPONENTS}
            setattr(self, name, pyo.Param(SFE_BLOCK_COMPONENTS, mutable=True, within=pyo.NonNegativeReals,
                                          initialize=initial, units=pyo.units.kmol / pyo.units.kg,
                                          doc=f"S/Fe component {element} content (kmol per kg of its basis)"))
            self.sfe_content[element] = getattr(self, name)
        self.sfe_reaction_idx = pyo.Set(initialize=SFE_REACTIONS, ordered=True,
                                        doc="the reactions the S/Fe block adds to the library's R1..R25")
        for reaction in SFE_REACTIONS:
            self.rate_reaction_idx.add(reaction)
        keys = [(r, j) for r in SFE_REACTIONS for j in components if j not in carriers.values()]
        self.sfe_stoichiometry = pyo.Param(keys, mutable=True, within=pyo.Reals, units=pyo.units.dimensionless,
                                           initialize=self._sfe_matrix(keys),
                                           doc="shared S/Fe kernel stoichiometry, every component but the carriers")
        for r, j in keys:
            self.rate_reaction_stoichiometry[r, "Liq", j] = self.sfe_stoichiometry[r, j]
        for element, carrier in carriers.items():
            group, mw = CARRIER_BASIS[element]
            core, extra = getattr(self, group), self.sfe_content[element]
            for r in SFE_REACTIONS:
                # in component order, never the content Var's own (hash-ordered) index (see the kernel block)
                self.rate_reaction_stoichiometry[r, "Liq", carrier] = -(
                    sum(self.sfe_stoichiometry[r, j] * core[j] for j in components if j in core)
                    + sum(self.sfe_stoichiometry[r, j] * extra[j] for j in SFE_BLOCK_COMPONENTS)) * mw
        self.kernel_identity = dict(self.kernel_identity, kernel_source=SFE_KERNEL_SOURCE,
                                    kernel_id=SFE_KERNEL.kernel_id, kernel_sha256=SFE_KERNEL.sha256,
                                    sfe_parameter_set=identity, fes_mineral_smoothing=FES_MINERAL_SMOOTHING,
                                    **iron_minerals_identity(), **iron_reduction_identity(),
                                    twin_process_index=dict(TWIN_PROCESS_INDEX))

    def _parameter_values(self) -> dict[str, float]:
        """The block's current core + S/Fe parameter values (the kernel's own names, no derived parameters)."""
        current = {k: pyo.value(self.kernel_parameter[k]) for k in self.kernel_parameter_names}
        current.update({k: pyo.value(self.sfe_parameter[k]) for k in self.sfe_parameter_names})
        return current

    def _mineral_values(self) -> dict[str, float]:
        return {k: pyo.value(self.iron_mineral_parameter[k]) for k in self.iron_mineral_parameter_names}

    def _reduction_values(self) -> dict[str, float]:
        return {k: pyo.value(self.iron_reduction_parameter[k]) for k in self.iron_reduction_parameter_names}

    def _sfe_matrix(self, keys) -> dict:
        current = self._parameter_values()
        matrix = madm1_stoichiometric_matrix(current, kernel=SFE_KERNEL)
        values = {**madm1_parameter_values(current, kernel=SFE_KERNEL), **self._mineral_values(),
                  **self._reduction_values()}
        minerals = {r: {cid: evaluate(coefficient, values) for cid, coefficient in p.stoichiometry.items()}
                    for r, p in {**MINERAL_REACTION_PROCESS, **IRON_REDUCTION_REACTION_PROCESS}.items()}
        rows = {r: minerals[r] if r in minerals else matrix[SFE_REACTION_PROCESS[r].process_id] for r in SFE_REACTIONS}
        return {(r, j): rows[r].get(j, 0.0) for r, j in keys}

    def apply_parameter_values(self, overrides: Mapping[str, float], *, identity: Optional[Mapping[str, Any]] = None):
        """Rebind core and S/Fe parameter values (rates AND stoichiometry) on a built model; unknown names refused."""
        overrides = dict(overrides)
        unknown = sorted(set(overrides) - set(self.kernel_parameter_names) - set(self.sfe_parameter_names)
                         - set(self.iron_mineral_parameter_names) - set(self.iron_reduction_parameter_names))
        if unknown:
            raise KeyError(f"{unknown} are not parameters of {SFE_KERNEL.kernel_id}, {IRON_MINERALS.extension_id} or "
                           f"{IRON_REDUCTION.extension_id}")
        core = {k: v for k, v in overrides.items() if k in self.kernel_parameter_names}
        super().apply_parameter_values(core, identity=None)
        for k, v in overrides.items():
            if k in self.sfe_parameter_names:
                self.sfe_parameter[k].set_value(float(v))
            elif k in self.iron_mineral_parameter_names:
                self.iron_mineral_parameter[k].set_value(float(v))
            elif k in self.iron_reduction_parameter_names:
                self.iron_reduction_parameter[k].set_value(float(v))
        for key, value in self._sfe_matrix(list(self.sfe_stoichiometry)).items():
            self.sfe_stoichiometry[key].set_value(value)
        if identity is not None:
            self.kernel_identity = dict(identity, kernel_source=SFE_KERNEL_SOURCE, kernel_id=SFE_KERNEL.kernel_id,
                                        kernel_sha256=SFE_KERNEL.sha256, **iron_minerals_identity(),
                                        **iron_reduction_identity())


# ------------------------------------------------------------------------------- organic-sulfur variant (opt-in)
@declare_process_block_class("SFEOrganicSKernelADM1ReactionBlock", block_class=_SFEReactionBlock)
class SFEOrganicSKernelADM1ReactionBlockData(SFEKernelADM1ReactionBlockData):
    """The S/Fe reaction block plus the extension's processes (R69 onward), each rate the extension's own law."""

    def _rxn_rate(self):
        super()._rxn_rate()          # R1..R68; R69 onward sit in sfe_reaction_idx, so the library chain never saw them
        self.rates.update({r: SFE_NOMINAL_RATE for r in ORGANIC_S_REACTIONS})
        mass = pyo.units.kg / pyo.units.m**3
        symbols = _sfe_symbols(self)
        symbols.update({cid: self.conc_mass_comp_ref[cid] / mass for cid in ORGANIC_S_COMPONENTS})
        for reaction, process in ORGANIC_S_REACTION_PROCESS.items():
            inhibition = (self.I[reaction] == (1 if process.inhibition is None
                                               else compile_pyomo(process.inhibition, symbols)))
            base = compile_pyomo(process.rate_base, symbols)
            rate = self.reaction_rate[reaction] == pyo.units.convert(
                base * self.I[reaction] * mass / pyo.units.day, to_units=mass / pyo.units.s)
            self._write(reaction, inhibition, rate)


@declare_process_block_class("SFEOrganicSKernelADM1ReactionParameterBlock")
class SFEOrganicSKernelADM1ReactionParameterData(SFEKernelADM1ReactionParameterData):
    """The S/Fe kernel reaction block plus the organic-sulfur extension (module docstring): R69 = the twin's process
    75, its rate and stoichiometry the extension's, and the extension's identity beside the S/Fe kernel's."""

    def build(self):
        super().build()
        self._reaction_block_class = SFEOrganicSKernelADM1ReactionBlock
        validate_organic_sulfur_extension(ORGANIC_S)       # defined against THIS S/Fe kernel, its S row closed
        components = list(self.config.property_package.component_list)
        missing = [c for c in ORGANIC_S_COMPONENTS if c not in components]
        if missing:
            raise ConfigurationError("the organic-sulfur reaction block needs the organic-sulfur property package "
                                     f"(missing {missing})")
        unknown = sorted({j for p in ORGANIC_S.processes for j in p.stoichiometry} - set(components))
        if unknown:
            raise ConfigurationError(f"the organic-sulfur extension names components {unknown} the package lacks")
        carriers = dict(SFE_KERNEL.conserved_carriers)
        # the extension's element contents on WaterTAP's molar basis, as the S/Fe block states its own (sfe_content)
        self.organic_s_content = {}
        for element in carriers:
            group, mw = CARRIER_BASIS[element]
            name = f"organic_s_{group}"
            initial = {c.component_id: c.content(element) / mw for c in ORGANIC_S.contents}
            setattr(self, name, pyo.Param(ORGANIC_S_COMPONENTS, mutable=True, within=pyo.NonNegativeReals,
                                          initialize=initial, units=pyo.units.kmol / pyo.units.kg,
                                          doc=f"organic-sulfur component {element} content (kmol per kg of its basis)"))
            self.organic_s_content[element] = getattr(self, name)
        for reaction in ORGANIC_S_REACTIONS:
            # held out of the library chain with the S/Fe reactions (_library_reactions_only), and carried after them
            self.sfe_reaction_idx.add(reaction)
            self.rate_reaction_idx.add(reaction)
        keys = [(r, j) for r in ORGANIC_S_REACTIONS for j in components if j not in carriers.values()]
        self.organic_s_stoichiometry = pyo.Param(keys, mutable=True, within=pyo.Reals, units=pyo.units.dimensionless,
                                                 initialize=self._organic_s_matrix(keys),
                                                 doc="organic-sulfur extension stoichiometry, every component but "
                                                     "the carriers")
        for r, j in keys:
            self.rate_reaction_stoichiometry[r, "Liq", j] = self.organic_s_stoichiometry[r, j]
        for element, carrier in carriers.items():
            group, mw = CARRIER_BASIS[element]
            core, sfe_extra, own = getattr(self, group), self.sfe_content[element], self.organic_s_content[element]
            for r in ORGANIC_S_REACTIONS:
                self.rate_reaction_stoichiometry[r, "Liq", carrier] = -(
                    sum(self.organic_s_stoichiometry[r, j] * core[j] for j in components if j in core)
                    + sum(self.organic_s_stoichiometry[r, j] * sfe_extra[j] for j in MADM1_SFE_COMPONENTS)
                    + sum(self.organic_s_stoichiometry[r, j] * own[j] for j in ORGANIC_S_COMPONENTS)) * mw
        self.kernel_identity = dict(self.kernel_identity, **organic_sulfur_identity(),
                                    twin_process_index=dict(ORGANIC_S_TWIN_PROCESS_INDEX))

    def _organic_s_matrix(self, keys) -> dict:
        values = madm1_parameter_values(self._parameter_values(), kernel=SFE_KERNEL)
        rows = {r: {cid: evaluate(coefficient, values) for cid, coefficient in process.stoichiometry.items()}
                for r, process in ORGANIC_S_REACTION_PROCESS.items()}
        return {(r, j): rows[r].get(j, 0.0) for r, j in keys}

    def apply_parameter_values(self, overrides: Mapping[str, float], *, identity: Optional[Mapping[str, Any]] = None):
        """The S/Fe rebind, then the extension's stoichiometry; a new identity keeps the extension's stamp."""
        super().apply_parameter_values(overrides, identity=identity)
        for key, value in self._organic_s_matrix(list(self.organic_s_stoichiometry)).items():
            self.organic_s_stoichiometry[key].set_value(value)
        if identity is not None:
            self.kernel_identity = dict(self.kernel_identity, **organic_sulfur_identity())


# ------------------------------------------------------------------------------------------------------- unit
class SFEADScaler(ADScaler):
    def variable_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().variable_scaling_routine(model, overwrite, submodel_scalers)
        for t in model.flowsheet().time:
            self.set_variable_scaling_factor(model.KH_h2s[t], 1e1, overwrite=overwrite)

    def constraint_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().constraint_scaling_routine(model, overwrite, submodel_scalers)
        for t in model.flowsheet().time:
            self.set_constraint_scaling_factor(model.H2S_Henrys_law[t], 1.0, overwrite=overwrite)
            self.set_constraint_scaling_factor(model.Sh2s_conc[t], 1e2, overwrite=overwrite)
            if model.config.oxidant_inlet:          # the micro-aeration variant's unit constraints (its own factors)
                self.set_constraint_scaling_factor(model.O2_Henrys_law[t], 1.0, overwrite=overwrite)
                self.set_constraint_scaling_factor(model.O2_transfer[t], 1e2, overwrite=overwrite)
                self.set_constraint_scaling_factor(model.N2_balance[t], 1e2, overwrite=overwrite)


@declare_process_block_class("SFEAD")
class SFEADData(ADData):
    """WaterTAP's AD with the headspace H2S of the S/Fe kernel (module docstring). Requires the S/Fe liquid and vapor
    packages; any other pairing is refused at build."""

    default_scaler = SFEADScaler
    CONFIG = ADData.CONFIG()
    CONFIG.declare("oxidant_inlet", ConfigValue(
        default=False, domain=Bool,
        description="micro-aeration: a vapor_inlet port for a dosed oxidant and the headspace oxidation "
                    "(chemistry.modified_adm1_microaeration; PUBLIC-RULE-6dce80fc160d). False "
                    "builds exactly the S/Fe digester"))

    def build(self):
        super().build()
        liquid = self.config.liquid_property_package
        vapor = self.config.vapor_property_package
        if "S_IS" not in liquid.component_list or "S_h2s" not in vapor.component_list:
            raise ConfigurationError(f"{self.name}: the S/Fe digester needs S_IS in the liquid package and S_h2s in "
                                     "the vapor package")
        rp = self.config.reaction_package
        if not hasattr(rp, "sfe_parameter"):
            raise ConfigurationError(f"{self.name}: the S/Fe digester needs the S/Fe kernel reaction package")
        time = self.flowsheet().time
        self.KH_h2s = pyo.Var(time, initialize=0.08, domain=pyo.NonNegativeReals,
                              units=pyo.units.kmol / pyo.units.m**3 * pyo.units.bar**-1,
                              doc="H2S Henry's law coefficient (the kernel's KH_h2s at T)")
        kp = rp.kernel_parameter

        def henry(b, t):
            return pyo.log(b.KH_h2s[t] / (pyo.units.kmol / pyo.units.m**3 * pyo.units.bar**-1)) == (
                pyo.log(kp["KH_h2s"]) + kp["dH_KH_h2s"] * pyo.units.J / pyo.units.mol / Constants.gas_constant
                * ((1 / b.config.vapor_property_package.temperature_ref) - (1 / b.vapor_phase[t].temperature)))

        self.H2S_Henrys_law = pyo.Constraint(time, rule=henry, doc="H2S Henry's law coefficient constraint")
        for t in time:
            self.unit_material_balance[t, "S_IS"].set_value(
                self.liquid_phase.mass_transfer_term[t, "Liq", "S_IS"]
                == -self.vapor_phase[t].get_material_flow_terms("Vap", "S_h2s"))

        def transfer(b, t):
            rx = b.liquid_phase.reactions[t]
            symbols = {
                "kLa": pyo.units.convert(b.K_La, to_units=1 / pyo.units.day) * pyo.units.day,
                "H2S_free_kg_S_m3": rx.conc_mol_h2s / (pyo.units.kmol / pyo.units.m**3) * rp.sfe_parameter["MW_S"],
                "KH_h2s_T": b.KH_h2s[t] / (pyo.units.kmol / pyo.units.m**3 * pyo.units.bar**-1)
                * rp.sfe_parameter["MW_S"],
                "p_h2s_bar": pyo.units.convert(b.vapor_phase[t].pressure_sat["S_h2s"], to_units=pyo.units.bar)
                / pyo.units.bar,
            }
            rate = compile_pyomo(MADM1_H2S_TRANSFER.rate, symbols)          # kg S / m3 / d
            return b.liquid_phase.mass_transfer_term[t, "Liq", "S_IS"] == -pyo.units.convert(
                rate * pyo.units.kg / pyo.units.m**3 / pyo.units.day, to_units=pyo.units.kg / pyo.units.m**3
                / pyo.units.s) * b.volume_liquid[t]

        self.Sh2s_conc = pyo.Constraint(time, rule=transfer, doc="Mass transfer rate of H2S to the headspace")
        if self.config.oxidant_inlet:
            from watertap_engine_mcp.chemistry.modified_adm1_microaeration import build_oxidant_inlet

            build_oxidant_inlet(self)

    def calculate_scaling_factors(self):
        super().calculate_scaling_factors()
        iscale.set_scaling_factor(self.KH_h2s, 1e1)
        for t, v in self.H2S_Henrys_law.items():
            iscale.constraint_scaling_transform(v, 1.0)
        for t, v in self.Sh2s_conc.items():
            iscale.constraint_scaling_transform(v, 1e2)
        for t in self.flowsheet().time:
            iscale.constraint_scaling_transform(self.unit_material_balance[t, "S_IS"], 1e2, overwrite=True)
        if self.config.oxidant_inlet:
            from watertap_engine_mcp.chemistry.modified_adm1_microaeration import oxidant_scaling

            oxidant_scaling(self)

    def initialize_build(self, liquid_state_args=None, vapor_state_args=None, outlvl=idaeslog.NOTSET, solver=None,
                         optarg=None):
        """Upstream's AD initialisation; with an oxidant inlet, the oxidant-off solution first and then the stated
        dose stepped in (``modified_adm1_microaeration.initialize_with_dose_steps``), the unit's own path."""
        if not self.config.oxidant_inlet:
            return super().initialize_build(liquid_state_args=liquid_state_args, vapor_state_args=vapor_state_args,
                                            outlvl=outlvl, solver=solver, optarg=optarg)
        from watertap_engine_mcp.chemistry.modified_adm1_microaeration import initialize_with_dose_steps

        def base(**kw):
            return super(SFEADData, self).initialize_build(liquid_state_args=liquid_state_args,
                                                           vapor_state_args=vapor_state_args, **kw)

        self.oxidant_dose_path = initialize_with_dose_steps(self, base, outlvl=outlvl, solver=solver, optarg=optarg)


def ad_unit_class(config: Mapping[str, Any], default: Any) -> Any:
    """The AD class for a built unit config: :class:`SFEAD` when its liquid package carries the S/Fe state (S_IS),
    upstream's AD otherwise (every non-S/Fe digester is untouched)."""
    liquid = config.get("liquid_property_package")
    components = getattr(liquid, "component_list", None)
    return SFEAD if components is not None and "S_IS" in components else default


def sulfur_iron_closure(digester) -> dict[str, Any]:
    """S, Fe and Mg mass closure across a solved S/Fe digester (kg/s in, kg/s out, relative residual), and the liquor's
    electroneutrality residual. Every S and Fe pool is counted on its own basis (FeS and mackinawite split by molar
    mass); Mg as the dissolved ion, the polyphosphate's and struvite's (the kernel moves Mg through nothing else)."""
    t = digester.flowsheet().time.first()
    pin = digester.liquid_phase.properties_in[t]
    pout = digester.liquid_phase.properties_out[t]
    vap = digester.vapor_phase[t]
    fes_s = MW_S / (MW_S + MW_FE)
    # the organic-sulfur variant's X_Sorg is kg S, a sulfur pool beside the rest (absent from every other S/Fe build)
    organic = [j for j in ORGANIC_S_COMPONENTS if j in digester.config.liquid_property_package.component_list]

    def mineral_s(c):
        return sum(IRON_MINERAL_S_SHARE[j] * c[j] for j in IRON_MINERAL_S_SHARE)

    def liquid_s(block):
        c = block.conc_mass_comp
        if organic:
            return pyo.value(block.flow_vol * (c["S_SO4"] + c["S_IS"] + c["S_S0"] + fes_s * c["X_FeS"] + mineral_s(c)
                                               + sum(c[j] for j in organic)))
        return pyo.value(block.flow_vol * (c["S_SO4"] + c["S_IS"] + c["S_S0"] + fes_s * c["X_FeS"] + mineral_s(c)))

    def liquid_fe(block):
        c = block.conc_mass_comp
        iron = ("S_Fe2", "S_Fe3", "X_HFO_H", "X_HFO_L", "X_HFO_old", "X_HFO_HP", "X_HFO_LP", "X_HFO_HP_old",
                "X_HFO_LP_old")
        minerals = sum(IRON_MINERAL_FE_SHARE[j] * c[j] for j in IRON_MINERAL_COMPONENTS)
        return pyo.value(block.flow_vol * (sum(c[j] for j in iron) + (1 - fes_s) * c["X_FeS"] + minerals))

    def liquid_mg(block):
        c = block.conc_mass_comp
        return pyo.value(block.flow_vol * (c["S_Mg"] + SFE_KERNEL.content("X_PP").content("Mg") * c["X_PP"]
                                           + sum(MINERAL_MG_SHARE[j] * c[j] for j in MINERAL_MG_SHARE)))

    s_in, fe_in = liquid_s(pin), liquid_fe(pin)
    s_gas = pyo.value(vap.flow_vol * vap.conc_mass_comp["S_h2s"])
    s_out, fe_out = liquid_s(pout) + s_gas, liquid_fe(pout)
    # the micro-aeration variant: H2S the dosed gas brings in, and the S0 left on the headspace surfaces (a sink)
    oxidant = bool(getattr(digester.config, "oxidant_inlet", False))
    s_gas_in = pyo.value(digester.vapor_inlet_state[t].flow_vol * digester.vapor_inlet_state[t].conc_mass_comp["S_h2s"]) \
        if oxidant else 0.0
    deposit = pyo.value(digester.headspace_s0_deposit[t]) if oxidant else 0.0
    s_in, s_out = s_in + s_gas_in, s_out + deposit
    rx = digester.liquid_phase.reactions[t]
    charge = float(pyo.value(rx.S_H_cons.body))

    def rel(a, b):
        return abs(a - b) / max(abs(a), abs(b), 1e-30)

    closure = {"sulfur_kg_s": {"in": s_in, "out_liquid": s_out - s_gas - deposit, "out_gas_h2s": s_gas, "out": s_out,
                               "relative_residual": rel(s_in, s_out),
                               **({"in_gas_h2s": s_gas_in, "out_headspace_s0_deposit": deposit} if oxidant else {})},
               "iron_kg_s": {"in": fe_in, "out": fe_out, "relative_residual": rel(fe_in, fe_out)},
               "magnesium_kg_s": {"in": liquid_mg(pin), "out": liquid_mg(pout),
                                  "relative_residual": rel(liquid_mg(pin), liquid_mg(pout))},
               "electroneutrality_residual_kmol_m3": charge}
    if organic:
        # the organic pool on its own: fed, left in the digestate, and released to sulfide by the extension's
        # processes (their extents, kg S/s: each moves X_Sorg to S_IS one for one)
        org_in = pyo.value(pin.flow_vol * sum(pin.conc_mass_comp[j] for j in organic))
        org_out = pyo.value(pout.flow_vol * sum(pout.conc_mass_comp[j] for j in organic))
        released = sum(float(pyo.value(digester.liquid_phase.rate_reaction_extent[t, r])) for r in ORGANIC_S_REACTIONS)
        closure["organic_sulfur_kg_s"] = {"in": org_in, "out": org_out, "released_to_sulfide": released,
                                          "relative_residual": rel(org_in - org_out, released)}
    return closure


#: The kernel's HFO reductions by donor (the H2 and sulfide routes, processes 34-37 and 46-55) and the extension's by
#: acetate (R62-R68): every route that turns an HFO host's Fe(III) into Fe2+.
HFO_REDUCTION_ROUTES = {
    "h2": tuple(r for r, p in SFE_REACTION_PROCESS.items()
                if p.process_id.startswith("reduction_") and p.process_id.endswith("_h2")),
    "sulfide": tuple(r for r, p in SFE_REACTION_PROCESS.items()
                     if p.process_id.startswith("reduction_") and p.process_id.endswith("_IS")),
    "acetate": tuple(IRON_REDUCTION_REACTION_PROCESS),
}


def hfo_reduction_kg_fe_s(digester) -> dict[str, float]:
    """kg Fe/s of HFO reduced to Fe2+ in a solved S/Fe digester, by donor: each route's extents (each moves its host's
    Fe to S_Fe2 one for one, so an extent is kg Fe/s)."""
    t = digester.flowsheet().time.first()
    extent = digester.liquid_phase.rate_reaction_extent
    return {route: math.fsum(float(pyo.value(extent[t, r])) for r in reactions)
            for route, reactions in HFO_REDUCTION_ROUTES.items()}


__all__ = [
    "FES_MINERAL_SMOOTHING",
    "FES_REACTION",
    "HFO_REDUCTION_ROUTES",
    "IRON_MINERALS",
    "IRON_MINERAL_COMPONENTS",
    "IRON_MINERAL_FE_SHARE",
    "IRON_MINERAL_S_SHARE",
    "IRON_REDUCTION",
    "IRON_REDUCTION_REACTION_PROCESS",
    "MINERAL_MG_SHARE",
    "MINERAL_REACTION_PROCESS",
    "SFE_BLOCK_COMPONENTS",
    "ORGANIC_S_COMPONENTS",
    "ORGANIC_S_REACTIONS",
    "ORGANIC_S_REACTION_PROCESS",
    "ORGANIC_S_TWIN_PROCESS_INDEX",
    "SFEAD",
    "SFEADM1VaporParameterBlock",
    "SFEKernelADM1ReactionParameterBlock",
    "SFEModifiedADM1ParameterBlock",
    "SFEOrganicSKernelADM1ReactionParameterBlock",
    "SFEOrganicSModifiedADM1ParameterBlock",
    "SFE_REACTIONS",
    "SFE_REACTION_PROCESS",
    "SRB_BIOMASS",
    "TWIN_PROCESS_INDEX",
    "ad_unit_class",
    "iron_minerals_identity",
    "hfo_reduction_kg_fe_s",
    "iron_reduction_identity",
    "organic_sulfur_identity",
    "sfe_design_parameter_values",
    "sulfur_iron_closure",
]

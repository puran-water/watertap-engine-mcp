"House mADM1 S/Fe digester with MICRO-AERATION: the opt-in extension compiled into WaterTAP (plan step 2).\n\nThe kinetics are ``process_model_schema.madm1_microaeration`` (twin processes 76-85, the O2-inhibition factor), read\nhere and never restated. The variant stacks on the organic-sulfur variant (an engine that compiles micro-aeration\ncompiles organic sulfur too; a feed without X_Sorg leaves R69 at zero):\n\n* :class:`MicroaerationModifiedADM1ParameterBlock` -- the organic-S liquid state plus S_O2 (kg O2/m3, dissolved; no\n  C/N/P, not in VSS/TSS);\n* :class:`MicroaerationADM1VaporParameterBlock` -- the S/Fe headspace plus S_o2 (kg O2/m3) and S_n2 (kg N2/m3; ARGON\n  LUMPED WITH N2 BY MOLES at N2's molar mass, stated; dissolved N2 omitted, N2 inert: out = in);\n* :class:`MicroaerationKernelADM1ReactionParameterBlock` -- the organic-S reaction block plus R70-R77 (the twin's\n  liquid processes 78-85: Botheju's aerobic uptakes and the S0 re-reduction bracket, k_S0_red = 0 in the base), and the\n  O2 inhibition (L2) multiplied into the I of the anaerobic uptakes (R4-R11) and the SRB uptakes (R26, R28, R30, R32,\n  R33). The extension's parameters sit in mutable Params (``microaeration_parameter``); a bracket rebinds them with\n  ``apply_parameter_values``;\n* :func:`build_oxidant_inlet` -- what ``SFEAD(oxidant_inlet=True)`` builds on the unit: a ``vapor_inlet`` port with its\n  own defined state block, O2's Henry constant, the headspace-surface oxidation G1 (twin 76) as a unit-level rate on\n  the wetted area ``headspace_wetted_area`` (a geometry input the caller fixes; never defaulted), the O2 gas-liquid\n  transfer G4 (twin 77: SURFACE transfer kL_O2 x that same wetted area / the liquid volume, with the H2S transfer's\n  sign and Henry form but never the digester's K_La), the vapour balances rewritten as out = in + transfer + G1\n  generation for every gas (the S_IS take-off pattern SFEAD already uses), N2 out = in, and G1's S0 fall-out and sulfate\n  entering the liquid as ``mass_transfer_term``. The headspace S0 deposit and the G1 reaction heat are reported\n  (``headspace_s0_deposit``, ``headspace_oxidation_heat``): WaterTAP's AD energy balance carries a vestigial vapour\n  enthalpy (dens_mass 0.01 kg/m3), so the heat is reported, not written into it.\n\nInitialisation is the unit's own (``SFEAD.initialize_build``): the oxidant-off solution first, then the dose stepped\nin (:data:`OXIDANT_DOSE_STEPS`, halved on a failed step), never an agent loop."

from __future__ import annotations

from typing import Any, Mapping, Optional

import idaes.core.util.scaling as iscale
import pyomo.environ as pyo
from idaes.core import Solute, declare_process_block_class
from idaes.core.util.constants import Constants
from idaes.core.util.exceptions import ConfigurationError
from watertap.property_models.unit_specific.anaerobic_digestion.adm1_properties_vapor import _ADM1_vaporStateBlock
from watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_properties import (
    _ModifiedADM1StateBlock,
)

from watertap_engine_mcp.chemistry.modified_adm1_kernel import CARRIER_BASIS
from watertap_engine_mcp.chemistry.modified_adm1_sfe import MADM1_SFE_COMPONENTS, ORGANIC_S_COMPONENTS, ORGANIC_S_REACTIONS, ORGANIC_S_TWIN_PROCESS_INDEX, SFE_KERNEL, SFE_NOMINAL_RATE, SFE_REACTION_PROCESS, SFEADM1VaporParameterData, SFEADM1VaporStateBlockData, SFEOrganicSKernelADM1ReactionBlockData, SFEOrganicSKernelADM1ReactionParameterData, SFEOrganicSModifiedADM1ParameterData, SFEOrganicSModifiedADM1StateBlockData, _SFEReactionBlock, _sfe_symbols
from watertap_engine_mcp.chemistry.pyomo_compiler import compile_pyomo
from puran_mcp_support.chemistry.evaluator import evaluate
from puran_mcp_support.schema.madm1_microaeration import DEPOSIT_COMPONENT, G1_REACTION_ENTHALPY_J_MOL, MADM1_MICROAERATION_EXTENSION, S_O2, microaeration_parameter_values, validate_microaeration_extension

EXT = MADM1_MICROAERATION_EXTENSION
EXT_SOURCE = 'puran_mcp_support.schema.madm1_microaeration.MADM1_MICROAERATION_EXTENSION'
MICROAERATION_COMPONENTS = EXT.component_ids                          # ("S_O2",)
MICROAERATION_GAS_COMPONENTS = EXT.gas_component_ids                  # ("S_o2", "S_n2")
_GAS = {g.component_id: g for g in EXT.gas_components}
G1 = next(p for p in EXT.processes if p.phase == "gas_surface")
G4 = next(p for p in EXT.processes if p.phase == "gas_liquid_transfer")
#: WaterTAP reaction id -> the extension's LIQUID processes (twin 78-85), after organic sulfur's (R69).
MICROAERATION_REACTION_PROCESS = {f"R{int(ORGANIC_S_REACTIONS[-1][1:]) + 1 + i}": p
                                  for i, p in enumerate(q for q in EXT.processes if q.phase == "liquid")}
MICROAERATION_REACTIONS = tuple(MICROAERATION_REACTION_PROCESS)
#: Every reaction's twin index, and the two unit-level processes (written on the AD, not the reaction block).
MICROAERATION_TWIN_PROCESS_INDEX = {**ORGANIC_S_TWIN_PROCESS_INDEX,
                                    **{r: p.index for r, p in MICROAERATION_REACTION_PROCESS.items()}}
UNIT_LEVEL_PROCESSES = {G1.index: G1.process_id, G4.index: G4.process_id}
#: The base reactions the O2 inhibition multiplies (WaterTAP R = twin index + 1).
O2_INHIBITED_REACTIONS = tuple(f"R{i + 1}" for i in EXT.inhibited_process_indices)
#: The dose steps the unit's own initialisation walks from the oxidant-off solution (share of the stated inlet flow).
OXIDANT_DOSE_STEPS = (0.05, 0.15, 0.35, 0.65, 1.0)
OXIDANT_DOSE_MAX_HALVINGS = 6
#: Smoothing (bar) of the partial pressures G1 reads, as WaterTAP binds the kernel's ``gas_smoothing``: at 1e-10 bar
#: (0.0001 ppmv) the H2S power law keeps a finite derivative where p -> 0 and moves no rate by more than 1e-7 relative
#: above 1 ppmv.
OXIDANT_GAS_SMOOTHING = 1e-10
MW_O2 = float(EXT.parameter("MW_O2").value)
MW_N2 = float(EXT.parameter("MW_N2").value)
MICROAERATION_COMPONENT_DOCS = {"S_O2": "Dissolved oxygen (kg O2; COD content -1, no C/N/P; not a solid)"}


#: The species a dosed oxidant gas may carry (kmol/d each): oxygen, nitrogen, ARGON (lumped into S_n2 by moles at N2's
#: molar mass, stated: inert in the gas, so only the partial-pressure sum moves) and carbon dioxide (air's). The gas is
#: DRY, and a fuel or H2S in it is refused by name: an oxidant inlet is not a second biogas inlet.
OXIDANT_SPECIES = ("O2", "N2", "Ar", "CO2")
OXIDANT_REFUSED_SPECIES = ("H2O", "CH4", "H2", "H2S")
OXIDANT_KEYS = frozenset({"kmol_d", "temperature_K", "pressure_Pa", "source"})
#: The vapour state block's own bounds (``SFEADM1VaporStateBlockData.build``): a stated oxidant outside them could not be
#: fixed on the port.
OXIDANT_TEMPERATURE_BOUNDS_K = (273.15, 323.15)
OXIDANT_PRESSURE_BOUNDS_PA = (1.0e3, 1.0e6)
R_GAS_J_MOL_K = 8.314462618


class OxidantInletError(ValueError):
    """A dosed oxidant gas that cannot be put on the digester's vapour inlet as stated."""


def oxidant_inlet_state(oxidant: Mapping[str, Any]) -> dict[str, Any]:
    """The ``vapor_inlet`` port state of a stated oxidant gas, on the micro-aeration vapour package's own bases.

    ``oxidant`` = {"kmol_d": {"O2", "N2", "Ar", "CO2"} (molar flows, a DRY gas), "temperature_K", "pressure_Pa",
    "source"}. Molar flows are the interface because the package measures each gas on its own basis (kg O2, kg N2,
    kg C): a mass or a mole fraction would need a second convention. Returns the ideal-gas volumetric flow at the stated
    T and P (m3/s) and the concentrations (kg/m3 of that gas): S_o2 = kmol O2 x MW_O2, S_n2 = (kmol N2 + kmol Ar) x
    MW_N2 -- ARGON LUMPED WITH NITROGEN BY MOLES -- and S_co2 = kmol CO2 x 12 kg C; S_h2, S_ch4 and S_h2s are zero. The
    argon's share of the lump is returned so a caller can split it back out of the biogas (N2 and Ar are both inert:
    out = in, so the outlet lump holds the inlet's own Ar:N2 ratio exactly)."""
    import math

    if not isinstance(oxidant, Mapping):
        raise OxidantInletError(f"an oxidant inlet is a mapping of {sorted(OXIDANT_KEYS)}, not {type(oxidant).__name__}")
    unknown, absent = sorted(set(oxidant) - OXIDANT_KEYS), sorted(OXIDANT_KEYS - set(oxidant))
    if unknown or absent:
        raise OxidantInletError(
            f"an oxidant inlet states exactly {sorted(OXIDANT_KEYS)}; unknown {unknown}, absent {absent} (the headspace "
            "wetted area is a stage-(b) geometry with its own binding, never a field of the gas)")
    if not str(oxidant["source"] or "").strip():
        raise OxidantInletError("an oxidant inlet names its source (the stream or the sizing that states the dose)")
    flows = oxidant["kmol_d"]
    if not isinstance(flows, Mapping) or not flows:
        raise OxidantInletError("oxidant kmol_d is a mapping of species to molar flow (kmol/d)")
    refused = sorted(set(flows) & set(OXIDANT_REFUSED_SPECIES))
    if refused:
        raise OxidantInletError(f"an oxidant inlet is a dry O2/N2/Ar(/CO2) gas; it carries {refused}, refused by name")
    stray = sorted(set(flows) - set(OXIDANT_SPECIES))
    if stray:
        raise OxidantInletError(f"oxidant species {stray} are not {list(OXIDANT_SPECIES)}")
    kmol = {k: float(flows.get(k, 0.0) or 0.0) for k in OXIDANT_SPECIES}
    if any(not math.isfinite(v) or v < 0 for v in kmol.values()):
        raise OxidantInletError(f"oxidant molar flows are finite and nonnegative, not {kmol}")
    if not kmol["O2"] > 0:
        raise OxidantInletError("an oxidant inlet carries oxygen: kmol_d['O2'] must be positive")
    temperature, pressure = float(oxidant["temperature_K"]), float(oxidant["pressure_Pa"])
    for name, value, (low, high) in (("temperature_K", temperature, OXIDANT_TEMPERATURE_BOUNDS_K),
                                     ("pressure_Pa", pressure, OXIDANT_PRESSURE_BOUNDS_PA)):
        if not math.isfinite(value) or not low <= value <= high:
            raise OxidantInletError(f"oxidant {name} = {value!r} is outside the vapour state's bounds {low}-{high}")
    total = math.fsum(kmol.values())
    flow_m3_d = total * 1000.0 * R_GAS_J_MOL_K * temperature / pressure       # kmol/d -> mol/d x RT/P
    molar_mass = MicroaerationADM1VaporStateBlockData._MOLAR_MASS
    inert = kmol["N2"] + kmol["Ar"]
    conc = {"S_o2": kmol["O2"] * molar_mass["S_o2"] / flow_m3_d,
            "S_n2": inert * molar_mass["S_n2"] / flow_m3_d,
            "S_co2": kmol["CO2"] * molar_mass["S_co2"] / flow_m3_d,
            "S_h2": 0.0, "S_ch4": 0.0, "S_h2s": 0.0}
    return {"flow_vol_m3_s": flow_m3_d / 86400.0, "flow_vol_m3_d": flow_m3_d, "temperature_K": temperature,
            "pressure_Pa": pressure, "conc_kg_m3": conc, "kmol_d": kmol,
            "argon_mole_share_of_inert_lump": kmol["Ar"] / inert if inert > 0 else 0.0,
            "argon_lumped_with_n2": "by moles, at N2's molar mass (inert in the gas: out = in)",
            "source": str(oxidant["source"])}


def microaeration_identity() -> dict[str, str]:
    """The extension's identity, stamped beside the S/Fe kernel's and the organic-S extension's."""
    return {"microaeration_extension_source": EXT_SOURCE, "microaeration_extension_id": EXT.extension_id,
            "microaeration_extension_sha256": EXT.sha256}


# --------------------------------------------------------------------------------------------- liquid property
@declare_process_block_class("MicroaerationModifiedADM1StateBlock", block_class=_ModifiedADM1StateBlock)
class MicroaerationModifiedADM1StateBlockData(SFEOrganicSModifiedADM1StateBlockData):
    """The organic-S state with dissolved O2 (a solute: counted in no solids pool)."""


@declare_process_block_class("MicroaerationModifiedADM1ParameterBlock")
class MicroaerationModifiedADM1ParameterData(SFEOrganicSModifiedADM1ParameterData):
    """The organic-S S/Fe property package plus the micro-aeration extension's liquid component (S_O2, kg O2)."""

    def build(self):
        super().build()
        self._state_block_class = MicroaerationModifiedADM1StateBlock
        for cid in MICROAERATION_COMPONENTS:
            setattr(self, cid, Solute(doc=MICROAERATION_COMPONENT_DOCS[cid]))


# ---------------------------------------------------------------------------------------------- vapor property
@declare_process_block_class("MicroaerationADM1VaporStateBlock", block_class=_ADM1_vaporStateBlock)
class MicroaerationADM1VaporStateBlockData(SFEADM1VaporStateBlockData):
    """The S/Fe headspace with O2 (kg O2/m3) and N2 (kg N2/m3, Ar lumped by moles); both sit at exactly zero without
    an oxidant and are equality-defined, so they are unbounded as S_h2s is."""

    _INITIAL_CONC = {**SFEADM1VaporStateBlockData._INITIAL_CONC, "S_o2": 1e-9, "S_n2": 1e-9}
    _INITIAL_PRESSURE = {**SFEADM1VaporStateBlockData._INITIAL_PRESSURE, "S_o2": 1e-4, "S_n2": 1e-4}
    _MOLAR_MASS = {**SFEADM1VaporStateBlockData._MOLAR_MASS, "S_o2": MW_O2, "S_n2": MW_N2}
    _UNBOUNDED = ("S_h2s", "S_o2", "S_n2")

    def calculate_scaling_factors(self):
        super().calculate_scaling_factors()
        for j in MICROAERATION_GAS_COMPONENTS:
            iscale.set_scaling_factor(self.conc_mass_comp[j], 1e2)
            iscale.set_scaling_factor(self.pressure_sat[j], 1e-3)
            iscale.set_scaling_factor(self.material_flow_expression[j],
                                      iscale.get_scaling_factor(self.flow_vol, default=1e2) * 1e2)
            iscale.set_scaling_factor(self.material_density_expression[j], 1e2)


@declare_process_block_class("MicroaerationADM1VaporParameterBlock")
class MicroaerationADM1VaporParameterData(SFEADM1VaporParameterData):
    """The S/Fe headspace package plus oxygen and nitrogen (Ar lumped)."""

    def build(self):
        super().build()
        self._state_block_class = MicroaerationADM1VaporStateBlock
        for cid in MICROAERATION_GAS_COMPONENTS:
            setattr(self, cid, Solute(doc=_GAS[cid].doc))


# ------------------------------------------------------------------------------------------------- reactions
def _microaeration_symbols(block) -> dict[str, Any]:
    mass = pyo.units.kg / pyo.units.m**3
    params = block.params
    symbols = _sfe_symbols(block)
    symbols.update({cid: block.conc_mass_comp_ref[cid] / mass
                    for cid in ORGANIC_S_COMPONENTS + MICROAERATION_COMPONENTS})
    symbols.update({name: params.microaeration_parameter[name] for name in params.microaeration_parameter_names})
    return symbols


@declare_process_block_class("MicroaerationKernelADM1ReactionBlock", block_class=_SFEReactionBlock)
class MicroaerationKernelADM1ReactionBlockData(SFEOrganicSKernelADM1ReactionBlockData):
    """The organic-S reaction block plus R70-R77 and the O2 inhibition of the anaerobic and SRB uptakes."""

    def _rxn_rate(self):
        super()._rxn_rate()                         # R1..R69; R70 onward sit in sfe_reaction_idx (held out of the library)
        self.rates.update({r: SFE_NOMINAL_RATE for r in MICROAERATION_REACTIONS})
        mass = pyo.units.kg / pyo.units.m**3
        symbols = _microaeration_symbols(self)
        i_o2 = compile_pyomo(EXT.inhibition, symbols)
        for reaction in O2_INHIBITED_REACTIONS:     # L2: the kernel's own inhibition x KI_O2 / (KI_O2 + S_O2)
            process = SFE_REACTION_PROCESS[reaction]
            self.I_fun[reaction].set_value(self.I[reaction] == compile_pyomo(process.inhibition, symbols) * i_o2)
        for reaction, process in MICROAERATION_REACTION_PROCESS.items():
            inhibition = (self.I[reaction] == (1 if process.inhibition is None
                                               else compile_pyomo(process.inhibition, symbols)))
            base = compile_pyomo(process.rate_base, symbols)
            rate = self.reaction_rate[reaction] == pyo.units.convert(
                base * self.I[reaction] * mass / pyo.units.day, to_units=mass / pyo.units.s)
            self._write(reaction, inhibition, rate)


@declare_process_block_class("MicroaerationKernelADM1ReactionParameterBlock")
class MicroaerationKernelADM1ReactionParameterData(SFEOrganicSKernelADM1ReactionParameterData):
    """The organic-S kernel reaction block plus the micro-aeration extension (module docstring)."""

    def build(self):
        super().build()
        self._reaction_block_class = MicroaerationKernelADM1ReactionBlock
        validate_microaeration_extension(EXT)
        components = list(self.config.property_package.component_list)
        missing = [c for c in MICROAERATION_COMPONENTS if c not in components]
        if missing:
            raise ConfigurationError("the micro-aeration reaction block needs the micro-aeration property package "
                                     f"(missing {missing})")
        names = tuple(p.name for p in EXT.parameters)
        self.microaeration_parameter_names = names
        self.microaeration_parameter = pyo.Param(names, mutable=True, within=pyo.Reals, units=pyo.units.dimensionless,
                                                 initialize=EXT.parameter_values(),
                                                 doc="micro-aeration extension parameters (the kernel's own units)")
        carriers = dict(SFE_KERNEL.conserved_carriers)
        self.microaeration_content = {}
        for element in carriers:
            group, mw = CARRIER_BASIS[element]
            initial = {c.component_id: c.content(element) / mw for c in EXT.contents}
            name = f"microaeration_{group}"
            setattr(self, name, pyo.Param(MICROAERATION_COMPONENTS, mutable=True, within=pyo.NonNegativeReals,
                                          initialize=initial, units=pyo.units.kmol / pyo.units.kg,
                                          doc=f"micro-aeration component {element} content (kmol per kg of its basis)"))
            self.microaeration_content[element] = getattr(self, name)
        for reaction in MICROAERATION_REACTIONS:
            self.sfe_reaction_idx.add(reaction)
            self.rate_reaction_idx.add(reaction)
        keys = [(r, j) for r in MICROAERATION_REACTIONS for j in components if j not in carriers.values()]
        self.microaeration_stoichiometry = pyo.Param(keys, mutable=True, within=pyo.Reals,
                                                     units=pyo.units.dimensionless,
                                                     initialize=self._microaeration_matrix(keys),
                                                     doc="micro-aeration liquid stoichiometry, every component but "
                                                         "the carriers")
        for r, j in keys:
            self.rate_reaction_stoichiometry[r, "Liq", j] = self.microaeration_stoichiometry[r, j]
        for element, carrier in carriers.items():
            group, mw = CARRIER_BASIS[element]
            core, sfe_extra = getattr(self, group), self.sfe_content[element]
            organic, own = self.organic_s_content[element], self.microaeration_content[element]
            for r in MICROAERATION_REACTIONS:
                self.rate_reaction_stoichiometry[r, "Liq", carrier] = -(
                    sum(self.microaeration_stoichiometry[r, j] * core[j] for j in components if j in core)
                    + sum(self.microaeration_stoichiometry[r, j] * sfe_extra[j] for j in MADM1_SFE_COMPONENTS)
                    + sum(self.microaeration_stoichiometry[r, j] * organic[j] for j in ORGANIC_S_COMPONENTS)
                    + sum(self.microaeration_stoichiometry[r, j] * own[j] for j in MICROAERATION_COMPONENTS)) * mw
        self.kernel_identity = dict(self.kernel_identity, **microaeration_identity(),
                                    twin_process_index=dict(MICROAERATION_TWIN_PROCESS_INDEX),
                                    unit_level_processes=dict(UNIT_LEVEL_PROCESSES),
                                    o2_inhibited_reactions=list(O2_INHIBITED_REACTIONS))

    def microaeration_values(self) -> dict[str, float]:
        """The block's current S/Fe and micro-aeration values (and the derived stoichiometric parameters)."""
        current = {**self._parameter_values(),
                   **{k: pyo.value(self.microaeration_parameter[k]) for k in self.microaeration_parameter_names}}
        return microaeration_parameter_values(current)

    def _microaeration_matrix(self, keys) -> dict:
        values = self.microaeration_values()
        rows = {r: {cid: evaluate(c, values) for cid, c in p.stoichiometry.items()}
                for r, p in MICROAERATION_REACTION_PROCESS.items()}
        return {(r, j): rows[r].get(j, 0.0) for r, j in keys}

    def apply_parameter_values(self, overrides: Mapping[str, float], *, identity: Optional[Mapping[str, Any]] = None):
        """Rebind S/Fe and micro-aeration values (rates and stoichiometry); unknown names refused. The unit-level G1
        and G4 read the same Params, so a bracket edge set here reaches them too."""
        overrides = dict(overrides)
        own = {k: v for k, v in overrides.items() if k in self.microaeration_parameter_names}
        super().apply_parameter_values({k: v for k, v in overrides.items() if k not in own}, identity=identity)
        for k, v in own.items():
            self.microaeration_parameter[k].set_value(float(v))
        for key, value in self._microaeration_matrix(list(self.microaeration_stoichiometry)).items():
            self.microaeration_stoichiometry[key].set_value(value)
        if identity is not None:
            self.kernel_identity = dict(self.kernel_identity, **microaeration_identity())


# ------------------------------------------------------------------------------------------------------- unit
def _bar(pressure):
    return pyo.units.convert(pressure, to_units=pyo.units.bar) / pyo.units.bar


def _g1_symbols(unit, t) -> dict[str, Any]:
    """The symbols G1 and its stoichiometry read on the unit (dimensionless numbers in the kernel's units)."""
    rp = unit.config.reaction_package
    mp = rp.microaeration_parameter
    vap = unit.vapor_phase[t]
    symbols = {name: mp[name] for name in rp.microaeration_parameter_names}
    symbols.update({
        "MW_S": rp.sfe_parameter["MW_S"],
        "a_wet_m2_per_m3": unit.headspace_wetted_area[t] / unit.volume_liquid[t] * pyo.units.m,
        "p_h2s_bar": _bar(vap.pressure_sat["S_h2s"]),
        "p_o2_bar": _bar(vap.pressure_sat["S_o2"]),
        "gas_smoothing": unit.oxidant_gas_smoothing,
    })
    return symbols


def build_oxidant_inlet(unit) -> None:
    """The oxidant inlet on an S/Fe AD (``SFEAD(oxidant_inlet=True)``; module docstring)."""
    liquid = unit.config.liquid_property_package
    vapor = unit.config.vapor_property_package
    rp = unit.config.reaction_package
    if not set(MICROAERATION_COMPONENTS) <= set(liquid.component_list):
        raise ConfigurationError(f"{unit.name}: oxidant_inlet needs the micro-aeration liquid package (S_O2)")
    if not set(MICROAERATION_GAS_COMPONENTS) <= set(vapor.component_list):
        raise ConfigurationError(f"{unit.name}: oxidant_inlet needs the micro-aeration vapour package (S_o2, S_n2)")
    if not hasattr(rp, "microaeration_parameter"):
        raise ConfigurationError(f"{unit.name}: oxidant_inlet needs the micro-aeration kernel reaction package")
    time = unit.flowsheet().time
    args = dict(**unit.config.vapor_property_package_args)
    args.update(has_phase_equilibrium=False, defined_state=True)
    unit.vapor_inlet_state = vapor.build_state_block(time, doc="Dosed oxidant gas (vapour inlet)", **args)
    unit.add_inlet_port(name="vapor_inlet", block=unit.vapor_inlet_state, doc="Dosed oxidant gas")
    unit.headspace_wetted_area = pyo.Var(time, initialize=1.0, domain=pyo.NonNegativeReals, units=pyo.units.m**2,
                                         doc="wetted headspace surface (liquid surface + wetted wall band): a "
                                             "geometry input the caller fixes")
    unit.oxidant_gas_smoothing = pyo.Param(initialize=OXIDANT_GAS_SMOOTHING, mutable=True, within=pyo.PositiveReals,
                                           units=pyo.units.dimensionless, doc="G1's partial-pressure smoothing (bar)")
    mp = rp.microaeration_parameter
    unit.KH_o2 = pyo.Var(time, initialize=pyo.value(mp["KH_o2"]), domain=pyo.NonNegativeReals,
                         units=pyo.units.kmol / pyo.units.m**3 * pyo.units.bar**-1,
                         doc="O2 Henry's law coefficient (the extension's KH_o2 at T)")

    def henry(b, t):
        return pyo.log(b.KH_o2[t] / (pyo.units.kmol / pyo.units.m**3 * pyo.units.bar**-1)) == (
            pyo.log(mp["KH_o2"]) + mp["dH_KH_o2"] * pyo.units.J / pyo.units.mol / Constants.gas_constant
            * ((1 / b.config.vapor_property_package.temperature_ref) - (1 / b.vapor_phase[t].temperature)))

    unit.O2_Henrys_law = pyo.Constraint(time, rule=henry, doc="O2 Henry's law coefficient constraint")
    per_day = pyo.units.kg / pyo.units.m**3 / pyo.units.day
    per_s = pyo.units.kg / pyo.units.m**3 / pyo.units.s

    def g1(b, t):
        rate = compile_pyomo(G1.rate, _g1_symbols(b, t))                    # kg S / m3 liquid / d
        return pyo.units.convert(rate * per_day, to_units=per_s) * b.volume_liquid[t]

    unit.headspace_h2s_oxidation = pyo.Expression(time, rule=g1, doc="H2S oxidised on the headspace surface (kg S/s)")

    def coefficient(b, t, mapping, key):
        return compile_pyomo(mapping[key], _g1_symbols(b, t)) if key in mapping else 0.0

    def g4_rate(b, t):
        symbols = {name: mp[name] for name in rp.microaeration_parameter_names}
        # surface transfer through the wetted headspace area G1 reads (kL_O2 a_wet), never the digester's K_La: that
        # describes gas leaving a gassing liquor and would overstate the liquid O2 sink about 4e4-fold
        symbols.update({"a_wet_m2_per_m3": b.headspace_wetted_area[t] / b.volume_liquid[t] * pyo.units.m,
                        S_O2: b.liquid_phase.properties_out[t].conc_mass_comp[S_O2] / (pyo.units.kg / pyo.units.m**3),
                        "KH_o2_T": b.KH_o2[t] / (pyo.units.kmol / pyo.units.m**3 * pyo.units.bar**-1) * mp["MW_O2"],
                        "p_o2_bar": _bar(b.vapor_phase[t].pressure_sat["S_o2"])})
        return compile_pyomo(G4.rate, symbols)                               # kg O2 / m3 / d, liquid -> gas

    unit.O2_transfer = pyo.Constraint(time, rule=lambda b, t: b.liquid_phase.mass_transfer_term[t, "Liq", S_O2] == (
        -pyo.units.convert(g4_rate(b, t) * per_day, to_units=per_s) * b.volume_liquid[t]),
        doc="O2 gas-liquid transfer (twin process 77)")

    def flow(block, j):
        return block.get_material_flow_terms("Vap", j)

    umb, mtt = unit.unit_material_balance, unit.liquid_phase.mass_transfer_term
    for t in time:
        vin, vout, r = unit.vapor_inlet_state[t], unit.vapor_phase[t], unit.headspace_h2s_oxidation[t]
        gas = {j: coefficient(unit, t, G1.gas_stoichiometry, j) for j in ("S_h2s", "S_o2")}
        for j in ("H2O", "S_h2", "S_ch4"):
            umb[t, j].set_value(mtt[t, "Liq", j] == -(flow(vout, j) - flow(vin, j)))
        umb[t, "S_IC"].set_value(mtt[t, "Liq", "S_IC"] == -(flow(vout, "S_co2") - flow(vin, "S_co2")))
        # gas out = in + transfer + G1 generation, so the liquid's transfer term is -(out - in - generation)
        umb[t, "S_IS"].set_value(mtt[t, "Liq", "S_IS"] == -(flow(vout, "S_h2s") - flow(vin, "S_h2s") - gas["S_h2s"] * r))
        umb[t, S_O2].set_value(mtt[t, "Liq", S_O2] == -(flow(vout, "S_o2") - flow(vin, "S_o2") - gas["S_o2"] * r))
        # G1's fall-out to the liquid: S0 and (the sulfate branch) sulfate, kg S/s
        for j in ("S_S0", "S_SO4"):
            umb[t, j].set_value(mtt[t, "Liq", j] == coefficient(unit, t, G1.stoichiometry, j) * r)
    unit.N2_balance = pyo.Constraint(time, rule=lambda b, t: flow(b.vapor_phase[t], "S_n2")
                                     == flow(b.vapor_inlet_state[t], "S_n2"), doc="N2 (+Ar) is inert in the gas")
    unit.headspace_s0_deposit = pyo.Expression(
        time, rule=lambda b, t: coefficient(b, t, G1.deposit_stoichiometry, DEPOSIT_COMPONENT)
        * b.headspace_h2s_oxidation[t], doc="S0 left on the headspace surfaces (kg S/s; a cleaning duty)")

    def heat(b, t):
        f = mp["f_SO4"]
        dh = (1 - f) * G1_REACTION_ENTHALPY_J_MOL["S0"] + f * G1_REACTION_ENTHALPY_J_MOL["SO4"]
        mol_s = b.headspace_h2s_oxidation[t] / (rp.sfe_parameter["MW_S"] * pyo.units.kg / pyo.units.kmol)
        return -pyo.units.convert(mol_s, to_units=pyo.units.mol / pyo.units.s) * dh * pyo.units.J / pyo.units.mol

    unit.headspace_oxidation_heat = pyo.Expression(time, rule=heat, doc="G1 reaction heat released (W)")
    unit.residual_o2_fraction = pyo.Expression(
        time, rule=lambda b, t: b.vapor_phase[t].pressure_sat["S_o2"] / b.vapor_phase[t].pressure,
        doc="O2 mole fraction of the headspace gas (wet)")


def oxidant_scaling(unit) -> None:
    """Scaling for what :func:`build_oxidant_inlet` added (the S_IS take-off's factors, its pattern)."""
    iscale.set_scaling_factor(unit.KH_o2, 1e3)
    iscale.set_scaling_factor(unit.headspace_wetted_area, 1e-3)
    for t in unit.flowsheet().time:
        iscale.constraint_scaling_transform(unit.O2_Henrys_law[t], 1.0)
        iscale.constraint_scaling_transform(unit.O2_transfer[t], 1e2)
        iscale.constraint_scaling_transform(unit.N2_balance[t], 1e2)
        for j in ("S_IS", S_O2, "S_S0", "S_SO4"):
            iscale.constraint_scaling_transform(unit.unit_material_balance[t, j], 1e2, overwrite=True)


def initialize_with_dose_steps(unit, base_initialize, *, outlvl, solver, optarg) -> dict:
    """The unit's own initialisation with an oxidant inlet: the oxidant-off solution first (the inlet flow held at
    zero), then the stated dose stepped in along :data:`OXIDANT_DOSE_STEPS`, a failed step halved (at most
    :data:`OXIDANT_DOSE_MAX_HALVINGS` times) from the last converged share. Returns the path."""
    from idaes.core.util.exceptions import InitializationError
    from watertap.core.solvers import get_solver

    time = unit.flowsheet().time
    stated = {t: pyo.value(unit.vapor_inlet_state[t].flow_vol) for t in time}
    unit.vapor_inlet_state.initialize(outlvl=outlvl, optarg=optarg, solver=solver, hold_state=False)
    for t in time:
        unit.vapor_inlet_state[t].flow_vol.fix(0.0)
    base_initialize(outlvl=outlvl, solver=solver, optarg=optarg)
    path = [{"dose_share": 0.0, "termination": "base_initialized"}]
    if all(v == 0 for v in stated.values()):
        return {"path": path, "stated_flow_m3_s": stated}
    solve = get_solver(solver, optarg or {})
    last = 0.0
    for target in OXIDANT_DOSE_STEPS:
        trial, halvings = target, 0
        while last < target:
            saved = {v: v.value for v in unit.component_data_objects(pyo.Var, descend_into=True) if not v.fixed}
            for t in time:
                unit.vapor_inlet_state[t].flow_vol.fix(trial * stated[t])
            condition = str(solve.solve(unit).solver.termination_condition)
            path.append({"dose_share": trial, "termination": condition})
            if condition == "optimal":
                last, trial = trial, target
                continue
            for var, value in saved.items():
                var.set_value(value, skip_validation=True)
            halvings += 1
            if halvings > OXIDANT_DOSE_MAX_HALVINGS:
                for t in time:
                    unit.vapor_inlet_state[t].flow_vol.fix(stated[t])
                raise InitializationError(f"{unit.name}: the oxidant dose could not be stepped past {last:g} of the "
                                          f"stated flow ({condition} at {trial:g}); path {path}")
            trial = last + 0.5 * (trial - last)
    return {"path": path, "stated_flow_m3_s": stated}


def microaeration_report(digester) -> dict[str, Any]:
    """The micro-aeration block of a solved digester: G1, the O2 and N2 balances, the S0 split (liquid fall-out vs
    deposit), the residual O2, the liquor DO, the reaction heat and the L1 / L3 extents (kg/d; heat W)."""
    t = digester.flowsheet().time.first()
    rp = digester.config.reaction_package
    values = rp.microaeration_values()
    day = 86400.0
    vin, vout = digester.vapor_inlet_state[t], digester.vapor_phase[t]

    def gflow(block, j):
        return float(pyo.value(block.get_material_flow_terms("Vap", j))) * day

    g1 = float(pyo.value(digester.headspace_h2s_oxidation[t])) * day
    s0_liquid = float(pyo.value(digester.liquid_phase.mass_transfer_term[t, "Liq", "S_S0"])) * day
    sulfate = float(pyo.value(digester.liquid_phase.mass_transfer_term[t, "Liq", "S_SO4"])) * day
    deposit = float(pyo.value(digester.headspace_s0_deposit[t])) * day
    o2_g1 = g1 * values["MW_O2"] / values["MW_S"] * (0.5 + 1.5 * values["f_SO4"])
    # the liquid's O2 transfer term is a SOURCE in the liquid balance: positive when O2 enters the liquor (G4), so the
    # gas balance reads in = out + consumed on the headspace surface + transferred to the liquor
    o2_to_liquid = float(pyo.value(digester.liquid_phase.mass_transfer_term[t, "Liq", S_O2])) * day
    extents = {MICROAERATION_REACTION_PROCESS[r].process_id:
               float(pyo.value(digester.liquid_phase.rate_reaction_extent[t, r])) * day for r in MICROAERATION_REACTIONS}
    rereduced = extents["S0_rereduction"]
    o2_in, o2_out = gflow(vin, "S_o2"), gflow(vout, "S_o2")
    return {
        "kinetics": microaeration_identity(),
        "headspace_h2s_oxidised_kg_s_d": g1,
        "s0_to_liquid_kg_s_d": s0_liquid, "s0_headspace_deposit_kg_s_d": deposit, "sulfate_to_liquid_kg_s_d": sulfate,
        "o2_in_kg_d": o2_in, "o2_out_kg_d": o2_out, "o2_consumed_headspace_kg_d": o2_g1,
        "o2_to_liquid_kg_d": o2_to_liquid,
        "o2_balance_residual_kg_d": o2_in - o2_out - o2_g1 - o2_to_liquid,
        "n2_in_kg_d": gflow(vin, "S_n2"), "n2_out_kg_d": gflow(vout, "S_n2"),
        "residual_o2_fraction_wet": float(pyo.value(digester.residual_o2_fraction[t])),
        "liquor_do_kg_m3": float(pyo.value(digester.liquid_phase.properties_out[t].conc_mass_comp[S_O2])),
        "headspace_oxidation_heat_w": float(pyo.value(digester.headspace_oxidation_heat[t])),
        "liquid_o2_sink_extents_kg_d": {k: v for k, v in extents.items() if k.startswith("aerobic")},
        "s0_rereduction": {"extent_kg_s_d": rereduced, "k_S0_red_per_d": values["k_S0_red"],
                           "extra_o2_kg_d": rereduced * 0.5 * values["MW_O2"] / values["MW_S"],
                           "lost_cod_kg_d": rereduced * values["COD_per_kmol_S0_reduced"] / values["MW_S"]},
        "headspace_wetted_area_m2": float(pyo.value(digester.headspace_wetted_area[t])),
        "parameters": {k: values[k] for k in ("r_A_ref", "n_h2s", "K_pO2", "f_SO4", "phi_liq", "kL_O2",
                                               "K_O2_fac", "KI_O2", "k_S0_red")},
    }


__all__ = [
    "EXT",
    "OXIDANT_KEYS",
    "OXIDANT_REFUSED_SPECIES",
    "OXIDANT_SPECIES",
    "OxidantInletError",
    "oxidant_inlet_state",
    "MICROAERATION_COMPONENTS",
    "MICROAERATION_GAS_COMPONENTS",
    "MICROAERATION_REACTIONS",
    "MICROAERATION_REACTION_PROCESS",
    "MICROAERATION_TWIN_PROCESS_INDEX",
    "MicroaerationADM1VaporParameterBlock",
    "MicroaerationKernelADM1ReactionParameterBlock",
    "MicroaerationModifiedADM1ParameterBlock",
    "O2_INHIBITED_REACTIONS",
    "OXIDANT_DOSE_STEPS",
    "UNIT_LEVEL_PROCESSES",
    "build_oxidant_inlet",
    "initialize_with_dose_steps",
    "microaeration_identity",
    "microaeration_report",
    "oxidant_scaling",
]

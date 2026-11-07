"""House mADM1 KERNEL reaction block: the shared biokinetic kernel, compiled into WaterTAP (Wave 2 item 6)."""
from __future__ import annotations
from typing import Any, Mapping, Optional
import pyomo.environ as pyo
from idaes.core import declare_process_block_class
from watertap.property_models.unit_specific.anaerobic_digestion import modified_adm1_reactions as _library
from watertap_engine_mcp.chemistry.modified_adm1_davies import DaviesADM1ReactionBlockData, DaviesADM1ReactionParameterData, DaviesReactionScaler, _DaviesReactionBlock
from watertap_engine_mcp.chemistry.pyomo_compiler import compile_factors, compile_pyomo
from puran_mcp_support.chemistry.madm1_biokinetics import madm1_parameter_values, madm1_stoichiometric_matrix
from puran_mcp_support.schema.madm1_ammonia_acclimation import free_ammonia_basis, free_ammonia_record
from puran_mcp_support.schema.madm1_biokinetics import DESIGN_CONTENT_DEPARTURES, MADM1_BIOKINETICS_KERNEL, MADM1_CORE_COMPONENTS
from puran_mcp_support.schema.madm1_rate_temperature import RATE_TEMPERATURE_PARAMETERS, rate_temperature_record
KERNEL = MADM1_BIOKINETICS_KERNEL
REACTION_PROCESS = {f'R{p.index + 1}': p for p in KERNEL.processes}
KERNEL_SOURCE = 'puran_mcp_support.schema.madm1_biokinetics.MADM1_BIOKINETICS_KERNEL'
NUMERICAL_FLOORS = {'competition_floor_kg_m3': 1e-10, 'pao_ratio_floor_kg_m3': 1e-10}
CARRIER_BASIS = {'C': ('Ci', _library.mw_c), 'N': ('Ni', _library.mw_n), 'P': ('Pi', _library.mw_p)}
KERNEL_MOLAR_MASS = {'C': 12.011, 'N': 14.0067, 'P': 30.973761}
CONTENT_AGREEMENT_REL = 0.002

def applied_design_departures(block) -> dict:
    """{(component, element): ruling source} the kernel block set on its content Vars at build."""
    return dict(getattr(block, 'kernel_design_departures', {}))

def design_parameter_values(store: Any=None) -> tuple[dict[str, float], dict[str, Any]]:
    """(values, identity) of the digester's design parameter set, read through custody."""
    from puran_mcp_support.engineering.heuristics.anaerobic.kinetics import madm1_design_parameters
    values = madm1_design_parameters(store)
    identity = values.pop('_identity')
    return (values, identity)

class KernelReactionScaler(DaviesReactionScaler):
    """The Davies scaler: the kernel block keeps every library variable and constraint name."""

class _KernelReactionBlock(_DaviesReactionBlock):
    default_scaler = KernelReactionScaler

def kernel_parameter_symbols(params) -> dict[str, Any]:
    """The kernel's parameters as rate-law symbols: a rate constant a rate-temperature basis moves reads its value x its
    factor (``kernel_rate_temperature_factor``, 1 until :meth:`KernelADM1ReactionParameterData.apply_rate_temperature`)."""
    factor = params.kernel_rate_temperature_factor
    return {name: params.kernel_parameter[name] * factor[name] if name in RATE_TEMPERATURE_PARAMETERS else params.kernel_parameter[name] for name in params.kernel_parameter_names}

def _dimensionless_symbols(block) -> dict[str, Any]:
    mass = pyo.units.kg / pyo.units.m ** 3
    molar = pyo.units.kmol / pyo.units.m ** 3
    params = block.params
    symbols: dict[str, Any] = {cid: block.conc_mass_comp_ref[cid] / mass for cid in MADM1_CORE_COMPONENTS}
    symbols.update(kernel_parameter_symbols(params))
    symbols['H_ion'] = 10 ** (-block.pH)
    symbols['NH3_M'] = block.conc_mol_nh3 / molar
    symbols['Na_M'] = block.sodium_mol_l / molar
    symbols['H2S_free_kg_S_m3'] = params.kernel_free_h2s_kg_S_m3
    symbols['competition_floor_kg_m3'] = params.kernel_numerical_floor['competition_floor_kg_m3']
    symbols['pao_ratio_floor_kg_m3'] = params.kernel_numerical_floor['pao_ratio_floor_kg_m3']
    return symbols

def record_inhibition(block, reaction: str, process, symbols: Mapping[str, Any], *also) -> None:
    """``block.inhibition_factors[reaction]`` = ``{"process": its id, "factors": ((name, Pyomo expression), ...)}``: the
    named factors (``chemistry.pyomo_compiler.compile_factors``) of what ``I_fun[reaction]`` multiplies, i.e.
    ``process.inhibition`` and any factor ``also`` multiplies in, compiled on the same ``symbols``; nothing where I is
    1. Called beside every compiled I_fun, a later compile of a reaction replacing its record. A record on the Python
    object only (no Pyomo component: the model and its solve are unchanged), read by
    ``native_digestion.uptake_inhibition``."""
    record = getattr(block, 'inhibition_factors', None)
    if record is None:
        record = block.inhibition_factors = {}
    parts = [part for part in (process.inhibition, *also) if part is not None]
    if not parts:
        record.pop(reaction, None)
        return
    record[reaction] = {'process': process.process_id, 'factors': tuple((factor for part in parts for factor in compile_factors(part, symbols)))}

@declare_process_block_class('KernelADM1ReactionBlock', block_class=_KernelReactionBlock)
class KernelADM1ReactionBlockData(DaviesADM1ReactionBlockData):

    def _rxn_rate(self):
        super()._rxn_rate()
        mass = pyo.units.kg / pyo.units.m ** 3
        symbols = _dimensionless_symbols(self)
        for reaction, process in REACTION_PROCESS.items():
            if process.inhibition is None:
                self.I_fun[reaction].set_value(self.I[reaction] == 1)
            else:
                self.I_fun[reaction].set_value(self.I[reaction] == compile_pyomo(process.inhibition, symbols))
            record_inhibition(self, reaction, process, symbols)
            base = compile_pyomo(process.rate_base, symbols)
            self.rate_expression[reaction].set_value(self.reaction_rate[reaction] == pyo.units.convert(base * self.I[reaction] * mass / pyo.units.day, to_units=mass / pyo.units.s))

@declare_process_block_class('KernelADM1ReactionParameterBlock')
class KernelADM1ReactionParameterData(DaviesADM1ReactionParameterData):
    """The Davies parameter block whose rates and stoichiometry are the shared kernel's (module docstring)."""

    def build(self):
        super().build()
        self._reaction_block_class = KernelADM1ReactionBlock
        values, identity = design_parameter_values()
        self.kernel_parameter_names = tuple((p.name for p in KERNEL.parameters))
        missing = sorted(set(self.kernel_parameter_names) - set(values))
        if missing:
            raise ValueError(f'the digester design parameter set lacks kernel parameters {missing}')
        self.kernel_parameter = pyo.Param(self.kernel_parameter_names, mutable=True, within=pyo.Reals, initialize={k: values[k] for k in self.kernel_parameter_names}, units=pyo.units.dimensionless, doc="shared mADM1 kernel parameters (the kernel's own units)")
        self.kernel_rate_temperature_factor = pyo.Param(RATE_TEMPERATURE_PARAMETERS, mutable=True, within=pyo.PositiveReals, initialize=1.0, units=pyo.units.dimensionless, doc="k(T)/k(35 C) of each rate constant a named rate-temperature basis moves (1: the kernel's set)")
        self.kernel_rate_temperature = rate_temperature_record(None)
        self.kernel_free_ammonia = free_ammonia_record(None)
        self.kernel_numerical_floor = pyo.Param(tuple(NUMERICAL_FLOORS), mutable=True, within=pyo.NonNegativeReals, initialize=dict(NUMERICAL_FLOORS), units=pyo.units.dimensionless, doc='kernel derived symbols: denominator floors on the competition shares and the X_PP/X_PAO ratio')
        self.kernel_free_h2s_kg_S_m3 = pyo.Param(initialize=0.0, mutable=True, within=pyo.NonNegativeReals, units=pyo.units.dimensionless, doc='free molecular H2S (kg S/m3): 0, the WaterTAP mADM1 state carries no sulfide')
        matrix = madm1_stoichiometric_matrix({k: values[k] for k in self.kernel_parameter_names})
        components = list(self.config.property_package.component_list)
        carriers = dict(KERNEL.conserved_carriers)
        self._apply_design_departures()
        self._check_carrier_contents(components, carriers)
        keys = [(reaction, j) for reaction in REACTION_PROCESS for j in components if j not in carriers.values()]
        self.kernel_stoichiometry = pyo.Param(keys, mutable=True, within=pyo.Reals, units=pyo.units.dimensionless, initialize={(r, j): matrix[REACTION_PROCESS[r].process_id].get(j, 0.0) for r, j in keys}, doc='shared mADM1 kernel stoichiometry, every component but the element carriers (kg component / kg reference component)')
        unknown = sorted(set(self.rate_reaction_idx) - set(REACTION_PROCESS))
        if unknown:
            raise ValueError(f'WaterTAP reactions {unknown} have no kernel process')
        for reaction, j in keys:
            self.rate_reaction_stoichiometry[reaction, 'Liq', j] = self.kernel_stoichiometry[reaction, j]
        for element, carrier in carriers.items():
            group, mw = CARRIER_BASIS[element]
            contents = getattr(self, group)
            for reaction in REACTION_PROCESS:
                self.rate_reaction_stoichiometry[reaction, 'Liq', carrier] = -sum((self.kernel_stoichiometry[reaction, j] * contents[j] for j in components if j in contents)) * mw
        self.kernel_identity = dict(identity, kernel_source=KERNEL_SOURCE, kernel_id=KERNEL.kernel_id, kernel_sha256=KERNEL.sha256)

    def _apply_design_departures(self) -> None:
        """Public engineering calculation interface."""
        applied = {}
        for (cid, element), source in DESIGN_CONTENT_DEPARTURES.items():
            group, mw = CARRIER_BASIS[element]
            contents = getattr(self, group)
            if cid not in contents:
                raise ValueError(f"the design kernel moves {cid}'s {element} content ({source}); WaterTAP's {group} has no entry to carry it")
            contents[cid].fix(pyo.value(KERNEL.content(cid).content(element) / mw))
            applied[cid, element] = source
        self.kernel_design_departures = applied

    def _check_carrier_contents(self, components, carriers) -> None:
        """Every non-zero kernel content has a WaterTAP content Var, at the kernel's value within CONTENT_AGREEMENT_REL."""
        problems = []
        for element, carrier in carriers.items():
            group, mw = CARRIER_BASIS[element]
            contents = getattr(self, group)
            for cid in components:
                if cid in carriers.values() or cid not in KERNEL.component_ids:
                    continue
                kernel_kg = KERNEL.content(cid).content(element)
                if cid not in contents:
                    if kernel_kg:
                        problems.append(f"{cid} carries {kernel_kg} kg {element}/kg in the kernel; WaterTAP's {group} has no entry")
                    continue
                watertap_kg = pyo.value(contents[cid] * mw)
                if abs(watertap_kg - kernel_kg) > CONTENT_AGREEMENT_REL * max(abs(kernel_kg), abs(watertap_kg)):
                    problems.append(f"{group}[{cid}] = {watertap_kg:.6g} kg {element}/kg, the kernel's {kernel_kg:.6g}")
        if problems:
            raise ValueError("the WaterTAP mADM1 element contents do not describe the kernel's matter: " + '; '.join(problems))

    def apply_parameter_values(self, overrides: Mapping[str, float], *, identity: Optional[Mapping[str, Any]]=None):
        """Rebind kernel parameter values (rates AND stoichiometry) on a built model; unknown names refused."""
        values = madm1_parameter_values({**{k: pyo.value(self.kernel_parameter[k]) for k in self.kernel_parameter_names}, **dict(overrides)})
        for name in self.kernel_parameter_names:
            self.kernel_parameter[name].set_value(values[name])
        matrix = madm1_stoichiometric_matrix({k: values[k] for k in self.kernel_parameter_names})
        for reaction, j in self.kernel_stoichiometry:
            self.kernel_stoichiometry[reaction, j].set_value(matrix[REACTION_PROCESS[reaction].process_id].get(j, 0.0))
        if identity is not None:
            self.kernel_identity = dict(identity, kernel_source=KERNEL_SOURCE, kernel_id=KERNEL.kernel_id, kernel_sha256=KERNEL.sha256)

    def apply_rate_temperature(self, basis_id: str, temperature_k: float, *, hold=(), hold_reason: str='') -> dict:
        """Move the rate constants by the named basis at ``temperature_k`` (refused outside its range; ``hold`` keeps
        rates at factor 1 with ``hold_reason``); the record is ``kernel_rate_temperature``. The factors are VALUES at that
        temperature: the caller holds the reactor there and checks the solved temperature against the record."""
        record = rate_temperature_record(basis_id, temperature_k, hold=hold, hold_reason=hold_reason)
        for name in RATE_TEMPERATURE_PARAMETERS:
            self.kernel_rate_temperature_factor[name].set_value(record['factors'][name])
        self.kernel_rate_temperature = record
        return record

    def apply_free_ammonia_basis(self, basis_id: str) -> dict:
        """Rebind KI_nh3 to the named acclimation basis (``process_model_schema.madm1_ammonia_acclimation``)."""
        basis = free_ammonia_basis(basis_id)
        self.apply_parameter_values({'KI_nh3': basis.ki_nh3_m})
        self.kernel_free_ammonia = basis.record()
        return self.kernel_free_ammonia
__all__ = ['KERNEL', 'KernelADM1ReactionBlock', 'KernelADM1ReactionParameterBlock', 'REACTION_PROCESS', 'design_parameter_values', 'kernel_parameter_symbols']

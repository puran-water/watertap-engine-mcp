"""Property-package-neutral steady-state inventory block for shared chemistry.

The block owns zero-accumulation aqueous and retained-solid balances. A host
WaterTAP flowsheet links its activity variables to the selected aqueous
speciation/activity model and fixes product-dose source terms. Thus the same
reaction/kinetics IR used by QSDsan supplies WaterTAP's rapid steady solve,
without embedding PHREEQC inside IPOPT.
"""

from __future__ import annotations

import math
from typing import Iterable

from puran_mcp_support.chemistry import conditional_ligand_free_fractions, infer_fixed_background_charge_eq_m3, solve_relative_charge_balance
from puran_mcp_support.schema import ACID_DISSOCIATION_CONSTANTS, GAS_CONSTANT_J_MOL_K, MADM1_FECL3_FE_S_P_KERNEL, PHREEQC_PHASE_GAS_CONSTANT_J_MOL_K, REFERENCE_TEMPERATURE_K, ChemistryKernelDefinition
from pyomo.environ import (
    Block,
    Expression,
    NonNegativeReals,
    Param,
    Reals,
    Set,
    Var,
    exp,
    log,
    log10,
    value,
)

from .pyomo_compiler import compile_pyomo


def build_shared_chemistry_steady_state_block(
    *,
    kernel: ChemistryKernelDefinition = MADM1_FECL3_FE_S_P_KERNEL,
    aqueous_component_ids: Iterable[str] = (
        "Fe_III_aq",
        "Fe_II_aq",
        "H2_aq",
        "O2_aq",
        "HS_minus",
        "SO4",
        "PO4",
        "CO3",
        "Cl_minus",
        "Ligand_sites",
        "H_plus",
    ),
) -> Block:
    block = Block(concrete=True)
    aqueous_ids = tuple(aqueous_component_ids)
    phase_ids = tuple(phase.phase_id for phase in kernel.phases)
    retained_ids = tuple(
        component.component_id
        for component in kernel.components
        if component.component_id not in aqueous_ids
        and component.component_id != "H2O"
        and component.permeability_class.value == "retained"
    )
    reaction_ids = tuple(reaction.reaction_id for reaction in kernel.reactions)

    block.kernel_id = Param(initialize=kernel.kernel_id, within=Set(initialize=[kernel.kernel_id]))
    block.aqueous_components = Set(initialize=aqueous_ids, ordered=True)
    block.phases = Set(initialize=phase_ids, ordered=True)
    block.retained_components = Set(initialize=retained_ids, ordered=True)
    block.reactions = Set(initialize=reaction_ids, ordered=True)
    block.hydraulic_residence_time_d = Param(initialize=1.0, mutable=True)
    block.solids_retention_time_d = Param(initialize=30.0, mutable=True)
    block.temperature_k = Param(
        initialize=REFERENCE_TEMPERATURE_K,
        mutable=True,
    )

    block.inlet = Var(block.aqueous_components, domain=NonNegativeReals, initialize=1e-6)
    block.outlet = Var(block.aqueous_components, domain=NonNegativeReals, initialize=1e-6)
    block.dose_source = Var(
        block.aqueous_components,
        domain=NonNegativeReals,
        initialize=0.0,
    )
    # Inventories are mol/m3; thermodynamic activities are on a mol/L
    # concentration scale in this reduced model. A positive lower bound avoids
    # singular negative exponents in proton-inclusive phase definitions.
    block.activity = Var(
        block.aqueous_components,
        domain=NonNegativeReals,
        bounds=(1e-30, None),
        initialize=1e-8,
    )
    block.activity_coefficient = Param(
        block.aqueous_components,
        initialize=1.0,
        mutable=True,
    )
    block.activity_floor_mol_l = Param(initialize=1e-30, mutable=False)

    # The reduced inventory variables named PO4, HS_minus, and CO3 carry
    # analytical P, S(-II), and inorganic-C totals. Mineral IAPs require the
    # corresponding free ions. Both WaterTAP and QSDsan render these constants
    # from the same immutable shared definitions, including source-backed
    # van't Hoff corrections where an enthalpy is available. A full coupled
    # charge/speciation closure remains an explicit release blocker; treating
    # analytical S_IP as free PO4 is never an acceptable interim shortcut.
    def _ka_expression(constant_id: str):
        definition = ACID_DISSOCIATION_CONSTANTS[constant_id]
        if definition.analytic is not None:
            # a published analytic law (sulfide_1, HOUSE-MADM1-ACID-CONSTANTS-EVALUATED-LITERATURE-20261008): the
            # shared definition's own law, rendered in Pyomo
            return 10 ** definition.log10_ka_law(block.temperature_k, log10)
        if definition.delta_h_j_mol is None:
            return definition.ka_25c
        return definition.ka_25c * exp(
            definition.delta_h_j_mol
            / GAS_CONSTANT_J_MOL_K
            * (1.0 / REFERENCE_TEMPERATURE_K - 1.0 / block.temperature_k)
        )

    block.phosphate_ka1 = Expression(expr=_ka_expression("phosphoric_1"))
    block.phosphate_ka2 = Expression(expr=_ka_expression("phosphoric_2"))
    block.phosphate_ka3 = Expression(expr=_ka_expression("phosphoric_3"))
    block.sulfide_ka1 = Expression(expr=_ka_expression("sulfide_1"))
    block.sulfide_ka2 = Expression(expr=_ka_expression("sulfide_2"))
    block.carbonate_ka1 = Expression(expr=_ka_expression("carbonic_1"))
    block.carbonate_ka2 = Expression(expr=_ka_expression("carbonic_2"))
    # Hydrated-solid saturation indices depend on solvent-water activity.  It
    # is explicit (rather than silently fixed at unity) so a PHREEQC or host
    # property-package closure can supply the same thermodynamic convention.
    block.solvent_water_activity = Param(initialize=1.0, mutable=True)
    block.mol_m3_to_mol_l = Param(initialize=1e-3, mutable=False)
    block.solid_inventory = Var(block.retained_components, domain=NonNegativeReals, initialize=1e-8)
    for component_id in (
        "HFO_reoxidized",
        "HFO_aged",
        "HFO_fresh_surface_P",
        "HFO_reoxidized_surface_P",
        "HFO_aged_surface_P",
    ):
        if component_id in block.retained_components:
            block.solid_inventory[component_id].set_value(0.0)
    block.solid_inlet = Var(block.retained_components, domain=NonNegativeReals, initialize=0.0)
    for component_id in block.retained_components:
        block.solid_inlet[component_id].fix(0.0)
    block.solid_outlet_fraction = Param(
        block.retained_components,
        domain=NonNegativeReals,
        initialize=0.0,
        mutable=True,
    )
    block.solid_wasting = Var(block.retained_components, domain=NonNegativeReals, initialize=1e-10)
    block.surface_area = Var(block.phases, domain=NonNegativeReals, initialize=1.0)
    block.saturation_index = Var(block.phases, domain=Reals, initialize=0.0)
    block.saturation_ratio = Expression(
        block.phases,
        rule=lambda b, phase_id: exp(log(10.0) * b.saturation_index[phase_id]),
    )
    block.net_phase_rate = Var(block.phases, domain=Reals, initialize=0.0)
    block.reaction_rate = Var(block.reactions, domain=Reals, initialize=0.0)

    ligand_definition = kernel.ligand_models[0] if kernel.ligand_models else None
    block.beta_fe_ii_l_mol = Param(
        initialize=(0.0 if ligand_definition is None else ligand_definition.beta_fe_ii_l_mol),
        mutable=True,
    )
    block.beta_fe_iii_l_mol = Param(
        initialize=(0.0 if ligand_definition is None else ligand_definition.beta_fe_iii_l_mol),
        mutable=True,
    )
    block.free_ligand_mol_l = Var(domain=NonNegativeReals, initialize=0.0)
    block.fe_ii_l_complex_mol_l = Expression(
        expr=(
            block.outlet["Fe_II_aq"]
            * block.mol_m3_to_mol_l
            * block.beta_fe_ii_l_mol
            * block.free_ligand_mol_l
            / (1.0 + block.beta_fe_ii_l_mol * block.free_ligand_mol_l)
        )
    )
    block.fe_iii_l_complex_mol_l = Expression(
        expr=(
            block.outlet["Fe_III_aq"]
            * block.mol_m3_to_mol_l
            * block.beta_fe_iii_l_mol
            * block.free_ligand_mol_l
            / (1.0 + block.beta_fe_iii_l_mol * block.free_ligand_mol_l)
        )
    )

    @block.Constraint()
    def conditional_ligand_balance(b):
        return b.outlet["Ligand_sites"] * b.mol_m3_to_mol_l == (
            b.free_ligand_mol_l
            + b.fe_ii_l_complex_mol_l
            + b.fe_iii_l_complex_mol_l
        )

    phase_by_id = {phase.phase_id: phase for phase in kernel.phases}
    kinetic_by_id = {kinetic.kinetic_id: kinetic for kinetic in kernel.kinetics}

    @block.Expression(block.phases)
    def equilibrium_constant(b, phase_id):
        phase = phase_by_id[phase_id]
        k_25c = 10.0**phase.log_k_25c
        if phase.delta_h_j_mol is None:
            return k_25c
        return k_25c * exp(
            phase.delta_h_j_mol
            / PHREEQC_PHASE_GAS_CONSTANT_J_MOL_K
            * (1.0 / REFERENCE_TEMPERATURE_K - 1.0 / b.temperature_k)
        )

    @block.Expression(block.aqueous_components)
    def free_ion_fraction(b, component_id):
        h = b.activity["H_plus"]
        if component_id == "PO4":
            k1 = b.phosphate_ka1
            k2 = b.phosphate_ka2
            k3 = b.phosphate_ka3
            return k1 * k2 * k3 / (h**3 + k1 * h**2 + k1 * k2 * h + k1 * k2 * k3)
        if component_id == "HS_minus":
            k1 = b.sulfide_ka1
            k2 = b.sulfide_ka2
            return k1 * h / (h**2 + k1 * h + k1 * k2)
        if component_id == "CO3":
            k1 = b.carbonate_ka1
            k2 = b.carbonate_ka2
            return k1 * k2 / (h**2 + k1 * h + k1 * k2)
        if component_id == "Fe_II_aq":
            return 1.0 / (1.0 + b.beta_fe_ii_l_mol * b.free_ligand_mol_l)
        if component_id == "Fe_III_aq":
            return 1.0 / (1.0 + b.beta_fe_iii_l_mol * b.free_ligand_mol_l)
        return 1.0

    @block.Constraint(block.aqueous_components)
    def activity_link(b, component_id):
        return b.activity[component_id] == b.activity_floor_mol_l + (
            b.activity_coefficient[component_id]
            * b.outlet[component_id]
            * b.mol_m3_to_mol_l
            * b.free_ion_fraction[component_id]
        )

    @block.Constraint(block.phases)
    def saturation_index_definition(b, phase_id):
        phase = phase_by_id[phase_id]
        log_iap = 0.0
        for component_id, coefficient in phase.dissolution_reaction.items():
            if component_id in b.phases:
                continue
            if component_id == "H2O":
                log_iap += float(coefficient) * log(b.solvent_water_activity)
                continue
            if component_id not in b.aqueous_components:
                raise KeyError(
                    f"phase {phase_id!r} requires unmapped aqueous activity {component_id!r}"
                )
            log_iap += float(coefficient) * log(b.activity[component_id])
        return log(10.0) * b.saturation_index[phase_id] == (
            log_iap - log(b.equilibrium_constant[phase_id])
        )

    def _kinetic_symbols(b, phase_id):
        return {
            f"k_n.{phase_id}": b.k_n[phase_id],
            f"k_g.{phase_id}": b.k_g[phase_id],
            f"k_d.{phase_id}": b.k_d[phase_id],
            "zone_volume": b.zone_volume,
            f"SI.{phase_id}": b.saturation_index[phase_id],
            f"SI_critical.{phase_id}": b.si_critical[phase_id],
            f"n_nucleation.{phase_id}": b.n_nucleation[phase_id],
            f"area.{phase_id}": b.surface_area[phase_id],
            f"saturation_ratio.{phase_id}": b.saturation_ratio[phase_id],
            f"n_growth.{phase_id}": b.n_growth[phase_id],
            f"n_dissolution.{phase_id}": b.n_dissolution[phase_id],
        }

    block.zone_volume = Param(initialize=1.0, mutable=True)
    block.k_n = Param(block.phases, initialize=0.0, mutable=True)
    block.k_g = Param(block.phases, initialize=0.0, mutable=True)
    block.k_d = Param(block.phases, initialize=0.0, mutable=True)
    block.si_critical = Param(block.phases, initialize=0.0, mutable=True)
    block.n_nucleation = Param(block.phases, initialize=1.0, mutable=True)
    block.n_growth = Param(block.phases, initialize=1.0, mutable=True)
    block.n_dissolution = Param(block.phases, initialize=1.0, mutable=True)
    block.area_per_inventory = Param(block.phases, initialize=1.0, mutable=True)
    block.seed_surface_area = Param(block.phases, initialize=0.0, mutable=True)

    @block.Constraint(block.phases)
    def surface_area_definition(b, phase_id):
        return b.surface_area[phase_id] == (
            b.area_per_inventory[phase_id] * b.solid_inventory[phase_id]
            + b.seed_surface_area[phase_id]
        )

    @block.Constraint(block.phases)
    def phase_rate_definition(b, phase_id):
        symbols = _kinetic_symbols(b, phase_id)
        total = sum(
            compile_pyomo(kinetic_by_id[f"{phase_id}.{kind}"].expression, symbols)
            for kind in ("nucleation", "growth", "dissolution")
        )
        return b.net_phase_rate[phase_id] == total

    # Phase precipitation reactions map one-to-one for the first vertical slice.
    phase_reaction = {
        "HFO": "ferric_hydrolysis_to_hfo",
        "FeS_amorphous": "precipitate_amorphous_fes",
        "Mackinawite": "precipitate_mackinawite",
        "Vivianite": "precipitate_vivianite",
        "Siderite": "precipitate_siderite",
    }
    redox_acceptor = {
        "ferric_reduction_by_hydrogen": "Fe_III_aq",
        "ferric_reduction_by_sulfide": "Fe_III_aq",
        "hfo_reduction_by_hydrogen": "HFO",
        "hfo_reduction_by_sulfide": "HFO",
        "reoxidized_hfo_reduction_by_hydrogen": "HFO_reoxidized",
        "reoxidized_hfo_reduction_by_sulfide": "HFO_reoxidized",
        "aged_hfo_reduction_by_hydrogen": "HFO_aged",
        "aged_hfo_reduction_by_sulfide": "HFO_aged",
    }
    redox_donor = {
        "ferric_reduction_by_hydrogen": "H2_aq",
        "ferric_reduction_by_sulfide": "HS_minus",
        "hfo_reduction_by_hydrogen": "H2_aq",
        "hfo_reduction_by_sulfide": "HS_minus",
        "reoxidized_hfo_reduction_by_hydrogen": "H2_aq",
        "reoxidized_hfo_reduction_by_sulfide": "HS_minus",
        "aged_hfo_reduction_by_hydrogen": "H2_aq",
        "aged_hfo_reduction_by_sulfide": "HS_minus",
    }
    block.reduction_reactions = Set(initialize=tuple(redox_acceptor), ordered=True)
    block.k_red = Param(
        block.reduction_reactions,
        domain=NonNegativeReals,
        initialize=0.0,
        mutable=True,
    )
    block.K_donor = Param(
        block.reduction_reactions,
        domain=NonNegativeReals,
        initialize=1e-6,
        mutable=True,
    )

    @block.Constraint(block.phases)
    def link_phase_reaction_rate(b, phase_id):
        return b.reaction_rate[phase_reaction[phase_id]] == b.net_phase_rate[phase_id]

    @block.Constraint(block.reduction_reactions)
    def redox_rate_definition(b, reaction_id):
        acceptor_id = redox_acceptor[reaction_id]
        donor_id = redox_donor[reaction_id]
        acceptor = (
            b.solid_inventory[acceptor_id]
            if acceptor_id in b.retained_components
            else b.outlet[acceptor_id]
        )
        symbols = {
            f"k_red.{reaction_id}": b.k_red[reaction_id],
            f"inventory.{acceptor_id}": acceptor,
            f"activity.{donor_id}": b.activity[donor_id],
            f"K_donor.{reaction_id}": b.K_donor[reaction_id],
        }
        return b.reaction_rate[reaction_id] == compile_pyomo(
            kinetic_by_id[f"{reaction_id}.rate"].expression,
            symbols,
        )

    oxidation_solid = {
        "oxidize_amorphous_fes_to_reoxidized_hfo_s0": "FeS_amorphous",
        "oxidize_amorphous_fes_to_reoxidized_hfo_sulfate": "FeS_amorphous",
        "oxidize_mackinawite_to_reoxidized_hfo_s0": "Mackinawite",
        "oxidize_mackinawite_to_reoxidized_hfo_sulfate": "Mackinawite",
    }
    block.oxidation_reactions = Set(initialize=tuple(oxidation_solid), ordered=True)
    block.k_oxidation = Param(block.oxidation_reactions, initialize=0.0, mutable=True)
    block.K_oxidant = Param(block.oxidation_reactions, initialize=1e-6, mutable=True)

    @block.Constraint(block.oxidation_reactions)
    def oxidation_rate_definition(b, reaction_id):
        solid_id = oxidation_solid[reaction_id]
        symbols = {
            f"k_oxidation.{reaction_id}": b.k_oxidation[reaction_id],
            f"K_oxidant.{reaction_id}": b.K_oxidant[reaction_id],
            f"inventory.{solid_id}": b.solid_inventory[solid_id],
            "activity.O2_aq": b.activity["O2_aq"],
        }
        return b.reaction_rate[reaction_id] == compile_pyomo(
            kinetic_by_id[f"{reaction_id}.rate"].expression,
            symbols,
        )

    aging_source = {
        "age_amorphous_fes_to_mackinawite": "FeS_amorphous",
        "age_fresh_hfo": "HFO",
        "age_reoxidized_hfo": "HFO_reoxidized",
        "age_fresh_hfo_surface_p": "HFO_fresh_surface_P",
        "age_reoxidized_hfo_surface_p": "HFO_reoxidized_surface_P",
    }
    block.aging_reactions = Set(initialize=tuple(aging_source), ordered=True)
    block.k_aging = Param(block.aging_reactions, initialize=0.0, mutable=True)

    @block.Constraint(block.aging_reactions)
    def aging_rate_definition(b, reaction_id):
        source_id = aging_source[reaction_id]
        symbols = {
            f"k_aging.{reaction_id}": b.k_aging[reaction_id],
            f"inventory.{source_id}": b.solid_inventory[source_id],
        }
        return b.reaction_rate[reaction_id] == compile_pyomo(
            kinetic_by_id[f"{reaction_id}.rate"].expression,
            symbols,
        )

    surface_history = {
        "fresh": ("HFO", "HFO_fresh_surface_P"),
        "reoxidized": ("HFO_reoxidized", "HFO_reoxidized_surface_P"),
        "aged": ("HFO_aged", "HFO_aged_surface_P"),
    }
    block.surface_histories = Set(initialize=tuple(surface_history), ordered=True)
    block.site_capacity = Param(block.surface_histories, initialize=0.0, mutable=True)
    block.k_surface_adsorption = Param(block.surface_histories, initialize=0.0, mutable=True)
    block.K_surface_P = Param(block.surface_histories, initialize=1e-6, mutable=True)
    block.K_surface_ligand = Param(block.surface_histories, initialize=1.0, mutable=True)
    block.k_surface_desorption = Param(block.surface_histories, initialize=0.0, mutable=True)

    surface_host = {
        "fresh": ("HFO", "HFO_fresh_surface_P"),
        "reoxidized": ("HFO_reoxidized", "HFO_reoxidized_surface_P"),
        "aged": ("HFO_aged", "HFO_aged_surface_P"),
    }

    @block.Constraint(block.surface_histories)
    def surface_capacity_constraint(b, history):
        host_id, surface_p_id = surface_host[history]
        return b.solid_inventory[surface_p_id] <= (
            b.site_capacity[history] * b.solid_inventory[host_id]
        )

    for history in block.surface_histories:
        block.surface_capacity_constraint[history].deactivate()

    @block.Constraint(block.surface_histories)
    def surface_adsorption_rate_definition(b, history):
        host_id, surface_p_id = surface_history[history]
        reaction_id = f"adsorb_p_on_{history}_hfo"
        symbols = {
            f"site_capacity.{history}": b.site_capacity[history],
            f"k_surface_adsorption.{history}": b.k_surface_adsorption[history],
            f"K_surface_P.{history}": b.K_surface_P[history],
            f"K_surface_ligand.{history}": b.K_surface_ligand[history],
            f"inventory.{host_id}": b.solid_inventory[host_id],
            f"inventory.{surface_p_id}": b.solid_inventory[surface_p_id],
            "activity.PO4": b.activity["PO4"],
            "activity.Ligand_sites": b.activity["Ligand_sites"],
        }
        return b.reaction_rate[reaction_id] == compile_pyomo(
            kinetic_by_id[f"{reaction_id}.rate"].expression,
            symbols,
        )

    @block.Constraint(block.surface_histories)
    def surface_desorption_rate_definition(b, history):
        _, surface_p_id = surface_history[history]
        reaction_id = f"release_p_from_{history}_hfo"
        symbols = {
            f"k_surface_desorption.{history}": b.k_surface_desorption[history],
            f"inventory.{surface_p_id}": b.solid_inventory[surface_p_id],
        }
        return b.reaction_rate[reaction_id] == compile_pyomo(
            kinetic_by_id[f"{reaction_id}.rate"].expression,
            symbols,
        )

    # Redox rates are now explicit finite-donor expressions. The aqueous and
    # solid balances limit them through donor and host availability; no fixed-pe
    # electron reservoir or unconstrained reversible rate is present.

    reaction_by_id = {reaction.reaction_id: reaction for reaction in kernel.reactions}

    @block.Constraint(block.aqueous_components)
    def aqueous_zero_accumulation(b, component_id):
        reaction_term = sum(
            reaction_by_id[reaction_id].stoichiometry.get(component_id, 0.0)
            * b.reaction_rate[reaction_id]
            for reaction_id in b.reactions
        )
        return 0 == (
            (b.inlet[component_id] - b.outlet[component_id]) / b.hydraulic_residence_time_d
            + b.dose_source[component_id]
            + reaction_term
        )

    @block.Constraint(block.retained_components)
    def solid_zero_accumulation(b, component_id):
        reaction_term = sum(
            reaction_by_id[reaction_id].stoichiometry.get(component_id, 0.0)
            * b.reaction_rate[reaction_id]
            for reaction_id in b.reactions
        )
        return 0 == (
            (
                b.solid_inlet[component_id]
                - b.solid_outlet_fraction[component_id]
                * b.solid_inventory[component_id]
            )
            / b.hydraulic_residence_time_d
            + reaction_term
            - b.solid_wasting[component_id]
        )

    @block.Constraint(block.retained_components)
    def solid_wasting_definition(b, component_id):
        return b.solid_wasting[component_id] == (
            b.solid_inventory[component_id] / b.solids_retention_time_d
        )

    # Optional relative electroneutrality closure. It is inactive for the
    # legacy single-block screen; the serial WaterTAP compiler activates it and
    # deactivates the proton pseudo-state balance after inferring the transported
    # unmodelled strong-ion background from the measured inlet pH.
    block.fixed_background_charge_eq_m3 = Param(initialize=0.0, mutable=True)
    block.water_kw = Expression(expr=_ka_expression("water"))

    @block.Expression()
    def phosphate_average_charge(b):
        h = b.activity["H_plus"]
        k1, k2, k3 = b.phosphate_ka1, b.phosphate_ka2, b.phosphate_ka3
        return -(k1 * h**2 + 2.0 * k1 * k2 * h + 3.0 * k1 * k2 * k3) / (
            h**3 + k1 * h**2 + k1 * k2 * h + k1 * k2 * k3
        )

    @block.Expression()
    def sulfide_average_charge(b):
        h = b.activity["H_plus"]
        k1, k2 = b.sulfide_ka1, b.sulfide_ka2
        return -(k1 * h + 2.0 * k1 * k2) / (h**2 + k1 * h + k1 * k2)

    @block.Expression()
    def carbonate_average_charge(b):
        h = b.activity["H_plus"]
        k1, k2 = b.carbonate_ka1, b.carbonate_ka2
        return -(k1 * h + 2.0 * k1 * k2) / (h**2 + k1 * h + k1 * k2)

    @block.Expression()
    def reduced_charge_residual_eq_m3(b):
        surface_charge = -sum(
            b.solid_inventory[component_id]
            for component_id in (
                "HFO_fresh_surface_P",
                "HFO_reoxidized_surface_P",
                "HFO_aged_surface_P",
            )
        )
        hydroxide_mol_m3 = b.water_kw / b.activity["H_plus"] * 1000.0
        return (
            b.fixed_background_charge_eq_m3
            + 3.0 * b.outlet["Fe_III_aq"]
            + 2.0 * b.outlet["Fe_II_aq"]
            - 2.0 * b.outlet["SO4"]
            - b.outlet["Cl_minus"]
            + b.phosphate_average_charge * b.outlet["PO4"]
            + b.sulfide_average_charge * b.outlet["HS_minus"]
            + b.carbonate_average_charge * b.outlet["CO3"]
            + surface_charge
            + b.outlet["H_plus"]
            - hydroxide_mol_m3
        )

    @block.Constraint()
    def relative_charge_balance(b):
        return b.reduced_charge_residual_eq_m3 == 0.0

    block.relative_charge_balance.deactivate()

    block.kernel_fingerprint = kernel.sha256
    block.commissioning_status = kernel.commissioning_status
    block.recommendation_eligible = kernel.recommendation_capable
    block.phase_definition_by_id = phase_by_id
    return block


def fix_forward_case(
    block: Block,
    *,
    inlet_mol_m3: dict[str, float],
    dose_source_mol_m3_d: dict[str, float] | None = None,
    hydraulic_residence_time_d: float = 1.0,
    solids_retention_time_d: float = 30.0,
    temperature_c: float = 25.0,
    solvent_water_activity: float = 1.0,
) -> None:
    """Fix a forward screening case without overwriting any outlet state."""

    dose_source_mol_m3_d = dose_source_mol_m3_d or {}
    if (
        not math.isfinite(float(hydraulic_residence_time_d))
        or not math.isfinite(float(solids_retention_time_d))
        or hydraulic_residence_time_d <= 0
        or solids_retention_time_d <= 0
    ):
        raise ValueError("HRT and SRT must be finite and positive")
    solvent_water_activity = float(solvent_water_activity)
    if not math.isfinite(solvent_water_activity) or solvent_water_activity <= 0:
        raise ValueError("solvent water activity must be finite and positive")
    temperature_c = float(temperature_c)
    if not math.isfinite(temperature_c) or temperature_c <= -273.15:
        raise ValueError("temperature_c must be finite and above absolute zero")
    unknown = sorted(
        (set(inlet_mol_m3) | set(dose_source_mol_m3_d)) - set(block.aqueous_components)
    )
    if unknown:
        raise KeyError(f"forward case contains unknown aqueous components {unknown}")
    block.hydraulic_residence_time_d.set_value(hydraulic_residence_time_d)
    block.solids_retention_time_d.set_value(solids_retention_time_d)
    block.temperature_k.set_value(temperature_c + 273.15)
    block.solvent_water_activity.set_value(solvent_water_activity)
    for component_id in block.aqueous_components:
        inlet = float(inlet_mol_m3.get(component_id, 0.0))
        dose = float(dose_source_mol_m3_d.get(component_id, 0.0))
        if not math.isfinite(inlet) or not math.isfinite(dose) or inlet < 0 or dose < 0:
            raise ValueError("inlet inventories and dose sources must be finite and nonnegative")
        block.inlet[component_id].fix(inlet)
        block.dose_source[component_id].fix(dose)


def configure_relative_charge_balance(
    block: Block,
    *,
    inlet_ph: float,
    fixed_background_charge_eq_m3: float | None = None,
) -> float:
    """Activate relative charge closure without a neutral-pH fallback."""

    ph = float(inlet_ph)
    if not math.isfinite(ph) or not 0.0 <= ph <= 14.0:
        raise ValueError("inlet_ph must be finite in [0, 14]")
    if fixed_background_charge_eq_m3 is None:
        inlet = {
            component_id: float(value(block.inlet[component_id]))
            for component_id in block.aqueous_components
        }
        background = infer_fixed_background_charge_eq_m3(
            inlet,
            measured_ph=ph,
            temperature_k=float(value(block.temperature_k)),
            hydrogen_activity_coefficient=float(
                value(block.activity_coefficient["H_plus"])
            ),
        )
    else:
        background = float(fixed_background_charge_eq_m3)
        if not math.isfinite(background):
            raise ValueError("fixed background charge must be finite")
    block.fixed_background_charge_eq_m3.set_value(background)
    block.aqueous_zero_accumulation["H_plus"].deactivate()
    block.relative_charge_balance.activate()
    return background


def fecl3_source_mol_m3_d(fe_mol_m3_d: float) -> dict[str, float]:
    """Return the explicit Fe(III)+3Cl source for a molar FeCl3 dose."""

    dose = float(fe_mol_m3_d)
    if not math.isfinite(dose) or dose < 0:
        raise ValueError("FeCl3 dose must be finite and nonnegative")
    return {"Fe_III_aq": dose, "Cl_minus": 3.0 * dose}


def initialize_forward_case(block: Block) -> None:
    """Set a thermodynamically consistent pass-through initial guess.

    This does not fix outlet variables or suppress reactions. It only avoids
    presenting IPOPT with unit saturation ratios when the analytical totals
    imply values many orders of magnitude away from one.
    """

    hrt = float(block.hydraulic_residence_time_d.value)
    for component_id in block.aqueous_components:
        outlet = max(
            float(block.inlet[component_id].value or 0.0)
            + hrt * float(block.dose_source[component_id].value or 0.0),
            1e-27,
        )
        block.outlet[component_id].set_value(outlet)

    ligand = conditional_ligand_free_fractions(
        ligand_total_mol_l=float(block.outlet["Ligand_sites"].value) * 1e-3,
        fe_ii_total_mol_l=float(block.outlet["Fe_II_aq"].value) * 1e-3,
        fe_iii_total_mol_l=float(block.outlet["Fe_III_aq"].value) * 1e-3,
        beta_fe_ii_l_mol=float(block.beta_fe_ii_l_mol.value),
        beta_fe_iii_l_mol=float(block.beta_fe_iii_l_mol.value),
    )
    block.free_ligand_mol_l.set_value(ligand["free_ligand_mol_l"])

    if block.relative_charge_balance.active:
        charge = solve_relative_charge_balance(
            {
                **{
                    component_id: float(block.outlet[component_id].value)
                    for component_id in block.aqueous_components
                },
                **{
                    component_id: float(block.solid_inventory[component_id].value)
                    for component_id in block.retained_components
                },
            },
            fixed_background_charge_eq_m3=float(
                block.fixed_background_charge_eq_m3.value
            ),
            temperature_k=float(block.temperature_k.value),
            hydrogen_activity_coefficient=float(
                block.activity_coefficient["H_plus"].value
            ),
        )
        if not charge.converged or charge.hydrogen_concentration_mol_m3 is None:
            raise RuntimeError(f"relative charge initialization failed: {charge.status}")
        block.outlet["H_plus"].set_value(charge.hydrogen_concentration_mol_m3)

    # H+ is not an analytical-total state, so its free-ion fraction is one and
    # it can seed the other acid-base fractions directly.
    h_activity = max(
        float(block.activity_coefficient["H_plus"].value)
        * float(block.outlet["H_plus"].value)
        * float(block.mol_m3_to_mol_l.value),
        1e-30,
    )
    block.activity["H_plus"].set_value(h_activity)
    for component_id in block.aqueous_components:
        outlet = float(block.outlet[component_id].value)
        activity = max(
            float(block.activity_coefficient[component_id].value)
            * outlet
            * float(block.mol_m3_to_mol_l.value)
            * float(value(block.free_ion_fraction[component_id])),
            1e-30,
        )
        block.activity[component_id].set_value(activity)

    for component_id in block.retained_components:
        inventory = max(float(block.solid_inventory[component_id].value or 0.0), 0.0)
        block.solid_wasting[component_id].set_value(
            inventory / float(block.solids_retention_time_d.value)
        )

    for phase_id in block.phases:
        phase = block.phase_definition_by_id[phase_id]
        iap = 1.0
        for component_id, coefficient in phase.dissolution_reaction.items():
            if component_id in block.phases:
                continue
            if component_id == "H2O":
                iap *= float(block.solvent_water_activity.value) ** float(coefficient)
                continue
            iap *= float(block.activity[component_id].value) ** float(coefficient)
        saturation_ratio = max(
            iap / float(value(block.equilibrium_constant[phase_id])),
            1e-30,
        )
        block.saturation_index[phase_id].set_value(math.log10(saturation_ratio))
        inventory = max(float(block.solid_inventory[phase_id].value or 0.0), 0.0)
        block.surface_area[phase_id].set_value(
            float(block.area_per_inventory[phase_id].value) * inventory
            + float(block.seed_surface_area[phase_id].value)
        )


__all__ = [
    "build_shared_chemistry_steady_state_block",
    "configure_relative_charge_balance",
    "fecl3_source_mol_m3_d",
    "fix_forward_case",
    "initialize_forward_case",
]

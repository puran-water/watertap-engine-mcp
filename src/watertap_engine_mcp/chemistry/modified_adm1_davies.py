"""Reduced molal Davies activity chemistry for the house mADM1 reaction block.

Extends phase 1 with the shared speciation's metal ion pairs (``process_chemistry_reference.madm1_speciation.ION_PAIRS``,
owner ruling HOUSE-MADM1-SPECIATION-ION-PAIRS-FIRST-20261008) and no mineral reactions: each pair is an activity
equilibrium on the same molal Davies basis, its log K(T) the shared table's; the pairing metals (Ca from the carried
calcium, Mg from S_Mg) and sulfate stay at their TOTALS in the charge-equivalent lumps and take a free share by their
own mass balance; the ligands' free species exclude what the pairs hold; charge and ionic strength are corrected for
what a pair takes from a total, exactly as the shared solve writes it (``_pair_corrections``). QSDsan's thermodynamics
module uses the same Davies/A(T) laws for mineral saturation; its reactor PCM
still uses ideal fractions. Activity-based reactor equilibria therefore extend
that PCM rather than reproduce an existing QSDsan acid-base implementation.

A(T): Kell (1975), doi:10.1021/je60064a005, water density; Malmberg & Maryott
(1956), NBS RP2641, dielectric constant. Fits intersect over 0-100 C. The reduced
Davies operational envelope follows QSDsan: I <= 0.5 mol/kg water, 0-50 C.
The result flags extrapolation; neither I nor gamma is clipped to the envelope.
"""
from __future__ import annotations

import math
import pyomo.environ as pyo
from idaes.core.util.constants import Constants
from idaes.core import declare_process_block_class
import idaes.core.util.scaling as iscale
from watertap_engine_mcp.chemistry.modified_adm1_phase1 import Phase1ADM1ReactionBlockData, Phase1ADM1ReactionParameterData, Phase1ReactionScaler, _Phase1ReactionBlock
from puran_mcp_support.chemistry.madm1_speciation import ION_PAIRS, IonPair

#: The pairs a block WITHOUT iron carries (the S/Fe block adds the iron pairs: ``chemistry.modified_adm1_sfe``).
CORE_ION_PAIRS = tuple(p for p in ION_PAIRS if all(key != "Fe2" for key, _ in p.reactants))
_KEY_CHARGE = {"Ca": 2, "Mg": 2, "Fe2": 2, "NH4": 1, "SO4": -2, "HCO3": -1, "CO3": -2, "HPO4": -2, "H2PO4": -1,
               "HS": -1, "H": 1}
#: species keys stated at their TOTAL in the lumps / explicit sums (the pair corrections subtract their share)
_TOTAL_KEYS = ("Ca", "Mg", "Fe2", "SO4")
#: which ligand balance each ligand species key belongs to
_LIGAND_BALANCE = {"HCO3": "IC", "CO3": "IC", "HPO4": "IP", "H2PO4": "IP", "NH4": "IN", "HS": "IS"}
ION_PAIR_VARIABLE_SCALING = 1e5
ION_PAIR_CONSTRAINT_SCALING = 1e5


def pair_log_k_expression(pair: IonPair, temperature):
    """log10 K(T) of one pair as a Pyomo expression, exactly the shared table's law (analytic, else van't Hoff)."""
    from puran_mcp_support.chemistry.madm1_speciation import LN10, PHREEQC_R_J_MOL_K

    t = temperature / pyo.units.K
    if pair.analytic is not None:
        a1, a2, a3, a4, a5 = pair.analytic
        return a1 + a2 * t + a3 / t + a4 * pyo.log10(t) + a5 / t**2
    return pair.log_k_25 - pair.delta_h_kj_mol * 1000.0 / (LN10 * PHREEQC_R_J_MOL_K) * (1.0 / t - 1.0 / 298.15)

DAVIES_MAX_IONIC_STRENGTH_MOLAL = 0.5
DAVIES_TEMPERATURE_C = (0.0, 50.0)
ACTIVITY_VARIABLE_SCALING = {"ionic_strength_molal": 1.0, "sqrt_ionic_strength": 1.0}
ACTIVITY_CONSTRAINT_SCALING = {
    "ionic_strength_balance": 1.0, "ionic_strength_root": 1.0,
    "pH_calc": 1e8, "concentration_of_hco3": 1e3,
    "concentration_of_nh3": 1e3, "phosphate_dissociation": 1e3,
    "carbonate_dissociation": 1e3,
}


class DaviesReactionScaler(Phase1ReactionScaler):
    def variable_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().variable_scaling_routine(model, overwrite, submodel_scalers)
        for name, factor in ACTIVITY_VARIABLE_SCALING.items():
            self.set_variable_scaling_factor(getattr(model, name), factor, overwrite=overwrite)
        for var in model.log_gamma.values():
            self.set_variable_scaling_factor(var, 1.0, overwrite=overwrite)

    def constraint_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().constraint_scaling_routine(model, overwrite, submodel_scalers)
        for name, factor in ACTIVITY_CONSTRAINT_SCALING.items():
            self.set_constraint_scaling_factor(getattr(model, name), factor, overwrite=True)
        for con in model.davies_equilibrium.values():
            self.set_constraint_scaling_factor(con, 1.0, overwrite=overwrite)


class _DaviesReactionBlock(_Phase1ReactionBlock):
    default_scaler = DaviesReactionScaler


@declare_process_block_class("DaviesADM1ReactionBlock", block_class=_DaviesReactionBlock)
class DaviesADM1ReactionBlockData(Phase1ADM1ReactionBlockData):
    def _rxn_rate(self):
        super()._rxn_rate()
        molar = pyo.units.kmol / pyo.units.m**3
        molal = pyo.units.mol / pyo.units.kg
        mw = pyo.units.kg / pyo.units.kmol
        # This is the actual native water inventory, not solution density or a
        # silently assumed equality between molarity and molality (rho_w=997).
        self.molar_to_molal = pyo.Expression(expr=(
            1000 * pyo.units.mol / pyo.units.kmol / self.state_ref.params.dens_mass))
        self.calcium_molar_flow = pyo.Param(initialize=0.0, mutable=True, within=pyo.NonNegativeReals,
                                          units=pyo.units.kmol / pyo.units.s)
        self.sulfate_molar_flow = pyo.Param(initialize=0.0, mutable=True, within=pyo.NonNegativeReals,
                                          units=pyo.units.kmol / pyo.units.s)
        self.calcium_mol_l = pyo.Expression(expr=self.calcium_molar_flow / self.state_ref.flow_vol)
        self.sulfate_mol_l = pyo.Expression(expr=self.sulfate_molar_flow / self.state_ref.flow_vol)
        self.ionic_strength_molal = pyo.Var(initialize=0.2, domain=pyo.NonNegativeReals, units=molal)
        self.sqrt_ionic_strength = pyo.Var(initialize=math.sqrt(0.2), domain=pyo.NonNegativeReals,
                                          units=pyo.units.dimensionless)
        self.log_gamma = pyo.Var((1, 2, 3), initialize=lambda b, z: -0.13 * z*z,
                                domain=pyo.Reals, units=pyo.units.dimensionless)
        self.gamma = pyo.Expression((1, 2, 3), rule=lambda b, z: 10 ** b.log_gamma[z])
        # A separate root variable avoids differentiating sqrt(I) at a trial I=0.
        # Water dissociation keeps physical I positive; no concentration floor.
        self.ionic_strength_root = pyo.Constraint(expr=(
            self.sqrt_ionic_strength**2 == self.ionic_strength_molal / molal))
        tc = self.temperature / pyo.units.K - 273.15
        self.water_density_kg_l = pyo.Expression(expr=(
            999.83952 + 16.945176 * tc - 7.9870401e-3 * tc**2
            - 46.170461e-6 * tc**3 + 105.56302e-9 * tc**4 - 280.54253e-12 * tc**5
        ) / (1 + 16.879850e-3 * tc) / 1000)
        self.water_dielectric = pyo.Expression(expr=(
            87.740 - 0.40008 * tc + 9.398e-4 * tc**2 - 1.410e-6 * tc**3))
        self.debye_huckel_A = pyo.Expression(expr=(
            1.82483e6 * pyo.sqrt(self.water_density_kg_l)
            / (self.water_dielectric * self.temperature / pyo.units.K)**1.5))
        self.davies_equilibrium = pyo.Constraint((1, 2, 3), rule=lambda b, z: (
            b.log_gamma[z] == -b.debye_huckel_A * z**2 * (
                b.sqrt_ionic_strength / (1 + b.sqrt_ionic_strength)
                - 0.3 * b.ionic_strength_molal / molal)))
        self.conc_mol_oh = pyo.Expression(expr=(
            10 ** (self.pH - self.pKW - self.log_gamma[1]) * molal / self.molar_to_molal))
        # Lumps carry equivalents: Na + 2 Ca and Cl + 2 SO4. Add (z²-|z|)c
        # for known divalents. Na is already in the lump; K/Mg are separate.
        self.ionic_strength_balance = pyo.Constraint(expr=(
            self.ionic_strength_molal == 0.5 * self.molar_to_molal * (
                self.state_ref.cations + self.state_ref.anions
                + 2 * self.calcium_mol_l + 2 * self.sulfate_mol_l
                + self.conc_mol_K + 4 * self.conc_mol_Mg
                + self.S_H + self.conc_mol_oh + self.conc_mol_nh4
                + self.conc_mol_hco3 + 4 * self.conc_mol_co3
                + self.conc_mol_h2po4 + 4 * self.conc_mol_hpo4
                + self.conc_mass_ac / (64 * mw) + self.conc_mass_pro / (112 * mw)
                + self.conc_mass_bu / (160 * mw) + self.conc_mass_va / (208 * mw))))
        # Exponentiated log equilibria remain defined at exactly zero acid totals.
        # pH is -log10(a_H); S_H remains a concentration for electroneutrality.
        self.pH_calc.set_value(self.S_H == (
            10 ** (-self.pH - self.log_gamma[1]) * molal / self.molar_to_molal))
        self.concentration_of_hco3.set_value(self.conc_mol_hco3 == (
            self.conc_mol_co2 * 10 ** (self.pH - self.pK_a_co2 - self.log_gamma[1])))
        self.carbonate_dissociation.set_value(self.conc_mol_co3 == (
            self.conc_mol_hco3 * 10 ** (self.pH - self.pK_a_co3 + self.log_gamma[1] - self.log_gamma[2])))
        self.phosphate_dissociation.set_value(self.conc_mol_hpo4 == (
            self.conc_mol_h2po4 * 10 ** (self.pH - self.pK_a_IP + self.log_gamma[1] - self.log_gamma[2])))
        self.concentration_of_nh3.set_value(self.conc_mol_nh3 == (
            self.conc_mol_nh4 * 10 ** (self.pH - self.pK_a_IN + self.log_gamma[1])))
        for acid in ("ac", "pro", "bu", "va"):
            ion = getattr(self, "conc_mass_" + acid)
            ka = getattr(self.params, "K_a_" + acid) / molar
            getattr(self, "concentration_of_" + acid).set_value(
                ion * (1 + 10 ** (-self.pH + self.log_gamma[1]) / ka)
                == self.conc_mass_comp_ref["S_" + acid])
        self.S_H_cons.set_value(
            self.state_ref.cations + self.conc_mol_nh4 + 2 * self.conc_mol_Mg + self.conc_mol_K + self.S_H
            - self.conc_mol_hco3 - 2 * self.conc_mol_co3 - self.conc_mol_h2po4 - 2 * self.conc_mol_hpo4
            - self.conc_mass_ac / (64 * mw) - self.conc_mass_pro / (112 * mw)
            - self.conc_mass_bu / (160 * mw) - self.conc_mass_va / (208 * mw)
            - self.conc_mol_oh - self.state_ref.anions == 0)
        if self._builds_ion_pairs_here():
            self._declare_ion_pairs(CORE_ION_PAIRS)
            self._ion_pair_constraints(CORE_ION_PAIRS, totals={"Ca": self.calcium_mol_l, "Mg": self.conc_mol_Mg,
                                                               "SO4": self.sulfate_mol_l})

    def _builds_ion_pairs_here(self) -> bool:
        """The S/Fe block builds ALL its pairs (iron included) once its sulfate, iron and sulfide exist."""
        return True

    def _declare_ion_pairs(self, pairs) -> None:
        molar = pyo.units.kmol / pyo.units.m**3
        self.ion_pair_set = pyo.Set(initialize=[p.name for p in pairs], ordered=True)
        self.conc_mol_pair = pyo.Var(self.ion_pair_set, initialize=1e-8, domain=pyo.NonNegativeReals, units=molar)
        self.conc_mol_free = pyo.Var([k for k in _TOTAL_KEYS if any(k == r for p in pairs for r, _ in p.reactants)],
                                     initialize=1e-4, domain=pyo.NonNegativeReals, units=molar)
        self._ion_pairs = {p.name: p for p in pairs}

    def _ion_pair_constraints(self, pairs, *, totals) -> None:
        """The pair equilibria, the metal/sulfate balances, the ligand balances net of the pairs, and the charge and
        ionic-strength corrections (the shared solve's ``_pair_corrections``)."""
        molal = pyo.units.mol / pyo.units.kg
        mw = pyo.units.kg / pyo.units.kmol
        conc = {"NH4": self.conc_mol_nh4, "HCO3": self.conc_mol_hco3, "CO3": self.conc_mol_co3,
                "HPO4": self.conc_mol_hpo4, "H2PO4": self.conc_mol_h2po4, **self._extra_ligand_conc(),
                **{k: self.conc_mol_free[k] for k in self.conc_mol_free}}

        def gamma(z):
            return 1.0 if z == 0 else 10 ** self.log_gamma[abs(z)]

        def activity(key):
            if key == "H":
                return 10 ** (-self.pH)
            return gamma(_KEY_CHARGE[key]) * conc[key] * self.molar_to_molal / molal

        self.ion_pair_log_k = pyo.Expression(self.ion_pair_set, rule=lambda b, name: pair_log_k_expression(
            self._ion_pairs[name], self.temperature))

        def equilibrium(b, name):
            p = self._ion_pairs[name]
            rhs = 10 ** b.ion_pair_log_k[name]
            for key, n in p.reactants:
                rhs = rhs * activity(key) ** n
            return gamma(p.charge) * b.conc_mol_pair[name] * b.molar_to_molal / molal == rhs
        self.ion_pair_equilibrium = pyo.Constraint(self.ion_pair_set, rule=equilibrium)

        def held(total_key):
            return sum(n * self.conc_mol_pair[p.name] for p in pairs for key, n in p.reactants
                       if key == total_key or _LIGAND_BALANCE.get(key) == total_key)
        self.ion_pair_metal_balance = pyo.Constraint(list(self.conc_mol_free.keys()), rule=lambda b, k: (
            totals[k] == b.conc_mol_free[k] + held(k)))
        # the ligand balances, net of the pairs
        self.concentration_of_co2.set_value(
            self.conc_mol_co2 == self.conc_mass_comp_ref["S_IC"] / (12 * mw)
            - self.conc_mol_hco3 - self.conc_mol_co3 - held("IC"))
        self.phosphate_mass_balance.set_value(
            self.conc_mol_h2po4 + self.conc_mol_hpo4 + held("IP") == self.conc_mass_comp_ref["S_IP"] / (31 * mw))
        self.concentration_of_nh4.set_value(
            self.conc_mol_nh4 == self.conc_mass_comp_ref["S_IN"] / (14 * mw) - self.conc_mol_nh3 - held("IN"))
        self._ligand_balances_net_of_pairs(held)
        charge = sum((p.charge - sum(n * _KEY_CHARGE[key] for key, n in p.reactants if key in _TOTAL_KEYS))
                     * self.conc_mol_pair[p.name] for p in pairs)
        ionic = sum((p.charge ** 2 - sum(n * _KEY_CHARGE[key] ** 2 for key, n in p.reactants if key in _TOTAL_KEYS))
                    * self.conc_mol_pair[p.name] for p in pairs)
        balance = self.S_H_cons.expr.args[0]
        self.S_H_cons.set_value(balance + charge == 0)
        lhs, rhs = self.ionic_strength_balance.expr.args
        self.ionic_strength_balance.set_value(lhs == rhs + 0.5 * self.molar_to_molal * ionic)

    def _extra_ligand_conc(self) -> dict:
        """Ligand species a subclass adds (the S/Fe block's HS-)."""
        return {}

    def _ligand_balances_net_of_pairs(self, held) -> None:
        """Ligand balances a subclass carries (the S/Fe block's sulfide)."""

    def calculate_scaling_factors(self):
        super().calculate_scaling_factors()
        if hasattr(self, "conc_mol_pair"):
            for var in list(self.conc_mol_pair.values()) + list(self.conc_mol_free.values()):
                iscale.set_scaling_factor(var, ION_PAIR_VARIABLE_SCALING)
            for con in self.ion_pair_equilibrium.values():
                iscale.constraint_scaling_transform(con, ION_PAIR_CONSTRAINT_SCALING)
            for con in self.ion_pair_metal_balance.values():
                iscale.constraint_scaling_transform(con, ION_PAIR_CONSTRAINT_SCALING)
        for name, factor in ACTIVITY_VARIABLE_SCALING.items():
            iscale.set_scaling_factor(getattr(self, name), factor)
        for var in self.log_gamma.values():
            iscale.set_scaling_factor(var, 1.0)
        for name, factor in ACTIVITY_CONSTRAINT_SCALING.items():
            iscale.constraint_scaling_transform(getattr(self, name), factor, overwrite=True)
        for con in self.davies_equilibrium.values():
            iscale.constraint_scaling_transform(con, 1.0)

    def activity_report(self):
        """Validity and basis travel with numerical results; never clamp I."""
        strength = float(pyo.value(self.ionic_strength_molal))
        temperature_c = float(pyo.value(self.temperature)) - 273.15
        in_domain = (math.isfinite(strength) and 0 <= strength <= DAVIES_MAX_IONIC_STRENGTH_MOLAL
                     and DAVIES_TEMPERATURE_C[0] <= temperature_c <= DAVIES_TEMPERATURE_C[1])
        return {
            "model": ("davies_molal_acid_base_ion_pairs_v1" if hasattr(self, "_ion_pairs") else "davies_molal_acid_base_v1"),
            "activity_model_in_domain": in_domain,
            "status": "in_domain" if in_domain else "outside_declared_davies_domain",
            "ionic_strength_mol_kg_water": strength,
            "ionic_strength_mol_l": strength / float(pyo.value(self.molar_to_molal)),
            "maximum_ionic_strength_mol_kg_water": DAVIES_MAX_IONIC_STRENGTH_MOLAL,
            "temperature_c": temperature_c, "temperature_range_c": list(DAVIES_TEMPERATURE_C),
            "A_temperature_fit_range_c": [0.0, 100.0],
            "A": float(pyo.value(self.debye_huckel_A)),
            "gammas": {str(z): float(pyo.value(self.gamma[z])) for z in (1, 2, 3)},
            "pH_basis": "-log10(gamma_H * mol_H_per_kg_native_water / (1 mol/kg))",
            "water_basis": "native property-package H2O inventory, not solution density",
            "water_inventory_kg_m3": float(pyo.value(self.state_ref.params.dens_mass)),
            "strong_ion_basis": "charge-equivalent lumps; explicit Ca/SO4 divalent corrections; remaining lumps monovalent",
            "calcium_mol_l": float(pyo.value(self.calcium_mol_l)),
            "sulfate_mol_l": float(pyo.value(self.sulfate_mol_l)),
            "ion_pairs": sorted(self._ion_pairs) if hasattr(self, "_ion_pairs") else False,
            # the pairs and the free pairing totals they leave (mol/L), so a consumer can close I and charge
            "ion_pair_mol_l": ({name: float(pyo.value(v)) for name, v in self.conc_mol_pair.items()}
                               if hasattr(self, "conc_mol_pair") else {}),
            "free_pairing_total_mol_l": ({k: float(pyo.value(v)) for k, v in self.conc_mol_free.items()}
                                         if hasattr(self, "conc_mol_free") else {}),
            "mineral_equilibria": False, "neutral_gamma": 1.0,
            "trivalent_scope": "gamma_3 reported; no explicit trivalent species in this reaction set",
        }


@declare_process_block_class("DaviesADM1ReactionParameterBlock")
class DaviesADM1ReactionParameterData(Phase1ADM1ReactionParameterData):
    def build(self):
        super().build()
        self._reaction_block_class = DaviesADM1ReactionBlock

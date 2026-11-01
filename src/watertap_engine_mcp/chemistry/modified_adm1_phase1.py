'House mADM1 phase 1: ideal phosphate/carbonate charge and sodium inhibition.'
from __future__ import annotations

import pyomo.environ as pyo
from idaes.core import declare_process_block_class
from idaes.core.util.constants import Constants
import idaes.core.util.scaling as iscale
from watertap.property_models.unit_specific.anaerobic_digestion.modified_adm1_reactions import (
    ModifiedADM1ReactionParameterData, ModifiedADM1ReactionBlockData,
    ModifiedADM1ReactionScaler, _ModifiedADM1ReactionBlock,
)

NA_INHIBITION_SOURCE = "QSDsan house KI_Na=0.3 mol/L; Chen et al. 2008, doi:10.1016/j.biortech.2007.01.057"
NEW_VARIABLE_SCALING = {"pK_a_IP": 1.0, "pK_a_co3": 1.0,
                        "conc_mol_h2po4": 1e3, "conc_mol_hpo4": 1e3, "conc_mol_co3": 1e3}
NEW_CONSTRAINT_SCALING = {"IP_acid_base_equilibrium": 1.0, "CO3_acid_base_equilibrium": 1.0,
                          "phosphate_mass_balance": 1e3, "phosphate_dissociation": 1e3,
                          "carbonate_dissociation": 1e3}


class Phase1ReactionScaler(ModifiedADM1ReactionScaler):
    def variable_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().variable_scaling_routine(model, overwrite, submodel_scalers)
        for name, factor in NEW_VARIABLE_SCALING.items():
            self.set_variable_scaling_factor(getattr(model, name), factor, overwrite=overwrite)

    def constraint_scaling_routine(self, model, overwrite=False, submodel_scalers=None):
        super().constraint_scaling_routine(model, overwrite, submodel_scalers)
        for name, factor in NEW_CONSTRAINT_SCALING.items():
            self.set_constraint_scaling_factor(getattr(model, name), factor, overwrite=overwrite)


class _Phase1ReactionBlock(_ModifiedADM1ReactionBlock):
    default_scaler = Phase1ReactionScaler


@declare_process_block_class("Phase1ADM1ReactionBlock", block_class=_Phase1ReactionBlock)
class Phase1ADM1ReactionBlockData(ModifiedADM1ReactionBlockData):
    def _rxn_rate(self):
        super()._rxn_rate()
        molar = pyo.units.kmol / pyo.units.m**3
        mw = pyo.units.kg / pyo.units.kmol
        self.pK_a_IP = pyo.Var(initialize=7.20, units=pyo.units.dimensionless)
        self.pK_a_co3 = pyo.Var(initialize=10.329, units=pyo.units.dimensionless)
        self.conc_mol_h2po4 = pyo.Var(initialize=1e-3, domain=pyo.NonNegativeReals, units=molar)
        self.conc_mol_hpo4 = pyo.Var(initialize=1e-3, domain=pyo.NonNegativeReals, units=molar)
        self.conc_mol_co3 = pyo.Var(initialize=1e-4, domain=pyo.NonNegativeReals, units=molar)
        correction = 1 / self.params.temperature_ref - 1 / self.temperature
        self.IP_acid_base_equilibrium = pyo.Constraint(expr=(
            -pyo.log(10.0) * self.pK_a_IP == -pyo.log(10.0) * 7.20
            + 3600 * pyo.units.J / pyo.units.mol / Constants.gas_constant * correction))
        self.CO3_acid_base_equilibrium = pyo.Constraint(expr=(
            -pyo.log(10.0) * self.pK_a_co3 == -pyo.log(10.0) * 10.329
            + 14899.224 * pyo.units.J / pyo.units.mol / Constants.gas_constant * correction))
        self.phosphate_mass_balance = pyo.Constraint(expr=(
            self.conc_mol_h2po4 + self.conc_mol_hpo4 == self.conc_mass_comp_ref["S_IP"] / (31 * mw)))
        # Log-equilibrium ratios expressed as smooth products: defined at EXACTLY
        # zero P/C too, without an artificial minimum concentration or log(0).
        self.phosphate_dissociation = pyo.Constraint(expr=(
            self.conc_mol_hpo4 == self.conc_mol_h2po4 * 10 ** (self.pH - self.pK_a_IP)))
        self.carbonate_dissociation = pyo.Constraint(expr=(
            self.conc_mol_co3 == self.conc_mol_hco3 * 10 ** (self.pH - self.pK_a_co3)))
        self.concentration_of_co2.set_value(
            self.conc_mol_co2 == self.conc_mass_comp_ref["S_IC"] / (12 * mw)
            - self.conc_mol_hco3 - self.conc_mol_co3)
        self.concentration_of_K.set_value(self.conc_mol_K == self.conc_mass_comp_ref["S_K"] / (39.1 * mw))
        self.concentration_of_Mg.set_value(self.conc_mol_Mg == self.conc_mass_comp_ref["S_Mg"] / (24.3 * mw))
        self.S_H_cons.set_value(
            self.state_ref.cations + self.conc_mol_nh4 + 2 * self.conc_mol_Mg + self.conc_mol_K + self.S_H
            - self.conc_mol_hco3 - 2 * self.conc_mol_co3 - self.conc_mol_h2po4 - 2 * self.conc_mol_hpo4
            - self.conc_mass_ac / (64 * mw) - self.conc_mass_pro / (112 * mw)
            - self.conc_mass_bu / (160 * mw) - self.conc_mass_va / (208 * mw)
            - 10 ** (self.pH - self.pKW) * molar - self.state_ref.anions == 0)
        # Na remains in the strong-ion lump for CHARGE. Its separate conserved
        # molar flow is used only for inhibition, so there is no second charge.
        self.sodium_molar_flow = pyo.Param(initialize=0.0, mutable=True, within=pyo.NonNegativeReals,
                                         units=pyo.units.kmol / pyo.units.s)
        self.K_I_Na = pyo.Param(initialize=0.3, units=molar)
        self.sodium_mol_l = pyo.Expression(expr=self.sodium_molar_flow / self.state_ref.flow_vol)
        self.I_Na = pyo.Expression(expr=self.K_I_Na / (self.K_I_Na + self.sodium_mol_l))
        self.I_fun["R10"].set_value(self.I["R10"] == pyo.exp(self.I_pH_ac) * self.I_IN_lim
                                   * self.I_nh3 * self.I_IP_lim * self.I_Na)
        self.I_fun["R11"].set_value(self.I["R11"] == pyo.exp(self.I_pH_h2) * self.I_IN_lim
                                   * self.I_IP_lim * self.I_Na)

    def calculate_scaling_factors(self):
        super().calculate_scaling_factors()
        for name, factor in NEW_VARIABLE_SCALING.items():
            iscale.set_scaling_factor(getattr(self, name), factor)
        for name, factor in NEW_CONSTRAINT_SCALING.items():
            iscale.constraint_scaling_transform(getattr(self, name), factor)
        iscale.constraint_scaling_transform(self.S_H_cons, 1e2)


@declare_process_block_class("Phase1ADM1ReactionParameterBlock")
class Phase1ADM1ReactionParameterData(ModifiedADM1ReactionParameterData):
    def build(self):
        super().build()
        self._reaction_block_class = Phase1ADM1ReactionBlock

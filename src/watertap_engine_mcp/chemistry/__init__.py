"""Shared chemistry adapters for WaterTAP steady-state models."""

from .capability import shared_mineral_chemistry_capability
from .pyomo_compiler import compile_pyomo
from .serial_steady import solve_serial_history_steady_state
from .shared_kernel import DEFAULT_MADM1_FE_S_P_ADAPTER, WaterTAPChemistryAdapter
from .steady_solver import (
    set_kinetic_profile,
    solve_commercial_fecl3_product_screen,
    solve_fixed_dose_screen,
    solve_fixed_dose_uncertainty_screen,
)
from .steady_state import (
    build_shared_chemistry_steady_state_block,
    configure_relative_charge_balance,
    fecl3_source_mol_m3_d,
    fix_forward_case,
    initialize_forward_case,
)

__all__ = [
    "DEFAULT_MADM1_FE_S_P_ADAPTER",
    "WaterTAPChemistryAdapter",
    "build_shared_chemistry_steady_state_block",
    "configure_relative_charge_balance",
    "compile_pyomo",
    "fecl3_source_mol_m3_d",
    "fix_forward_case",
    "initialize_forward_case",
    "set_kinetic_profile",
    "shared_mineral_chemistry_capability",
    "solve_commercial_fecl3_product_screen",
    "solve_fixed_dose_screen",
    "solve_fixed_dose_uncertainty_screen",
    "solve_serial_history_steady_state",
]

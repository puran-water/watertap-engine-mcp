"""WaterTAP steady-state adapter for the shared Fe-S-P kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from puran_mcp_support.schema import MADM1_FECL3_FE_S_P_KERNEL, ChemistryKernelDefinition, validate_kernel_definition

from .pyomo_compiler import compile_pyomo


@dataclass(frozen=True)
class WaterTAPChemistryAdapter:
    kernel: ChemistryKernelDefinition = MADM1_FECL3_FE_S_P_KERNEL

    @property
    def expression_hash(self) -> str:
        return self.kernel.sha256

    @property
    def recommendation_capable(self) -> bool:
        return self.kernel.recommendation_capable

    def validate(self) -> None:
        validate_kernel_definition(self.kernel)

    def compile_kinetic_rates(self, symbols: Mapping[str, Any]) -> dict[str, Any]:
        return {
            kinetic.kinetic_id: compile_pyomo(kinetic.expression, symbols)
            for kinetic in self.kernel.kinetics
        }


DEFAULT_MADM1_FE_S_P_ADAPTER = WaterTAPChemistryAdapter()


__all__ = ["DEFAULT_MADM1_FE_S_P_ADAPTER", "WaterTAPChemistryAdapter"]

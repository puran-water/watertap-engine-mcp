"""Pydantic API schemas for typed MCP tool responses."""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from puran_mcp_support.engineering.pydantic_base import StrictFiniteBaseModel

from .errors import ApiErrorEnvelope

JsonDict = dict[str, Any]


class ApiModel(StrictFiniteBaseModel):
    """Base model for API payloads."""

    model_config = ConfigDict(extra="forbid")


class UnitSpecRequest(ApiModel):
    unit_type: str


class UnitConfigSchemaRequest(ApiModel):
    unit_type: str


class UnitRequirementsRequest(ApiModel):
    unit_type: str
    property_package: Optional[str] = None
    property_package_config: Optional[JsonDict] = None
    config: Optional[JsonDict] = None


class ROAreaFixedSpec(ApiModel):
    mode: Literal["area_fixed"]
    area_m2: float = Field(gt=0)


class RORecoveryFixedSpec(ApiModel):
    mode: Literal["recovery_fixed"]
    recovery: float = Field(gt=0, lt=1)


ROSpecSet = Annotated[
    Union[ROAreaFixedSpec, RORecoveryFixedSpec],
    Field(discriminator="mode"),
]


class SetFeedStateInput(ApiModel):
    session_id: str
    unit_id: str
    flow_vol_m3_hr: Optional[float] = None
    tds_mg_L: Optional[float] = None
    nacl_mg_L: Optional[float] = None
    components: Optional[dict[str, float]] = None
    
    temperature_C: Optional[float] = Field(default=None, gt=-23.15)
    pressure_bar: float = Field(default=1.0, gt=0)
    concentration_units: str = "mg/L"
    concentration_basis: str = "mass"
    component_charges: Optional[dict[str, int]] = None
    electroneutrality_species: Optional[str] = None
    phase_mass_flow_kg_s: Optional[dict[str, float]] = None
    component_mass_flow_kg_s: Optional[dict[str, float]] = None
    component_mol_flow_mol_s: Optional[dict[str, float]] = None
    H2O_mass_kg_s: Optional[float] = None
    NaCl_mass_kg_s: Optional[float] = None
    temperature_K: Optional[float] = Field(default=None, gt=250)
    pressure_Pa: Optional[float] = Field(default=None, gt=0)


class CreateUnitInput(ApiModel):
    session_id: str
    unit_id: str
    unit_type: str
    config: Optional[JsonDict] = None
    engineering_inputs: Optional[dict[str, float]] = None
    role: Optional[str] = None
    role_stage_index: Optional[int] = None
    role_group: Optional[str] = None


class SetUnitEngineeringInputsInput(ApiModel):
    session_id: str
    unit_id: str
    inputs: dict[str, float]


class SetScalingFactorInput(ApiModel):
    session_id: str
    unit_id: str
    var_name: str
    factor: float


class EngineeringInput(ApiModel):
    key: str
    target_variable: str
    description: str
    units: Optional[str] = Field(...)
    kind: str
    typical_min: Optional[float] = Field(...)
    typical_max: Optional[float] = Field(...)
    typical_default: Optional[float] = Field(...)
    indexed: bool
    satisfaction_mode: str


class InputContract(ApiModel):
    mode: str
    complete: bool
    requires_agent_inputs: bool
    engineering_input_count: int
    auto_fixed_variable_count: int
    reason: str
    next_step: str


class AutoFixedVariable(ApiModel):
    name: str
    value: Any = Field(...)
    use_existing_value: bool = False
    description: Optional[str] = Field(...)


class UnitSupportProfile(ApiModel):
    name: str
    description: str
    property_package: Optional[str] = Field(...)
    require_session_default_package: bool = False
    unit_config_defaults: JsonDict = Field(default_factory=dict)
    unit_config_constraints: JsonDict = Field(default_factory=dict)
    allowed_config_keys: list[str] = Field(default_factory=list)
    required_session_package_config: JsonDict = Field(default_factory=dict)
    required_solutes: list[str] = Field(default_factory=list)
    session_package_config_constraints: JsonDict = Field(default_factory=dict)
    package_config_normalizer: Optional[str] = Field(...)
    engineering_inputs: list[EngineeringInput] = Field(default_factory=list)
    auto_fixed_variables: list[AutoFixedVariable] = Field(default_factory=list)


class UnitSpecData(ApiModel):
    unit_type: str
    class_name: str
    module_path: str
    category: str
    compatible_property_packages: list[str]
    engineering_inputs: list[EngineeringInput]
    input_contract: InputContract
    typical_values: JsonDict = Field(default_factory=dict)
    n_inlets: int
    n_outlets: int
    inlet_names: list[str] = Field(default_factory=list)
    outlet_names: list[str] = Field(default_factory=list)
    port_roles: JsonDict = Field(default_factory=dict)
    config_schema: JsonDict = Field(default_factory=dict)
    is_idaes_unit: bool
    description: str
    support_profiles: list[UnitSupportProfile] = Field(default_factory=list)
    solve_supported: bool
    calculation_class: str
    recommendation_eligible: bool
    costing_supported: bool
    costing_supported_packages: list[str] = Field(default_factory=list)
    costing_source: str


class UnitConfigSchemaData(ApiModel):
    unit_type: str
    module_path: str
    class_name: str
    config_schema: JsonDict = Field(default_factory=dict)


class UnitEngineeringRequirements(ApiModel):
    engineering_inputs: list[EngineeringInput] = Field(default_factory=list)
    input_contract: InputContract
    typical_values: JsonDict = Field(default_factory=dict)


class UnitPorts(ApiModel):
    inlets: int
    outlets: int
    inlet_names: list[str] = Field(default_factory=list)
    outlet_names: list[str] = Field(default_factory=list)


class UnitRequirementsData(ApiModel):
    unit_type: str
    category: Optional[str] = Field(...)
    engineering_requirements: UnitEngineeringRequirements
    ports: UnitPorts
    compatible_property_packages: list[str]
    config_schema: JsonDict = Field(default_factory=dict)
    is_idaes_unit: bool
    module_path: str
    live_preview: Optional[JsonDict] = None


DataT = TypeVar("DataT")


class ApiToolResponse(StrictFiniteBaseModel, Generic[DataT]):
    """Success/error envelope for typed tool adapters."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Optional[DataT] = None
    error: Optional[ApiErrorEnvelope] = None

    @model_validator(mode="after")
    def _validate_envelope(self) -> "ApiToolResponse[DataT]":
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("successful responses require data and no error")
        elif self.error is None or self.data is not None:
            raise ValueError("error responses require error and no data")
        return self


class UnitSpecResponse(ApiToolResponse[UnitSpecData]):
    ok: Literal[True, False]


class UnitRequirementsResponse(ApiToolResponse[UnitRequirementsData]):
    ok: Literal[True, False]


class UnitConfigSchemaResponse(ApiToolResponse[UnitConfigSchemaData]):
    ok: Literal[True, False]


UNIT_INTROSPECTION_SCHEMAS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "get_unit_spec": (UnitSpecRequest, UnitSpecResponse),
    "get_unit_requirements": (UnitRequirementsRequest, UnitRequirementsResponse),
    "get_unit_config_schema": (UnitConfigSchemaRequest, UnitConfigSchemaResponse),
}

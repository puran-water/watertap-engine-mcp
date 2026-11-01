"""Engineering readiness contracts for live WaterTAP sessions."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ReadinessModel(BaseModel):
    """Base model for readiness payloads."""

    model_config = ConfigDict(extra="forbid")


class ConfigValidStage(ReadinessModel):
    stage: Literal["config_valid"] = "config_valid"
    status: Literal["passed", "failed"]
    issues: list[str] = Field(default_factory=list)


class ModelBuiltStage(ReadinessModel):
    stage: Literal["model_built"] = "model_built"
    status: Literal["passed", "failed"]
    issues: list[str] = Field(default_factory=list)


class DofClosedStage(ReadinessModel):
    stage: Literal["dof_closed"] = "dof_closed"
    status: Literal["passed", "failed"]
    total_dof: Optional[int] = None


class InitializedStage(ReadinessModel):
    stage: Literal["initialized"] = "initialized"
    status: Literal["not_started", "passed", "failed"] = "not_started"
    message: str = "Initialization is owned by solve()."


class SolveAdmissibleStage(ReadinessModel):
    stage: Literal["solve_admissible"] = "solve_admissible"
    status: Literal["passed", "failed"]
    issues: list[str] = Field(default_factory=list)
    issue_codes: list[str] = Field(default_factory=list)


ReadinessStage = Annotated[
    Union[
        ConfigValidStage,
        ModelBuiltStage,
        DofClosedStage,
        InitializedStage,
        SolveAdmissibleStage,
    ],
    Field(discriminator="stage"),
]


class UnitSpecSetStatus(ReadinessModel):
    unit_id: str
    unit_type: str
    contract: Literal["ro_membrane_spec_set"]
    valid: bool
    mode: Optional[Literal["area_fixed", "recovery_fixed"]] = None
    provided_modes: list[str] = Field(default_factory=list)
    issue: Optional[str] = None
    target_variables: list[str] = Field(default_factory=list)


class EngineeringReadinessReport(ReadinessModel):
    session_id: str
    ready: bool
    status: str
    stages: list[ReadinessStage]
    unit_spec_sets: dict[str, UnitSpecSetStatus] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    issue_codes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _normalized_keys(data: Any) -> set[str]:
    if not isinstance(data, dict):
        return set()
    return {str(key).replace(" ", "") for key in data}


def _has_session_value(unit_inst: Any, keys: set[str]) -> bool:
    fixed = _normalized_keys(getattr(unit_inst, "fixed_vars", {}))
    engineering = _normalized_keys(getattr(unit_inst, "engineering_inputs", {}))
    return bool(keys & fixed) or bool(keys & engineering)


def _component_fixed(builder: Any, unit_block: Any, path: str) -> bool:
    if builder is None or unit_block is None:
        return False
    try:
        target, index = builder._resolve_variable_path(unit_block, path)
    except Exception:
        return False
    if target is None:
        return False
    try:
        component = target[index] if index is not None else target
    except Exception:
        return False
    return bool(getattr(component, "fixed", False))


def evaluate_unit_spec_set(
    *,
    unit_id: str,
    unit_inst: Any,
    builder: Any | None = None,
    unit_block: Any | None = None,
) -> UnitSpecSetStatus | None:
    """Return the spec-set status for units with multiple valid spec modes."""

    unit_type = getattr(unit_inst, "unit_type", "")
    if unit_type != "ReverseOsmosis0D":
        return None

    area_keys = {"area", "area_m2"}
    recovery_keys = {
        "recovery",
        "recovery_vol_phase[0,Liq]",
        "recovery_vol_phase[0.0,Liq]",
    }
    area_set = _has_session_value(unit_inst, area_keys) or _component_fixed(
        builder,
        unit_block,
        "area",
    )
    recovery_set = _has_session_value(unit_inst, recovery_keys) or _component_fixed(
        builder,
        unit_block,
        "recovery_vol_phase[0,Liq]",
    )

    provided = []
    targets = []
    if area_set:
        provided.append("area_fixed")
        targets.append("area")
    if recovery_set:
        provided.append("recovery_fixed")
        targets.append("recovery_vol_phase[0,Liq]")

    if len(provided) == 1:
        return UnitSpecSetStatus(
            unit_id=unit_id,
            unit_type=unit_type,
            contract="ro_membrane_spec_set",
            valid=True,
            mode=provided[0],
            provided_modes=provided,
            target_variables=targets,
        )

    if not provided:
        issue = "ReverseOsmosis0D requires exactly one membrane spec mode: area_fixed or recovery_fixed."
    else:
        issue = "ReverseOsmosis0D received both area_fixed and recovery_fixed; choose exactly one."

    return UnitSpecSetStatus(
        unit_id=unit_id,
        unit_type=unit_type,
        contract="ro_membrane_spec_set",
        valid=False,
        provided_modes=provided,
        issue=issue,
        target_variables=targets,
    )


def build_readiness_report(
    *,
    session_id: str,
    status: str,
    build_success: bool,
    total_dof: int | None,
    feed_units_without_state: list[str],
    unit_spec_sets: dict[str, UnitSpecSetStatus],
    issues: list[str],
    warnings: list[str],
    issue_codes: list[str] | None = None,
) -> EngineeringReadinessReport:
    """Build the staged engineering-readiness contract."""

    spec_issues = [
        spec.issue
        for spec in unit_spec_sets.values()
        if not spec.valid and spec.issue
    ]
    codes = list(issue_codes or [])
    all_issues = list(issues)
    config_ok = not spec_issues
    dof_ok = build_success and total_dof == 0
    solve_issues = list(all_issues)
    if feed_units_without_state:
        solve_issues.append(
            "Feed state missing for: " + ", ".join(feed_units_without_state)
        )
    solve_issues.extend(spec_issues)
    solve_ok = config_ok and build_success and dof_ok and not solve_issues

    stages: list[ReadinessStage] = [
        ConfigValidStage(
            status="passed" if config_ok else "failed",
            issues=spec_issues,
        ),
        ModelBuiltStage(
            status="passed" if build_success else "failed",
            issues=[] if build_success else all_issues,
        ),
        DofClosedStage(
            status="passed" if dof_ok else "failed",
            total_dof=total_dof,
        ),
        InitializedStage(),
        SolveAdmissibleStage(
            status="passed" if solve_ok else "failed",
            issues=solve_issues,
            issue_codes=codes,
        ),
    ]
    return EngineeringReadinessReport(
        session_id=session_id,
        ready=solve_ok,
        status=status,
        stages=stages,
        unit_spec_sets=unit_spec_sets,
        issues=all_issues,
        issue_codes=codes,
        warnings=list(warnings),
    )

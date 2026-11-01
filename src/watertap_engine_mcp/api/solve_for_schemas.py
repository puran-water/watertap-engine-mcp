"""Typed response models for the solve_for / capabilities tools (roadmap A7 PR1).

These are plain Pydantic models used by ``actions.solve_for`` and
``actions.list_solve_for_capabilities`` to shape + validate their JSON response
payloads. They are intentionally permissive on nested ``Any`` blobs (the applied
design spec, actuator/target descriptors) so the canonical process-constraint
dicts pass through unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SolveForResponse(BaseModel):
    """Result of a ``solve_for`` specification swap."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    prepared_session_id: str
    job_id: Optional[str] = None
    status: str  # "solved" | "infeasible" | "prepared" | "dry_run"
    write_mode: str
    solve: bool
    dry_run: bool
    applied_design_spec: Dict[str, Any] = Field(default_factory=dict)
    dof_before: Optional[int] = None
    dof_after: Optional[int] = None
    actuator_resolved_to: Optional[Dict[str, Any]] = None
    target_resolved_to: Optional[Dict[str, Any]] = None
    # Pre-solve, build-state target residual (value(body) - bound). Informational
    # — it says whether the spec is well-posed at the build point, not whether the
    # solve met it.
    target_residual: Optional[float] = None
    # --- A2 SolveResult contract (roadmap A7 PR3) ---------------------------- #
    # The SAME fine-grained verdict ``solve_flowsheet`` returns. A design-spec
    # solve targets a determinate optimum, so it reads ``strict_optimal`` (no
    # ``residual_basis``); a structurally-unreachable spec reads
    # ``infeasible_structured``. The pipeline runs with near-feasible acceptance
    # OFF so a design-spec result is NEVER presented as strict when it isn't — if
    # a near-feasible iterate were ever accepted it would read ``near_feasible``
    # WITH the structured ``residual_basis`` that justifies it.
    solution_quality: (
        Literal["strict_optimal", "near_feasible", "infeasible_structured"] | None
    ) = None
    residual_basis: Optional[Dict[str, Any]] = None
    # Post-solve achieved-vs-target residual (value(body) - bound at the solution).
    # ~0 at a strict optimum; non-trivial when the actuator could not drive the
    # metric to target.
    target_residual_post_solve: Optional[float] = None
    # The freed actuator's post-solve bound status: ``"lower"`` / ``"upper"`` when
    # the solver pinned it against a bound, else None. A pinned actuator on a
    # non-converged solve is the signature of an unreachable target.
    actuator_at_bound: Optional[str] = None
    # Structured post-solve failure code (e.g. ``TARGET_AT_BOUND_UNREACHABLE``)
    # when the cause is diagnosable; None on success.
    solve_error_code: Optional[str] = None
    # Recommendation safety is independent from mathematical solvability.  A
    # legacy stoichiometric chemical-P block may solve exactly while remaining
    # ineligible to recommend an operating dose.
    target_met: Optional[bool] = None
    recommendation_eligible: bool = False
    recommendation_blockers: List[str] = Field(default_factory=list)
    certification: Dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    warnings: List[str] = Field(default_factory=list)


class ActuatorCandidate(BaseModel):
    """A live manipulable variable the model exposes as an actuator."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    role: Optional[str] = None
    var_path: str
    var_kind: str
    fixed: Optional[bool] = None
    value: Optional[float] = None
    lower: Optional[float] = None
    upper: Optional[float] = None
    units: Optional[str] = None
    named_actuator: Optional[str] = None


class TargetCandidate(BaseModel):
    """A target quantity computable for THIS model, with valid locations."""

    model_config = ConfigDict(extra="forbid")

    quantity: str
    label: str
    constraint_type: str
    value_units: str
    supported_kinds: List[str]
    locations: List[Dict[str, Any]] = Field(default_factory=list)
    computable: bool = True


class PresetCapability(BaseModel):
    """A preset's availability for THIS model + a ready-to-send request template."""

    model_config = ConfigDict(extra="forbid")

    preset: str
    label: str
    available: bool
    description: str = ""
    request_template: Optional[Dict[str, Any]] = None
    remediation: Optional[str] = None
    missing_roles: List[str] = Field(default_factory=list)
    calculation_class: str = "mechanistic"
    recommendation_eligible: bool = False
    recommendation_blockers: List[str] = Field(default_factory=list)
    operational_use: str = "diagnostic_or_design_spec"


class SolveForCapabilities(BaseModel):
    """Discovery payload for ``list_solve_for_capabilities``."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    session_id: str
    presets: List[PresetCapability] = Field(default_factory=list)
    actuator_candidates: List[ActuatorCandidate] = Field(default_factory=list)
    target_candidates: List[TargetCandidate] = Field(default_factory=list)
    report_only_quantities: List[Dict[str, Any]] = Field(default_factory=list)
    unavailable_capabilities: List[Dict[str, Any]] = Field(default_factory=list)
    raw_variable_targets: Optional[List[Dict[str, Any]]] = None
    certification: Dict[str, Any] = Field(default_factory=dict)
    message: str = ""

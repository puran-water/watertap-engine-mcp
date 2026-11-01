"""Cross-engine handoff registry for supported ontology translations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentMappingRule:
    """One source-to-target component mapping rule."""

    source: str
    target: str
    policy: str


@dataclass(frozen=True)
class HandoffSpec:
    """Public description of one supported cross-engine handoff."""

    source_model: str
    target_model: str
    supported: bool
    mappings: tuple[ComponentMappingRule, ...]
    x_tss_policy: str


HANDOFF_REGISTRY: dict[tuple[str, str], HandoffSpec] = {
    ("ASM2d", "mASM2d"): HandoffSpec(
        source_model="ASM2d",
        target_model="mASM2d",
        supported=True,
        mappings=(
            ComponentMappingRule("shared_components", "shared_components", "copy by component ID"),
            ComponentMappingRule("S_ALK", "S_IC", "alkalinity mapped to inorganic carbon surrogate"),
            ComponentMappingRule("X_TSS", "X_TSS", "computed sidecar using ASM2D_I_TSS coefficients"),
        ),
        x_tss_policy="compute_from_i_tss_coefficients",
    ),
    ("ASM2d", "mADM1"): HandoffSpec(
        source_model="ASM2d",
        target_model="mADM1",
        supported=True,
        mappings=(
            ComponentMappingRule("shared_components", "shared_components", "copy by component ID"),
            ComponentMappingRule("S_ALK", "S_IC", "alkalinity mapped to inorganic carbon surrogate"),
            ComponentMappingRule("X_TSS", "", "drop; mADM1 resolves particulate fractions natively"),
        ),
        x_tss_policy="drop_for_madm1",
    ),
    ("ASM2d", "WaterTAP_ASM2D"): HandoffSpec(
        source_model="ASM2d",
        target_model="WaterTAP_ASM2D",
        supported=True,
        mappings=(
            ComponentMappingRule("shared_components", "shared_components", "copy by component ID"),
            ComponentMappingRule("X_TSS", "TSS", "map to TSS-equivalent aggregate"),
        ),
        x_tss_policy="map_to_tss_equivalent",
    ),
    ("WaterTAP_ASM2D", "mASM2d"): HandoffSpec(
        source_model="WaterTAP_ASM2D",
        target_model="mASM2d",
        supported=True,
        mappings=(
            ComponentMappingRule("shared_components", "shared_components", "copy by component ID"),
            ComponentMappingRule("S_ALK", "S_IC", "alkalinity mapped to inorganic carbon surrogate"),
            ComponentMappingRule("X_TSS", "X_TSS", "preserve if present, otherwise compute"),
        ),
        x_tss_policy="preserve_or_compute_from_i_tss_coefficients",
    ),
}


def get_handoff_spec(source_model: str, target_model: str) -> HandoffSpec:
    """Return the handoff contract for a supported model pair."""

    key = (source_model, target_model)
    try:
        return HANDOFF_REGISTRY[key]
    except KeyError as exc:
        supported = ", ".join(f"{src}->{dst}" for src, dst in sorted(HANDOFF_REGISTRY))
        raise ValueError(
            f"Unsupported handoff {source_model!r}->{target_model!r}. Supported: {supported}"
        ) from exc


def list_supported_handoffs() -> tuple[HandoffSpec, ...]:
    """Return all supported cross-engine handoff contracts."""

    return tuple(HANDOFF_REGISTRY[key] for key in sorted(HANDOFF_REGISTRY))


__all__ = [
    "ComponentMappingRule",
    "HANDOFF_REGISTRY",
    "HandoffSpec",
    "get_handoff_spec",
    "list_supported_handoffs",
]

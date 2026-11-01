"""Pre-built flowsheet templates for common water treatment configurations."""

from .ro_train import ROTrainTemplate
from .nf_softening import NFSofteningTemplate
from .asm2d import (
    ASM2DBiologicalConfig,
    ASM2DMLETemplate,
    ASM2DMLEPOSTTemplate,
    ASM2DMBRAerobicTemplate,
    ASM2DMBRMLETemplate,
    ASM2DMBRMLEPOSTTemplate,
)
from .registry import (
    TEMPLATES,
    TemplateSpec,
    build_template,
    declared_recycle_ratio_vars,
    get_template_spec,
    list_template_specs,
    serialize_template_spec,
)

__all__ = [
    "ROTrainTemplate",
    "NFSofteningTemplate",
    "ASM2DBiologicalConfig",
    "ASM2DMLETemplate",
    "ASM2DMLEPOSTTemplate",
    "ASM2DMBRAerobicTemplate",
    "ASM2DMBRMLETemplate",
    "ASM2DMBRMLEPOSTTemplate",
    "TEMPLATES",
    "TemplateSpec",
    "build_template",
    "declared_recycle_ratio_vars",
    "get_template_spec",
    "list_template_specs",
    "serialize_template_spec",
]

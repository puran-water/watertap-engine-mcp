"``reaction_config`` (`REACTION_CONFIG_TARGET`, a design target of the direct ModifiedASM2d MBR rating) names a variant\nof the installed reaction package for a rating to run on; absent, or a field unstated, is the installed form:\n\n* ``decay_switch``: WaterTAP's own CONFIG option of the reaction package. False: lysis and decay at constant rates,\n  independent of the electron acceptor (R9, R15-R17, R19; ASM2d as published, Henze et al. 1999).\n* ``pha_storage`` ``anaerobic_only``: R10 (PHA storage) times KP_O2/(KP_O2 + S_O2) x KP_NO3/(KP_NO3 + S_NO3), the\n  installed row's own right-hand side multiplied, never retyped.\n\nA stated variant names its ``source``. What a rating ran on is read back off the built model (`built_reaction_config`),\nnever copied from the request, and recorded on its result under `REACTION_CONFIG_TARGET`."

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

REACTION_CONFIG_RULING_ID = 'PUBLIC-RULE-f7a359de4625'
#: the ModelBuilder session route
SESSION_ROUTE_RULING_ID = 'PUBLIC-RULE-539a5e932413'
REACTION_CONFIG_TARGET = "reaction_config"
#: a ModelBuilder build's read-back, beside the session's stated REACTION_CONFIG_TARGET in runtime_metadata
BUILT_REACTION_CONFIG_KEY = "reaction_config_built"
INSTALLED = "installed"
ANAEROBIC_ONLY = "anaerobic_only"
PHA_STORAGE_REACTION = "R10"


class ReactionConfig(BaseModel):
    """The model-form variant a rating runs on; every field unstated is the installed form."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decay_switch: Optional[bool] = None
    pha_storage: Literal["installed", "anaerobic_only"] = INSTALLED
    source: Optional[str] = None

    @model_validator(mode="after")
    def _a_variant_names_its_source(self) -> "ReactionConfig":
        if self.is_variant and not (self.source or "").strip():
            raise ValueError(f"{REACTION_CONFIG_TARGET} states a variant of the installed ModifiedASM2d form: name its "
                             f"source ({REACTION_CONFIG_RULING_ID})")
        return self

    @property
    def is_variant(self) -> bool:
        return self.decay_switch is not None or self.pha_storage != INSTALLED


def reaction_config_target(design_targets: Dict[str, Any]) -> ReactionConfig:
    """`REACTION_CONFIG_TARGET` typed: absent is the installed form; an unknown field or value refuses by name."""
    return ReactionConfig.model_validate(design_targets.get(REACTION_CONFIG_TARGET) or {})


class ReactionConfigRefused(ValueError):
    """A session's `REACTION_CONFIG_TARGET` that cannot be typed or carried; refused by name, never dropped."""


def typed_reaction_config(value: Any) -> ReactionConfig:
    """A session's stated `REACTION_CONFIG_TARGET` typed (absent: the installed form); an unknown field or value, or a
    variant without a source, refuses by name."""
    try:
        return ReactionConfig.model_validate(value or {})
    except ValidationError as exc:
        raise ReactionConfigRefused(f"reaction_config_refused: {exc} ({SESSION_ROUTE_RULING_ID} G3)") from exc


def session_reaction_config(value: Any) -> Optional[Dict[str, Any]]:
    """The stated variant as a session's ``runtime_metadata`` holds it; absent or the installed form: None."""
    config = typed_reaction_config(value)
    return config.model_dump(mode="json") if config.is_variant else None


def reaction_package_options(config: ReactionConfig) -> Dict[str, Any]:
    """The ModifiedASM2dReactionParameterBlock CONFIG options the variant states (none for the installed form)."""
    return {} if config.decay_switch is None else {"decay_switch": config.decay_switch}


def _reaction_blocks(reactors: Sequence[Any]):
    for unit in reactors:
        yield from unit.control_volume.reactions.values()


def apply_reaction_config(rxn: Any, reactors: Sequence[Any], config: ReactionConfig) -> Dict[str, Any]:
    """Apply the variant's rate-law changes to every reactor's built reaction blocks (before scaling), then return the
    record read back off the build. The R10 row must still read ``reaction_rate[R10] == rhs``; else refused by name."""
    if config.pha_storage == ANAEROBIC_ONLY:
        for block in _reaction_blocks(reactors):
            row, rate = block.rate_expression[PHA_STORAGE_REACTION], block.reaction_rate[PHA_STORAGE_REACTION]
            lhs, rhs = row.expr.args
            if lhs is not rate:
                raise ValueError(f"{block.name}.rate_expression[{PHA_STORAGE_REACTION}] is not reaction_rate == rhs "
                                 "(already transformed?): the anaerobic-only switch has no row to multiply")
            c, p = block.conc_mass_comp_ref, block.params
            row.set_value(lhs == rhs * (p.KP_O2 / (p.KP_O2 + c["S_O2"])) * (p.KP_NO3 / (p.KP_NO3 + c["S_NO3"])))
    return built_reaction_config(rxn, reactors, config)


def built_reaction_config(rxn: Any, reactors: Sequence[Any], config: ReactionConfig) -> Dict[str, Any]:
    """What the build carries: ``decay_switch`` off the reaction package's CONFIG, ``pha_storage`` off the variables
    every reactor's R10 row reads (S_O2 and S_NO3 both: anaerobic_only; neither: installed). A form the request did not
    produce, or one that differs between reactors, refuses by name."""
    from pyomo.core.expr.visitor import identify_variables

    forms = set()
    for block in _reaction_blocks(reactors):
        c = block.conc_mass_comp_ref
        read = {id(var) for var in identify_variables(block.rate_expression[PHA_STORAGE_REACTION].body)}
        switches = (id(c["S_O2"]) in read, id(c["S_NO3"]) in read)
        if switches == (True, True):
            forms.add(ANAEROBIC_ONLY)
        elif switches == (False, False):
            forms.add(INSTALLED)
        else:
            raise ValueError(f"{block.name}.rate_expression[{PHA_STORAGE_REACTION}] reads one electron acceptor of two: "
                             "neither the installed nor the anaerobic-only PHA storage form")
    if len(forms) != 1:
        raise ValueError(f"the reactors' {PHA_STORAGE_REACTION} rows carry different PHA storage forms {sorted(forms)}")
    built = {"decay_switch": bool(rxn.config.decay_switch), "pha_storage": forms.pop()}
    asked = {"decay_switch": built["decay_switch"] if config.decay_switch is None else config.decay_switch,
             "pha_storage": config.pha_storage}
    if built != asked:
        raise ValueError(f"the built ModifiedASM2d form {built} is not the stated {REACTION_CONFIG_TARGET} {asked}")
    return {"ruling": REACTION_CONFIG_RULING_ID, "stated": config.is_variant, **built, "source": config.source}

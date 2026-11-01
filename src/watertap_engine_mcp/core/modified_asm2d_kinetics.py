'One ModifiedASM2d kinetic rule on every route that builds its reaction package.'

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

#: the house set on the template path (OQ-C181-1)
TEMPLATE_KINETICS_RULING_ID = 'PUBLIC-RULE-36295b4c8f1e'
#: unstated temperature refused; the same set on every route; strict fixes; read-back
ROUTES_RULING_ID = 'PUBLIC-RULE-cdceb7262cb4'
#: a session feed's unstated temperature refused (OQ-C194-1); strict property fixes (G2); the seed temperature (G5)
SESSION_ROUTE_RULING_ID = 'PUBLIC-RULE-d02ab9040cc2'
#: the feed temperature every other property package keeps when a feed states none
UNSTATED_FEED_TEMPERATURE_C = 25.0
MODIFIED_ASM2D_KINETICS_KEY = "modified_asm2d_kinetics"
KINETICS_SOURCE = 'puran_mcp_support.engineering.house_standards.biological_kinetics.native_kinetics_at'


class ModifiedAsm2dKineticsRefused(ValueError):
    """A ModifiedASM2d build cannot state its kinetic set; refused by name, never defaulted."""


def stated_feed_temperature_c(*, modified_asm2d: bool, temperature_c: Optional[float],
                              temperature_k: Optional[float], where: str) -> float:
    """The temperature a feed states (``temperature_k`` wins); unstated, 25 C on every package but ModifiedASM2d, whose
    feed fixes the temperature its kinetic set is taken at and is refused by name (OQ-C194-1)."""
    if temperature_k is not None:
        return float(temperature_k) - 273.15
    if temperature_c is not None:
        return float(temperature_c)
    if modified_asm2d:
        raise ModifiedAsm2dKineticsRefused(
            f"modified_asm2d_kinetics_refused: {where} states neither temperature_C nor temperature_K; a ModifiedASM2d "
            f"feed fixes the temperature its kinetic set is taken at, and no default stands in for it: state one "
            f"({SESSION_ROUTE_RULING_ID} OQ-C194-1)")
    return UNSTATED_FEED_TEMPERATURE_C


def house_parameter_names() -> Tuple[str, ...]:
    from puran_mcp_support.engineering.house_standards.biological_kinetics import CORRECTED_PARAMETERS, RATE_CONSTANTS

    return (*RATE_CONSTANTS, *CORRECTED_PARAMETERS)


def house_kinetics(
    *,
    reaction_fixes: Mapping[str, Any],
    temperature_k: Optional[float],
    temperature_source_clause_ref: Optional[str],
    route: str,
    ruling: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The fixes with every house rate and correction the caller did not state, and their record.

    With every rate stated only the corrections are defaulted and no temperature is needed. Otherwise the set is
    taken at ``temperature_k``; none, or one below the fit, is refused by name.
    """
    from puran_mcp_support.engineering.house_standards.biological_kinetics import CORRECTED_PARAMETERS, RATE_CONSTANTS, KineticsTemperatureRangeError, native_kinetics_at

    supplied = dict(reaction_fixes)
    unstated = sorted(set(RATE_CONSTANTS) - set(supplied))
    kinetic_set = None
    if not unstated:
        house = {name: correction.value for name, correction in CORRECTED_PARAMETERS.items()}
    else:
        if temperature_k is None:
            raise ModifiedAsm2dKineticsRefused(
                f"modified_asm2d_kinetics_refused: {route} states no temperature for the ModifiedASM2d rates "
                f"{unstated} ({temperature_source_clause_ref}): state one feed temperature, or every rate constant "
                f"in reaction_parameter_fixes ({ROUTES_RULING_ID})")
        try:
            kinetics = native_kinetics_at(temperature_k=float(temperature_k),
                                          temperature_source_clause_ref=str(temperature_source_clause_ref))
        except KineticsTemperatureRangeError as exc:
            raise ModifiedAsm2dKineticsRefused(
                f"modified_asm2d_kinetics_refused: {route}: ModifiedASM2d rates {unstated} cannot be defaulted: "
                f"{exc} State every rate constant in reaction_parameter_fixes, or rate at 15 C or above "
                f"({ruling})") from exc
        house = dict(kinetics.reaction_parameter_fixes)
        kinetic_set = {
            "reference_set": kinetics.reference_set,
            "temperature_c": kinetics.temperature_c,
            "bounded": list(kinetics.bounded),
            "screens": kinetics.screens,
            "basis_content_hash": kinetics.basis_content_hash,
            "parameter_basis": kinetics.parameter_basis,
        }
    defaulted = {name: value for name, value in house.items() if name not in supplied}
    fixes = {**supplied, **defaulted}
    names = house_parameter_names()
    record = {
        "ruling": ruling,
        "routes_ruling": ROUTES_RULING_ID,
        "route": route,
        "source": KINETICS_SOURCE,
        "temperature_k": None if temperature_k is None else float(temperature_k),
        "temperature_source_clause_ref": temperature_source_clause_ref,
        # None where a name is not applied (the installed default would run)
        "reaction_parameter_fixes": {name: fixes.get(name) for name in names},
        "house_defaults": defaulted,
        "caller_supplied": {name: supplied[name] for name in names if name in supplied},
        "kinetic_set": kinetic_set,
    }
    return fixes, record


def _fixable(block: Any, name: str) -> Any:
    target = getattr(block, str(name), None)
    if target is None or not (hasattr(target, "fix") or hasattr(target, "set_value")):
        return None
    if getattr(target, "is_indexed", lambda: False)():
        return None
    return target


def fix_parameters(blocks: Iterable[Any], fixes: Mapping[str, Any], *, where: str, label: str) -> None:
    'Fix every named ``label`` parameter on each block that declares it; refuse a name no block can take.'
    blocks = list(blocks)
    unknown, unfixable = [], []
    for name, value in fixes.items():
        targets = [target for target in (_fixable(block, name) for block in blocks) if target is not None]
        if not targets:
            unknown.append(str(name))
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            unfixable.append(f"{name}={value!r}")
            continue
        for target in targets:
            try:
                if hasattr(target, "fix"):
                    target.fix(numeric)
                else:
                    target.set_value(numeric)
            except Exception as exc:
                unfixable.append(f"{name} ({type(exc).__name__}: {exc})")
    if unknown or unfixable:
        ruling = f"{ROUTES_RULING_ID} G6" if label == "reaction" else f"{SESSION_ROUTE_RULING_ID} G2"
        raise ModifiedAsm2dKineticsRefused(
            f"unsupported {label} parameter prescription on {where}: "
            + "; ".join(part for part in (
                f"unknown {sorted(unknown)} (no {label} package declares them as a scalar parameter)"
                if unknown else "",
                f"unfixable {unfixable}" if unfixable else "") if part)
            + f" ({ruling})")


def fix_reaction_parameters(blocks: Iterable[Any], fixes: Mapping[str, Any], *, where: str) -> None:
    """`fix_parameters` on reaction packages."""
    fix_parameters(blocks, fixes, where=where, label="reaction")


def built_reaction_parameters(block: Any) -> Dict[str, Optional[float]]:
    """The house names read back off a built ModifiedASM2d reaction package (G7)."""
    import pyomo.environ as pyo

    return {name: (None if getattr(block, name, None) is None else float(pyo.value(getattr(block, name))))
            for name in house_parameter_names()}

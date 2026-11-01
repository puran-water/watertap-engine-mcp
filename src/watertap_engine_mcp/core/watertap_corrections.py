'Server-owned corrections to verified defects in the installed WaterTAP.'

from __future__ import annotations

import copy
import functools
import logging
import math
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

UPSTREAM_REPOSITORY = "watertap-org/watertap"
UPSTREAM_COMMIT = "5bfe7940bae135a32b96310c119987259df21cb9"
UPSTREAM_TITLE = "Repair enth_mass coeffs (#1856)"
UPSTREAM_DATE = "2026-08-27"
UPSTREAM_CITATION = (
    f"{UPSTREAM_REPOSITORY}@{UPSTREAM_COMMIT[:12]} \"{UPSTREAM_TITLE}\", {UPSTREAM_DATE}"
)
INSTALLED_RELEASE_OBSERVED = "watertap 1.7.0"

# Attribute names stored on a parameter block (plain attributes, not Pyomo
# components, so they never enter the model).
_RECORD_ATTR = "_watertap_engine_property_corrections"
_SCOPE_ATTR = "_watertap_engine_correction_scope"
_HOOK_MARKER = "_watertap_engine_enthalpy_correction_hook"

# Relative tolerance for classifying a coefficient.  The literals are exact
# decimal constants; anything further away than float round-off is a
# different coefficient, not the one this layer knows how to correct.
_CLASSIFY_REL_TOL = 1e-12


class PropertyCorrectionError(RuntimeError):
    """The coefficient state is not one this layer recognises; refused."""


@dataclass(frozen=True)
class CoefficientCorrection:
    """One upstream coefficient repair, with the diff lines that prove it.

    ``removed_line`` / ``added_line`` are the upstream diff lines with their
    leading indentation stripped (the indentation differs between packages).
    """

    parameter: str
    defective: float
    corrected: float
    removed_line: str
    added_line: str


# Nayar et al. (2016) Table 9 enthalpy coefficients repaired by #1856.  The
# same two literals, with the same two diff lines, in both packages; #1856
# changes no other coefficient in either file.
_NAYAR_ENTHALPY_CORRECTIONS: Tuple[CoefficientCorrection, ...] = (
    CoefficientCorrection(
        parameter="enth_mass_param_B6",
        defective=-4.41733,
        corrected=-4.41733e1,
        removed_line="-initialize=-4.41733,",
        added_line="+initialize=-4.41733e1,",
    ),
    CoefficientCorrection(
        parameter="enth_mass_param_B10",
        defective=9.72801,
        corrected=9.72801e1,
        removed_line="-initialize=9.72801,",
        added_line="+initialize=9.72801e1,",
    ),
)


@dataclass(frozen=True)
class PackageCorrections:
    """The corrections one WaterTAP property package needs."""

    package: str
    module_path: str
    parameter_data_class: str
    state_block_data_class: str
    lazy_enthalpy_parameters: bool
    upstream_file: str
    upstream_hunks: Tuple[Tuple[str, str], ...]
    corrections: Tuple[CoefficientCorrection, ...]


PACKAGE_CORRECTIONS: Tuple[PackageCorrections, ...] = (
    PackageCorrections(
        package="SEAWATER",
        module_path="watertap.property_models.seawater_prop_pack",
        parameter_data_class="SeawaterParameterData",
        state_block_data_class="SeawaterStateBlockData",
        lazy_enthalpy_parameters=False,
        upstream_file="watertap/property_models/seawater_prop_pack.py",
        upstream_hunks=(
            ("enth_mass_param_B6", "@@ -418,7 +418,7 @@ def build(self):"),
            ("enth_mass_param_B10", "@@ -442,7 +442,7 @@ def build(self):"),
        ),
        corrections=_NAYAR_ENTHALPY_CORRECTIONS,
    ),
    PackageCorrections(
        package="MCAS",
        module_path="watertap.property_models.multicomp_aq_sol_prop_pack",
        parameter_data_class="MCASParameterData",
        state_block_data_class="MCASStateBlockData",
        lazy_enthalpy_parameters=True,
        upstream_file="watertap/property_models/multicomp_aq_sol_prop_pack.py",
        upstream_hunks=(
            ("enth_mass_param_B6", "@@ -2356,7 +2356,7 @@ def _enth_mass_phase(self):"),
            ("enth_mass_param_B10", "@@ -2380,7 +2380,7 @@ def _enth_mass_phase(self):"),
        ),
        corrections=_NAYAR_ENTHALPY_CORRECTIONS,
    ),
)


def _stored(block: Any, attr: str, default: Any = None) -> Any:
    """Read a plain attribute this layer stored, bypassing ``__getattr__``.

    IDAES state blocks answer unknown attributes through build-on-demand;
    reading the instance ``__dict__`` never triggers a property build.
    """
    try:
        return vars(block).get(attr, default)
    except TypeError:
        return default


def _package_for_block(param_block: Any) -> Optional[PackageCorrections]:
    """Return the correction set for ``param_block``'s package, if any.

    Identified by ``isinstance`` against the package's parameter-data class;
    IDAES-declared block classes live in ``idaes.core.base.process_block`` so
    neither the class name nor ``__module__`` identifies the package.
    """
    for package in PACKAGE_CORRECTIONS:
        # A block of this package cannot exist unless its module is imported,
        # so an unimported module is simply not this block's package.
        module = sys.modules.get(package.module_path)
        if module is None:
            continue
        data_class = getattr(module, package.parameter_data_class, None)
        if data_class is not None and isinstance(param_block, data_class):
            return package
    return None


def _classify(value: Optional[float], correction: CoefficientCorrection) -> str:
    if value is None:
        return "missing"
    if math.isclose(value, correction.defective, rel_tol=_CLASSIFY_REL_TOL, abs_tol=0.0):
        return "defective"
    if math.isclose(value, correction.corrected, rel_tol=_CLASSIFY_REL_TOL, abs_tol=0.0):
        return "corrected"
    return "unknown"


def _correction_entry(package: PackageCorrections, correction: CoefficientCorrection) -> Dict[str, Any]:
    return {
        "parameter": correction.parameter,
        "installed_value": correction.defective,
        "applied_value": correction.corrected,
        "upstream_file": package.upstream_file,
        "upstream_hunk": dict(package.upstream_hunks)[correction.parameter],
        "upstream_diff": [correction.removed_line, correction.added_line],
    }


def _new_record(package: PackageCorrections, status: str) -> Dict[str, Any]:
    return {
        "package": package.package,
        "module_path": package.module_path,
        "status": status,
        "applied": [],
        "upstream": {
            "repository": UPSTREAM_REPOSITORY,
            "commit": UPSTREAM_COMMIT,
            "title": UPSTREAM_TITLE,
            "date": UPSTREAM_DATE,
            "citation": UPSTREAM_CITATION,
        },
        "detection": "by coefficient value, not by WaterTAP version",
    }


def apply_property_corrections(param_block: Any) -> Dict[str, Any]:
    """Correct the known WaterTAP coefficient defects on one parameter block.

    Returns the correction record (a copy).  ``status`` is one of:

    * ``not_applicable`` -- the package has no known defect; nothing touched.
    * ``corrected`` -- the defective values were found and replaced (now or on
      an earlier call; the call is idempotent).
    * ``already_correct`` -- the installed WaterTAP already carries the
      upstream values; nothing touched.
    * ``pending_lazy_enthalpy`` -- MCAS block whose enthalpy parameters do not
      exist yet; the block is marked and corrected when they are built.

    Raises:
        PropertyCorrectionError: partially-corrected, missing or unknown
            coefficient state.
    """
    package = _package_for_block(param_block)
    if package is None:
        return {"package": None, "status": "not_applicable", "applied": []}

    setattr(param_block, _SCOPE_ATTR, True)
    if package.lazy_enthalpy_parameters:
        _install_lazy_enthalpy_hook(package)

    previous = _stored(param_block, _RECORD_ATTR)
    block_name = getattr(param_block, "name", repr(param_block))

    present = [hasattr(param_block, c.parameter) for c in package.corrections]
    if package.lazy_enthalpy_parameters and not any(present):
        record = previous or _new_record(package, "pending_lazy_enthalpy")
        setattr(param_block, _RECORD_ATTR, record)
        return copy.deepcopy(record)

    states: List[Tuple[CoefficientCorrection, Any, str]] = []
    for correction in package.corrections:
        var = getattr(param_block, correction.parameter, None)
        current = None if var is None else var.value
        states.append((correction, var, _classify(current, correction)))

    kinds = {state for _, _, state in states}
    described = ", ".join(
        f"{c.parameter}={None if v is None else v.value!r} ({state}; defective "
        f"{c.defective!r}, corrected {c.corrected!r})"
        for c, v, state in states
    )
    if kinds & {"missing", "unknown"}:
        raise PropertyCorrectionError(
            f"{package.package} parameter block {block_name}: unrecognised Nayar "
            f"enthalpy coefficient state -- {described}. This layer corrects only "
            f"the exact literals repaired by {UPSTREAM_CITATION}; refusing to guess."
        )
    if kinds == {"defective", "corrected"}:
        raise PropertyCorrectionError(
            f"{package.package} parameter block {block_name}: partially corrected "
            f"Nayar enthalpy coefficients -- {described}. {UPSTREAM_CITATION} "
            f"repairs both together; a mixed state is not the installed package, "
            f"not the upstream package, and not a correlation anyone published."
        )

    if kinds == {"corrected"}:
        if previous is not None and previous.get("status") == "corrected":
            record = previous
        else:
            record = _new_record(package, "already_correct")
        setattr(param_block, _RECORD_ATTR, record)
        return copy.deepcopy(record)

    # Every coefficient is at its defective literal: correct them together.
    record = _new_record(package, "corrected")
    for correction, var, _ in states:
        if var.fixed:
            var.fix(correction.corrected)
        else:
            var.set_value(correction.corrected)
        if not math.isclose(var.value, correction.corrected, rel_tol=_CLASSIFY_REL_TOL):
            raise PropertyCorrectionError(
                f"{package.package} parameter block {block_name}: setting "
                f"{correction.parameter} to {correction.corrected!r} left "
                f"{var.value!r}; the correction did not take."
            )
        record["applied"].append(_correction_entry(package, correction))
    setattr(param_block, _RECORD_ATTR, record)
    logger.info(
        "Corrected WaterTAP %s enthalpy coefficients on %s: %s (%s)",
        package.package,
        block_name,
        ", ".join(
            f"{entry['parameter']} {entry['installed_value']} -> {entry['applied_value']}"
            for entry in record["applied"]
        ),
        UPSTREAM_CITATION,
    )
    return copy.deepcopy(record)


def property_corrections_record(param_block: Any) -> Optional[Dict[str, Any]]:
    """Return the correction record stored on ``param_block`` (a copy), if any."""

    record = _stored(param_block, _RECORD_ATTR)
    return None if record is None else copy.deepcopy(record)


def collect_property_corrections(model: Any) -> Dict[str, Dict[str, Any]]:
    """Collect every correction record in ``model``, keyed by block name."""

    from pyomo.environ import Block

    found: Dict[str, Dict[str, Any]] = {}
    blocks = [model]
    blocks.extend(model.component_data_objects(Block, descend_into=True))
    for block in blocks:
        record = _stored(block, _RECORD_ATTR)
        if record is not None:
            found[block.name] = copy.deepcopy(record)
    return found


def _install_lazy_enthalpy_hook(package: PackageCorrections) -> None:
    """Complete the correction when a lazily-built enthalpy block appears.

    Wraps ``<StateBlockData>._enth_mass_phase`` once.  The wrapper builds the
    property exactly as installed, then corrects the parameter block ONLY if
    this layer marked it in scope; unmarked blocks are untouched.
    """
    import importlib

    module = importlib.import_module(package.module_path)
    data_class = getattr(module, package.state_block_data_class)
    original = data_class.__dict__.get("_enth_mass_phase")
    if original is None:
        raise PropertyCorrectionError(
            f"{package.module_path}.{package.state_block_data_class} has no "
            f"_enth_mass_phase; the lazy enthalpy build this layer hooks has moved."
        )
    if getattr(original, _HOOK_MARKER, False):
        return

    @functools.wraps(original)
    def _enth_mass_phase_with_corrections(self):
        original(self)
        params = self.params
        if _stored(params, _SCOPE_ATTR, False):
            apply_property_corrections(params)

    setattr(_enth_mass_phase_with_corrections, _HOOK_MARKER, True)
    data_class._enth_mass_phase = _enth_mass_phase_with_corrections

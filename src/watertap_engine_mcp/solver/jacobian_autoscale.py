"""Exactly-equal vectorised replacement for the IPOPT wrapper's Jacobian constraint autoscaling.

Owner ruling HOUSE-JACOBIAN-AUTOSCALE-VECTORISED-EXACT-20261019 ("Open a lane, prove exact"): a SERVER-SIDE override,
never an edit inside the vendored venv, proved bit-identical on cached Jacobians before use.

``vectorised_constraint_autoscale_large_jac`` computes the same row maxima with array operations that perform the same
IEEE operations on the same operands (one division per stored entry, ``abs``, and scipy's own max rule over the
stored values; an empty row is zero), and then sets the factors with the same
IDAES calls, in the same order, from the same Python scalar expressions. The pure part is ``row_gradient_maxima``.

The override replaces only that module function. The wrapper method, its error handling, its scaling-factor cache
and reset, and every IDAES helper stay the vendor's. ``install`` refuses (keeps the vendor function, logged) unless
every distribution version and every vendor source hash equals the one this was proved on (``PROVED_ON``), and a
call whose input is outside what was proved (non-canonical CSR, non-float64 data, an empty column space, a
non-numeric variable factor) is handed to the vendor function itself, logged."""
from __future__ import annotations
import hashlib
import inspect
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from numbers import Real
RULING_ID = 'HOUSE-JACOBIAN-AUTOSCALE-VECTORISED-EXACT-20261019'
SWITCH_ENV = 'WATERTAP_JACOBIAN_AUTOSCALE'
PROVED_ON = {'versions': {'idaes-pse': '2.13.0', 'watertap-solvers': '24.12.9', 'watertap': '1.8.0', 'pyomo': '6.10.1', 'scipy': '1.17.0', 'numpy': '2.4.2'}, 'source_sha256': {'watertap_solvers._base._constraint_autoscale_large_jac': 'c67a937293f1ff9798b6e924b087c7783b7c5eb2c938db2e3c81d49839bca57c', 'watertap_solvers._base._WaterTAPSolverWrapper._scale_constraints': 'b7107408400c59c0dc5529d935c1f615ead00eab8a04acc219dfe37066cf781c', 'idaes.core.util.scaling.get_scaling_factor': '09b4fbee77240da39a298ced618b564f436372bd5303655dda0b3a2155596a50', 'idaes.core.util.scaling.set_scaling_factor': '326dad9db603cb181c224141bc8827ef3542e6023d24a0599d3554e7dcfdf9f6', 'scipy.sparse._data._minmax_mixin._min_or_max': '916e5f899e34a314fe57b5208d0be28439c25efb3ba79d86129f7665ce2b3107'}}
_log = logging.getLogger('jacobian_autoscale')
_VENDOR = None
STATUS: dict = {'implementation': 'vendor', 'reason': 'not installed', 'ruling': RULING_ID}
_FALLBACK_REASONS: set = set()

def _vendor_objects() -> dict:
    from scipy.sparse import _data
    from idaes.core.util import scaling
    from watertap_solvers import _base
    function = _VENDOR if _VENDOR is not None else _base._constraint_autoscale_large_jac
    return {'watertap_solvers._base._constraint_autoscale_large_jac': function, 'watertap_solvers._base._WaterTAPSolverWrapper._scale_constraints': inspect.unwrap(_base._WaterTAPSolverWrapper.__dict__['_scale_constraints']), 'idaes.core.util.scaling.get_scaling_factor': scaling.get_scaling_factor, 'idaes.core.util.scaling.set_scaling_factor': scaling.set_scaling_factor, 'scipy.sparse._data._minmax_mixin._min_or_max': _data._minmax_mixin._min_or_max}

def vendor_identity() -> dict:
    """The installed versions and the vendor sources' SHA-256, in the shape of ``PROVED_ON``."""
    versions = {}
    for distribution in PROVED_ON['versions']:
        try:
            versions[distribution] = version(distribution)
        except PackageNotFoundError:
            versions[distribution] = None
    sources = {}
    for name, obj in _vendor_objects().items():
        if getattr(obj, '_puranos_vectorised', False):
            raise RuntimeError(f'{name} is already the override; the vendor original was not captured')
        sources[name] = hashlib.sha256(inspect.getsource(obj).encode()).hexdigest()
    return {'versions': versions, 'source_sha256': sources}

def identity_differences(identity: dict, proved: dict | None=None) -> list[str]:
    """Every entry where ``identity`` differs from what the proof ran on (empty when identical)."""
    proved = PROVED_ON if proved is None else proved
    differences = []
    for group in ('versions', 'source_sha256'):
        for key, expected in proved[group].items():
            found = identity[group].get(key)
            if found != expected:
                differences.append(f'{group}[{key}]: proved on {expected}, installed {found}')
    return differences

def row_gradient_maxima(indptr, indices, data, variable_factors):
    """Each row's ``max |J_ij / s_j|`` exactly as the vendor's ``row.max()`` returns it.

    ``variable_factors`` is a float64 array over the columns. scipy's rule (``_minmax_mixin._min_or_max``, axis None):
    a row with no stored entry is ``float64(0)``; otherwise ``maximum.reduce`` of the stored values, then
    ``maximum(0, m)`` when the row also has an implicit zero. That last step is the identity here, because every value
    is an ``abs`` (>= 0, or NaN, which ``maximum`` propagates), so it is not repeated (mutation J5 proved it inert).
    """
    import numpy as np
    scaled = np.abs(data / variable_factors[indices])
    counts = np.diff(indptr)
    maxima = np.zeros(len(counts), dtype=np.float64)
    stored = counts > 0
    if stored.any():
        maxima[stored] = np.maximum.reduceat(scaled, indptr[:-1][stored])
    return maxima

def _inadmissible(jac, factors) -> str | None:
    import numpy as np
    if jac.dtype != np.float64:
        return f'Jacobian dtype {jac.dtype} (proved on float64)'
    if not jac.has_canonical_format:
        return 'non-canonical CSR Jacobian (duplicate or unsorted entries)'
    if jac.shape[1] == 0 and jac.shape[0] > 0:
        return "empty column space (the vendor's row.max() raises; it is asked so it raises the same)"
    for factor in factors:
        if isinstance(factor, bool) or not isinstance(factor, (Real, np.floating, np.integer)):
            return f'variable scaling factor of type {type(factor).__name__}'
    return None

def vectorised_constraint_autoscale_large_jac(nlp, ignore_constraint_scaling=False, ignore_variable_scaling=False, max_grad=100, min_scale=1e-06):
    """Drop-in for ``watertap_solvers._base._constraint_autoscale_large_jac``: same signature, same side effects."""
    import numpy as np
    from idaes.core.util.scaling import get_scaling_factor, set_scaling_factor
    jac = nlp.evaluate_jacobian().tocsr()
    nlp.clist = clist = nlp.get_pyomo_constraints()
    nlp.vlist = vlist = nlp.get_pyomo_variables()
    n_columns = jac.shape[1]
    columns = np.unique(jac.indices)
    if ignore_variable_scaling:
        factors = [1] * len(columns)
    else:
        factors = [get_scaling_factor(vlist[j], default=1) for j in columns.tolist()]
    reason = _inadmissible(jac, factors)
    if reason is not None:
        if reason not in _FALLBACK_REASONS:
            _FALLBACK_REASONS.add(reason)
            _log.warning('jacobian autoscale: vendor function used for this call (%s); %s', reason, RULING_ID)
        return vendor_function()(nlp, ignore_constraint_scaling=ignore_constraint_scaling, ignore_variable_scaling=ignore_variable_scaling, max_grad=max_grad, min_scale=min_scale)
    variable_factors = np.ones(n_columns, dtype=np.float64)
    variable_factors[columns] = np.array([float(f) for f in factors], dtype=np.float64)
    maxima = row_gradient_maxima(jac.indptr, jac.indices, jac.data, variable_factors)
    for i, c in enumerate(clist):
        if ignore_constraint_scaling or get_scaling_factor(c) is None:
            sc = 1
            mg = maxima[i]
            if mg > max_grad:
                sc = max(min_scale, max_grad / mg)
            set_scaling_factor(c, sc)
    return None
vectorised_constraint_autoscale_large_jac._puranos_vectorised = True

def install() -> dict:
    """Install the override in this process, once, behind the identity guard; return ``STATUS``.

    Refuses (vendor kept, logged once) when ``WATERTAP_JACOBIAN_AUTOSCALE=vendor``, when ``watertap_solvers`` does
    not import, or when any version or vendor source hash differs from ``PROVED_ON``.
    """
    global _VENDOR
    try:
        from watertap_solvers import _base
    except ImportError as error:
        STATUS.update(implementation='vendor', reason=f'watertap_solvers unavailable: {error}')
        return dict(STATUS)
    current = _base._constraint_autoscale_large_jac
    if getattr(current, '_puranos_vectorised', False):
        return dict(STATUS)
    _VENDOR = current
    if os.environ.get(SWITCH_ENV, '').strip().lower() == 'vendor':
        STATUS.update(implementation='vendor', reason=f'{SWITCH_ENV}=vendor')
        return dict(STATUS)
    identity = vendor_identity()
    differences = identity_differences(identity)
    STATUS['identity'] = identity
    if differences:
        STATUS.update(implementation='vendor', reason='not proved on the installed code: ' + '; '.join(differences))
        _log.warning('jacobian autoscale override NOT installed, vendor function kept (%s): %s', RULING_ID, '; '.join(differences))
        return dict(STATUS)
    _base._constraint_autoscale_large_jac = vectorised_constraint_autoscale_large_jac
    STATUS.update(implementation='vectorised', reason='identity equals PROVED_ON')
    return dict(STATUS)

def uninstall() -> None:
    """Restore the vendor function (tests and reference runs)."""
    if _VENDOR is None:
        return
    from watertap_solvers import _base
    _base._constraint_autoscale_large_jac = _VENDOR
    STATUS.update(implementation='vendor', reason='uninstalled')

def vendor_function():
    """The vendor ``_constraint_autoscale_large_jac`` as imported (captures it if ``install`` has not run)."""
    global _VENDOR
    if _VENDOR is None:
        from watertap_solvers import _base
        current = _base._constraint_autoscale_large_jac
        if getattr(current, '_puranos_vectorised', False):
            raise RuntimeError('override installed without capturing the vendor function')
        _VENDOR = current
    return _VENDOR

def record() -> dict:
    """What ran in this process, for a result's diagnostics: the implementation, why, and the ruling."""
    return {'implementation': STATUS['implementation'], 'reason': STATUS['reason'], 'ruling': RULING_ID}

"The pinned route (utils/non_ebpr_pin.py) holds X_PAO, X_PHA and X_PP at zero. Zero is a steady state of the unpinned\nequations, not the only one (an EBPR steady state exists at ZLR W1's own inlet, c160), and it is the physical one only\nwhere it is STABLE: a small PAO population, with its stored PHA and polyphosphate, put into the solved state must wash\nout. This module answers that from the solved model itself, never from a typed P or VFA threshold (the nitrifier\nanalogue: solver/nitrifier_invasion.py):\n\n* the pool rates are the INSTALLED rate expressions of the reactions that move a pool (read off the stoichiometry,\n  ``utils.non_ebpr_pin.pool_reactions``: WaterTAP R10-R17), each reactor at its solved state with the pools set to a\n  trial vector; every other state stays at its solved value (an infinitesimal population moves nothing else). Each\n  rate is read implicitly from its ``rate_expression`` row (robust to a constraint scaling transform);\n* those rates are homogeneous of degree 1 in (X_PAO, X_PHA, X_PP) and not differentiable at zero (R10's\n  X_PP / (KP_PP X_PAO + X_PP)), so there is no Jacobian at the pin to take an eigenvalue of. The population is read on\n  its BALANCED-GROWTH RAY: the pool balances over the reactor train, F(v) = transport(v) + N^T r(v) (CSTR flows, the\n  membrane's particulate split and the RAS split, so wastage), are homogeneous of degree 1 too, so the direction of a\n  small population converges to a ray u, F(u) = lambda u, and lambda is the population's invasion rate: lambda < 0,\n  the inoculum washes out (zero is stable; the caller admits the pinned route); lambda >= 0, it invades;\n* the ray is found by integrating the projected dynamics du/dt = F(u) - (1.F(u)) u (sum u = 1) from each of\n  ``START_DIRECTIONS`` into its basin and polishing by Newton on F(u) = lambda u, sum u = 1 (the exact Jacobian), every\n  Newton step from inside the storage domain 0 <= X_PP/X_PAO <= K_MAX held there (a step leaving it is rejected and the\n  integration goes on, POLISH_DOMAIN_RULING_ID; a ray found outside, from a start outside, is refused below), and\n  checked, not assumed: the ray residual |F(u) - lambda u|; homogeneity on the ray, dF/dv(u) u\n  = F(u) (Euler), which fails if the installed rates are not degree-1 homogeneous; and lambda the DOMINANT eigenvalue of\n  dF/dv(u), which is the statement that the ray attracts (the nitrifier test's dominant eigenvalue, read on the ray).\n  The verdict is the largest lambda over the starts. A check that fails leaves no ray;\n* where no ray answers, the storage substrate does (`storage_substrate_bound`, STORAGE_SUBSTRATE_RULING_ID): a population\n  that, every PHA it could store counted as growth, still cannot outgrow its lysis and wastage washes out, recorded with\n  that upper bound as its rate. Otherwise there is no lambda: the caller refuses."

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

SECONDS_PER_DAY = 86400.0
#: The ruling that built this test; carried on every record.
PAO_INVASION_RULING_ID = 'PUBLIC-RULE-b926a39289d8'
POOLS = ("X_PAO", "X_PHA", "X_PP")
#: Starting directions of the ray search in every reactor, (X_PHA/X_PAO, X_PP/X_PAO as a fraction of the installed
#: K_MAX): numerical starts, not inputs (the ray they converge to is the model's; c160's probe used the first two at
#: the default K_MAX 0.34). Every pool positive (with X_PHA or X_PP at zero every PAO production rate is zero, so such
#: a start can only wash out), and X_PP/X_PAO inside the model's storage domain [0, K_MAX]: R11/R12's
#: (K_MAX X_PAO - X_PP)/((KI_PP + K_MAX) X_PAO - X_PP) has its pole at K_MAX + KI_PP and changes sign past it, so a
#: start there finds a ray of no physical population (c166: a (1, 1, 1) start, X_PP/X_PAO = 1).
START_DIRECTIONS = ((0.1, 0.88), (0.5, 0.15), (0.01, 0.5))
#: The ray is converged when |F(u) - lambda u|_inf <= this x |F(u)|_inf (relative, the dynamics' own scale).
RAY_RELATIVE_TOLERANCE = 1e-8
#: Euler's identity on the ray, dF/dv(u) u = F(u), holds to this relative tolerance (derivatives are exact, by pyomo
#: reverse-mode differentiation of the installed rows): homogeneity checked, not assumed.
HOMOGENEITY_RELATIVE_TOLERANCE = 1e-6
#: Integration of the projected dynamics: one leg (d), and the most legs before the search is refused. The integration
#: only carries a start into the ray's basin (its tolerances set the pace, never the answer: the Newton polish meets
#: RAY_RELATIVE_TOLERANCE and the checks below judge the point it reaches); NEWTON_MAX_STEPS bounds one polish.
LEG_DAYS = 10.0
MAX_LEGS = 300
INTEGRATION_RTOL, INTEGRATION_ATOL = 1e-4, 1e-10
NEWTON_MAX_STEPS = 25
NEWTON_EXTRA_STEPS = 3

#: substrate (X_PHA) the train offers it washes out, read off `storage_substrate_bound`. c176: ZLR's aerobic-only train
#: with R10 made anaerobic-only stores PHA at 1 % of PAO lysis, X_PP/X_PAO is then neutral, and one search's Newton
#: polish ends past the K_MAX pole.
STORAGE_SUBSTRATE_RULING_ID = 'PUBLIC-RULE-d6ccf0230992'

#: on. With PHA near zero b_PAO = b_PP makes X_PP/X_PAO near-neutral, and an unconfined polish of c176's variant left
#: the domain past the K_MAX pole on a ray of no physical population (c183 FINDING 2).
POLISH_DOMAIN_RULING_ID = 'PUBLIC-RULE-f7a359de4625'


class PaoInvasionError(ValueError):
    """The invasion test has no answer at this solution; the message names the check that failed."""


def _value(expr: Any) -> float:
    import pyomo.environ as pyo

    return float(pyo.value(expr))


class _ReactorRates:
    """One reactor's pool rates (kg/m3/d) and their derivatives at its solved state, the pools set to a trial vector."""

    def __init__(self, unit: Any, reaction_package: Any, reactions: Sequence[str]) -> None:
        from pyomo.core.expr.calculus.derivatives import Modes, differentiate

        self.unit = unit
        self.block = unit.control_volume.reactions[0.0]
        self.reactions = list(reactions)
        self.pool_vars = [self.block.conc_mass_comp_ref[p] for p in POOLS]
        stoichiometry = reaction_package.rate_reaction_stoichiometry
        #: nu[r][p], read off the installed stoichiometry
        self.nu = [[_value(stoichiometry.get((r, "Liq", p), 0)) for p in POOLS] for r in self.reactions]
        self.rows = [self.block.rate_expression[r] for r in self.reactions]
        self.rates = [self.block.reaction_rate[r] for r in self.reactions]
        self._mode, self._differentiate = Modes.reverse_numeric, differentiate
        #: d(row body)/d(reaction_rate): constant (each row is affine in its rate), so read once, off the pin (every
        #: R10-R17 row reads 0/0 at it)
        self._set((1.0, 0.1, 0.0))
        self.rate_coefficients = [float(differentiate(row.body, wrt=rate, mode=self._mode))
                                  for row, rate in zip(self.rows, self.rates)]
        for reaction, coefficient in zip(self.reactions, self.rate_coefficients):
            if coefficient == 0.0:
                raise PaoInvasionError(f"rate_expression[{reaction}] does not define reaction_rate[{reaction}]")

    def _set(self, pools: Sequence[float]) -> None:
        for var, value in zip(self.pool_vars, pools):
            var.set_value(float(value), skip_validation=True)

    def rates_per_d(self, pools: Sequence[float]) -> List[float]:
        """Each pool reaction's rate implied by its row at ``pools`` (kg/m3/d)."""
        self._set(pools)
        return [(_value(rate) - (_value(row.body) - _value(row.upper)) / coefficient) * SECONDS_PER_DAY
                for row, rate, coefficient in zip(self.rows, self.rates, self.rate_coefficients)]

    def generation(self, pools: Sequence[float]) -> List[float]:
        """N^T r: each pool's net generation by reaction (kg/m3/d)."""
        rates = self.rates_per_d(pools)
        return [math.fsum(nu[j] * rate for nu, rate in zip(self.nu, rates)) for j in range(len(POOLS))]

    def rate_derivatives(self, pools: Sequence[float]) -> List[List[float]]:
        """d(rate)/d(pools) of each reaction (1/d), implied by its row: -d body/d pool / (d body/d rate)."""
        self._set(pools)
        return [[-float(self._differentiate(row.body, wrt=var, mode=self._mode)) / coefficient * SECONDS_PER_DAY
                 for var in self.pool_vars] for row, coefficient in zip(self.rows, self.rate_coefficients)]

    def jacobian(self, pools: Sequence[float]) -> List[List[float]]:
        """d(N^T r)/d(pools) (1/d)."""
        d_rates = self.rate_derivatives(pools)
        return [[math.fsum(nu[i] * d_rate[j] for nu, d_rate in zip(self.nu, d_rates)) for j in range(len(POOLS))]
                for i in range(len(POOLS))]


def _train(model: Any, reactors: Sequence[Any]) -> Dict[str, Any]:
    """The pool transport over the reactor train: volumes, outlet flows, and the fraction of the last reactor's pool
    flow that returns to the first (membrane retentate x RAS split, per pool)."""
    fs = model.fs
    recycle = _value(fs.SludgeSplitter.split_fraction[0.0, "recycle"])
    return {
        "volumes_m3": [_value(unit.volume[0.0]) for unit in reactors],
        "flows_m3_d": [_value(unit.control_volume.properties_out[0.0].flow_vol) * SECONDS_PER_DAY for unit in reactors],
        "return_fraction": [(1.0 - _value(fs.MBR.split_fraction[0.0, "permeate", p])) * recycle for p in POOLS],
    }


def _transport_matrix(train: Dict[str, Any]):
    """The linear pool transport (1/d) over the train, state ordered reactor-major then pool (one pool per return
    fraction)."""
    import numpy as np

    volumes, flows, returned = train["volumes_m3"], train["flows_m3_d"], train["return_fraction"]
    count, width = len(volumes), len(returned)
    matrix = np.zeros((count * width, count * width))
    for k in range(count):
        for j in range(width):
            row = k * width + j
            matrix[row, row] -= flows[k] / volumes[k]
            if k:
                matrix[row, (k - 1) * width + j] += flows[k - 1] / volumes[k]
            else:
                matrix[row, (count - 1) * width + j] += returned[j] * flows[-1] / volumes[0]
    return matrix


def _outside_storage_domain(u, k_max: float) -> List[tuple]:
    """(reactor index, X_PP/X_PAO) of every reactor of ``u`` (reactor-major POOLS) outside 0 <= X_PP/X_PAO <= K_MAX."""
    width = len(POOLS)
    return [(k, float(u[k * width + 2] / u[k * width])) if u[k * width] > 0.0 else (k, None)
            for k in range(len(u) // width)
            if not (u[k * width] > 0.0 and 0.0 <= u[k * width + 2] <= k_max * u[k * width])]


def _ray(dynamics: Any, start, size: int, domain=None) -> Dict[str, Any]:
    """Integrate the projected dynamics from ``start`` to its balanced-growth ray; the ray, lambda and the checks.
    ``domain(u)``, where given, is True inside the model's domain: a Newton step from inside it to outside is
    rejected (the integration goes on)."""
    import numpy as np
    from scipy.integrate import solve_ivp

    def projected(_t, u):
        f = dynamics.f(u)
        return f - f.sum() * u

    def projected_jacobian(_t, u):
        f, jac = dynamics.f(u), dynamics.jacobian(u)
        return jac - np.outer(u, jac.sum(axis=0)) - f.sum() * np.eye(size)

    def polish(u):
        """Newton on F(u) - lambda u = 0, sum u = 1 from ``u``: the polished ray, or None (no convergence, a pool
        driven negative or a step leaving ``domain``: the integration goes on). Past RAY_RELATIVE_TOLERANCE it takes up
        to NEWTON_EXTRA_STEPS more while the residual still falls (quadratic convergence: to the rounding floor),
        keeping the best point."""
        lam = float(dynamics.f(u).sum())
        best, extra = None, 0
        inside = domain is not None and domain(u)
        for _ in range(NEWTON_MAX_STEPS):
            f = dynamics.f(u)
            relative = float(np.max(np.abs(f - lam * u))) / float(np.max(np.abs(f)))
            if relative <= RAY_RELATIVE_TOLERANCE and abs(u.sum() - 1.0) <= RAY_RELATIVE_TOLERANCE:
                if best is not None and relative >= best[0]:
                    return best[1]
                best, extra = (relative, u), extra + 1
                if extra > NEWTON_EXTRA_STEPS:
                    return u
            system = np.zeros((size + 1, size + 1))
            system[:size, :size] = dynamics.jacobian(u) - lam * np.eye(size)
            system[:size, size] = -u
            system[size, :size] = 1.0
            try:
                step = np.linalg.solve(system, -np.concatenate([f - lam * u, [u.sum() - 1.0]]))
            except np.linalg.LinAlgError:
                return None
            u, lam = u + step[:size], lam + float(step[size])
            if float(u.min()) < -RAY_RELATIVE_TOLERANCE:
                return None if best is None else best[1]
            u = np.clip(u, 0.0, None)
            if inside and not domain(u):
                rejected["steps"] += 1
                return None if best is None else best[1]
        return None if best is None else best[1]

    u = np.asarray(start, dtype=float)
    u = u / u.sum()
    legs, residual, lam, attracting, rejected = 0, math.inf, None, None, {"steps": 0}
    while legs < MAX_LEGS:
        legs += 1
        solution = solve_ivp(projected, (0.0, LEG_DAYS), u, method="BDF", jac=projected_jacobian,
                             rtol=INTEGRATION_RTOL, atol=INTEGRATION_ATOL)
        if not solution.success:
            raise PaoInvasionError(f"the ray integration failed on leg {legs}: {solution.message}")
        u = np.clip(solution.y[:, -1], 0.0, None)
        if u.sum() <= 0.0:
            raise PaoInvasionError("the ray integration lost every pool")
        u = u / u.sum()
        polished = polish(u)
        if polished is None:
            continue
        ray = polished / polished.sum()
        f = dynamics.f(ray)
        lam = float(f.sum())
        residual = float(np.max(np.abs(f - lam * ray)))
        if residual > RAY_RELATIVE_TOLERANCE * float(np.max(np.abs(f))):
            continue
        jac = dynamics.jacobian(ray)
        euler = float(np.max(np.abs(jac @ ray - f)))
        if euler > HOMOGENEITY_RELATIVE_TOLERANCE * float(np.max(np.abs(f))):
            raise PaoInvasionError(f"the pool balances are not homogeneous of degree 1 on the ray: |dF/dv u - F| "
                                   f"{euler!r} against |F| {float(np.max(np.abs(f)))!r} (the method's premise; R10-R17 "
                                   "changed?)")
        eigenvalues = np.linalg.eigvals(jac)
        dominant = float(max(eigenvalues.real))
        matched = float(min(abs(eigenvalues - lam)))
        scale = max(abs(lam), float(np.max(np.abs(np.diag(jac)))))
        attracting = (matched <= HOMOGENEITY_RELATIVE_TOLERANCE * scale
                      and dominant <= lam + HOMOGENEITY_RELATIVE_TOLERANCE * scale)
        if attracting:
            u = ray
            break
    else:
        if attracting is False:
            raise PaoInvasionError(f"lambda {lam!r} per d is not the dominant eigenvalue of dF/dv on the ray (dominant "
                                   f"{dominant!r}, nearest {matched!r}): the ray found does not attract")
        raise PaoInvasionError(f"the ray did not settle in {MAX_LEGS} legs of {LEG_DAYS:g} d: residual {residual!r} "
                               f"at lambda {lam!r} per d")
    return {"start": [float(x) for x in start], "invasion_rate_per_d": lam, "ray": u, "legs": legs,
            "integrated_days": legs * LEG_DAYS, "ray_residual": residual, "euler_residual": euler,
            "dominant_eigenvalue_per_d": dominant, "polish_steps_rejected_outside_domain": rejected["steps"]}


class _Dynamics:
    def __init__(self, rates: Sequence[_ReactorRates], transport) -> None:
        self.rates, self.transport, self.width = list(rates), transport, len(POOLS)

    def f(self, u):
        import numpy as np

        out = self.transport @ u
        for k, reactor in enumerate(self.rates):
            out[k * self.width:(k + 1) * self.width] += np.asarray(
                reactor.generation(u[k * self.width:(k + 1) * self.width]))
        return out

    def jacobian(self, u):
        import numpy as np

        out = self.transport.copy()
        for k, reactor in enumerate(self.rates):
            block = slice(k * self.width, (k + 1) * self.width)
            out[block, block] += np.asarray(reactor.jacobian(u[block]))
        return out


def storage_substrate_bound(rates: Sequence[_ReactorRates], train: Dict[str, Any], k_max: float) -> Dict[str, Any]:
    """An upper bound on the invasion rate from the storage substrate (X_PHA) the train offers a PAO population, read
    off the installed rows (STORAGE_SUBSTRATE_RULING_ID). W = X_PAO + w X_PHA, w the largest X_PAO yield on X_PHA of the
    rows that grow X_PAO (each must consume X_PHA), counts every stored PHA as grown PAO, so only a PHA source raises W.
    Per reactor, W's generation is at most (w s - b_PAO) X_PAO - b_PHA w X_PHA: s the PHA the sources store per unit
    X_PAO at the storage domain's largest X_PP/X_PAO (K_MAX), b each pool's lysis (the rows consuming it alone); every
    other row that moves W lowers it and is dropped. Transport + diag(max(w s - b_PAO, -b_PHA)) is a Metzler matrix that
    bounds W, so its dominant eigenvalue bounds the growth of W, hence of X_PAO and X_PHA; X_PP, stored only from X_PHA,
    then decays at transport - b_PP. The bound is the larger of the two. Checked, not assumed, at the domain's corner
    (X_PHA/X_PAO 1, X_PP/X_PAO K_MAX): every row homogeneous of degree 1 (Euler), the sources reading no X_PHA and not
    falling with X_PP, each lysis row linear in its pool alone; and every row non-negative there and at X_PP/X_PAO
    K_MAX/2. Raises `PaoInvasionError` naming the check that fails."""
    import numpy as np

    nu = np.asarray(rates[0].nu, dtype=float)          # reaction x pool: one reaction package over the train
    growth = nu[:, 0] > 0.0
    if not growth.any() or bool((nu[growth, 1] >= 0.0).any()):
        raise PaoInvasionError("no storage substrate bound: a row grows X_PAO without consuming X_PHA")
    w = float(np.max(nu[growth, 0] / -nu[growth, 1]))
    gain = nu[:, 0] + w * nu[:, 1]                       # each row's W per unit rate; zero within its rounding
    gain[np.abs(gain) <= 8.0 * np.finfo(float).eps * (np.abs(nu[:, 0]) + w * np.abs(nu[:, 1]))] = 0.0
    lysis = [[r for r in range(len(nu)) if nu[r, j] < 0.0 and not np.delete(nu[r], j).any()] for j in range(len(POOLS))]
    lysed = {r for rows in lysis for r in rows}
    sources = [r for r in range(len(nu)) if gain[r] > 0.0]
    dropped = [r for r in range(len(nu)) if gain[r] < 0.0 and r not in lysed]
    corner, interior = np.array([1.0, 1.0, k_max]), np.array([1.0, 1.0, 0.5 * k_max])
    rows, w_rate, pp_rate = [], [], []
    for reactor in rates:
        inside = np.asarray(reactor.rates_per_d(interior))
        rate = np.asarray(reactor.rates_per_d(corner))
        d_rate = np.asarray(reactor.rate_derivatives(corner))
        # each check to HOMOGENEITY_RELATIVE_TOLERANCE of the reactor's largest row term
        allowed = HOMOGENEITY_RELATIVE_TOLERANCE * float(max(np.max(np.abs(rate)), np.max(np.abs(d_rate) @ corner)))
        failed = [(name, check) for r, name in enumerate(reactor.reactions) for check, ok in (
            ("homogeneous of degree 1", abs(float(d_rate[r] @ corner) - rate[r]) <= allowed),
            ("non-negative", min(rate[r], inside[r]) >= -allowed),
            ("a source reading no X_PHA and not falling with X_PP",
             r not in sources or (abs(d_rate[r, 1]) <= allowed and d_rate[r, 2] >= -allowed)),
            ("lysis linear in its pool alone", r not in lysed or bool(
                (np.abs(np.delete(d_rate[r], int(np.nonzero(nu[r])[0][0]))) <= allowed).all())),
        ) if not ok]
        if failed:
            raise PaoInvasionError(f"no storage substrate bound in {reactor.unit.local_name}: {failed}")
        stored = math.fsum(gain[r] * rate[r] for r in sources)             # W per unit X_PAO
        per_pool = [math.fsum(-nu[r, j] * d_rate[r, j] for r in lysis[j]) for j in range(len(POOLS))]
        w_rate.append(max(stored - per_pool[0], -per_pool[1]))
        pp_rate.append(-per_pool[2])
        rows.append({"unit": reactor.unit.local_name, "w_stored_per_x_pao_per_d": stored,
                     "lysis_per_d": dict(zip(POOLS, per_pool)), "w_rate_bound_per_d": w_rate[-1],
                     "dropped_rows_per_x_pao_per_d_at_interior": {reactor.reactions[r]: float(inside[r]) for r in dropped}})
    returned = train["return_fraction"]
    w_matrix = _transport_matrix({**train, "return_fraction": [max(returned[0], returned[1])]}) + np.diag(w_rate)
    pp_matrix = _transport_matrix({**train, "return_fraction": [returned[2]]}) + np.diag(pp_rate)
    w_dominant = float(max(np.linalg.eigvals(w_matrix).real))
    pp_dominant = float(max(np.linalg.eigvals(pp_matrix).real))
    return {
        "ruling": STORAGE_SUBSTRATE_RULING_ID,
        "method": ("upper bound on the PAO invasion rate from the storage substrate: W = X_PAO + w X_PHA over the "
                   "train (transport + per-reactor (w x PHA stored per X_PAO at X_PP/X_PAO = K_MAX - X_PAO lysis, or "
                   "- X_PHA lysis)), and X_PP at its lysis and transport; the larger dominant eigenvalue"),
        "invasion_rate_upper_bound_per_d": max(w_dominant, pp_dominant),
        "w_dominant_eigenvalue_per_d": w_dominant, "x_pp_dominant_eigenvalue_per_d": pp_dominant,
        "w_x_pao_per_x_pha": w, "source_reactions": [rates[0].reactions[r] for r in sources],
        "lysis_reactions": {pool: [rates[0].reactions[r] for r in lysis[j]] for j, pool in enumerate(POOLS)},
        "dropped_reactions": [rates[0].reactions[r] for r in dropped], "corner": corner.tolist(),
        "interior": interior.tolist(), "reactors": rows,
    }


def _reset_pools(reactors: Sequence[Any]) -> None:
    for unit in reactors:   # every reactor, including one whose rates raised after moving its pools
        for pool in POOLS:
            unit.control_volume.reactions[0.0].conc_mass_comp_ref[pool].set_value(0.0, skip_validation=True)


def _state_mg_l(unit: Any) -> Dict[str, float]:
    state = unit.control_volume.properties_out[0.0]
    return {c: _value(state.conc_mass_comp[c]) * 1000.0 for c in ("S_A", "S_F", "S_PO4", "S_O2", "S_NO3", "S_NH4")}


def pao_invasion(model: Any, reactors: Sequence[Any], *, pool_reactions: Sequence[str],
                 srt_d: float | None = None) -> Dict[str, Any]:
    """The PAO invasion rate at the pinned solution of ``model`` (built and solved with the pools pinned at zero),
    over ``reactors`` in flow order. Returns the record (``invasion_rate_basis`` ``storage_substrate_upper_bound`` where
    no ray answered and the bound did); raises `PaoInvasionError` naming the checks that have no answer. The pools are
    back at exactly zero on return, either way."""
    import numpy as np

    fs = model.fs
    k_max = _value(fs.rxn.K_MAX)
    starts = [(1.0, pha, pp * k_max) for pha, pp in START_DIRECTIONS]
    rates: List[_ReactorRates] = []
    width, bound, unanswered = len(POOLS), None, None
    try:
        for unit in reactors:
            rates.append(_ReactorRates(unit, fs.rxn, pool_reactions))
        train = _train(model, reactors)
        transport = _transport_matrix(train)
        dynamics = _Dynamics(rates, transport)
        size = len(reactors) * len(POOLS)
        try:
            # POLISH_DOMAIN_RULING_ID: a polish step leaving the storage domain is rejected
            searches = [_ray(dynamics, np.tile(start, len(reactors)), size,
                             domain=lambda u: not _outside_storage_domain(u, k_max)) for start in starts]
            for search in searches:
                outside = [(reactors[k].local_name, ratio) for k, ratio in _outside_storage_domain(search["ray"], k_max)]
                if outside:
                    raise PaoInvasionError(f"the ray from {search['start']} leaves the model's polyphosphate storage "
                                           f"domain X_PP/X_PAO <= K_MAX {k_max!r}: {outside}")
        except PaoInvasionError as exc:
            # STORAGE_SUBSTRATE_RULING_ID: no ray answers; a population its storage substrate cannot grow washes out
            unanswered = f"{type(exc).__name__}: {exc}"
            try:
                bound = storage_substrate_bound(rates, train, k_max)
            except PaoInvasionError as bound_exc:
                raise PaoInvasionError(f"{exc}; {bound_exc}") from exc
            if not bound["invasion_rate_upper_bound_per_d"] < 0.0:
                raise PaoInvasionError(f"{exc}; the storage substrate bound does not settle it: "
                                       f"{bound['invasion_rate_upper_bound_per_d']!r} per d >= 0") from exc
    finally:
        _reset_pools(reactors)
    if bound is not None:
        return {
            "ruling": PAO_INVASION_RULING_ID, "method": bound["method"],
            "invasion_rate_per_d": bound["invasion_rate_upper_bound_per_d"], "invasive": False,
            "invasion_rate_basis": "storage_substrate_upper_bound", "ray_search_unanswered": unanswered,
            "storage_substrate": bound,
            "reactors": [{**row, "state_mg_L": _state_mg_l(unit)} for unit, row in zip(reactors, bound["reactors"])],
            "searches": [], "srt_d": srt_d, "wastage_rate_per_d": 1.0 / srt_d if srt_d and srt_d > 0 else None,
            "train": train, "starts": [list(start) for start in starts], "k_max": k_max,
            "tolerances": {"ray_relative": RAY_RELATIVE_TOLERANCE, "homogeneity_relative": HOMOGENEITY_RELATIVE_TOLERANCE},
        }
    try:
        worst = max(searches, key=lambda search: search["invasion_rate_per_d"])
        u = worst["ray"]
        reactor_rows = []
        for k, (unit, reactor) in enumerate(zip(reactors, rates)):
            share = u[k * width:(k + 1) * width]
            generation = reactor.generation(share)
            reactor_rows.append({
                "unit": unit.local_name,
                "ray_share": {p: float(x) for p, x in zip(POOLS, share)},
                "x_pha_to_x_pao": float(share[1] / share[0]) if share[0] > 0 else None,
                "x_pp_to_x_pao": float(share[2] / share[0]) if share[0] > 0 else None,
                "x_pao_net_specific_growth_per_d": float(generation[0] / share[0]) if share[0] > 0 else None,
                "reaction_rates_per_unit_pao_per_d": (
                    {r: float(rate / share[0]) for r, rate in zip(reactor.reactions, reactor.rates_per_d(share))}
                    if share[0] > 0 else None),
                "state_mg_L": _state_mg_l(unit),
            })
    finally:
        _reset_pools(reactors)
    rays = np.array([search["ray"] for search in searches])
    lam = worst["invasion_rate_per_d"]
    return {
        "ruling": PAO_INVASION_RULING_ID,
        "method": ("balanced-growth ray of the PAO pools (X_PAO, X_PHA, X_PP) over the reactor train at the pinned "
                   "solution: F(u) = lambda u, F the pool balances (CSTR transport, membrane particulate split, RAS "
                   "split) plus N^T r from the installed rate expressions of " + ", ".join(pool_reactions)
                   + "; integrated from each start, checked by its residual, Euler homogeneity and dominance"),
        "invasion_rate_per_d": lam,
        "invasive": lam >= 0.0,
        "reactors": reactor_rows,
        "searches": [{key: (value.tolist() if hasattr(value, "tolist") else value) for key, value in search.items()}
                     for search in searches],
        "rays_spread": float(np.max(np.abs(rays - rays[0]))),
        "invasion_rate_spread_per_d": float(max(s["invasion_rate_per_d"] for s in searches)
                                            - min(s["invasion_rate_per_d"] for s in searches)),
        "srt_d": srt_d,
        "wastage_rate_per_d": 1.0 / srt_d if srt_d and srt_d > 0 else None,
        "train": train,
        "starts": [list(start) for start in starts],
        "k_max": k_max,
        "tolerances": {"ray_relative": RAY_RELATIVE_TOLERANCE, "homogeneity_relative": HOMOGENEITY_RELATIVE_TOLERANCE},
    }

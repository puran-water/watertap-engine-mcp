"COD in (every Feed unit's outlet) = COD out in the liquid and solids (every liquid Product unit's inlet) + the methane at\nthe COD equivalence (house_standards.methane_cod: the biogas products' S_ch4, kg COD, as normal m3 over 0.3502) + the\nnamed other routes: H2 and H2S in the biogas, the headspace S0 deposit, and the O2 a dosed oxidant brought (in less what\nleft in the gas). Each state is weighted by its oxygen demand to CO2 / sulfate / Fe(III): the shared kernel's\n``process_model_schema.madm1_microaeration.OXYGEN_DEMAND`` (COD-measured states 1, O2 -1, sulfide and organic S 2\nmol O2/mol S, S0 1.5, sulfate 0), plus iron by its electrons (Fe(II) one, FeS nine: 8 kg COD per kmol electrons).\nA state the table does not know is refused, never weighted 0.\n\nPure over port snapshots (``results.streams.extract_port_snapshot``: ``flow_vol[t]`` m3/s, ``conc_mass_comp[t,j]``\nkg/m3), so it is checked without a solver; ``actions._solve_built_adm1_session`` reads the live ports and demotes a\nsolve whose balance does not close."

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

__all__ = ["COD_WEIGHTS", "ZERO_COD_STATES", "CodBalanceNotEvaluable", "adm1_cod_methane_balance", "stream_loads_kg_d"]

SECONDS_PER_DAY = 86400.0
_KEY = re.compile(r"^(?P<member>[A-Za-z_]+)\[(?P<index>[^\]]*)\]$")


class CodBalanceNotEvaluable(ValueError):
    """A port carries a state whose oxygen demand the house does not hold, or no Feed / Product could be read."""


def _weights() -> tuple[dict[str, float], frozenset[str]]:
    from puran_mcp_support.engineering._sibling_libs import ensure_sibling_importable
    from puran_mcp_support.engineering.house_standards.methane_cod import COD_KG_PER_KMOL_ELECTRON

    ensure_sibling_importable('puran_mcp_support.schema')
    from puran_mcp_support.schema.madm1_microaeration import OXYGEN_DEMAND
    from puran_mcp_support.schema.madm1_iron_minerals import FORMULA_MASS, IRON_PER_FORMULA, SULFUR_PER_FORMULA
    from puran_mcp_support.schema.madm1_sfe_kinetics import MADM1_SFE_KERNEL, MW_Fe, MW_S

    weights = dict(OXYGEN_DEMAND)
    # iron to Fe(III): Fe(II) gives one electron, FeS nine (Fe(II) -> Fe(III) and S(-II) -> S(+VI)), vivianite and
    # siderite one per Fe(II), mackinawite FeS's nine, struvite none (its ammonium is N, never COD)
    weights["S_Fe2"] = COD_KG_PER_KMOL_ELECTRON / float(MW_Fe)
    weights["X_FeS"] = 9.0 * COD_KG_PER_KMOL_ELECTRON / (float(MW_Fe) + float(MW_S))
    for mineral, mass in FORMULA_MASS.items():
        electrons = IRON_PER_FORMULA[mineral] + 8.0 * SULFUR_PER_FORMULA.get(mineral, 0.0)
        weights[mineral] = electrons * COD_KG_PER_KMOL_ELECTRON / float(mass)
    zero = {c.component_id for c in MADM1_SFE_KERNEL.components
            if c.measured_as in ("C", "N", "P", "K", "Mg") or c.component_id in ("S_Fe3", "S_SO4")
            or c.component_id.startswith("X_HFO")}
    # the gas package's carbon dioxide, nitrogen and water; the liquid package's charge lumps
    zero |= {"S_co2", "S_n2", "H2O", "S_cat", "S_an", "S_Na", "S_Cl", "S_Ca", "X_PP"}
    return weights, frozenset(zero - set(weights))


COD_WEIGHTS, ZERO_COD_STATES = _weights()


def stream_loads_kg_d(snapshot: Mapping[str, Any]) -> dict[str, float]:
    """``{component: kg/d}`` of one port snapshot (flow_vol m3/s x conc_mass_comp kg/m3).

    Both key shapes are read: a LIVE port's members are references at the time point (``flow_vol[None]``,
    ``conc_mass_comp[S_su]``) and a solved-state export carries the time index (``flow_vol[0.0]``,
    ``conc_mass_comp[0.0,S_su]``; the first time point is taken). A port whose concentrations cannot be read is refused,
    never balanced as empty (an empty read once made every live solve "close" at 0 = 0)."""
    flow = None
    conc: dict[str, float] = {}
    for key, entry in snapshot.items():
        key = str(key)
        m = _KEY.match(key)
        member, index = (m["member"], [tok.strip().strip("'\"") for tok in m["index"].split(",")]) if m else (key, [])
        value = entry.get("value") if isinstance(entry, Mapping) else entry
        if value is None:
            continue
        if member == "flow_vol" and len(index) <= 1:
            flow = float(value) if flow is None else flow
        elif member == "conc_mass_comp" and len(index) in (1, 2):
            conc.setdefault(index[-1], float(value))
    if flow is None:
        raise CodBalanceNotEvaluable(f"a port snapshot carries no flow_vol ({sorted(snapshot)[:6]} ...)")
    if not conc:
        raise CodBalanceNotEvaluable(f"a port snapshot carries no readable conc_mass_comp ({sorted(snapshot)[:6]} ...)")
    return {comp: c * flow * SECONDS_PER_DAY for comp, c in conc.items()}


def _cod(loads: Mapping[str, float], where: str) -> dict[str, float]:
    out = {}
    for comp, kg_d in loads.items():
        if comp in COD_WEIGHTS:
            out[comp] = COD_WEIGHTS[comp] * kg_d
        elif comp not in ZERO_COD_STATES:
            raise CodBalanceNotEvaluable(f"{where}: state {comp!r} has no oxygen demand the house holds "
                                         "(process_model_schema.madm1_microaeration.OXYGEN_DEMAND or the iron "
                                         "weights); it is not weighted 0 silently")
    return out


def _is_gas(loads: Mapping[str, float]) -> bool:
    return "S_co2" in loads


def adm1_cod_methane_balance(engine: str, *, feeds: Iterable[Mapping[str, Any]], products: Mapping[str, Mapping[str, Any]],
                             oxidant_inlets: Iterable[Mapping[str, Any]] = (), s0_deposit_kg_s_d: float = 0.0):
    """The balance of one solved ADM1-family flowsheet (house_standards.methane_cod.CodMethaneBalance).

    ``feeds``: the Feed units' outlet snapshots; ``products``: ``{unit id: its inlet snapshot}`` for every Product unit
    (a biogas product is recognised by its gas package, S_co2); ``oxidant_inlets``: a dosed digester's ``vapor_inlet``
    snapshots; ``s0_deposit_kg_s_d``: the headspace S0 deposit (micro-aeration)."""
    from puran_mcp_support.engineering.house_standards.methane_cod import CH4_COD_KG_PER_KMOL, cod_methane_balance
    from puran_mcp_support.engineering.thermal.gas_constants import NORMAL_MOLAR_VOLUME_M3_KMOL

    feeds = list(feeds)
    if not feeds or not products:
        raise CodBalanceNotEvaluable(f"{engine}: no Feed outlet or no Product inlet to balance")
    cod_in = sum(sum(_cod(stream_loads_kg_d(s), f"{engine} feed").values()) for s in feeds)
    if not cod_in > 0:
        raise CodBalanceNotEvaluable(f"{engine}: the feeds carry no COD ({cod_in!r} kg/d): nothing to balance")
    cod_out, ch4_cod, h2, h2s, o2_gas_out = 0.0, 0.0, 0.0, 0.0, 0.0
    for unit_id, snapshot in products.items():
        loads = stream_loads_kg_d(snapshot)
        cod = _cod(loads, f"{engine} product {unit_id}")
        if _is_gas(loads):
            ch4_cod += cod.pop("S_ch4", 0.0)
            h2 += cod.pop("S_h2", 0.0)
            h2s += cod.pop("S_h2s", 0.0)
            o2_gas_out += -cod.pop("S_o2", 0.0)            # weight -1: the kg O2 that left in the gas
            rest = {k: v for k, v in cod.items() if v}
            if rest:
                raise CodBalanceNotEvaluable(f"{engine}: biogas product {unit_id} carries COD in {sorted(rest)}, which "
                                             "the balance names no route for")
        else:
            cod_out += sum(cod.values())
    o2_in = sum(-_cod(stream_loads_kg_d(s), f"{engine} oxidant").get("S_o2", 0.0) for s in oxidant_inlets)
    deposit = COD_WEIGHTS["S0_deposit"] * float(s0_deposit_kg_s_d)
    methane_nm3_d = ch4_cod / CH4_COD_KG_PER_KMOL * NORMAL_MOLAR_VOLUME_M3_KMOL
    others = {"h2_gas": h2, "h2s_gas": h2s}
    if o2_in or o2_gas_out:
        others["oxidant_consumed"] = o2_in - o2_gas_out
    if deposit:
        others["s0_headspace_deposit"] = deposit
    return cod_methane_balance(engine, cod_in_kg_d=cod_in, cod_out_kg_d=cod_out, methane_nm3_d=methane_nm3_d,
                               engine_methane_cod_kg_d=ch4_cod, other_terms_kg_d=others,
                               notes=("liquid and solids out: every non-gas Product inlet, each state at its oxygen "
                                      "demand (process_model_schema.madm1_microaeration.OXYGEN_DEMAND, iron by its "
                                      "electrons); methane from the biogas products' S_ch4",))

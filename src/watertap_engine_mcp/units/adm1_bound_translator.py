"""ADM1 to ASM2d interface retaining live-biomass N/P in the solids.

X_H is a biomass-equivalent particulate carrier, not a claim that digester
methanogens become aerobic heterotrophs. The small difference between ADM1's
biomass contents and ASM2d's rounded i_NBM/i_PBM is retained by redistributing
COD between X_H and the existing X_S/X_I pools. No biomass N/P goes to NH4/PO4.
A component table unable to represent that nonnegative mixture is refused.
Non-biomass substrate conversions retain WaterTAP's mapping. Carbon uses the
explicit inorganic reconciliation already inherent in that mapping.

Phosphorus the mapping takes for ASM2d X_S/S_F beyond their mADM1 matter's comes
from S_IP; where an S/Fe digester's struvite took that orthophosphate, the
struvite gives back its share (``PHOSPHATE_DRAW_RULE``).
"""
from idaes.core import declare_process_block_class
import pyomo.environ as pyo
from watertap.unit_models.translators.translator_adm1_asm2d import TranslatorDataADM1ASM2D

LIVE_BIOMASS = ("X_su", "X_aa", "X_fa", "X_c4", "X_pro", "X_ac", "X_h2", "X_PAO")


#: phosphorus and takes from S_IP what their mADM1 matter does not carry (a lipid-poor residue: X_ch and X_pr carry

PHOSPHATE_DRAW_RULE = (
    "where the reverse interface's mapping takes phosphorus from S_IP, the digestate's struvite gives back the share "
    "that holds the liquor's Mg x N x P (kmol/m3; the kernel's pH, speciation and activity coefficients held) at the "
    "kernel's, never more struvite P than the mapping takes: its Mg, N and P join S_Mg, S_IN and S_IP before the "
    "mapping and the struvite returned is that much less; the dissolved orthophosphate gives the rest. A P-limited "
    "struvite gives nearly all of the draw, a Mg-limited one almost none. With no Mg, N or P dissolved (a zero "
    "product) the orthophosphate goes first. A mapping that gives P to S_IP stays WaterTAP's")


@declare_process_block_class("BoundNutrientADM1ASM2D")
class BoundNutrientADM1ASM2DData(TranslatorDataADM1ASM2D):
    def build(self):
        super().build()
        rd, ra = self.config.inlet_reaction_package, self.config.outlet_reaction_package
        mw = pyo.units.kg / pyo.units.kmol
        for t in self.flowsheet().time:
            # Upstream's biomass expression feeds its artificial complete decay.
            # Zero that mapping contribution only; the reactor state is unchanged.
            self.biomass[t].set_value(0 * pyo.units.kg / pyo.units.m**3)
            self.return_zero_flow_comp[t, "X_H"].deactivate()

        @self.Expression(self.flowsheet().time)
        def live_biomass(blk, t):
            return sum(blk.properties_in[t].conc_mass_comp[k] for k in LIVE_BIOMASS)

        @self.Expression(self.flowsheet().time, ("C", "N", "P"))
        def live_elements(blk, t, element):
            mass = {"C": 12, "N": 14, "P": 31}[element] * mw
            contents = getattr(rd, element + "i")
            return mass * sum(contents[k] * blk.properties_in[t].conc_mass_comp[k] for k in LIVE_BIOMASS)

        @self.Constraint(self.flowsheet().time)
        def bound_cod(blk, t):
            out = blk.properties_out[t].conc_mass_comp
            xs = blk.Xch_AD1[t] + blk.Xpr_AD1[t] + blk.Xli_AD1[t]
            return out["X_H"] + out["X_S"] + out["X_I"] == blk.live_biomass[t] + xs + blk.XI_AD1[t]

        for t in self.flowsheet().time:
            out = self.properties_out[t].conc_mass_comp
            xs = self.Xch_AD1[t] + self.Xpr_AD1[t] + self.Xli_AD1[t]
            xi = self.XI_AD1[t]
            self.XS_output[t].set_value(
                ra.i_NBM * out["X_H"] + ra.i_NXS * (out["X_S"] - xs)
                + ra.i_NXI * (out["X_I"] - xi) == self.live_elements[t, "N"])
            self.XI_output[t].set_value(
                ra.i_PBM * out["X_H"] + ra.i_PXS * (out["X_S"] - xs)
                + ra.i_PXI * (out["X_I"] - xi) == self.live_elements[t, "P"])
            # Organic carbon changes with the chosen particulate equivalents;
            # preserve total C using the explicit inorganic pool, never N or P.
            self.SIC_output[t].set_value(
                out["S_IC"] == self.properties_in[t].conc_mass_comp["S_IC"]
                + 12 * mw * (self.SIC_AD1[t] + self.SIC_AD2[t])
                + self.live_elements[t, "C"] - ra.i_CXB * out["X_H"]
                - ra.i_CXS * (out["X_S"] - xs) - ra.i_CXI * (out["X_I"] - xi))


def mapped_inorganic_kg_m3(block, t) -> dict:
    """The mapping's S_PO4 (kg P/m3) and S_NH4 (kg N/m3) at a fixed inlet, read off WaterTAP's own output constraints
    (no solve); negative where the liquor cannot give the mapping what it takes."""
    return {"S_PO4": float(pyo.value(block.SPO4_output[t].expr.args[1])),
            "S_NH4": float(pyo.value(block.SNH4_output[t].expr.args[1]))}


def struvite_dissolved_for_draw(*, draw_p: float, draw_n: float, mg: float, n: float, p: float,
                                available: float) -> float:
    """kmol/m3 of struvite giving back the mapping's phosphorus draw (`PHOSPHATE_DRAW_RULE`). All in kmol/m3: the P
    (``draw_p``) and N (``draw_n``, either sign) the mapping takes from S_IP / S_IN, the liquor's dissolved Mg, N and P,
    and the struvite held. Solves (mg + x)(n - draw_n + x)(p - draw_p + x) = mg n p for x in [0, min(draw_p,
    available)]: past the point where every factor is nonnegative the product rises with x, so the root is unique;
    bisection to round-off."""
    hi = min(draw_p, available)
    if not hi > 0.0:
        return 0.0
    target = mg * n * p
    if not target > 0.0:
        return min(max(0.0, draw_p - p), hi)

    def excess(x):
        return (mg + x) * (n - draw_n + x) * (p - draw_p + x) - target

    lo = max(0.0, draw_p - p, draw_n - n)
    if lo >= hi or excess(hi) <= 0.0:
        return hi
    if excess(lo) >= 0.0:
        return lo
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if excess(mid) > 0.0:
            hi = mid
        else:
            lo = mid
        if hi - lo <= 1e-15 * hi:
            break
    return 0.5 * (lo + hi)

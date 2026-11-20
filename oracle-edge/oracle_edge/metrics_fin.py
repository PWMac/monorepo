"""Bank and insurer metrics (their own rulebooks: §3H of the plan).

Standard rules don't fit financials: a bank has no gross margin and its debt (deposits) is its raw
material; an insurer's "debt" is float. These metrics judge them the way Buffett judges them.
"""

from __future__ import annotations

from .metrics import MetricResult, _avg, _clean, _div, _med, cagr


def _avg_base(xs, i):
    a = xs[i]
    b = xs[i - 1] if i > 0 else None
    if a is None:
        return None
    return (a + b) / 2 if b is not None else a


def compute_bank(s: dict) -> dict[str, MetricResult]:
    y = s["years"]
    n = len(y)
    NI, A, EQ = s["net_income"], s["total_assets"], s["equity"]
    out: dict[str, MetricResult] = {}

    roa = [_div(NI[i], _avg_base(A, i)) for i in range(n)]
    out["roa"] = MetricResult(_avg(roa), roa, y)

    def _eff(i):
        nie = s["noninterest_expense"][i]
        nii = s["net_interest_income"][i]
        noni = s["noninterest_income"][i]
        if nie is None:
            return None
        if nii is not None:
            rev = nii + (noni or 0)
        else:
            rev = s["revenue"][i]
        return _div(nie, rev) if rev and rev > 0 else None

    eff = [_eff(i) for i in range(n)]
    out["efficiency"] = MetricResult(_med(eff), eff, y)

    ea = [_div(EQ[i], A[i]) for i in range(n)]
    out["equity_to_assets"] = MetricResult(ea[-1] if ea else None, ea, y)

    ld = [_div(s["loans"][i], s["deposits"][i]) for i in range(n)]
    out["loans_to_deposits"] = MetricResult(ld[-1] if ld else None, ld, y)

    def _nco(i):
        loans = s["loans"][i]
        v = s["net_charge_offs"][i]
        if v is None:
            v = s["provision"][i]
        return _div(abs(v), loans) if (v is not None and loans) else None

    nco = [_nco(i) for i in range(n)]
    nc = _clean(nco)
    res = MetricResult(_avg(nco), nco, y, detail=f"worst year {max(nc):.2%}" if nc else "")
    out["nco_ratio"] = res
    return out


def compute_insurer(s: dict) -> dict[str, MetricResult]:
    y = s["years"]
    n = len(y)
    P, EQ = s["premiums_earned"], s["equity"]
    out: dict[str, MetricResult] = {}

    approx = False

    def _comb(i):
        nonlocal approx
        prem = P[i]
        if not prem or prem <= 0:
            return None
        losses = s["losses_incurred"][i]
        exp = [v for v in (s["dac_amort"][i], s["other_uw_expense"][i]) if v is not None]
        if losses is not None and exp:
            return (losses + sum(exp)) / prem
        # fallback: underwriting result ≈ pre-tax income - net investment income
        pretax, nii = s["pretax_income"][i], s["net_investment_income"][i]
        if pretax is not None and nii is not None:
            approx = True
            return (prem + nii - pretax) / prem
        tbe = s["total_benefits_expenses"][i]
        if tbe is not None:
            approx = True
            return tbe / prem
        return None

    comb = [_comb(i) for i in range(n)]
    cc = _clean(comb)
    share = (sum(1 for c in cc if c < 1.0) / len(cc)) if cc else None
    out["combined_ok_years"] = MetricResult(
        share, comb, y, detail=(f"median combined ratio {_med(cc):.1%}" if cc else "") + (" (approximated)" if approx else ""))

    fl = [((s["loss_reserves"][i] or 0) + (s["unearned_premiums"][i] or 0))
          if (s["loss_reserves"][i] is not None or s["unearned_premiums"][i] is not None) else None for i in range(n)]
    idx = [i for i, v in enumerate(fl) if v]
    fg = cagr(fl[idx[0]], fl[idx[-1]], idx[-1] - idx[0]) if len(idx) >= 3 else None
    ups = [1.0 if fl[i] > fl[i - 1] else 0.0 for i in range(1, n) if fl[i] and fl[i - 1]]
    out["float_growth"] = MetricResult(fg, fl, y, ok_years=(sum(ups) / len(ups)) if ups else None)

    sh = s["shares_diluted"]
    bv = [_div(EQ[i], sh[i]) for i in range(n)]
    bidx = [i for i, v in enumerate(bv) if v and v > 0]
    out["bvps_growth"] = MetricResult(cagr(bv[bidx[0]], bv[bidx[-1]], bidx[-1] - bidx[0]) if len(bidx) >= 3 else None,
                                      bv, y)

    pe = [_div(P[i], EQ[i]) for i in range(n)]
    out["premiums_to_equity"] = MetricResult(_med(pe), pe, y)

    dev = s["prior_year_development"]
    adverse = [1.0 if (dev[i] is not None and EQ[i] and dev[i] > 0.02 * EQ[i]) else 0.0
               for i in range(n) if dev[i] is not None]
    out["adverse_reserve_years"] = MetricResult((sum(adverse) / len(adverse)) if adverse else None, dev, y,
                                                detail="" if adverse else "reserve development not tagged")
    return out


def financial_valuation_inputs(s: dict, m: dict, kind: str) -> dict:
    """Book-value basis for the ROE-justified price-to-book model (tangible book for banks)."""
    n = len(s["years"])
    if not n:
        return {}
    eq, sh = s["equity"][-1], s["shares_diluted"][-1]
    if kind == "bank":
        tang = (eq or 0) - (s["goodwill"][-1] or 0) - (s["intangibles"][-1] or 0)
        basis = _div(tang, sh) if tang and tang > 0 else _div(eq, sh)
    else:
        basis = _div(eq, sh)
    return {"book_ps": basis, "book_kind": "tangible book" if kind == "bank" else "book"}

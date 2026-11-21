"""Intrinsic value, expected return and price levels (the two conservative models from §5 of the plan).

Model A: owner-earnings DCF discounted at the long-term Treasury rate (+2 pts while rates are low).
Model B: Buffettology-style 10-year projection: grow EPS at sustainable growth, apply a capped P/E.
The app always uses the LOWER value and the LOWER expected return.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass
class Valuation:
    iv: float | None
    iv_a: float | None
    iv_b: float | None
    er: float | None
    er_a: float | None
    er_b: float | None
    mos: float | None
    buy_below: float | None
    strong_buy_below: float | None
    expensive_above: float | None
    discount_rate: float
    growth: float
    exit_pe: float
    pe_now: float | None
    pe_median: float | None
    earnings_yield: float | None
    pepg: float | None
    note: str = ""
    extra: dict | None = None

    def to_dict(self):
        return self.__dict__


def discount_rate(t30: float, st: dict) -> float:
    r = t30 + (st.get("low_rate_addon", 0.02) if t30 < st.get("low_rate_threshold", 0.05) else 0.0)
    return max(r, st["terminal_growth"] + 0.02)


def _flows(oe0: float, g: float, tg: float) -> list[float]:
    flows, v = [], oe0
    for t in range(1, 11):
        gt = g if t <= 5 else g + (tg - g) * (t - 5) / 5
        v *= 1 + gt
        flows.append(v)
    return flows


def _pv(flows, terminal, rate) -> float:
    return sum(f / (1 + rate) ** t for t, f in enumerate(flows, 1)) + terminal / (1 + rate) ** 10


def _irr(price, flows, terminal) -> float | None:
    if price <= 0:
        return None
    lo, hi = -0.5, 1.0
    if _pv(flows, terminal, hi) > price:
        return hi
    if _pv(flows, terminal, lo) < price:
        return lo
    for _ in range(80):
        mid = (lo + hi) / 2
        if _pv(flows, terminal, mid) > price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def median_pe(eps_by_fy: dict[str, float], fy_prices: dict[str, float]) -> float | None:
    pes = [fy_prices[y] / e for y, e in eps_by_fy.items() if y in fy_prices and e and e > 0]
    pes = [p for p in pes if 0 < p < 200]
    return median(pes) if len(pes) >= 3 else None


def value(price: float, vi: dict, st: dict, t30: float, mos_gate: float, fy_prices: dict[str, float]) -> Valuation:
    tg = st["terminal_growth"]
    r = discount_rate(t30, st)
    hurdle = st["hurdle"]
    cap = st["exit_pe_cap"]
    oe0 = vi.get("oe_ps")
    eps0 = vi.get("eps3")
    sust = max(0.0, vi.get("sust_g") or 0.0)
    eg = vi.get("eps_cagr")
    g = min(sust, eg if eg is not None else sust, st["growth_cap"])
    g = max(0.0, g)
    pe_med = median_pe(vi.get("eps_by_fy", {}), fy_prices)
    exit_pe = min(pe_med, cap) if pe_med else 15.0
    eps_last = vi.get("eps_last")
    pe_now = price / eps_last if eps_last and eps_last > 0 else None
    ey = (oe0 / price) if (oe0 and price) else None
    pepg = None
    if pe_now and vi.get("ebitda_g") and vi["ebitda_g"] > 0:
        pepg = pe_now / (vi["ebitda_g"] * 100)

    # ---- Model A
    iv_a = er_a = None
    if oe0 and oe0 > 0:
        flows = _flows(oe0, g, tg)
        tv = min(flows[-1] * (1 + tg) / (r - tg), flows[-1] * cap)
        iv_a = _pv(flows, tv, r)
        er_a = _irr(price, flows, flows[-1] * exit_pe)
        price_at_hurdle_a = _pv(flows, flows[-1] * exit_pe, hurdle)
    else:
        price_at_hurdle_a = 0.0

    # ---- Model B
    iv_b = er_b = None
    if eps0 and eps0 > 0:
        gb = min(sust, 0.20)
        eps10 = eps0 * (1 + gb) ** 10
        p10 = eps10 * exit_pe
        divs = sum(eps0 * (1 + gb) ** t * vi.get("payout", 0.0) for t in range(1, 11))
        er_b = ((p10 + divs) / price) ** 0.1 - 1 if price > 0 else None
        iv_b = (p10 + divs) / (1 + hurdle) ** 10

    ivs = [v for v in (iv_a, iv_b) if v is not None]
    ers = [v for v in (er_a, er_b) if v is not None]
    if not ivs:
        return Valuation(None, iv_a, iv_b, None, er_a, er_b, None, None, None, None, r, g, exit_pe, pe_now, pe_med, ey,
                         pepg, "no positive owner earnings or EPS")
    iv = min(ivs) if iv_a is not None and iv_b is not None else ivs[0]
    if iv_a is None or iv_b is None:
        iv *= 0.85  # only one model available: be extra conservative
    er = min(ers) if ers else None
    mos = 1 - price / iv if iv > 0 else None
    hurdle_prices = [p for p in (price_at_hurdle_a if iv_a is not None else None, iv_b) if p is not None]
    hp = min(hurdle_prices) if hurdle_prices else iv
    buy_below = min(iv * (1 - mos_gate), hp)
    strong_mos = mos_gate + st.get("strong_extra_mos", 0.15)
    strong = min(iv * (1 - strong_mos), hp)
    return Valuation(iv, iv_a, iv_b, er, er_a, er_b, mos, buy_below, strong, 1.5 * iv, r, g, exit_pe, pe_now, pe_med,
                     ey, pepg)


def value_enhanced(price: float, vi: dict, st: dict, t30: float, mos_gate: float,
                   fy_prices: dict[str, float]) -> Valuation:
    """Enhanced model (profile setting valuation_model: enhanced). Same two methods as value(), with:
      - growth from retained earnings x the LOWER of ROIC and incremental ROIC (ROE can be flattered by
        buybacks shrinking equity; incremental ROIC shows what the next retained dollar earns),
      - normalized earnings: the cycle's median margin x recent sales, not one good or bad year,
      - owner earnings net of MAINTENANCE capex only (growth capex is a reinvestment, already in growth),
      - per-share value after expected dilution (net share-count growth),
      - bear / base / bull scenarios, probability-weighted (scenario_weights).
    Falls back to the classic inputs when a newer figure is missing."""
    tg = st["terminal_growth"]
    r = discount_rate(t30, st)
    hurdle = st["hurdle"]
    cap = st["exit_pe_cap"]
    oe0 = vi.get("oe_norm_ps") or vi.get("oe_ps")
    eps0 = vi.get("eps_norm") or vi.get("eps3")
    if oe0 is not None and vi.get("oe_ps") is not None:
        oe0 = min(oe0, max(vi["oe_ps"], 0.0) * 1.25) if vi["oe_ps"] > 0 else oe0  # don't let normalizing run away
    roic = vi.get("roic_avg")
    inc = vi.get("incr_roic")
    base_ret = roic if roic is not None else (vi.get("roe") or 0.0)
    if inc is not None:
        base_ret = min(base_ret, max(inc, 0.0))
    retention = max(0.0, 1.0 - (vi.get("payout") or 0.0) - min(vi.get("buyback_payout") or 0.0, 0.5))
    g = retention * max(base_ret, 0.0)
    eg = vi.get("eps_cagr")
    if eg is not None:
        g = min(g, max(eg, 0.0) + 0.02)
    g = min(max(g, 0.0), st["growth_cap"])
    dil = min(max(vi.get("share_cagr") or 0.0, 0.0), 0.05)
    pe_med = median_pe(vi.get("eps_by_fy", {}), fy_prices)
    exit_pe = min(pe_med, cap) if pe_med else 15.0
    eps_last = vi.get("eps_last")
    pe_now = price / eps_last if eps_last and eps_last > 0 else None
    ey = (oe0 / price) if (oe0 and price) else None
    pepg = (pe_now / (vi["ebitda_g"] * 100)) if (pe_now and vi.get("ebitda_g") and vi["ebitda_g"] > 0) else None
    w = st.get("scenario_weights") or [0.25, 0.5, 0.25]
    scen = {"bear": (max(g * 0.5 - 0.01, -0.02), exit_pe * 0.75, w[0]),
            "base": (g, exit_pe, w[1]),
            "bull": (min(g * 1.25 + 0.01, st["growth_cap"] + 0.03), min(exit_pe * 1.15, cap), w[2])}
    out = {}
    for name, (gs, pe_s, wt) in scen.items():
        iv_a = er_a = iv_b = er_b = None
        if oe0 and oe0 > 0:
            flows = [f / (1 + dil) ** t for t, f in enumerate(_flows(oe0, gs, min(tg, gs + 0.02)), 1)]
            tv = min(flows[-1] * (1 + tg) / (r - tg), flows[-1] * cap)
            iv_a = _pv(flows, tv, r)
            er_a = _irr(price, flows, flows[-1] * pe_s)
        if eps0 and eps0 > 0:
            eps10 = eps0 * (1 + gs) ** 10 / (1 + dil) ** 10
            divs = sum(eps0 * (1 + gs) ** t / (1 + dil) ** t * (vi.get("payout") or 0.0) for t in range(1, 11))
            p10 = eps10 * pe_s
            er_b = ((p10 + divs) / price) ** 0.1 - 1 if price > 0 else None
            iv_b = (p10 + divs) / (1 + hurdle) ** 10
        ivs = [v for v in (iv_a, iv_b) if v is not None]
        ers = [v for v in (er_a, er_b) if v is not None]
        if not ivs:
            continue
        iv_s = min(ivs) if len(ivs) == 2 else ivs[0] * 0.85
        hp_a = _pv(flows, flows[-1] * pe_s, hurdle) if iv_a is not None else None
        hps = [x for x in (hp_a, iv_b) if x is not None]
        out[name] = {"iv": iv_s, "er": min(ers) if ers else None, "g": gs, "exit_pe": pe_s, "w": wt,
                     "iv_a": iv_a, "iv_b": iv_b, "hp": min(hps) if hps else iv_s}
    if "base" not in out:
        return Valuation(None, None, None, None, None, None, None, None, None, None, r, g, exit_pe, pe_now, pe_med,
                         ey, pepg, "no positive owner earnings or EPS", {"model": "enhanced"})
    tw = sum(v["w"] for v in out.values())
    iv = sum(v["iv"] * v["w"] for v in out.values()) / tw
    ers = [(v["er"], v["w"]) for v in out.values() if v["er"] is not None]
    er = sum(e * wt for e, wt in ers) / sum(wt for _, wt in ers) if ers else None
    mos = 1 - price / iv if iv > 0 else None
    b = out["base"]
    # price earning the hurdle: each scenario's own hurdle price, probability-weighted like the value itself
    hp = sum(v["hp"] * v["w"] for v in out.values()) / tw
    buy_below = min(iv * (1 - mos_gate), hp)
    strong = min(iv * (1 - (mos_gate + st.get("strong_extra_mos", 0.15))), hp)
    extra = {"model": "enhanced", "scenarios": {k: {"iv": v["iv"], "er": v["er"], "g": v["g"], "exit_pe": v["exit_pe"]}
                                                for k, v in out.items()},
             "incr_roic": inc, "roic": roic, "dilution": dil, "normalized_eps": vi.get("eps_norm"),
             "normalized_oe": vi.get("oe_norm_ps"), "maint_capex_share": vi.get("maint_capex_share")}
    return Valuation(iv, b["iv_a"], b["iv_b"], er, None, None, mos, buy_below, strong, 1.5 * iv, r, g, exit_pe,
                     pe_now, pe_med, ey, pepg, "", extra)


# ===================================================================== value-driver model (0.16)
def classic_growth(vi: dict, cap: float = 0.15) -> float:
    """The classic model's growth: the lower of ROE x retained share and the 10-year EPS growth, 0..cap."""
    sust = max(0.0, vi.get("sust_g") or 0.0)
    eg = vi.get("eps_cagr")
    return max(0.0, min(sust, eg if eg is not None else sust, cap))


def blended_growth(vi: dict, st: dict) -> dict:
    """The growth the record supports: the average of 5-year sales growth, the classic estimate and a long-run
    rate (7%), capped (12%). Past growth barely predicts future growth (Chan, Karceski & Lakonishok 2003), so
    each source gets a third and none is trusted alone."""
    gc = classic_growth(vi)
    lr = float(st.get("vd_long_run_growth", 0.07))
    rv = vi.get("vd_rev5")
    rv = gc if rv is None else min(max(rv, -0.05), 0.25)
    g = min(max((rv + gc + lr) / 3, 0.0), float(st.get("vd_growth_cap", 0.12)))
    return {"g": g, "sales": vi.get("vd_rev5"), "classic": gc, "long_run": lr}


def vd_value(nopat: float, g: float, roic: float | None, r: float, tg: float, g12: float | None = None) -> float:
    """Enterprise value: 10 years of free cash flow, then a terminal value, by the value-driver formula
    V = NOPAT x (1 - g / ROIC) / (r - g). Growth g for years 1-5 (g12 for years 1-2 when analyst estimates are
    used), fading to tg over years 6-10; each year reinvests g / ROIC of its profit. In the terminal value new
    capital earns max(r, ROIC / 2): competition erodes the excess over time. Each year's profit funds the next
    year's growth, so growth that earns exactly r adds nothing, as the formula says it should."""
    roic = min(max(roic or r, r), 1.0)
    gs = [0.0] + [(g12 if (g12 is not None and t <= 2) else g) if t <= 5 else g + (tg - g) * (t - 5) / 5
                  for t in range(1, 11)] + [tg]
    pv, e = 0.0, nopat
    for t in range(1, 11):
        e *= 1 + gs[t]
        # this year's profit funds next year's growth: reinvest g(t+1) / ROIC of it
        pv += e * (1 - max(gs[t + 1], 0.0) / roic) / (1 + r) ** t
    ronic = max(r, roic / 2)
    tv = e * (1 + tg) * (1 - tg / ronic) / (r - tg)
    return pv + tv / (1 + r) ** 10


def _solve(f, target: float, lo: float, hi: float, increasing: bool) -> float:
    """x in [lo, hi] with f(x) = target (bisection); clamps to the ends."""
    flo, fhi = f(lo), f(hi)
    if increasing:
        if target <= flo:
            return lo
        if target >= fhi:
            return hi
    else:
        if target >= flo:
            return lo
        if target <= fhi:
            return hi
    for _ in range(70):
        mid = (lo + hi) / 2
        if (f(mid) < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def vd_base(vi: dict, ttm: dict | None = None) -> dict | None:
    """Normalized after-tax operating profit (median margin of 7 years x average sales of 3), with the latest
    trailing-twelve-month figures taking the oldest year's place when a 10-Q is newer than the annual report."""
    sales = [v for v in (vi.get("vd_sales") or []) if v]
    margins = [v for v in (vi.get("vd_margins") or []) if v is not None]
    cash, claims, shares = vi.get("vd_cash") or 0.0, dict(vi.get("vd_claims") or {}), vi.get("vd_shares")
    used_ttm = False
    if ttm and ttm.get("sales") and ttm.get("oi") is not None and len(sales) >= 1:
        sales = sales[-2:] + [ttm["sales"]]
        margins = margins[-6:] + [ttm["oi"] / ttm["sales"]]
        if ttm.get("cash") is not None:
            cash = ttm["cash"]
        if ttm.get("shares"):
            shares = ttm["shares"]
        if ttm.get("debt") is not None:
            claims["debt"] = ttm["debt"]
        if ttm.get("leases") is not None:
            claims["leases"] = ttm["leases"]
        used_ttm = True
    if not sales or len(margins) < 3 or not shares or shares <= 0:
        return None
    from statistics import median as _m

    margin = _m(margins)
    t = vi.get("vd_tax") or 0.21
    rev = sum(sales) / len(sales)
    return {"nopat": margin * rev * (1 - t), "margin": margin, "sales": rev, "tax": t, "roic": vi.get("vd_roic"),
            "cash": cash, "claims": claims, "net_cash": cash - sum(claims.values()), "shares": shares,
            "ttm": used_ttm}


def value_driver(price: float, vi: dict, st: dict, mos_gate: float, fy_prices: dict[str, float],
                 ttm: dict | None = None, est: dict | None = None) -> Valuation:
    """One consistent model (valuation_model: value_driver). Value = the free cash flow of a normalized
    operating profit growing at the blended growth rate, discounted at ONE required return (the profile's hurdle),
    plus net cash, per diluted share. Also: a low / likely / high range (growth -/+ 3 points, required return
    +/- 1 point), the expected return the price implies, and the growth the price assumes (reverse DCF)."""
    r = float(st.get("vd_required_return") or st["hurdle"])
    tg = min(st["terminal_growth"], r - 0.02)
    pe_med = median_pe(vi.get("eps_by_fy", {}), fy_prices)
    eps_last = vi.get("eps_last")
    pe_now = price / eps_last if eps_last and eps_last > 0 else None
    oe0 = vi.get("oe_ps")
    ey = (oe0 / price) if (oe0 and price) else None
    pepg = (pe_now / (vi["ebitda_g"] * 100)) if (pe_now and vi.get("ebitda_g") and vi["ebitda_g"] > 0) else None
    gb = blended_growth(vi, st)
    g = gb["g"]
    b = vd_base(vi, ttm if st.get("vd_use_ttm", True) else None)
    none = Valuation(None, None, None, None, None, None, None, None, None, None, r, g, 0.0, pe_now, pe_med, ey, pepg,
                     "no operating profit to value", {"model": "value_driver", "growth": gb})
    if not b or b["nopat"] <= 0:
        return none
    g12 = None
    w = float(st.get("vd_estimate_weight", 0.5))
    if est and st.get("vd_use_estimates", True) and est.get("growth") is not None and (est.get("analysts") or 0) >= 3:
        g12 = w * min(max(est["growth"], -0.20), 0.40) + (1 - w) * g
    mult = vi.get("vd_ps_mult", 1.0)  # foreign filers: reporting currency per share -> US$ per traded share
    sh = b["shares"]

    def iv_at(gg, rr, g12_=g12):
        return (vd_value(b["nopat"], gg, b["roic"], rr, min(tg, rr - 0.02), g12_) + b["net_cash"]) / sh * mult

    iv = iv_at(g, r)
    if iv <= 0:
        return none
    dg, dr = float(st.get("vd_range_growth", 0.03)), float(st.get("vd_range_rate", 0.01))
    low = max(iv_at(max(g - dg, -0.03), r + dr, None if g12 is None else g12 - dg), 0.0)
    high = iv_at(g + dg, max(r - dr, tg + 0.015), None if g12 is None else g12 + dg)
    target = price
    er = _solve(lambda rr: iv_at(g, rr), target, max(tg + 0.005, 0.02), 0.60, increasing=False)
    implied = _solve(lambda gg: iv_at(gg, r, None), target, -0.15, 0.60, increasing=True)
    mos = 1 - price / iv
    buy_below = iv * (1 - mos_gate)
    strong = iv * (1 - (mos_gate + st.get("strong_extra_mos", 0.15)))
    extra = {"model": "value_driver", "low": low, "high": high, "required_return": r, "growth": gb, "g12": g12,
             "estimates_used": g12 is not None, "implied_growth": implied, "nopat": b["nopat"], "margin": b["margin"],
             "sales": b["sales"], "tax": b["tax"], "roic": b["roic"], "net_cash": b["net_cash"], "cash": b["cash"],
             "claims": b["claims"], "shares": sh, "ttm": b["ttm"], "rnd": vi.get("vd_rnd"),
             "reinvest": min(max(g, 0.0) / min(max(b["roic"] or r, r), 1.0), 1.0)}
    return Valuation(iv, iv, None, er, er, None, mos, buy_below, strong, 1.5 * iv, r, g, 0.0, pe_now, pe_med, ey,
                     pepg, "", extra)


def predictability_mos(mos_gate: float, vi: dict, st: dict) -> float:
    """Ask for a bigger margin of safety when earnings are less predictable (predictability 0-1)."""
    if not st.get("predictability_mos"):
        return mos_gate
    p = vi.get("predictability")
    if p is None:
        return mos_gate + float(st.get("predictability_mos_add", 0.20)) * 0.5
    return mos_gate + float(st.get("predictability_mos_add", 0.20)) * min(max(1.0 - p, 0.0), 1.0)


def value_financial(price: float, vi: dict, st: dict, t30: float, mos_gate: float,
                    fy_prices: dict[str, float]) -> Valuation:
    """Banks & insurers: ROE-justified price-to-book (Model A) and the 10-year projection (Model B).

    Justified P/B = (ROE - g) / (k - g), with k = the higher of the hurdle and the Treasury-based rate
    (owner earnings are meaningless for a bank, so there is no DCF), capped by fin_pb_cap.
    """
    st2 = dict(st)
    st2["growth_cap"] = st.get("fin_growth_cap", 0.06)
    st2["exit_pe_cap"] = st.get("fin_exit_pe_cap", 15)
    vi2 = dict(vi)
    vi2["oe_ps"] = None
    vi2["sust_g"] = min(max(vi.get("sust_g") or 0.0, 0.0), st2["growth_cap"])
    vb = value(price, vi2, st2, t30, mos_gate, fy_prices)
    book = vi.get("book_ps")
    roe = vi.get("roe") or 0.0
    g = vi2["sust_g"]
    k = max(st["hurdle"], discount_rate(t30, st))
    cap = st.get("fin_pb_cap", 3.0)
    pb = cap if k <= g else min(max((roe - g) / (k - g), 0.0), cap)
    iv_a = book * pb if book and book > 0 else None
    ivs = [x for x in (iv_a, vb.iv_b) if x is not None]
    if not ivs:
        return Valuation(None, iv_a, vb.iv_b, None, None, vb.er_b, None, None, None, None, k, g, vb.exit_pe,
                         vb.pe_now, vb.pe_median, None, vb.pepg, "no book value or EPS")
    iv = min(ivs) if len(ivs) == 2 else ivs[0] * 0.85
    payout = min(max(vi.get("payout") or 0.0, 0.0), 1.0)
    div_ps = payout * roe * book if (book and book > 0 and roe > 0) else 0.0  # this year's dividend per share

    def er_a_at(p: float) -> float | None:
        """Model A's yearly return at price p over 10 years: book grows at g, the price/book catches up to the
        justified level, plus the dividend yield (the earnings not retained are paid out)."""
        if not (iv_a and p and p > 0):
            return None
        return (iv_a / p) ** 0.1 * (1 + g) - 1 + div_ps / p

    er = vb.er_b
    er_a = er_a_at(price)
    if er is not None and er_a is not None:
        er = min(er, er_a)
    elif er is None:
        er = er_a
    mos = 1 - price / iv if iv > 0 else None
    # the highest price that still earns the hurdle under every model used (er_a falls as the price rises)
    hps = [vb.iv_b] if vb.iv_b is not None else []
    if iv_a:
        hps.append(_solve_price(er_a_at, st["hurdle"], iv_a))
    hp = min(hps) if hps else iv
    buy_below = min(iv * (1 - mos_gate), hp)
    strong = min(iv * (1 - (mos_gate + st.get("strong_extra_mos", 0.15))), hp)
    return Valuation(iv, iv_a, vb.iv_b, er, er_a, vb.er_b, mos, buy_below, strong, 1.5 * iv, k, g, vb.exit_pe,
                     vb.pe_now, vb.pe_median, None, vb.pepg, f"P/B justified {pb:.2f}×")


def _solve_price(er_at, hurdle: float, scale: float) -> float:
    """The price at which er_at(price) = hurdle, for a return that falls as the price rises (bisection)."""
    lo, hi = scale * 1e-4, scale * 100.0
    if (er_at(hi) or -1) >= hurdle:
        return hi
    if (er_at(lo) or -1) < hurdle:
        return 0.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if (er_at(mid) or -1) >= hurdle:
            lo = mid
        else:
            hi = mid
    return lo


def value_score(er: float | None, rule: dict | None = None) -> float:
    """0 at 5%, 70 at the hurdle, 100 at 20%+ (or 5 points above the hurdle when the hurdle is 15%+)."""
    if er is None:
        return 0.0
    t = (rule or {}).get("target", 0.10)
    lo, hi = 0.05, max(0.20, t + 0.05)
    if er <= lo:
        return 0.0
    if er >= hi:
        return 100.0
    if er <= t:
        return 70.0 * (er - lo) / (t - lo)
    return 70.0 + 30.0 * (er - t) / (hi - t)


def rank_score(quality: float, value_sc: float, conf: float, wq: float) -> float:
    q = max(quality, 0.01)
    v = max(value_sc, 0.01)
    return (q ** wq) * (v ** (1 - wq)) * conf

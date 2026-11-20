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
    er = vb.er_b
    er_a = ((iv_a / price) ** 0.1 * (1 + g) - 1) if (iv_a and price) else None  # value catch-up + growth, 10 yrs
    if er is not None and er_a is not None:
        er = min(er, er_a)
    mos = 1 - price / iv if iv > 0 else None
    hp = vb.iv_b if vb.iv_b is not None else iv
    buy_below = min(iv * (1 - mos_gate), hp)
    strong = min(iv * (1 - (mos_gate + st.get("strong_extra_mos", 0.15))), hp)
    return Valuation(iv, iv_a, vb.iv_b, er, er_a, vb.er_b, mos, buy_below, strong, 1.5 * iv, k, g, vb.exit_pe,
                     vb.pe_now, vb.pe_median, None, vb.pepg, f"P/B justified {pb:.2f}×")


def value_score(er: float | None, rule: dict | None = None) -> float:
    """0 at 5%, 70 at the hurdle, 100 at 20%+."""
    if er is None:
        return 0.0
    lo, t, hi = 0.05, (rule or {}).get("target", 0.10), 0.20
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

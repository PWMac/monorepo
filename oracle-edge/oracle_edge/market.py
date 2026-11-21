"""Market context: Buffett Indicator (market cap / GDP), Treasury yields, regime zones."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from . import db

ZONES = [  # (upper bound, name, mos gate when regime modifier is on)
    (0.90, "Undervalued", 0.20),
    (1.20, "Fair", 0.25),
    (1.50, "Modestly overvalued", 0.30),
    (2.00, "Significantly overvalued", 0.35),
    (99.0, "Extreme", 0.40),
]


def quarter_end(q_start: str) -> str:
    """Last day of the quarter that starts on q_start (FRED's quarterly date)."""
    from datetime import date as _date, timedelta as _td

    q0 = _date.fromisoformat(q_start)
    return (_date(q0.year + (q0.month + 2) // 12, (q0.month + 2) % 12 + 1, 1) - _td(days=1)).isoformat()


def _add_days(d: str, n: int) -> str:
    from datetime import date as _date, timedelta as _td

    return (_date.fromisoformat(d) + _td(days=n)).isoformat()


@dataclass
class Regime:
    ratio: float | None
    zone: str
    mos_gate: float
    as_of: str | None
    t30: float
    t10: float | None


class Macro:
    """Loads macro series once; answers 'as of date' questions fast (used by backtests too)."""

    def __init__(self):
        self.s: dict[str, tuple[list[str], list[float]]] = {}
        for (series,) in db.get().execute("SELECT DISTINCT series FROM macro").fetchall():
            rs = db.rows("SELECT date, value FROM macro WHERE series=? ORDER BY date", (series,))
            self.s[series] = ([r["date"] for r in rs], [r["value"] for r in rs])

    def fx(self, currency: str, d: str) -> float | None:
        """US dollars per 1 unit of `currency` on (or just before) date d."""
        from .sources.fred import FX_SERIES

        if not currency or currency.upper() == "USD":
            return 1.0
        spec = FX_SERIES.get(currency.upper())
        if spec:
            _, v = self.asof(spec[0], d)
            if v:
                return 1.0 / v if spec[1] else v
        _, v = self.asof(f"YFX_{currency.upper()}", d)  # Yahoo fallback: foreign units per 1 USD
        return 1.0 / v if v else None

    def asof(self, series: str, d: str):
        dates, vals = self.s.get(series, ([], []))
        i = bisect_right(dates, d) - 1
        return (dates[i], vals[i]) if i >= 0 else (None, None)

    def t30(self, d: str) -> float:
        _, v = self.asof("DGS30", d)
        return (v / 100.0) if v else 0.045

    def t10(self, d: str) -> float | None:
        _, v = self.asof("DGS10", d)
        return v / 100.0 if v else None

    # ---- Buffett Indicator ------------------------------------------------------------------------------
    # FRED dates a quarter by its first day. Z.1 equity levels are as of the quarter's LAST day and are published
    # about ten weeks later; GDP's advance estimate about a month after the quarter. A reading on date d may only
    # use quarters already published by d, or backtests would see numbers nobody had yet.
    PUB_LAG_DAYS = {"NCBEILQ027S": 75, "BOGZ1LM893064105Q": 75, "GDP": 30}

    def asof_published(self, series: str, d: str):
        """Latest quarterly observation that had been published by date d: (quarter start, value)."""
        dates, vals = self.s.get(series, ([], []))
        lag = self.PUB_LAG_DAYS.get(series, 0)
        for i in range(bisect_right(dates, d) - 1, -1, -1):
            if _add_days(quarter_end(dates[i]), lag) <= d:
                return dates[i], vals[i]
        return None, None

    def us_components(self, d: str, price=None, point_in_time: bool = True) -> dict | None:
        """The parts of the US Buffett Indicator on date d (all in US$):
        equities_q   nonfinancial corporate equities at the end of the latest published quarter (Fed Z.1)
        equities     that level scaled to d by the total-market ETF's PRICE change (VTI, dividends excluded:
                     paid-out dividends don't add to market value)
        gdp          latest published nominal GDP, seasonally adjusted annual rate
        ratio        equities / gdp"""
        get = self.asof_published if point_in_time else self.asof
        qd, eq = get("NCBEILQ027S", d)
        src = "NCBEILQ027S"
        if eq is None:
            qd, eq = get("BOGZ1LM893064105Q", d)
            src = "BOGZ1LM893064105Q"
        gd, gdp = get("GDP", d)
        if eq is None or not gdp:
            return None
        q_end = quarter_end(qd)
        scale = 1.0
        if price and q_end < d:
            p0, p1 = price(q_end), price(d)
            if p0 and p1:
                scale = p1 / p0
        eq_q = eq * 1e6     # $ millions -> $
        gdp_usd = gdp * 1e9  # $ billions -> $
        return {"date": d, "equities_q": eq_q, "equities": eq_q * scale, "quarter_end": q_end, "scale": scale,
                "gdp": gdp_usd, "gdp_quarter": gd, "gdp_quarter_end": quarter_end(gd), "series": src,
                "ratio": eq_q * scale / gdp_usd}

    def buffett_indicator(self, d: str, vti_px=None, point_in_time: bool = True) -> tuple[float | None, str | None]:
        c = self.us_components(d, vti_px, point_in_time)
        return (c["ratio"], c["quarter_end"]) if c else (None, None)

    def us_history(self) -> list[dict]:
        """Quarterly history at quarter ends: equities and the same quarter's GDP (final, revised figures)."""
        eqd, eqv = self.s.get("NCBEILQ027S") or self.s.get("BOGZ1LM893064105Q") or ([], [])
        gdp = dict(zip(*self.s.get("GDP", ([], []))))
        out = []
        for q, e in zip(eqd, eqv):
            g = gdp.get(q)
            if g:
                out.append({"date": quarter_end(q), "equities": e * 1e6, "gdp": g * 1e9, "ratio": e * 1e6 / (g * 1e9)})
        return out

    def world_history(self) -> list[dict]:
        """Annual world figures from the World Bank (end-of-year market value of listed domestic companies, GDP)."""
        mc = dict(zip(*self.s.get("WB_WLD_MCAP", ([], []))))
        gd = dict(zip(*self.s.get("WB_WLD_GDP", ([], []))))
        pub = dict(zip(*self.s.get("WB_WLD_MCAP_GDP", ([], []))))
        out = []
        for y in sorted(set(mc) | set(pub)):
            m, g = mc.get(y), gd.get(y)
            ratio = (m / g) if (m and g) else ((pub[y] / 100.0) if pub.get(y) is not None else None)
            if ratio is not None:
                out.append({"date": y, "equities": m, "gdp": g, "ratio": ratio})
        return out

    def world_now(self, d: str, vt_px=None) -> dict | None:
        """Today's estimate: the latest year-end world market value scaled by the world total-market ETF's PRICE
        change since then (VT), over the latest world GDP the World Bank has published."""
        mc = [(y, v) for y, v in zip(*self.s.get("WB_WLD_MCAP", ([], []))) if y <= d]
        gd = [(y, v) for y, v in zip(*self.s.get("WB_WLD_GDP", ([], []))) if y <= d]
        if not mc or not gd:
            return None
        (my, m), (gy, g) = mc[-1], gd[-1]
        scale = 1.0
        if vt_px:
            p0, p1 = vt_px(my), vt_px(d)
            if p0 and p1:
                scale = p1 / p0
        return {"date": d, "equities_y": m, "equities": m * scale, "year_end": my, "scale": scale, "gdp": g,
                "gdp_year": gy[:4], "ratio": m * scale / g}

    def regime(self, d: str, base_mos: float, modifier_on: bool, vti_px=None) -> Regime:
        ratio, asof = self.buffett_indicator(d, vti_px)
        zone, gate = "Unknown", base_mos
        if ratio is not None:
            for ub, name, g in ZONES:
                if ratio < ub:
                    zone, gate = name, (max(g, base_mos) if modifier_on else base_mos)
                    break
        return Regime(ratio, zone, gate, asof, self.t30(d), self.t10(d))


# ---------------------------------------------------------------- market overlay (new purchases only)
OVERLAY_TEXT = {
    "normal": "S&P 500 above its 200-day average: normal buying",
    "caution": "S&P 500 below its 200-day average: new purchases limited, cash builds",
    "stress": "S&P 500 below its 200-day average and VIX above the stress level: new buying sharply reduced",
}


def overlay_state(pb, macro, d: str, st: dict) -> str:
    """normal | caution | stress, from SPY vs its 200-day average and the VIX. Recovering above the average
    resumes normal purchases. Used only for new purchases and reserve deployment, never to sell holdings."""
    px = pb.adj("SPY", d)
    ma = pb.sma("SPY", d, int(st.get("overlay_ma_days", 200)))
    if px is None or ma is None or px >= ma:
        return "normal"
    vix = None
    if macro is not None:
        _, vix = macro.asof("VIXCLS", d)
    if vix is not None and vix > float(st.get("overlay_vix_stress", 30)):
        return "stress"
    return "caution"


def overlay_factor(state: str, st: dict) -> float:
    """Share of the usual new-money purchases still made in this state."""
    return {"normal": 1.0, "caution": float(st.get("overlay_caution_buy", 0.35)),
            "stress": float(st.get("overlay_stress_buy", 0.10))}.get(state, 1.0)


def price_lookup(ticker: str):
    """date -> last close (split-adjusted price, dividends NOT added) on or before it."""
    import bisect

    rows = db.rows("SELECT date, close FROM prices WHERE ticker=? AND close > 0 ORDER BY date", (ticker,))
    dates = [r["date"] for r in rows]
    vals = [r["close"] for r in rows]

    def px(x):
        i = bisect.bisect_right(dates, x) - 1
        return vals[i] if i >= 0 else None
    return px


def regime_now(d: str | None = None):
    """Today's Buffett Indicator regime for the default profile (scaled daily by VTI's price), plus the Macro used."""
    from datetime import date

    from . import rules

    m = Macro()
    vti = price_lookup("VTI")
    p = rules.default_profile()
    return m.regime(d or date.today().isoformat(), p.settings["mos_gate"], p.settings.get("regime_modifier", False),
                    vti), m, vti

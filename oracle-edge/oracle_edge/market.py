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
        if not spec:
            return None
        _, v = self.asof(spec[0], d)
        if not v:
            return None
        return 1.0 / v if spec[1] else v

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

    def buffett_indicator(self, d: str, vti_adj=None) -> tuple[float | None, str | None]:
        qd, eq = self.asof("BOGZ1LM893064105Q", d)
        if eq is None:
            qd, eq = self.asof("NCBEILQ027S", d)
        _, gdp = self.asof("GDP", d)
        if eq is None or not gdp:
            return None, None
        ratio = (eq / 1000.0) / gdp
        # scale by the total-market ETF's move since the quarter's value date
        if vti_adj and qd:
            p0, p1 = vti_adj(qd), vti_adj(d)
            if p0 and p1:
                ratio *= p1 / p0
        return ratio, qd

    def regime(self, d: str, base_mos: float, modifier_on: bool, vti_adj=None) -> Regime:
        ratio, asof = self.buffett_indicator(d, vti_adj)
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

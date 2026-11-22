"""What a stock is worth (the value-driver model), shown on every stock's Report card whatever model the profile
uses: the likely value and its range, the expected return at today's price, what the price assumes compared with
what the record supports, the P/E against the company's own history, and how the value is built."""

from __future__ import annotations

import html
from datetime import date

from nicegui import ui

from .. import db, estimates
from ..engine import PriceBook, _foreign_scale, split_scale, ttm_inputs
from ..splits import Splits
from ..valuation import value_driver
from .layout import kpi
from .theme import money, pct

GREEN, BLUE, AMBER, RED, GREY = "#1f6f4a", "#2c5d8f", "#A8741F", "#a33a2b", "#6b7280"


def view(s: dict, prof, snap: dict | None, price: float | None = None):
    """The value-driver valuation of the latest annual report at today's (or the given) price, or None."""
    if not snap or (snap.get("routed") or "") in ("bank", "insurer"):
        return None
    t = s["ticker"]
    vi = dict(snap["val_inputs"] or {})
    if not vi.get("vd_sales"):
        return None
    sp = Splits()
    vi = split_scale(vi, sp.factor(t, snap["filed"]))
    pb = PriceBook([t])
    today = date.today().isoformat()
    price = price or pb.close(t, today)
    if not price:
        return None
    foreign = bool(s.get("is_foreign"))
    if foreign:
        from ..market import Macro

        info = db.one("SELECT * FROM securities WHERE cik=?", (s["cik"],))
        vi, why = _foreign_scale(vi, dict(info), today, Macro(), None)
        if vi is None:
            return None
    q = None if foreign else db.one("SELECT * FROM qsnaps WHERE cik=? AND filed>? ORDER BY filed DESC LIMIT 1",
                                    (s["cik"], snap["filed"]))
    ttm = ttm_inputs(q, vi, sp.factor(t, q["filed"])) if q else None
    est = None if foreign else estimates.asof(t, today)
    st = prof.settings
    v = value_driver(price, vi, st, st["mos_gate"], pb.fy_prices(t, list(vi.get("eps_by_fy", {}).keys())), ttm, est)
    return v if (v.iv or (v.extra or {}).get("implausible")) else None


def value_line_html(price, low, likely, high, buy_below=None, targets: dict | None = None) -> str:
    """One number line: today's price, our low-likely-high value, the buy-below price and analysts' targets."""
    tl, th, tm = ((targets or {}).get("low"), (targets or {}).get("high"), (targets or {}).get("mean"))
    vals = [v for v in (price, low, likely, high, buy_below, tl, th) if v]
    lo, hi = min(vals) * 0.88, max(vals) * 1.08
    L, W = 150, 820

    def x(v):
        return L + (v - lo) / (hi - lo) * W
    rows = [("Our value", low, likely, high, GREEN, "#d6eadf", "likely")]
    if tl and th:
        rows.append(("Analysts' targets", tl, tm, th, BLUE, "#dbe6f2", "average"))
    top = 58  # room for two staggered labels: today's price, then buy below
    h = top + 34 * len(rows)
    p = [f'<svg viewBox="0 0 1000 {h}" width="100%" style="max-width:1000px;font-family:inherit">']
    for k, (name, a, m, b, c, fill, mlab) in enumerate(rows):
        y = top + 34 * k
        p.append(f'<text x="0" y="{y + 5}" font-size="13" fill="#374151">{name}</text>')
        p.append(f'<rect x="{x(a):.1f}" y="{y - 9}" width="{max(x(b) - x(a), 2):.1f}" height="18" rx="4" fill="{fill}"/>')
        p.append(f'<text x="{x(a) - 4:.1f}" y="{y + 4}" font-size="11" text-anchor="end" fill="{c}">${a:,.0f}</text>')
        p.append(f'<text x="{x(b) + 4:.1f}" y="{y + 4}" font-size="11" fill="{c}">${b:,.0f}</text>')
        if m:
            p.append(f'<rect x="{x(m) - 1.5:.1f}" y="{y - 11}" width="3" height="22" fill="{c}"/>')
            p.append(f'<text x="{x(m):.1f}" y="{y + 22}" font-size="10" text-anchor="middle" fill="{c}">'
                     f'{mlab} ${m:,.0f}</text>')
    if buy_below:
        bx = x(buy_below)
        p.append(f'<line x1="{bx:.1f}" x2="{bx:.1f}" y1="34" y2="{h - 6}" stroke="{GREEN}" stroke-dasharray="4,3"/>')
        p.append(f'<text x="{bx:.1f}" y="30" font-size="11" text-anchor="middle" fill="{GREEN}">buy below '
                 f'${buy_below:,.0f}</text>')
    px = x(price)
    p.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="18" y2="{h - 6}" stroke="#111827" stroke-width="2"/>')
    p.append(f'<text x="{px:.1f}" y="13" font-size="12" font-weight="600" text-anchor="middle" '
             f'fill="#111827">today ${price:,.2f}</text>')
    p.append("</svg>")
    return "".join(p)


def assumes_text(v, est: dict | None = None) -> tuple[str, str, str]:
    """(verdict, colour, sentence) comparing the growth the price assumes with what the record supports."""
    x = v.extra
    imp, sup = x["implied_growth"], x["growth"]["g"]
    gb = x["growth"]
    parts = []
    if gb.get("sales") is not None:
        parts.append(f"sales grew {pct(gb['sales'], 0)} a year over 5 years")
    parts.append(f"the classic estimate is {pct(gb['classic'], 0)}")
    parts.append(f"the long-run rate {pct(gb['long_run'], 0)}")
    s = (f"Today's price assumes about {pct(imp, 0)} a year growth for ten years (fading to "
         f"{pct(min(0.03, x['required_return'] - 0.02), 0)} by year twenty) at a {pct(x['required_return'], 0)} "
         f"discount rate. The record supports {pct(sup, 0)} ({', '.join(parts)}; averaged and capped).")
    if est and est.get("growth") is not None:
        s += f" Analysts expect {pct(est['growth'], 0)} a year over the next {est.get('years', 2)} years."
    if imp >= 0.59:
        return "Price assumes more than any plausible growth", RED, s
    if imp > sup + 0.03:
        return "Price expects more than the record supports", AMBER, s
    if imp < sup - 0.03:
        return "Price expects less than the record supports", GREEN, s
    return "Price is in line with the record", GREY, s


def pe_check(v) -> tuple[str, str, str] | None:
    if not (v.pe_now and v.pe_median):
        return None
    rel = v.pe_now / v.pe_median
    verdict, c = (("cheaper than usual", GREEN) if rel <= 1.0 else ("within 1.5× its usual P/E", GREY) if rel <= 1.5
                  else ("well above its usual P/E", AMBER))
    return verdict, c, (f"P/E {v.pe_now:.1f} on last year's EPS vs its own 10-year median of {v.pe_median:.1f} "
                        f"({rel:.2f}×). A cross-check only: on the app's history it said a little about the next one "
                        "to two years, not reliably.")


def _big(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    sgn = "-" if v < 0 else ""
    return (f"{sgn}${a / 1e12:,.2f}T" if a >= 1e12 else f"{sgn}${a / 1e9:,.2f}B" if a >= 1e9 else
            f"{sgn}${a / 1e6:,.0f}M" if a >= 1e6 else f"{sgn}${a:,.0f}")


def _chip(text, color):
    return (f"<span style='border:1px solid {color};color:{color};border-radius:10px;padding:1px 10px;"
            f"font-size:12px;white-space:nowrap'>{html.escape(text)}</span>")


def render(s: dict, prof, snap: dict | None, ev: dict | None) -> None:
    v = view(s, prof, snap, ev["price"] if ev else None)
    if v is None:
        return
    if not v.iv:
        with ui.card().classes("oe-card w-full"):
            ui.label("What it's worth").classes("oe-h2")
            ui.label(f"No value shown: the model's {v.note}. A value ten times the price or more, either way, has "
                     "almost always meant bad inputs in the app's backtest, not a real bargain or bubble.").classes(
                "oe-muted")
        return
    x = v.extra
    uses = prof.settings.get("valuation_model") == "value_driver"
    est = estimates.asof(s["ticker"], date.today().isoformat())
    tg = None
    r = db.one("SELECT json FROM estimates WHERE ticker=? ORDER BY day DESC LIMIT 1", (s["ticker"],))
    if r:
        import json

        tg = json.loads(r["json"]).get("targets")
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("What it's worth").classes("oe-h2")
            ui.label(("Value-driver model: used for this profile's signals" if uses else
                      "Value-driver model, for comparison: this profile's signals use the "
                      f"{prof.settings.get('valuation_model', 'classic')} model (Profiles → Valuation)")).classes(
                "oe-small")
        price = ev["price"] if ev else None
        with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
            kpi(money(v.iv), "Likely value", f"range {money(x['low'])}–{money(x['high'])}")
            hur = x.get("hurdle") or x["required_return"]
            kpi(pct(v.er, 1) if v.er is not None else "—", "Expected return at today's price",
                f"a year; your hurdle {pct(hur, 0)}", GREEN if (v.er or 0) >= hur else None)
            kpi(money(v.buy_below), "Buy below",
                f"strict: earns your {pct(hur, 0)} hurdle on cautious assumptions, less "
                f"{pct(prof.settings['mos_gate'], 0)}")
            kpi(pct(v.mos, 0) if v.mos is not None else "—", "Margin of safety", "1 − price ÷ likely value")
        if price:
            ui.html(value_line_html(price, x["low"], v.iv, x["high"], v.buy_below, tg), sanitize=False).classes(
                "w-full")
        verdict, c, text = assumes_text(v, est)
        with ui.row().classes("items-start gap-3 no-wrap pt-1"):
            ui.html(_chip(verdict, c), sanitize=False)
            ui.label(text).classes("text-sm")
        pe = pe_check(v)
        if pe:
            with ui.row().classes("items-start gap-3 no-wrap"):
                ui.html(_chip("P/E " + pe[0], pe[1]), sanitize=False)
                ui.label(pe[2]).classes("text-sm")
        ui.label("The range is the likely value with growth 3 points lower and the discount rate 1 point higher "
                 "(low), and the reverse (high). Tested on 3,500 past valuations (2013–2019) against what the companies "
                 "turned out to be worth by 2026, the likely value was right on average (about 10% low), but for any "
                 "one company the outcome landed in this range only about a third of the time; half the time it was "
                 "between 0.5× and 2.3× the likely value. Read it as the middle of a wide band, not a precise figure "
                 "(Guide → How accurate is the value).").classes("oe-small")
        with ui.expansion("How the value is built", icon="calculate").classes("w-full"):
            cl = x["claims"]
            rows = [
                ("Sales base", _big(x["sales"]), "the latest 12 months from the 10-Q" if x["ttm"] else
                 "the latest year"),
                ("Operating margin", pct(x["margin"], 1), "median of the last 3 years; halfway back to the 7-year "
                 "median when that is higher (a dip tends to recover)" + (
                    "; R&D treated as an investment (see below)" if (x.get("rnd") or {}).get("capitalized") else "")),
                ("Tax rate", pct(x["tax"], 0), "median of 5 years, 10–30%"),
                ("Normalized after-tax operating profit", _big(x["nopat"]), "sales × margin × (1 − tax)"),
                ("Return on capital", pct(x["roic"], 0) if x["roic"] is not None else "—",
                 "median of 5 years; growth is paid for at this rate"),
                ("Growth, years 1–10", pct(x["growth"]["g"], 1),
                 "blend of sales growth, the classic estimate and 7%, capped at "
                 f"{pct(prof.settings.get('vd_growth_cap', 0.12), 0)}; fades to 3% by year 20" + (
                     f"; years 1–2 {pct(x['g12'], 1)} with analysts' estimates" if x.get("g12") is not None else "")),
                ("Reinvested each year", pct(x["reinvest"], 0), "growth ÷ return on capital"),
                ("Discount rate", pct(x["required_return"], 1),
                 "the stock market's long-run return (Profiles → Valuation)"),
                ("Your hurdle", pct(x.get("hurdle") or x["required_return"], 1),
                 "buy below = the cautious model's value at this rate, less the margin of safety (the buy signals' "
                 "rule)"),
                ("+ Cash and short-term investments", _big(x["cash"]), ""),
                ("− Debt", _big(cl.get("debt")), ""),
                ("− Lease liabilities", _big(cl.get("leases")), "operating leases counted as debt"),
                ("− Pension shortfall (after tax)", _big(cl.get("pension")), ""),
                ("− Preferred stock", _big(cl.get("preferred")), ""),
                ("− Minority interests", _big(cl.get("minority")), ""),
                ("÷ Diluted shares", f"{x['shares'] / 1e6:,.1f}M", ""),
            ]
            body = "".join(f"<tr><td style='padding:3px 12px 3px 0'>{html.escape(a)}</td><td style='text-align:right;"
                           f"padding:3px 12px;font-weight:600'>{b}</td><td style='color:#6b7280'>{html.escape(c)}</td></tr>"
                           for a, b, c in rows)
            ui.html(f"<table style='font-size:13px;border-collapse:collapse'>{body}</table>", sanitize=False)
            rd = x.get("rnd") or {}
            if rd.get("capitalized"):
                ui.label(f"R&D is {pct(rd.get('share'), 0)} of sales, so it is treated as an investment: each year's "
                         f"spending is written off over {rd.get('life', 5)} years instead of all at once, which lifts "
                         "operating profit for a growing researcher and adds the unwritten-off research "
                         f"({_big(rd.get('asset'))}) to invested capital.").classes("oe-small")
            ui.label("Value = 20 years of free cash flow (profit minus the reinvestment growth needs) and a terminal "
                     "value by the value-driver formula NOPAT × (1 − g ÷ ROIC) ÷ (r − g), in which new capital "
                     "earns the company's return on capital (at least the discount rate).").classes(
                "oe-small")

"""The stock page's Outlook tab: what analysts expect, how their estimates are moving, how often the company has
beaten them, and how all of that compares with what the app's value assumes and what today's price assumes."""

from __future__ import annotations

import json
from datetime import date

from nicegui import run, ui

from .. import estimates, wonderful
from ..splits import Splits
from . import valuecard
from .layout import kpi
from .theme import money, pct

GREEN, BLUE, AMBER, RED, GREY = valuecard.GREEN, valuecard.BLUE, valuecard.AMBER, valuecard.RED, valuecard.GREY


def _g(a, b):
    return (b / a - 1) if (a and b and a > 0) else None


def _next_fy_labels(x: dict, fy_last: str | None) -> tuple[str, str]:
    """Labels of Yahoo's "this year" (0y) and "next year" (+1y): the years after its last fiscal year end."""
    base = x.get("last_fy_end") or fy_last
    y = int(wonderful.fy_label(base)[2:]) if base else date.today().year - 1
    return f"FY{y + 1}e", f"FY{y + 2}e"


def eps_chart(hist: list[tuple[str, float]], x: dict | None, v, labels: tuple[str, str]) -> dict:
    """Reported EPS (bars), analysts' average with their low-high spread, and two paths from the last reported
    year: the growth the app's value assumes and the growth today's price assumes. Everything is placed by fiscal
    year, so estimates line up with the reported years whatever the data sources' timing."""
    hist = hist[-7:]
    rep = {wonderful.fy_label(fy): e for fy, e in hist}
    p = (x or {}).get("periods") or {}
    e0, e1 = p.get("0y") or {}, p.get("+1y") or {}
    y0 = int(labels[0][2:6])
    last_rep = max((int(k[2:]) for k in rep), default=y0 - 1)
    years = list(range(min([int(k[2:]) for k in rep] + [y0]), max(last_rep + 5, y0 + 1) + 1))
    cats = [f"FY{y}" + ("" if f"FY{y}" in rep else "e") for y in years]
    idx = {y: i for i, y in enumerate(years)}

    def col(points: dict[int, float | None]):
        out = [None] * len(years)
        for y, val in points.items():
            if y in idx and val is not None:
                out[idx[y]] = round(val, 2)
        return out
    series = [{"type": "bar", "name": "Reported EPS (SEC)", "data": col({int(k[2:]): e for k, e in rep.items()}),
               "itemStyle": {"color": "#9aa5a0"}, "barMaxWidth": 34}]
    shown = [e for e in rep.values() if e is not None]
    if e0.get("eps") is not None:
        series.append({"type": "line", "name": "Analysts (average)", "connectNulls": True,
                       "data": col({y0 - 1: e0.get("eps_year_ago"), y0: e0.get("eps"), y0 + 1: e1.get("eps")}),
                       "lineStyle": {"color": BLUE, "width": 3}, "itemStyle": {"color": BLUE}, "symbolSize": 8})
        for k, rot in (("eps_low", 0), ("eps_high", 180)):
            series.append({"type": "line", "name": "Analysts (low – high)", "symbol": "triangle", "symbolSize": 6,
                           "symbolRotate": rot, "data": col({y0: e0.get(k), y0 + 1: e1.get(k)}),
                           "lineStyle": {"color": BLUE, "type": "dotted", "width": 1}, "itemStyle": {"color": BLUE}})
        shown += [e for e in (e0.get("eps_high"), e1.get("eps_high")) if e is not None]
    if v is not None and hist and hist[-1][1] and hist[-1][1] > 0:
        last = hist[-1][1]
        for name, g, c, dash in (("Growth our value assumes", v.extra["growth"]["g"], GREEN, "solid"),
                                 ("Growth today's price assumes", v.extra["implied_growth"], AMBER, "dashed")):
            path = {last_rep + k: last * (1 + g) ** k for k in range(0, 6)}
            series.append({"type": "line", "name": f"{name} ({pct(g, 0)}/yr)", "data": col(path), "symbol": "none",
                           "lineStyle": {"color": c, "width": 2, "type": dash}, "itemStyle": {"color": c}})
            if c == GREEN:
                shown += list(path.values())
    top = max(shown) * 1.35 if shown else None  # a price that assumes extreme growth runs off the top
    return {"tooltip": {"trigger": "axis"}, "legend": {"top": 0, "type": "scroll"},
            "grid": {"left": 48, "right": 16, "top": 40, "bottom": 28},
            "xAxis": {"type": "category", "data": cats},
            "yAxis": {"type": "value", "name": "EPS $", "nameGap": 12,
                      **({"max": float(f"{top:.2g}")} if top else {})},
            "series": series}


def surprise_chart(sp: list[dict]) -> dict:
    xs = list(reversed(sp[:12]))
    return {"tooltip": {"trigger": "axis", ":formatter":
                        "ps => { const d = " + json.dumps([[v["est"], v["actual"]] for v in xs]) + "[ps[0].dataIndex]; "
                        "return `<b>${ps[0].axisValue}</b><br>estimate $${d[0]} · reported $${d[1]}<br>"
                        "surprise <b>${ps[0].value}%</b>`; }"},
            "grid": {"left": 44, "right": 12, "top": 16, "bottom": 28},
            "xAxis": {"type": "category", "data": [v["date"] for v in xs]},
            "yAxis": {"type": "value", "name": "%", "nameGap": 8},
            "series": [{"type": "bar", "barMaxWidth": 28,
                        "data": [{"value": round(100 * (v["surprise"] or 0), 1),
                                  "itemStyle": {"color": GREEN if v["actual"] >= v["est"] else RED}} for v in xs]}]}


def _trend_rows(x: dict, labels: tuple[str, str]) -> str:
    out = []
    for p, lab in (("0y", labels[0]), ("+1y", labels[1])):
        t = (x.get("trend") or {}).get(p) or {}
        r = (x.get("revisions") or {}).get(p) or {}
        if t.get("now") is None:
            continue
        ch = _g(t.get("90d"), t.get("now"))
        col = GREEN if (ch or 0) > 0.005 else RED if (ch or 0) < -0.005 else GREY
        cells = "".join(f"<td style='text-align:right;padding:3px 10px'>{money(t.get(k))}</td>"
                        for k in ("90d", "60d", "30d", "7d", "now"))
        up, dn = r.get("up30"), r.get("down30")
        out.append(f"<tr><td style='padding:3px 10px 3px 0'><b>{lab}</b> EPS</td>{cells}"
                   f"<td style='text-align:right;padding:3px 10px;color:{col};font-weight:600'>"
                   f"{'—' if ch is None else f'{ch:+.1%}'}</td>"
                   f"<td style='padding:3px 10px'>{'' if up is None else f'▲ {up:.0f}'} "
                   f"{'' if dn is None else f'▼ {dn:.0f}'}</td></tr>")
    if not out:
        return ""
    head = ("<tr style='color:#6b7280'><th></th><th style='text-align:right;padding:3px 10px'>90 days ago</th>"
            "<th style='text-align:right;padding:3px 10px'>60</th><th style='text-align:right;padding:3px 10px'>30</th>"
            "<th style='text-align:right;padding:3px 10px'>7</th><th style='text-align:right;padding:3px 10px'>Now</th>"
            "<th style='text-align:right;padding:3px 10px'>Change</th><th style='padding:3px 10px'>Raised / cut "
            "(30 days)</th></tr>")
    return f"<table style='font-size:13px;border-collapse:collapse'>{head}{''.join(out)}</table>"


def summary_lines(x: dict | None, rec: dict, v, labels) -> list[tuple[str, str]]:
    """Plain-English takeaways, each with a colour."""
    out = []
    ng = estimates.near_term_growth(x)
    if v is not None:
        g, imp = v.extra["growth"]["g"], v.extra["implied_growth"]
        a = f"analysts expect {pct(ng['growth'], 0)} a year next {ng['years']} years, " if ng else ""
        out.append((f"Growth: {a}the record supports {pct(g, 0)}, and today's price assumes {pct(imp, 0)} for five "
                    "years.", AMBER if imp > g + 0.03 else GREEN if imp < g - 0.03 else GREY))
    if rec:
        c = GREEN if rec["beats"] >= 0.75 * rec["n"] else AMBER if rec["beats"] >= 0.5 * rec["n"] else RED
        out.append((f"Beat the EPS estimate in {rec['beats']} of the last {rec['n']} quarters (median surprise "
                    f"{rec['median']:+.1%}); {'beat ' + str(rec['streak']) + ' in a row' if rec['streak'] else 'missed last time'}."
                    , c))
    t = ((x or {}).get("trend") or {}).get("0y") or {}
    ch = _g(t.get("90d"), t.get("now"))
    if ch is not None:
        out.append((f"Estimates for {labels[0]} have {'risen' if ch > 0 else 'fallen'} {abs(ch):.1%} in 90 days." if
                    abs(ch) >= 0.005 else f"Estimates for {labels[0]} are unchanged over 90 days.",
                    GREEN if ch > 0.005 else RED if ch < -0.005 else GREY))
    tg = (x or {}).get("targets") or {}
    if tg.get("mean") and v is not None:
        out.append((f"Analysts' average 12-month price target {money(tg['mean'])}; the app's likely value "
                    f"{money(v.iv)} at a {pct(v.extra['required_return'], 0)} required return.", GREY))
    return out


def render(s: dict, prof, snap: dict | None, ev: dict | None) -> None:
    t = s["ticker"]
    box = ui.column().classes("w-full gap-4")
    with box:
        ui.spinner(size="lg")

    async def load():
        x = await run.io_bound(estimates.get, t)
        sp = estimates.surprises(t, 16)
        v = valuecard.view(s, prof, snap, ev["price"] if ev else None)
        box.clear()
        with box:
            draw(x, sp, v)

    def draw(x, sp, v):
        fy_last = snap["fy_end"] if snap else None
        labels = _next_fy_labels(x or {}, fy_last)
        rec = estimates.beat_record(sp)
        if not x and not sp:
            with ui.card().classes("oe-card w-full"):
                ui.label("No analyst estimates found").classes("oe-h2")
                ui.label("Yahoo Finance has no estimates for this ticker, or it couldn't be reached. Many small and "
                         "foreign companies have little or no analyst coverage.").classes("oe-muted")
        p = ((x or {}).get("periods") or {})
        e0, e1 = p.get("0y") or {}, p.get("+1y") or {}
        tg = (x or {}).get("targets") or {}
        price = ev["price"] if ev else None
        if x:
            with ui.grid(columns=5).classes("w-full gap-3 oe-kpis"):
                kpi(money(e0.get("eps")), f"{labels[0]} EPS estimate",
                    f"{pct(_g(e0.get('eps_year_ago'), e0.get('eps')), 0)} vs last year · "
                    f"{(e0.get('analysts') or 0):.0f} analysts")
                kpi(money(e1.get("eps")), f"{labels[1]} EPS estimate",
                    f"{pct(_g(e0.get('eps'), e1.get('eps')), 0)} on {labels[0]}")
                kpi(f"{rec['beats']} of {rec['n']}" if rec else "—", "Quarters beating the estimate",
                    f"median surprise {rec['median']:+.1%}" if rec else "no record yet",
                    GREEN if rec and rec["beats"] >= 0.75 * rec["n"] else None)
                tr = ((x.get("trend") or {}).get("0y") or {})
                ch = _g(tr.get("90d"), tr.get("now"))
                kpi("—" if ch is None else f"{ch:+.1%}", "Estimate change, 90 days", labels[0],
                    GREEN if (ch or 0) > 0.005 else RED if (ch or 0) < -0.005 else None)
                kpi(money(tg.get("mean")), "Average price target",
                    (f"{_g(price, tg.get('mean')):+.0%} vs today" if price and tg.get("mean") else "")
                    + (f" · {tg['analysts']:.0f} analysts" if tg.get("analysts") else ""))
        lines = summary_lines(x, rec, v, labels)
        if lines:
            with ui.card().classes("oe-card w-full"):
                ui.label("In short").classes("oe-h2")
                for text, c in lines:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.icon("circle").style(f"color:{c};font-size:10px;margin-top:6px")
                        ui.label(text).classes("text-sm")
        vi = (snap or {}).get("val_inputs") or {}
        f = Splits().factor(t, snap["filed"]) if snap else 1.0
        hist = sorted((fy, e / f) for fy, e in (vi.get("eps_by_fy") or {}).items() if e is not None)
        if hist or x:
            with ui.card().classes("oe-card w-full"):
                ui.label("Earnings: reported, forecast and assumed").classes("oe-h2")
                ui.label("Grey bars: EPS reported to the SEC. Blue: analysts' average for this year and next, with "
                         "the lowest and highest estimate. Green: the growth the app's value assumes; amber dashed: "
                         "the growth today's price assumes (both from the last reported year). When the amber line "
                         "runs far above the others, the price needs more than the record or the analysts suggest."
                         ).classes("oe-small")
                ui.echart(eps_chart(hist, x, v, labels)).classes("w-full").style("height:340px")
                base = e0.get("eps_year_ago")
                if base and hist and hist[-1][1] and abs(base / hist[-1][1] - 1) > 0.10:
                    ui.label(f"Analysts work from \"adjusted\" EPS ({money(base)} last year) while the SEC figure was "
                             f"{money(hist[-1][1])}: adjusted earnings usually leave out items such as stock pay, "
                             "amortization of acquired intangibles and one-off charges.").classes(
                        "oe-small text-amber-800")
        with ui.row().classes("w-full gap-4 no-wrap items-start"):
            if sp:
                with ui.card().classes("oe-card flex-1"):
                    ui.label("Beat or miss, quarter by quarter").classes("oe-h2")
                    ui.label("Reported EPS vs the average estimate just before the report. Green = beat, red = "
                             "miss. Companies guide analysts low, so most beat; a miss or a shrinking beat is the "
                             "signal worth noticing.").classes("oe-small")
                    ui.echart(surprise_chart(sp)).classes("w-full").style("height:240px")
            if x and _trend_rows(x, labels):
                with ui.card().classes("oe-card flex-1"):
                    ui.label("Are the estimates rising or falling?").classes("oe-h2")
                    ui.label("The average EPS estimate over the last 90 days, and how many analysts raised or cut "
                             "theirs in the last 30. Rising estimates have tended to lead earnings surprises; "
                             "falling ones are an early warning.").classes("oe-small")
                    ui.html(_trend_rows(x, labels), sanitize=False)
                    own = estimates.history(t, "0y")
                    if len(own) >= 5:
                        ui.echart({"tooltip": {"trigger": "axis"}, "grid": {"left": 44, "right": 12, "top": 12,
                                                                            "bottom": 24},
                                   "xAxis": {"type": "category", "data": [d for d, _ in own]},
                                   "yAxis": {"type": "value", "scale": True},
                                   "series": [{"type": "line", "data": [round(e, 3) for _, e in own], "symbol": "none",
                                               "lineStyle": {"color": BLUE}}]}).classes("w-full").style(
                            "height:150px")
                        ui.label("This year's estimate as stored each day by the app.").classes("oe-small")
        if v is not None and price:
            with ui.card().classes("oe-card w-full"):
                ui.label("Price, value and analysts' targets").classes("oe-h2")
                ui.html(valuecard.value_line_html(price, v.extra["low"], v.iv, v.extra["high"], v.buy_below, tg),
                        sanitize=False).classes("w-full")
                ui.label("Analysts' targets are 12-month price forecasts, not values; they follow the price more than "
                         "they lead it. The app's range is what the business is worth at your required return."
                         ).classes("oe-small")
        with ui.expansion("How to use forecasts", icon="help_outline").classes("w-full oe-card"):
            ui.markdown(
                "- **Next one to two years only.** Analysts' near-term estimates are a reasonable starting point; "
                "their long-term growth forecasts are too optimistic and add little (Chan, Karceski & Lakonishok, "
                "*Journal of Finance* 2003). The value-driver valuation blends analysts' next-two-year EPS growth "
                "into years 1–2 at half weight, when at least three analysts cover the company.\n"
                "- **Adjusted EPS.** Yahoo's figures are usually the company's adjusted EPS, often above the EPS "
                "reported to the SEC.\n"
                "- **Beat records flatter everyone.** Most companies beat most quarters because they guide low; "
                "compare a company's record with its own past, and watch for misses and falling estimates.\n"
                "- **Source:** Yahoo Finance (free, unofficial). The app keeps one copy a day for holdings and the "
                "shortlist, and one whenever you open this tab, so it builds its own record over time.")

    ui.timer(0.2, load, once=True)

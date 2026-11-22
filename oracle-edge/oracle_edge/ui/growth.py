"""Stock page → Total return tab: what money put into this stock would have grown to, dividends reinvested, next to
up to 10 other tickers (oracle_edge.growth does the arithmetic)."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

from nicegui import app, run, ui

from .. import db, growth
from .theme import money, pct

MAX_COMPARE = 10
PALETTE = ["#1f6f4a", "#2c5d8f", "#c9a227", "#a33a2b", "#7b5ea7", "#3a8f8f", "#d9822b", "#5b6b7a", "#8c564b", "#c2477a",
           "#6b8e23"]
RANGES = [("1Y", 1), ("3Y", 3), ("5Y", 5), ("10Y", 10), ("Max", None)]


def _series(ticker: str) -> list[tuple[str, float]]:
    return [(r["date"], r["adj_close"] or r["close"]) for r in db.rows(
        "SELECT date, close, adj_close FROM prices WHERE ticker=? ORDER BY date", (ticker,))]


def _cpi() -> list[tuple[str, float]]:
    return [(r["date"], r["value"]) for r in db.rows("SELECT date, value FROM macro WHERE series='CPIAUCSL' ORDER BY date")]


def _fetch(tickers: list[str]) -> list[str]:
    """Download histories the app doesn't have yet (Yahoo); returns the tickers still without prices."""
    from ..pipeline import fetch_prices_for

    try:
        fetch_prices_for(tickers)
    except Exception as e:
        db.log("yahoo", "total return prices", False, str(e)[:200])
    return [t for t in tickers if not db.scalar("SELECT 1 FROM prices WHERE ticker=? LIMIT 1", (t,))]


def clean_tickers(main: str, extra: list[str]) -> list[str]:
    out = [main.upper()]
    for t in extra or []:
        t = re.sub(r"[^A-Za-z0-9.\-^=]", "", str(t)).upper()
        if t and t not in out:
            out.append(t)
    return out[: MAX_COMPARE + 1]


def compute(tickers: list[str], cfg: dict) -> dict:
    """Every ticker's result over one common window: from the later of the chosen start and the first date every
    ticker has prices, so they are compared like for like."""
    data = {t: _series(t) for t in tickers}
    have = {t: s for t, s in data.items() if s}
    if not have:
        return {"results": {}, "missing": tickers}
    start = max([cfg["start"]] + [s[0][0] for s in have.values()])
    end = min([cfg["end"]] + [s[-1][0] for s in have.values()])
    cpi = _cpi() if cfg.get("real") else None
    res = {}
    for t, s in have.items():
        r = growth.simulate(s, start, end, float(cfg["initial"] or 0), float(cfg["amount"] or 0), cfg["freq"], cpi)
        if r:
            res[t] = r
    late = [t for t, s in have.items() if s[0][0] > cfg["start"]]
    return {"results": res, "missing": [t for t in tickers if t not in have], "start": start, "end": end,
            "late": late, "cpi_ok": bool(cpi)}


def chart(out: dict, real: bool, log: bool) -> dict:
    res = out["results"]
    series = []
    first = next(iter(res.values()))
    for k, (t, r) in enumerate(res.items()):
        vals = r["real_values"] if real and "real_values" in r else r["values"]
        wd, (v,) = growth.weekly(r["dates"], vals)
        series.append({"type": "line", "name": t, "showSymbol": False, "data": [[d, round(x, 2)] for d, x in zip(wd, v)],
                       "lineStyle": {"width": 2.4 if k == 0 else 1.6}, "itemStyle": {"color": PALETTE[k % len(PALETTE)]},
                       "z": 3 if k == 0 else 2})
    inv = first["real_invested"] if real and "real_invested" in first else first["invested"]
    wd, (v,) = growth.weekly(first["dates"], inv)
    series.append({"type": "line", "name": "Money put in", "showSymbol": False, "step": "end",
                   "data": [[d, round(x, 2)] for d, x in zip(wd, v)],
                   "lineStyle": {"type": "dashed", "width": 1.4, "color": "#6b7280"}, "itemStyle": {"color": "#6b7280"}})
    return {"tooltip": {"trigger": "axis", ":valueFormatter": "v => '$' + Math.round(v).toLocaleString()"},
            "legend": {"top": 0, "type": "scroll"}, "grid": {"left": 70, "right": 24, "top": 36, "bottom": 60},
            "xAxis": {"type": "time"},
            "yAxis": {"type": "log" if log else "value", "scale": True,
                      "axisLabel": {":formatter": "v => '$' + (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? "
                                                  "(v / 1e3).toFixed(0) + 'k' : v.toFixed(0))"}},
            "dataZoom": [{"type": "inside"}, {"type": "slider", "bottom": 8, "height": 18}],
            "series": series}


def table_html(out: dict, real: bool, names: dict[str, str]) -> str:
    res = out["results"]
    best = max(res, key=lambda t: res[t]["real_final" if real and "real_final" in res[t] else "final"])
    head = ("<tr><th style='text-align:left'>Ticker</th><th>End value</th><th>Put in</th><th>Gain</th><th>Total return</th>"
            "<th title='The stock itself, a year, over the period: what a single lump sum earned (time-weighted)'>"
            "Yearly return</th><th title='Your yearly return counting when each contribution went in (money-weighted, "
            "IRR)'>Your yearly return</th><th title='The worst fall from a high along the way'>Worst fall</th></tr>")
    rows = []
    for k, (t, r) in enumerate(res.items()):
        if real and "real_final" in r:
            fin, inv = r["real_final"], r["real_invested"][-1]
            cagr, irr = r.get("real_cagr"), r.get("real_irr")
        else:
            fin, inv, cagr, irr = r["final"], r["total_invested"], r["cagr"], r["irr"]
        c = PALETTE[k % len(PALETTE)]
        star = " ★" if t == best and len(res) > 1 else ""
        nm = html.escape(names.get(t, ""))[:34]
        rows.append(
            f"<tr><td style='text-align:left;white-space:nowrap'><span style='display:inline-block;width:10px;height:10px;"
            f"border-radius:2px;background:{c};margin-right:6px'></span><a href='/stock/{html.escape(t)}' target='_blank' "
            f"style='color:inherit;font-weight:700;text-decoration:none'>{html.escape(t)}</a>{star} "
            f"<span style='color:#6b7280;font-size:12px'>{nm}</span></td>"
            f"<td><b>{money(fin, 0)}</b></td><td>{money(inv, 0)}</td>"
            f"<td style='color:{'#1f6f4a' if fin >= inv else '#a33a2b'}'>{money(fin - inv, 0)}</td>"
            f"<td>{pct(fin / inv - 1, 1)}</td><td>{pct(cagr, 1) if cagr is not None else '—'}</td>"
            f"<td>{pct(irr, 1) if irr is not None else '—'}</td><td>{pct(r['max_drawdown'], 0)}</td></tr>")
    return ("<table class='oe-tr-table' style='width:100%;border-collapse:collapse;font-size:13px'>"
            f"<thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"
            "<style>.oe-tr-table td,.oe-tr-table th{padding:6px 10px;text-align:right;border-bottom:1px solid #eef0ee}"
            ".oe-tr-table th{color:#6b7280;font-weight:600;font-size:12px}</style>")


def render(s: dict) -> None:
    store = app.storage.user
    saved = dict(store.get("total_return") or {})
    today = date.today()
    cfg = {"initial": saved.get("initial", 10_000), "amount": saved.get("amount", 0), "freq": saved.get("freq", "none"),
           "start": saved.get("start") or (today - timedelta(days=365 * 10)).isoformat(),
           "end": today.isoformat(), "real": bool(saved.get("real", False)), "log": bool(saved.get("log", False)),
           "compare": saved.get("compare", ["SPY"])}
    known = sorted({r["ticker"] for r in db.rows("SELECT DISTINCT ticker FROM prices")} | set(cfg["compare"]))
    has_cpi = bool(db.scalar("SELECT 1 FROM macro WHERE series='CPIAUCSL' LIMIT 1"))

    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Total return: what an investment would have grown to").classes("oe-h2")
            ui.label("Dividends reinvested (adjusted closes) · prices from 2009").classes("oe-small")
        with ui.row().classes("items-end gap-3 w-full"):
            init = ui.number("Initial investment", value=cfg["initial"], min=0, step=1000, format="%.0f").props(
                "dense outlined prefix=$").classes("w-40")
            amt = ui.number("Recurring amount", value=cfg["amount"], min=0, step=100, format="%.0f").props(
                "dense outlined prefix=$").classes("w-36")
            freq = ui.select(growth.FREQS, value=cfg["freq"], label="Recurring").props("dense outlined").classes("w-52")
            start = ui.input("Start date", value=cfg["start"]).props("dense outlined type=date").classes("w-40")
            end = ui.input("End date", value=cfg["end"]).props("dense outlined type=date").classes("w-40")
            with ui.button_group().props("flat"):
                for lab, yrs in RANGES:
                    ui.button(lab, on_click=lambda y=yrs: set_range(y)).props("flat dense no-caps")
        with ui.row().classes("items-end gap-3 w-full"):
            comp = ui.select(known, value=cfg["compare"], multiple=True, with_input=True, new_value_mode="add-unique",
                             label=f"Compare with (up to {MAX_COMPARE} tickers; type any ticker and press Enter)").props(
                "dense outlined use-chips clearable").classes("flex-1 min-w-[320px]")
            real = ui.switch("After inflation", value=cfg["real"] and has_cpi)
            if not has_cpi:
                real.disable()
                real.tooltip("Needs consumer prices (CPI): the next daily update downloads them from FRED")
            logsw = ui.switch("Log scale", value=cfg["log"])
            go = ui.button("Calculate", icon="calculate").props("unelevated no-caps")
        status = ui.label("").classes("oe-small")
        ch = ui.echart({"series": []}).classes("w-full").style("height:420px")
        tbl = ui.html("", sanitize=False).classes("w-full")
        foot = ui.label("").classes("oe-small")

    async def set_range(yrs):
        e = date.fromisoformat(end.value or today.isoformat())
        start.value = "2009-01-01" if yrs is None else _years_back(e, yrs).isoformat()
        await calc()

    async def calc():
        extra = list(comp.value or [])
        if len(extra) > MAX_COMPARE:
            extra = extra[:MAX_COMPARE]
            comp.value = extra
            ui.notify(f"Up to {MAX_COMPARE} tickers to compare.", type="warning")
        tickers = clean_tickers(s["ticker"], extra)
        c = {"initial": init.value or 0, "amount": amt.value or 0, "freq": freq.value, "start": start.value or cfg["start"],
             "end": end.value or today.isoformat(), "real": bool(real.value), "log": bool(logsw.value)}
        if c["start"] >= c["end"]:
            status.text = "The start date must be before the end date."
            return
        if not c["initial"] and not (c["amount"] and c["freq"] != "none"):
            status.text = "Enter an initial investment or a recurring amount."
            return
        store["total_return"] = {**c, "compare": extra}
        need = [t for t in tickers if not db.scalar("SELECT 1 FROM prices WHERE ticker=? LIMIT 1", (t,))]
        if need:
            status.text = f"Downloading price history for {', '.join(need)}…"
            await run.io_bound(_fetch, need)
        status.text = "Calculating…"
        out = await run.io_bound(compute, tickers, c)
        if not out["results"]:
            status.text = "No prices for these tickers in that window."
            ch.options.clear()
            ch.options.update({"series": []})
            ch.update()
            tbl.content = ""
            return
        names = {r["ticker"]: r["name"] for r in db.rows(
            f"SELECT ticker, name FROM securities WHERE ticker IN ({','.join('?' * len(tickers))})", tickers)}
        names.setdefault("SPY", "S&P 500 (SPDR ETF)")
        use_real = c["real"] and out.get("cpi_ok")
        ch.options.clear()
        ch.options.update(chart(out, use_real, c["log"]))
        ch.update()
        tbl.content = table_html(out, use_real, names)
        first = next(iter(out["results"].values()))
        bits = [f"{out['start']} to {out['end']} ({first['years']:.1f} years)",
                f"{money(c['initial'], 0)} at the start" if c["initial"] else "",
                f"{money(c['amount'], 0)} {growth.FREQS[c['freq']].lower()} ({first['contributions'] - (1 if c['initial'] else 0)} "
                "contributions)" if c["amount"] and c["freq"] != "none" else ""]
        status.text = " · ".join(b for b in bits if b) + (
            f" · in start-date dollars (prices rose {pct(first.get('inflation'), 0)})" if use_real else "")
        notes = []
        if out["late"]:
            notes.append(f"Started {out['start']}, when {', '.join(out['late'])} first had prices, so every ticker "
                         "covers the same days.")
        if out["missing"]:
            notes.append(f"No prices found for {', '.join(out['missing'])}.")
        notes.append("Yearly return is the stock's own (time-weighted; what a lump sum earned). Your yearly return counts "
                     "when each contribution went in (money-weighted). Taxes and trading costs aren't included.")
        foot.text = " ".join(notes)

    for el in (freq, real, logsw):
        el.on_value_change(lambda e: calc())
    go.on_click(calc)
    ui.timer(0.1, calc, once=True)


def _years_back(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return d.replace(year=d.year - n, day=28)

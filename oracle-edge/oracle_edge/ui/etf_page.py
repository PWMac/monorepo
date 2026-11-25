"""ETFs: the ETF screener (/etfs) and each fund's page (/etf/{ticker}).

Funds are judged the way the app judges companies, through what they own: each holding carries its Quality,
Wonderfulness and value, so a fund gets a Oracle Score (the weighted average Combined score of what it holds), a
look-through value, and a Fund Score that adds its track record and how it is run.
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import date

from nicegui import app, run, ui

from .. import db, etfs, etfscore, rules
from .growth import PALETTE
from .layout import frame
from .wonder_page import BAND_COL, BAND_NAME, SHAPE
from .theme import BRAND, NEGATIVE, badge_html, money, pct

GREY = "#6b7280"
PRESETS = {
    "business": ("Best businesses", "The highest Oracle Score: the weighted average Combined (Quality + "
                 "Wonderfulness) of what it holds, with at least half the fund scored", "business", True,
                 lambda r: (r.get("business_cov") or 0) >= 0.5),
    "wonderful": ("Most wonderful", "The largest share in wonderful businesses (Wonderfulness 70+)", "wonderful", True,
                  None),
    "quality": ("Highest quality", "The highest weighted Quality of what they hold", "avg_q", True, None),
    "rising": ("Improving businesses", "The biggest 3-year rise in the Oracle Score of what they hold (each holding's "
               "Combined score then and now, weighted)", "trend", True, lambda r: (r.get("business_cov") or 0) >= 0.5),
    "falling": ("Weakening businesses", "The biggest 3-year fall in the Oracle Score of what they hold", "trend", False,
                lambda r: (r.get("business_cov") or 0) >= 0.5 and r.get("trend") is not None),
    "discount": ("Quality at a discount", "At least a third in wonderful, quality businesses; cheapest holdings first",
                 "pvv", False, lambda r: (r.get("both_scored") or 0) >= 0.33),
    "score": ("Best Fund Score", "The best all-round funds", "score", True, None),
    "track": ("Best track record", "The best 5-year return against their benchmark", "ex5", True, None),
    "cheap": ("Lowest cost core", "The cheapest funds with $1B+ in assets", "expense", False,
              lambda r: (r.get("aum") or 0) >= 1e9),
    "mine": ("My ETFs", "The funds you own", "score", True, lambda r: r.get("held")),
}


def _col(v):
    return GREY if v is None else (BRAND if v >= 0 else NEGATIVE)


def _score_col(v):
    if v is None:
        return GREY
    return "#1f6f4a" if v >= 70 else "#3c8d5f" if v >= 60 else "#8A5A12" if v >= 45 else NEGATIVE


def _p(v, d=0):
    return "—" if v is None else f"{v * 100:.{d}f}%"


def _held() -> set[str]:
    from ..holdings import held_tickers

    return set(held_tickers())


def rows() -> list[dict]:
    """One row per fund on file with its scores and facts."""
    sc = etfscore.scores()
    held = _held()
    out = []
    for f in db.rows("SELECT * FROM etfs"):
        s = sc.get(f["ticker"]) or {}
        m = s.get("m") or {}
        p = s.get("pillars") or {}
        r5, b5 = m.get("t_r5"), m.get("t_b5")
        out.append({
            "ticker": f["ticker"], "name": f["name"] or "", "category": f["category"] or "", "family": f["family"] or "",
            "held": f["ticker"] in held, "score": s.get("score"), "owns": p.get("owns"), "price_p": p.get("price"),
            "track": p.get("track"), "cost": p.get("cost"), "sig": m.get("verdict") or "", "why": m.get("verdict_why"),
            "business": m.get("business"), "business_cov": m.get("business_cov"),
            "wonderful": m.get("wonderful"), "quality": m.get("quality"),
            "both": m.get("both"), "both_scored": m.get("both_scored"), "buy_zone": m.get("buy_zone"),
            "fails": m.get("fails"), "avg_q": m.get("avg_q"), "avg_w": m.get("avg_w"), "avg_comb": m.get("avg_comb"),
            "pvv": m.get("price_vs_value"), "lt_er": m.get("lt_er"), "coverage": m.get("coverage"),
            "expense": f["expense"], "aum": f["aum"] or f["net_assets"], "r1": m.get("t_r1"), "r5": r5,
            "ex5": (r5 - b5) if (r5 is not None and b5 is not None) else None, "sharpe": m.get("t_sharpe"),
            "max_dd": m.get("t_max_dd"), "beat": m.get("t_beat_share"), "bench": m.get("t_bench") or f["benchmark"],
            "trend": m.get("trend"), "trend_dir": m.get("trend_dir") or "new", "rising": m.get("rising"),
            "falling": m.get("falling"), "price": m.get("price"), "fair": m.get("fair_value"),
            "asof": f["holdings_asof"] or "", "n": f["n_holdings"], "source": f["holdings_source"] or ""})
    return out


def _download_card():
    from .. import jobs

    with ui.card().classes("oe-card w-full").style("border-left:4px solid #C9A227"):
        ui.label("ETF data hasn't been downloaded yet").classes("oe-h2")
        ui.label("The first download reads every fund's holdings from the SEC and its facts from Yahoo: about 170 funds, "
                 "10–20 minutes. After that the daily update keeps it current (holdings when a fund files new ones, "
                 "scores every day).").classes("text-sm")
        bar = ui.linear_progress(value=0, show_value=False).classes("w-full")
        lab = ui.label("").classes("oe-small")

        def start():
            try:
                jobs.launch("etf_refresh", {})
                ui.notify("Downloading ETF data")
            except RuntimeError as e:
                ui.notify(str(e), type="warning")

        def tick():
            j = jobs.running()
            if j and j.get("kind") == "etf_refresh":
                bar.value = float(j.get("progress") or 0)
                lab.text = f"{j.get('step') or 'Working'} · {float(j.get('progress') or 0):.0%}"
            elif db.scalar("SELECT 1 FROM etf_scores LIMIT 1"):
                ui.navigate.reload()
        ui.button("Download ETF data", icon="download", on_click=start).props("unelevated no-caps")
        ui.timer(3, tick)


# ===================================================================== the screener
@ui.page("/etfs")
def etfs_page(preset: str | None = None):
    from .wonder_page import universe_switch

    store = app.storage.user
    with frame("ETFs"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label("ETFs: what's inside, and how well they're run").classes("oe-h1")
            universe_switch("etfs")
        ui.label("Every fund opened up: each holding carries the app's Quality, Wonderfulness and value, so you can see "
                 "how good the businesses in a fund are on average (its Oracle Score: the weighted average Combined "
                 "score of what it holds), what its holdings are worth against their prices, and a Fund Score that "
                 "adds its track record and how it is run. Click a fund for everything it holds.").classes("oe-muted")
        rs = rows()
        if not any(r["score"] is not None for r in rs):
            _download_card()
            if not rs:
                return
        st = {"preset": preset if preset in PRESETS else store.get("etf_preset", "business"),
              "cats": list(store.get("etf_cats") or []), "maxexp": store.get("etf_maxexp"),
              "minaum": store.get("etf_minaum", 0), "stock_only": store.get("etf_stock_only", True)}
        dates = sorted({r["asof"] for r in rs if r["asof"]})
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(f"{len(rs)} funds · {sum(1 for r in rs if r['score'] is not None)} scored"
                     + (f" · holdings filed {dates[0]} to {dates[-1]}" if dates else "")).classes("oe-small")
            with ui.row().classes("items-center gap-1"):
                add_in = ui.input(placeholder="Add an ETF (ticker)").props("dense outlined").classes("w-44")

                async def add():
                    t = etfs.add(add_in.value or "")
                    if not t:
                        return
                    n = ui.notification(f"Downloading {t}…", spinner=True, timeout=None)
                    r = await run.io_bound(etfs.refresh, [t], None, True, True, True, True)
                    n.dismiss()
                    if r["errors"]:
                        ui.notify("; ".join(r["errors"])[:300], type="warning", multi_line=True)
                    ui.navigate.to(f"/etf/{t}")
                ui.button(icon="add", on_click=add).props("flat round dense").tooltip("Add this fund and download it")

                def refresh_all():
                    from .. import jobs

                    try:
                        jobs.launch("etf_refresh", {"force": False})
                        ui.notify("Updating ETF data in the background (Data page shows progress)")
                    except RuntimeError as e:
                        ui.notify(str(e), type="warning")
                ui.button("Update ETF data", icon="sync", on_click=refresh_all).props("flat dense no-caps")
        with ui.row().classes("items-center gap-1 w-full"):
            chips = {}
            for k, (lab, tip, *_x) in PRESETS.items():
                chips[k] = ui.button(lab, on_click=lambda k=k: (st.update(preset=k), store.update(etf_preset=k),
                                                                 draw())).props("rounded dense no-caps size=sm")
                chips[k].tooltip(tip)
        with ui.row().classes("items-end gap-3 w-full"):
            cats = sorted({r["category"] for r in rs if r["category"]})
            csel = ui.select(cats, multiple=True, value=[c for c in st["cats"] if c in cats], label="Categories (none = all)"
                             ).props("dense outlined use-chips clearable").classes("min-w-[260px]")
            mx = ui.number("Max expense ratio", value=st["maxexp"], suffix="%", min=0, step=0.05).props(
                "dense outlined clearable").classes("w-44")
            ma = ui.select({0: "Any size", 1e8: "$100M+", 1e9: "$1B+", 1e10: "$10B+"}, value=st["minaum"],
                           label="Assets").props("dense outlined").classes("w-36")
            so = ui.switch("Stock funds only", value=st["stock_only"])

            def filt_change():
                st.update(cats=csel.value or [], maxexp=mx.value, minaum=ma.value or 0, stock_only=so.value)
                store.update(etf_cats=st["cats"], etf_maxexp=st["maxexp"], etf_minaum=st["minaum"],
                             etf_stock_only=st["stock_only"])
                draw()
            for w in (csel, mx, ma, so):
                w.on_value_change(lambda e: filt_change())
        note = ui.label("").classes("text-sm").style("color:#2c5d8f")
        bar_q = float(rules.default_profile().settings.get("quality_min", 70))
        with ui.card().classes("oe-card w-full"):
            ui.label("Quality vs Wonderfulness of what they hold").classes("oe-h2")
            ui.label("The Screener's map for funds: each dot is a fund, placed at the weighted Quality (across) and "
                     "Wonderfulness (up) of what it holds, so it sits where its average business would. Their average is "
                     "the Oracle Score. Dashed lines: the checklist bar and Wonderfulness 70, as on the Screener. Colour = "
                     "how its holdings are priced against their value, in the Screener's colours. Shape = which way the "
                     "businesses it holds have moved over 3 years. Zoom with the sliders, the box tool (top right; ⟲ "
                     "resets) or Shift + mouse wheel: the table then lists only the funds in view. Click a legend entry "
                     "to hide a group (from the table too). Click a dot to open the fund in a new tab.").classes("oe-small")
            ch = ui.echart({}, on_point_click=lambda e: ui.navigate.to(f"/etf/{e.name}", new_tab=True)
                           if e.name else None).classes("w-full").style("height:480px")
            ui.html(MAP_KEY, sanitize=False).classes("w-full")
        with ui.row().classes("items-center gap-2") as zrow:
            znote = ui.label("").classes("text-sm").style("color:#2c5d8f")
            ui.button("Show the whole map", icon="zoom_out_map", on_click=lambda: reset_map()).props(
                "flat dense no-caps size=sm")
        zrow.set_visibility(False)
        g = ui.aggrid({"columnDefs": _cols(), "rowData": [], "rowHeight": 34,
                       "defaultColDef": {"sortable": True, "resizable": True, "wrapHeaderText": True,
                                         "autoHeaderHeight": True, "suppressHeaderMenuButton": True},
                       "tooltipShowDelay": 300, "suppressCellFocus": True}, theme="quartz",
                      auto_size_columns=False).classes("w-full")
        g.on("cellClicked", lambda e: ui.navigate.to(f"/etf/{e.args['data']['ticker']}"))
        ui.add_css(_BADGE_CSS)
        ui.label("Oracle Score: the holdings' Combined scores ((Quality + Wonderfulness) / 2) averaged by their weight, "
                 "over the part the app scores (Coverage). Trend: its 3-year change, and the shares of the fund in "
                 "companies rising or falling 5+ points. Wonderful, Quality and Buy Zone are shares of the whole fund. "
                 "Holdings vs value: the fund's holdings' prices against their values, weighted (−20% = they trade 20% "
                 "below what they're worth). Returns are yearly, dividends included.").classes("oe-small")
        allx = [r for r in rs if r["avg_q"] is not None and r["avg_w"] is not None]
        rng = _ranges(allx)
        mv = {"zoom": None, "hidden": set(), "xs": []}

        def shown(xs):
            z, hid = mv["zoom"], mv["hidden"]
            if not z and not hid:
                return xs
            return [r for r in xs if r["avg_q"] is not None and fund_band(r) not in hid and _in_zoom(r, z)]

        def show_rows():
            xs = mv["xs"]
            sh = shown(xs)
            g.options["rowData"] = sh
            g.style(f"height:{min(110 + 35 * len(sh), 700)}px")
            g.update()
            why = (["the zoomed area of the map"] if mv["zoom"] else []) + (
                ["hiding " + ", ".join(BAND_NAME[k] for k in BAND_COL if k in mv["hidden"])] if mv["hidden"] else [])
            znote.text = f"Showing {len(sh)} of {len(xs)} funds: " + " · ".join(why) if why else ""
            zrow.set_visibility(bool(why))

        def draw_map():
            ch.options.clear()
            ch.options.update(fund_map(allx, {r["ticker"] for r in mv["xs"]}, bar_q, rng, mv["zoom"], mv["hidden"],
                                       mv["xs"]))
            ch.update()

        def draw():
            for k, b in chips.items():
                b.props(f"{'unelevated' if k == st['preset'] else 'outline'}")
            lab, tip, key, desc, f = PRESETS[st["preset"]]
            xs = [r for r in rs if r["score"] is not None or r["held"]]
            if st["cats"]:
                xs = [r for r in xs if r["category"] in st["cats"]]
            if st["maxexp"] not in (None, ""):
                xs = [r for r in xs if r["expense"] is not None and r["expense"] * 100 <= float(st["maxexp"])]
            if st["minaum"]:
                xs = [r for r in xs if (r["aum"] or 0) >= st["minaum"]]
            if st["stock_only"]:
                xs = [r for r in xs if (r["coverage"] or 0) >= 0.3 or r["held"]]
            if f:
                xs = [r for r in xs if f(r)]
            xs.sort(key=lambda r: (r[key] is None, -(r[key] or 0) if desc else (r[key] or 0)))
            note.text = f"{tip} · {len(xs)} funds"
            mv["xs"] = xs
            show_rows()
            draw_map()

        async def on_zoom(_e=None):
            mv["seq"] = mv.get("seq", 0) + 1
            me = mv["seq"]
            await asyncio.sleep(0.35)
            if me != mv["seq"]:
                return
            try:
                ext = await ui.run_javascript(
                    f"const c = getElement({ch.id}).chart, m = c.getModel();"
                    " return [m.getComponent('xAxis', 0).axis.scale.getExtent(),"
                    " m.getComponent('yAxis', 0).axis.scale.getExtent()];", timeout=3)
            except Exception:
                return
            z = _norm_zoom(*(ext or [None, None]), rng)
            if z == mv["zoom"]:
                return
            mv["zoom"] = z
            show_rows()
            draw_map()

        def on_legend(e):
            sel = (e.args or {}).get("selected") or {}
            mv["hidden"] = {k for k in BAND_COL if sel.get(BAND_NAME[k]) is False}
            show_rows()
            draw_map()

        def reset_map():
            mv["zoom"], mv["hidden"] = None, set()
            show_rows()
            draw_map()

        ch.on("chart:datazoom", on_zoom, [])
        ch.on("chart:restore", lambda e: reset_map(), [])
        ch.on("chart:legendselectchanged", on_legend, ["selected"])
        draw()


_BADGE_CSS = (".oe-badge-mini{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px}"
              ".oe-b-BUY{background:#e3f1ea;color:#1f6f4a}.oe-b-TRIM{background:#7a4f9e;color:#fff}"
              ".oe-b-WATCH{background:#fbf0dc;color:#9a5b00}.oe-b-HOLD{background:#eceae3;color:#4b524f}")


def _cols() -> list[dict]:
    pc = "p => p.value == null ? '—' : (p.value*100).toFixed(0) + '%'"
    pc1 = "p => p.value == null ? '—' : (p.value*100).toFixed(1) + '%'"
    sp = "p => p.value == null ? '—' : (p.value >= 0 ? '+' : '') + (p.value*100).toFixed(1) + '%'"
    n0 = "p => p.value == null ? '—' : Math.round(p.value)"
    colr = "p => ({color: p.value == null ? '#6b7280' : (p.value >= 0 ? '#1f6f4a' : '#A8322D')})"
    colr_inv = "p => ({color: p.value == null ? '#6b7280' : (p.value <= 0 ? '#1f6f4a' : '#A8322D')})"
    sc = ("p => ({fontWeight: 700, color: p.value == null ? '#6b7280' : p.value >= 70 ? '#1f6f4a' : p.value >= 60 ? "
          "'#3c8d5f' : p.value >= 45 ? '#8A5A12' : '#A8322D'})")
    big = ("p => p.value == null ? '—' : p.value >= 1e9 ? '$' + (p.value/1e9).toFixed(1) + 'B' : '$' + "
           "(p.value/1e6).toFixed(0) + 'M'")
    return [
        {"headerName": "ETF", "field": "ticker", "pinned": "left", "width": 82,
         "cellStyle": {"fontWeight": 700, "cursor": "pointer"},
         ":cellRenderer": "p => p.value + (p.data.held ? ' <span title=\"You own it\" style=\"color:#c9a227\">●</span>' : '')"},
        {"headerName": "Name", "field": "name", "width": 210, "tooltipField": "name"},
        {"headerName": "Fund Score", "field": "score", "width": 88, "type": "rightAligned", ":valueFormatter": n0,
         ":cellStyle": sc, "headerTooltip": "0-100: what it owns, price, track record, cost & structure"},
        {"headerName": "Price", "field": "sig", "width": 92, "tooltipField": "why",
         ":cellRenderer": "p => p.value ? `<span class='oe-badge-mini oe-b-${p.value}'>${p.value}</span>` : ''"},
        {"headerName": "Oracle Score", "field": "business", "width": 96, "type": "rightAligned",
         ":valueFormatter": n0, ":cellStyle": sc,
         "headerTooltip": "Weighted average Combined score ((Quality + Wonderfulness) / 2) of what it holds"},
        {"headerName": "Trend (3 yrs)", "field": "trend", "width": 92, "type": "rightAligned",
         ":cellRenderer": "p => p.value == null ? '—' : `<span style='color:${p.value >= 2 ? '#1f6f4a' : p.value <= -2 ? "
                          "'#A8322D' : '#6b7280'}'>${p.value >= 2 ? '▲' : p.value <= -2 ? '▼' : '●'} ${p.value >= 0 ? "
                          "'+' : ''}${p.value.toFixed(1)}</span>`",
         "headerTooltip": "3-year change of the Oracle Score of what it holds (each holding's Combined score then and "
                          "now, weighted)"},
        {"headerName": "Rising", "field": "rising", "width": 74, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in companies whose Combined score rose 5+ points in 3 years"},
        {"headerName": "Falling", "field": "falling", "width": 74, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in companies whose Combined score fell 5+ points in 3 years"},
        {"headerName": "Wonderful", "field": "wonderful", "width": 88, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in businesses with Wonderfulness 70+"},
        {"headerName": "Quality", "field": "quality", "width": 80, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in businesses that pass the checklist with Quality at or above the bar"},
        {"headerName": "Both", "field": "both", "width": 72, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "In Buy Zone", "field": "buy_zone", "width": 84, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "Fails checklist", "field": "fails", "width": 84, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "Avg Quality", "field": "avg_q", "width": 84, "type": "rightAligned", ":valueFormatter": n0},
        {"headerName": "Avg Wonder", "field": "avg_w", "width": 84, "type": "rightAligned", ":valueFormatter": n0},
        {"headerName": "Holdings vs value", "field": "pvv", "width": 96, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr_inv, "headerTooltip": "Weighted price of its holdings against their values"},
        {"headerName": "Exp. return", "field": "lt_er", "width": 84, "type": "rightAligned", ":valueFormatter": pc1,
         "headerTooltip": "Weighted expected yearly return of its holdings at today's prices"},
        {"headerName": "Coverage", "field": "coverage", "width": 82, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund the app scores"},
        {"headerName": "Expense", "field": "expense", "width": 80, "type": "rightAligned",
         ":valueFormatter": "p => p.value == null ? '—' : (p.value*100).toFixed(2) + '%'"},
        {"headerName": "Assets", "field": "aum", "width": 84, "type": "rightAligned", ":valueFormatter": big},
        {"headerName": "1Y", "field": "r1", "width": 72, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr},
        {"headerName": "5Y a year", "field": "r5", "width": 80, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr},
        {"headerName": "5Y vs benchmark", "field": "ex5", "width": 92, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr},
        {"headerName": "Sharpe", "field": "sharpe", "width": 72, "type": "rightAligned",
         ":valueFormatter": "p => p.value == null ? '—' : p.value.toFixed(2)"},
        {"headerName": "Worst drop", "field": "max_dd", "width": 84, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "Beat bench (12m)", "field": "beat", "width": 92, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of rolling 12-month periods (last 5 years) it beat its benchmark"},
        {"headerName": "Category", "field": "category", "width": 150},
        {"headerName": "Issuer", "field": "family", "width": 130},
        {"headerName": "Holdings as of", "field": "asof", "width": 104},
    ]


def fund_band(r: dict) -> str:
    """The Screener's colour groups for a fund, from what its holdings trade at against their value: Buy zone (the
    fund's BUY badge: 10%+ below value), below value, up to 25% over, more than that, or not valued."""
    pv = r.get("pvv")
    if pv is None:
        return "none"
    if r.get("sig") == "BUY":
        return "buy"
    return "below" if pv < 0 else "near" if pv <= 0.25 else "over"


def fund_size(aum: float | None) -> int:
    """Dot size: assets, in the Screener's sizes."""
    a = aum or 0
    return 6 if a < 1e8 else 9 if a < 1e9 else 11 if a < 1e10 else 14 if a < 1e11 else 17


def _ranges(xs: list[dict]) -> tuple[list[float], list[float]]:
    """The map's axes: the Screener's scale, trimmed to where the funds are (they average many companies, so they
    sit closer together than single companies)."""
    if not xs:
        return [20.0, 100.0], [0.0, 100.0]
    qs = [r["avg_q"] for r in xs]
    ws = [r["avg_w"] for r in xs]
    return ([max(0.0, 5 * (min(qs) // 5) - 5), min(100.0, 5 * (max(qs) // 5) + 10)],
            [max(0.0, 5 * (min(ws) // 5) - 5), min(100.0, 5 * (max(ws) // 5) + 10)])


def _norm_zoom(x, y, rng) -> dict | None:
    if not x or not y:
        return None
    (X0, X1), (Y0, Y1) = rng
    x = [max(X0, float(x[0])), min(X1, float(x[1]))]
    y = [max(Y0, float(y[0])), min(Y1, float(y[1]))]
    if x[0] <= X0 + 0.3 and x[1] >= X1 - 0.3 and y[0] <= Y0 + 0.3 and y[1] >= Y1 - 0.3:
        return None
    return {"x": x, "y": y}


def _in_zoom(r: dict, z: dict | None) -> bool:
    if not z:
        return True
    (x0, x1), (y0, y1) = z["x"], z["y"]
    return x0 - 0.05 <= r["avg_q"] <= x1 + 0.05 and y0 - 0.05 <= r["avg_w"] <= y1 + 0.05


TREND_WORD = {"up": "▲ businesses improving", "down": "▼ businesses weakening", "flat": "● steady", "new": "◆ no trend yet"}


def fund_map(allx: list[dict], inview: set[str], bar: float, rng, zoom: dict | None, hidden: set[str],
             xs: list[dict]) -> dict:
    """The Screener's map, for funds: same axes, lines, colours, shapes, zoom and legend; no labels (hover a dot)."""
    groups: dict[str, list] = {k: [] for k in reversed(BAND_COL)}
    for r in allx:
        groups[fund_band(r)].append(r)
    series = []
    for gk, rs in groups.items():
        data = []
        for r in rs:
            sym, rot = SHAPE.get(r.get("trend_dir") or "new", SHAPE["new"])
            data.append({"name": r["ticker"], "symbol": sym, "symbolRotate": rot, "symbolSize": fund_size(r["aum"]),
                         "value": [round(r["avg_q"], 1), round(r["avg_w"], 1), r["name"], r.get("trend_dir") or "new",
                                   None if r.get("trend") is None else round(r["trend"], 1),
                                   round((r.get("rising") or 0) * 100), round((r.get("falling") or 0) * 100),
                                   None if r.get("pvv") is None else round(r["pvv"] * 100),
                                   None if r.get("business") is None else round(r["business"]),
                                   None if r.get("score") is None else round(r["score"]), BAND_NAME[gk],
                                   r["held"]],
                         "itemStyle": {"opacity": 0.92 if r["ticker"] in inview else 0.15,
                                       **({"borderColor": "#1C2420", "borderWidth": 1.5} if r["held"] else {})}})
        series.append({"type": "scatter", "name": BAND_NAME[gk], "itemStyle": {"color": BAND_COL[gk]}, "data": data,
                       "emphasis": {"focus": "none", "scale": 1.6}})
    series.append({"type": "scatter", "name": "lines", "data": [], "silent": True,
                   "markLine": {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed", "color": "#6b7280"},
                                "label": {"show": False}, "data": [{"xAxis": bar}, {"yAxis": 70}]}})
    fmt = ("p => { const v = p.value, d = " + json.dumps(TREND_WORD) + "[v[3]];"
           " const c = v[4] == null ? '' : ` (${v[4] >= 0 ? '+' : ''}${v[4]} in 3 yrs)`;"
           " return `<b>${p.name}</b> ${v[2]}${v[11] ? ' · <i>you own it</i>' : ''}<br>Oracle Score <b>${v[8]}</b>"
           " (Quality <b>${v[0]}</b> · Wonderfulness <b>${v[1]}</b>)<br>${d}${c}<br>${v[5]}% in companies rising, "
           "${v[6]}% falling${v[7] == null ? '<br>Holdings not valued' : `<br><b>${v[10]}</b>: holdings ${Math.abs(v[7])}%"
           " ${v[7] < 0 ? 'below' : 'above'} their value`}<br>Fund Score ${v[9] == null ? '—' : v[9]}"
           "<br><i>click to open in a new tab</i>`; }")
    counts = {}
    for k in BAND_COL:
        tot = sum(1 for r in allx if fund_band(r) == k)
        n = sum(1 for r in xs if r["avg_q"] is not None and fund_band(r) == k and _in_zoom(r, zoom))
        counts[BAND_NAME[k]] = f"{BAND_NAME[k]} ({n})" if n == tot else f"{BAND_NAME[k]} ({n} of {tot})"
    (X0, X1), (Y0, Y1) = rng
    zx, zy = (zoom["x"], zoom["y"]) if zoom else ([X0, X1], [Y0, Y1])
    win_x = {"startValue": zx[0], "endValue": zx[1]}
    win_y = {"startValue": zy[0], "endValue": zy[1]}
    return {"tooltip": {":formatter": fmt},
            "legend": {"top": 0, "icon": "circle", "data": list(BAND_NAME.values()),
                       "selected": {BAND_NAME[k]: k not in hidden for k in BAND_COL},
                       ":formatter": f"n => ({json.dumps(counts)})[n] || n"},
            "grid": {"left": 50, "right": 56, "top": 40, "bottom": 70},
            "toolbox": {"right": 8, "top": 0, "feature": {"dataZoom": {"title": {"zoom": "Drag a box to zoom",
                                                                                 "back": "Undo zoom"}},
                                                          "restore": {"title": "Reset"}}},
            "dataZoom": [{"type": "slider", "xAxisIndex": 0, "bottom": 8, "height": 16, "filterMode": "none",
                          "showDataShadow": False, **win_x},
                         {"type": "slider", "yAxisIndex": 0, "right": 8, "width": 16, "filterMode": "none",
                          "showDataShadow": False, **win_y},
                         {"type": "inside", "xAxisIndex": 0, "filterMode": "none", "zoomOnMouseWheel": "shift",
                          "moveOnMouseMove": False, **win_x},
                         {"type": "inside", "yAxisIndex": 0, "filterMode": "none", "zoomOnMouseWheel": "shift",
                          "moveOnMouseMove": False, **win_y}],
            "xAxis": {"name": "Quality of what it holds (weighted)", "nameLocation": "middle", "nameGap": 26,
                      "min": X0, "max": X1},
            "yAxis": {"name": "Wonderfulness of what it holds", "nameLocation": "middle", "nameGap": 34, "min": Y0,
                      "max": Y1},
            "series": series}


MAP_KEY = ("<div style='display:flex;gap:22px;flex-wrap:wrap;font-size:12px;color:#4b5563;align-items:center'>"
           "<span><b>Shape</b>: what it holds over 3 years</span>"
           "<span><span style='color:#374151'>▲</span> improving</span><span><span style='color:#374151'>▼</span> "
           "weakening</span><span>● steady</span><span>◆ no history</span>"
           "<span style='margin-left:10px'><b>Size</b>: assets</span>"
           + "".join(f"<span style='display:inline-flex;align-items:center;gap:4px'><span style='display:inline-block;"
                     f"width:{s}px;height:{s}px;border-radius:50%;background:#6b7280'></span>{lab}</span>"
                     for s, lab in ((6, "<$100M"), (9, "$100M+"), (11, "$1B+"), (14, "$10B+"), (17, "$100B+")))
           + "<span style='margin-left:10px'><b>Colour</b>: holdings' price against their value (the Screener's "
             "colours; legend above: click one to hide it)</span><span><b>Dark outline</b>: you own it</span></div>")


# ===================================================================== one fund
def _bar(label: str, v: float | None, weight: float | None = None, tip: str = ""):
    with ui.row().classes("w-full items-center no-wrap gap-2") as r:
        ui.label(label).classes("text-sm").style("width:46%")
        w = 0 if v is None else max(2, v)
        ui.html(f"<div style='height:9px;border-radius:5px;background:#eceae3;width:100%'><div style='height:9px;"
                f"border-radius:5px;width:{w:.0f}%;background:{_score_col(v)}'></div></div>", sanitize=False).style(
            "flex:1")
        ui.label("—" if v is None else f"{v:.0f}").classes("text-sm font-semibold").style(
            f"width:34px;text-align:right;color:{_score_col(v)}")
        if weight is not None:
            ui.label(f"{weight:.0%}").classes("oe-small").style("width:34px;text-align:right")
    if tip:
        r.tooltip(tip)


def _tile(value: str, label: str, sub: str = "", color: str | None = None, tip: str = ""):
    with ui.card().classes("oe-card").style("min-width:140px;flex:1") as c:
        ui.label(value).classes("oe-kpi").style(f"color:{color}" if color else "")
        ui.label(label).classes("oe-kpi-l")
        if sub:
            ui.label(sub).classes("oe-small")
    if tip:
        c.tooltip(tip)


PILLAR_NAMES = {"owns": ("What it owns", "Look-through Quality and Wonderfulness"),
                "price": ("Price", "What its holdings are worth against their prices"),
                "track": ("Track record", "Returns and risk against its benchmark"),
                "cost": ("Cost & structure", "Expense, size, age, trading, turnover, premium/discount")}

FLAG = {"oracle": ("Wonderful & quality, below value", "#1f6f4a"), "both": ("Wonderful & quality", "#3c8d5f"),
        "wonderful": ("Wonderful", "#7b5ea7"), "quality": ("Quality", "#2c5d8f"),
        "fails": ("Fails checklist", "#A8322D"), "": ("", GREY)}


@ui.page("/etf/{ticker}")
def etf_page(ticker: str):
    t = ticker.upper()
    with frame(t):
        f = etfs.get(t)
        if not f:
            _not_on_file(t)
            return
        s = etfscore.scores().get(t) or {}
        m = s.get("m") or {}
        book = etfscore.Book()
        lt = etfscore.look_through(t, book)
        px = db.rows("SELECT date, close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 2", (t,))
        price = px[0]["close"] if px else None
        chg = (px[0]["close"] / px[1]["close"] - 1) if len(px) > 1 and px[1]["close"] else None
        # ---- header
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(t).classes("oe-h1")
                    if m.get("verdict"):
                        ui.html(badge_html(m["verdict"]), sanitize=False)
                    if t in _held():
                        ui.label("You own it").classes("oe-small").style("color:#9a5b00")
                ui.label(" · ".join(x for x in (f.get("name"), f.get("family"), f.get("category")) if x)).classes(
                    "oe-muted")
                if m.get("verdict_why"):
                    ui.label(f"Price: {m['verdict_why']}").classes("text-sm")
                with ui.row().classes("items-baseline gap-2"):
                    ui.label(money(price, 2)).classes("text-xl font-semibold")
                    if chg is not None:
                        ui.label(f"{chg * 100:+.2f}%").style(f"color:{_col(chg)}")
                    if m.get("fair_value"):
                        ui.label(f"look-through value {money(m['fair_value'], 2)}").classes("text-sm").tooltip(
                            "What its holdings are worth per ETF share: each valued holding at the app's fair value, "
                            "the rest at its price")
            with ui.card().classes("oe-card").style("min-width:360px"):
                with ui.row().classes("items-baseline gap-2"):
                    sc = s.get("score")
                    ui.label("—" if sc is None else f"{sc:.0f}").classes("oe-kpi").style(f"color:{_score_col(sc)}")
                    ui.label("Fund Score").classes("oe-kpi-l")
                for k, (lab, tip) in PILLAR_NAMES.items():
                    _bar(lab, (s.get("pillars") or {}).get(k), etfscore.PILLAR_WEIGHTS[k], tip)
        # ---- tiles
        with ui.row().classes("w-full gap-3 mt-2"):
            b = m.get("business")
            tr = m.get("trend")
            arrow = "" if tr is None else " ▲" if tr >= 2 else " ▼" if tr <= -2 else " ●"
            _tile("—" if b is None else f"{b:.0f}{arrow}", "Oracle Score",
                  f"Quality {m.get('avg_q') or 0:.0f} · Wonderfulness {m.get('avg_w') or 0:.0f}"
                  + ("" if tr is None else f" · {tr:+.1f} in 3 yrs") + f" · {_p(m.get('business_cov'))} scored",
                  _score_col(b),
                  "The holdings' Combined scores ((Quality + Wonderfulness) / 2) averaged by their share of the fund, "
                  "over the part the app scores on both")
            _tile(_p(m.get("wonderful")), "Wonderful", f"of the fund · {_p(m.get('wonderful_scored'))} of what's scored")
            _tile(_p(m.get("quality")), "Quality", f"of the fund · {_p(m.get('quality_scored'))} of what's scored")
            pv = m.get("price_vs_value")
            _tile("—" if pv is None else f"{abs(pv) * 100:.0f}% {'below' if pv < 0 else 'above'}", "Holdings vs value",
                  f"expected return {_p(m.get('lt_er'), 1)} a year", BRAND if (pv or 0) < 0 else NEGATIVE if pv else None)
            _tile(f"{f['expense'] * 100:.2f}%" if f.get("expense") is not None else "—", "Expense ratio",
                  f"category {f['expense_cat'] * 100:.2f}%" if f.get("expense_cat") else "")
            aum = f.get("aum") or f.get("net_assets")
            _tile(f"${aum / 1e9:.1f}B" if aum and aum >= 1e9 else f"${(aum or 0) / 1e6:.0f}M" if aum else "—", "Assets",
                  f"{f.get('n_holdings') or 0:,} holdings")
        with ui.row().classes("w-full gap-4 items-stretch mt-2"):
            with ui.card().classes("oe-card").style("flex:1;min-width:380px"):
                _owns_card(m, lt, book)
            with ui.card().classes("oe-card").style("flex:1;min-width:380px"):
                _track_card(t, f, m)
        with ui.row().classes("w-full gap-4 items-stretch mt-2"):
            with ui.card().classes("oe-card").style("flex:1;min-width:380px"):
                _cost_card(f, m, s)
            with ui.card().classes("oe-card").style("flex:1;min-width:380px"):
                _overlap_card(t, lt)
        with ui.card().classes("oe-card w-full mt-2"):
            _holdings_table(t, f, lt)
        with ui.row().classes("gap-2 mt-2"):
            async def refresh():
                n = ui.notification(f"Updating {t}…", spinner=True, timeout=None)
                r = await run.io_bound(etfs.refresh, [t], None, True, True, True, True)
                n.dismiss()
                if r["errors"]:
                    ui.notify("; ".join(r["errors"])[:300], type="warning", multi_line=True)
                ui.navigate.reload()
            ui.button("Update this fund's data", icon="sync", on_click=refresh).props("flat dense no-caps")
            if t not in _held():
                ui.button("Remove from the ETF list", icon="delete", on_click=lambda: (
                    etfs.remove(t), ui.navigate.to("/etfs"))).props("flat dense no-caps color=negative")


def _not_on_file(t: str):
    ui.label(t).classes("oe-h1")
    with ui.card().classes("oe-card w-full"):
        ui.label(f"{t} isn't in the app's ETF list yet.").classes("oe-h2")
        ui.label("Add it to download its holdings (SEC), facts (Yahoo) and prices, and score it. About 20 seconds."
                 ).classes("text-sm")

        async def go():
            etfs.add(t)
            n = ui.notification(f"Downloading {t}…", spinner=True, timeout=None)
            r = await run.io_bound(etfs.refresh, [t], None, True, True, True, True)
            n.dismiss()
            if r["errors"]:
                ui.notify("; ".join(r["errors"])[:300], type="warning", multi_line=True)
            ui.navigate.reload()
        ui.button(f"Add {t}", icon="add", on_click=go).props("unelevated no-caps")


def _owns_card(m: dict, lt: dict, book):
    ui.label("What it really owns").classes("oe-h2")
    if not lt["rows"]:
        ui.label("No holdings on file yet.").classes("oe-muted")
        return
    segs = [("Wonderful and quality", m.get("both") or 0, "#1f6f4a"),
            ("Wonderful only", (m.get("wonderful") or 0) - (m.get("both") or 0), "#7b5ea7"),
            ("Quality only", (m.get("quality") or 0) - (m.get("both") or 0), "#2c5d8f"),
            ("Fails the checklist", m.get("fails") or 0, "#A8322D")]
    other = max(0.0, (m.get("coverage") or 0) - sum(v for _, v, _ in segs))
    segs.append(("Other scored companies", other, "#c9c6bb"))
    segs.append(("Not scored (foreign, bonds, cash, small)", max(0.0, 1 - (m.get("coverage") or 0)), "#eceae3"))
    bar = "".join(f"<div title='{html.escape(lab)}: {v * 100:.0f}%' style='width:{max(v, 0) * 100:.2f}%;background:{c}'>"
                  "</div>" for lab, v, c in segs if v > 0.0005)
    ui.html(f"<div style='display:flex;height:18px;border-radius:6px;overflow:hidden;width:100%'>{bar}</div>",
            sanitize=False).classes("w-full")
    for lab, v, c in segs:
        if v > 0.0005:
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.html(f"<span style='display:inline-block;width:10px;height:10px;border-radius:2px;background:{c}'>"
                        "</span>", sanitize=False)
                ui.label(f"{lab}").classes("text-sm").style("flex:1")
                ui.label(f"{v * 100:.0f}%").classes("text-sm font-semibold")
    ui.label(f"Oracle Score {m.get('business') or 0:.0f} (weighted Quality {m.get('avg_q') or 0:.0f} · "
             f"Wonderfulness {m.get('avg_w') or 0:.0f}, over the {_p(m.get('coverage'))} the app scores) · top 10 = "
             f"{_p(m.get('top10'))} · acts like {m.get('eff_n') or 0:.0f} equal holdings").classes("oe-small mt-1")
    if m.get("trend") is not None:
        tr = m["trend"]
        ui.html(f"<div class='text-sm mt-1'>Over 3 years the businesses it holds moved <b style='color:"
                f"{BRAND if tr >= 2 else NEGATIVE if tr <= -2 else GREY}'>{tr:+.1f}</b> points: "
                f"<span style='color:{BRAND}'>▲ {_p(m.get('rising'))} of the fund rising</span> · "
                f"<span style='color:{NEGATIVE}'>▼ {_p(m.get('falling'))} falling</span> (Combined score, 5+ points)</div>",
                sanitize=False)
    secs = etfscore.sectors(lt["rows"], book)[:7]
    if secs:
        ui.label("Sectors (look-through)").classes("font-semibold text-sm mt-2")
        for k, w in secs:
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                ui.label(k).classes("text-sm").style("width:45%")
                ui.html(f"<div style='height:7px;width:{max(w * 100, 1):.0f}%;background:{BRAND};border-radius:4px'>"
                        "</div>", sanitize=False).style("flex:1")
                ui.label(pct(w, 0)).classes("text-sm").style("width:40px;text-align:right")


def _track_card(t: str, f: dict, m: dict):
    from ..holdings import Px

    b = m.get("t_bench") or f.get("benchmark") or "SPY"
    ui.label(f"Track record vs {b}").classes("oe-h2")
    px = Px([t, b])
    if not px.has(t):
        ui.label("No prices yet.").classes("oe-muted")
        return
    d0 = px.d[t][0]
    end = px.d[t][-1]
    start = max(d0, date.fromisoformat(end).replace(year=date.fromisoformat(end).year - 10).isoformat())
    ds = [d for d in px.d[t] if d >= start][::5]
    a0, b0 = px.adj(t, ds[0]), px.adj(b, ds[0]) if px.has(b) else None
    ser = [{"type": "line", "name": t, "showSymbol": False, "itemStyle": {"color": PALETTE[0]},
            "lineStyle": {"width": 2.4}, "data": [[d, round(px.adj(t, d) / a0 * 10000)] for d in ds]}]
    if b0:
        ser.append({"type": "line", "name": b, "showSymbol": False, "itemStyle": {"color": PALETTE[1]},
                    "data": [[d, round((px.adj(b, d) or b0) / b0 * 10000)] for d in ds]})
    ui.echart({"tooltip": {"trigger": "axis", ":valueFormatter": "v => '$' + Math.round(v).toLocaleString()"},
               "legend": {"top": 0}, "grid": {"left": 56, "right": 12, "top": 28, "bottom": 24},
               "xAxis": {"type": "time"}, "yAxis": {"type": "value", "scale": True}, "series": ser}).classes(
        "w-full").style("height:220px")
    cells = []
    for y in (1, 3, 5, 10):
        r, bb = m.get(f"t_r{y}"), m.get(f"t_b{y}")
        if r is None:
            continue
        cells.append(f"<td><b style='color:{_col(r)}'>{r * 100:+.1f}%</b><br><span style='color:#6b7280'>"
                     f"{b} {bb * 100:+.1f}%</span></td>" if bb is not None else f"<td>{r * 100:+.1f}%</td>")
    heads = "".join(f"<th>{y}Y a year</th>" for y in (1, 3, 5, 10) if m.get(f"t_r{y}") is not None)
    ui.html(f"<table class='oe-st'><tr>{heads}</tr><tr>{''.join(cells)}</tr></table>"
            "<style>.oe-st{width:100%;border-collapse:collapse;font-size:13px}.oe-st td,.oe-st th{padding:4px 8px;"
            "text-align:center;border-bottom:1px solid #eef0ee}.oe-st th{color:#6b7280;font-weight:600}</style>",
            sanitize=False).classes("w-full")
    ui.label(f"Worst drop {_p(m.get('t_max_dd'))} ({b} {_p(m.get('t_bench_max_dd'))}) · volatility "
             f"{_p(m.get('t_vol'))} · Sharpe {m.get('t_sharpe') or 0:.2f} · beat {b} in {_p(m.get('t_beat_share'))} of "
             "rolling 12-month periods (last 5 years)").classes("oe-small")


def _cost_card(f: dict, m: dict, s: dict):
    ui.label("How it's run").classes("oe-h2")
    age = m.get("age")
    dv = m.get("dollar_volume")
    rows_ = [("Expense ratio", f"{f['expense'] * 100:.2f}% a year" if f.get("expense") is not None else "—",
              f"category average {f['expense_cat'] * 100:.2f}%" if f.get("expense_cat") else ""),
             ("Issuer", f.get("family") or "—", ""),
             ("Age", f"{age:.0f} years" if age else "—", f"since {f['inception']}" if f.get("inception") else ""),
             ("Trading", f"${dv / 1e6:,.0f}M a day" if dv else "—", "average over 3 months"),
             ("Turnover", f"{f['turnover'] * 100:.0f}% a year" if f.get("turnover") is not None else "—",
              f"category {f['turnover_cat'] * 100:.0f}%" if f.get("turnover_cat") else ""),
             ("Price vs NAV", f"{m['premium'] * 100:+.2f}%" if m.get("premium") is not None else "—",
              "premium (+) or discount (−) to the value of what it holds"),
             ("Yield", pct(f.get("yield"), 2) if f.get("yield") else "—", "trailing 12 months"),
             ("P/E", f"{f['pe']:.1f}" if f.get("pe") else "—", f"category {f['pe_cat']:.1f}" if f.get("pe_cat") else "")]
    for lab, v, sub in rows_:
        with ui.row().classes("w-full items-baseline no-wrap gap-2"):
            ui.label(lab).classes("text-sm").style("width:30%")
            ui.label(v).classes("text-sm font-semibold")
            ui.label(sub).classes("oe-small")
    with ui.expansion("How the Fund Score was worked out").classes("w-full mt-1"):
        for k, (lab, _tip) in PILLAR_NAMES.items():
            parts = (s.get("parts") or {}).get(k) or []
            ui.label(f"{lab} · {etfscore.PILLAR_WEIGHTS[k]:.0%} of the score").classes("font-semibold text-sm mt-1")
            if not parts:
                ui.label("Not enough data: left out, the other pillars count more.").classes("oe-small")
            for p_lab, v, w in parts:
                _bar(p_lab, v, w)
        ui.label("Each part runs 0–100 between plain thresholds (for example an expense ratio of 0.75% scores 0 and "
                 "0.05% scores 100). Pillars resting on little data (under 60% of the fund scored, or under 3 years of "
                 "prices) are pulled toward 50.").classes("oe-small mt-1")


def _overlap_card(t: str, lt: dict):
    from ..holdings import holdings as hold_fn

    ui.label("Overlap with what you own").classes("oe-h2")
    hs, _ = hold_fn()
    mine = [(h.ticker, h.value or 0) for h in hs if h.ticker != t and h.value]
    if not mine:
        ui.label("Nothing else on file in My holdings.").classes("oe-muted")
        return
    xr = etfscore.xray(mine)
    owned = {e["cik"]: e for e in xr["rows"] if e.get("cik")}
    ov = [(h, owned[h["cik"]]) for h in lt["rows"] if h.get("cik") in owned]
    share = sum(h["w"] for h, _ in ov)
    ui.label(f"{share * 100:.0f}% of {t} is in companies you already own (directly or through your other funds).").classes(
        "text-sm")
    for h, e in sorted(ov, key=lambda x: -x[0]["w"])[:10]:
        via = ", ".join(e["funds"]) if e["funds"] else ""
        with ui.row().classes("w-full items-center no-wrap gap-2"):
            ui.label(h.get("ticker_m") or h.get("ticker") or "").classes("font-semibold text-sm").style("width:60px")
            ui.label(f"{h['w'] * 100:.1f}% of {t}").classes("text-sm").style("width:110px")
            ui.label(f"you hold {money(e['total'], 0)}" + (f" (via {via})" if via else "")).classes("oe-small")


def holding_band(h: dict) -> str:
    """A holding's colour on the map, as on the Screener: Buy zone, below value, up to 25% over, more, not valued."""
    if not h.get("ratio"):
        return "none"
    if h.get("buy"):
        return "buy"
    over = 1 / h["ratio"] - 1
    return "below" if over < 0 else "near" if over <= 0.25 else "over"


def holdings_map(rows: list[dict], bar: float, zoom: dict | None, hidden: set[str], fund: dict | None = None) -> dict:
    """The fund's holdings on the Screener's map: Quality across, Wonderfulness up, colour = price against value,
    shape = 3-year trend, size = weight in the fund."""
    groups: dict[str, list] = {k: [] for k in reversed(BAND_COL)}
    for h in rows:
        if h.get("q") is not None and h.get("wscore") is not None:
            groups[holding_band(h)].append(h)
    wmax = max((h["w"] for h in rows), default=0.01) or 0.01
    series = []
    for gk, hs in groups.items():
        data = []
        for h in hs:
            sym, rot = SHAPE.get(h.get("trend_dir") or "new", SHAPE["new"])
            data.append({"name": h.get("ticker_m") or "", "symbol": sym, "symbolRotate": rot,
                         "symbolSize": round(5 + 19 * (h["w"] / wmax) ** 0.5, 1),
                         "value": [round(h["q"], 1), round(h["wscore"], 1), h.get("name_m") or h.get("name") or "",
                                   round(h["w"] * 100, 2), None if h.get("trend") is None else round(h["trend"], 1),
                                   None if not h.get("ratio") else round((1 / h["ratio"] - 1) * 100), BAND_NAME[gk]]})
        series.append({"type": "scatter", "name": BAND_NAME[gk], "itemStyle": {"color": BAND_COL[gk], "opacity": 0.9},
                       "data": data, "emphasis": {"focus": "none", "scale": 1.5}})
    series.append({"type": "scatter", "name": "lines", "data": [], "silent": True,
                   "markLine": {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed", "color": "#6b7280"},
                                "label": {"show": False}, "data": [{"xAxis": bar}, {"yAxis": 70}]}})
    if fund and fund.get("avg_q") is not None and fund.get("avg_w") is not None:
        # where the fund itself sits: the weighted average of the dots (its Oracle Score is the average of the two)
        series.append({"type": "scatter", "name": "fund", "silent": True, "z": 10, "symbol": "pin",
                       "symbolSize": 34, "itemStyle": {"color": "#1C2420"},
                       "label": {"show": True, "formatter": "fund avg", "position": "top", "fontSize": 10,
                                 "color": "#1C2420"},
                       "data": [{"name": "", "value": [round(fund["avg_q"], 1), round(fund["avg_w"], 1)]}]})
    fmt = ("p => { const v = p.value; if (v.length < 3) return ''; return `<b>${p.name}</b> ${v[2]}<br>${v[3]}% of the fund<br>Quality <b>${v[0]}</b>"
           " · Wonderfulness <b>${v[1]}</b>${v[4] == null ? '' : `<br>${v[4] >= 5 ? '▲' : v[4] <= -5 ? '▼' : '●'} "
           "${v[4] >= 0 ? '+' : ''}${v[4]} in 3 yrs`}${v[5] == null ? '<br>Not valued' : `<br><b>${v[6]}</b>: price "
           "${Math.abs(v[5])}% ${v[5] < 0 ? 'below' : 'above'} value`}<br><i>click to open in a new tab</i>`; }")
    zx, zy = (zoom["x"], zoom["y"]) if zoom else ([20, 100], [0, 100])
    win_x, win_y = {"startValue": zx[0], "endValue": zx[1]}, {"startValue": zy[0], "endValue": zy[1]}
    return {"tooltip": {":formatter": fmt},
            "legend": {"top": 0, "icon": "circle", "data": list(BAND_NAME.values()),
                       "selected": {BAND_NAME[k]: k not in hidden for k in BAND_COL}},
            "grid": {"left": 50, "right": 56, "top": 40, "bottom": 70},
            "toolbox": {"right": 8, "top": 0, "feature": {"dataZoom": {"title": {"zoom": "Drag a box to zoom",
                                                                                 "back": "Undo zoom"}},
                                                          "restore": {"title": "Reset"}}},
            "dataZoom": [{"type": "slider", "xAxisIndex": 0, "bottom": 8, "height": 16, "filterMode": "none",
                          "showDataShadow": False, **win_x},
                         {"type": "slider", "yAxisIndex": 0, "right": 8, "width": 16, "filterMode": "none",
                          "showDataShadow": False, **win_y},
                         {"type": "inside", "xAxisIndex": 0, "filterMode": "none", "zoomOnMouseWheel": "shift",
                          "moveOnMouseMove": False, **win_x},
                         {"type": "inside", "yAxisIndex": 0, "filterMode": "none", "zoomOnMouseWheel": "shift",
                          "moveOnMouseMove": False, **win_y}],
            "xAxis": {"name": "Quality (Oracle checklist)", "nameLocation": "middle", "nameGap": 26, "min": 20, "max": 100},
            "yAxis": {"name": "Wonderfulness", "nameLocation": "middle", "nameGap": 34, "min": 0, "max": 100},
            "series": series}


def _holdings_table(t: str, f: dict, lt: dict):
    with ui.row().classes("w-full items-center justify-between"):
        ui.label(f"Holdings ({len(lt['rows']):,})").classes("oe-h2")
        ui.label(f"{f.get('holdings_source') or ''} · as of {f.get('holdings_asof') or '—'} · weights moved to today's "
                 "prices").classes("oe-small")
    rows_ = []
    for h in lt["rows"]:
        lab, col = FLAG.get(h.get("flag") or "", ("", GREY))
        rows_.append({"w": round(h["w"] * 100, 2), "name": h.get("name_m") or h.get("name") or h.get("title") or "",
                      "ticker": h.get("ticker_m") or h.get("ticker") or "", "q": None if h.get("q") is None
                      else round(h["q"]), "wscore": None if h.get("wscore") is None else round(h["wscore"]),
                      "val": None if not h.get("ratio") else round((1 / h["ratio"] - 1) * 100, 1), "flag": lab,
                      "fcol": col, "link": bool(h.get("ticker_m")), "country": h.get("country") or "",
                      "trend": None if h.get("trend") is None else round(h["trend"], 1),
                      "kind": h.get("asset_cat") or ""})
    cols = [{"headerName": "Weight", "field": "w", "width": 96, "type": "rightAligned", "sort": "desc",
             ":valueFormatter": "p => p.value.toFixed(2) + '%'"},
            {"headerName": "Ticker", "field": "ticker", "width": 80, "cellStyle": {"fontWeight": 700}},
            {"headerName": "Company", "field": "name", "width": 240},
            {"headerName": "Quality", "field": "q", "width": 90, "type": "rightAligned"},
            {"headerName": "Wonderfulness", "field": "wscore", "width": 134, "type": "rightAligned"},
            {"headerName": "Trend (3 yrs)", "field": "trend", "width": 124, "type": "rightAligned",
             ":cellRenderer": "p => p.value == null ? '—' : `<span style='color:${p.value >= 5 ? '#1f6f4a' : p.value <= -5 "
                              "? '#A8322D' : '#6b7280'}'>${p.value >= 5 ? '▲' : p.value <= -5 ? '▼' : '●'} ${p.value >= 0"
                              " ? '+' : ''}${p.value}</span>`",
             "headerTooltip": "3-year change of the company's Combined score"},
            {"headerName": "Price vs value", "field": "val", "width": 126, "type": "rightAligned",
             ":valueFormatter": "p => p.value == null ? '—' : (p.value >= 0 ? '+' : '') + p.value + '%'",
             ":cellStyle": "p => ({color: p.value == null ? '#6b7280' : p.value <= 0 ? '#1f6f4a' : '#A8322D'})"},
            {"headerName": "", "field": "flag", "width": 210,
             ":cellRenderer": "p => p.value ? `<span style='color:${p.data.fcol};font-weight:600'>${p.value}</span>` "
                              ": ''"},
            {"headerName": "Country", "field": "country", "width": 90},
            {"headerName": "Type", "field": "kind", "width": 76}]
    bar_q = float(rules.default_profile().settings.get("quality_min", 70))
    mz = {"zoom": None, "hidden": set(), "only": False}
    ui.label("Its holdings on the Screener's map: Quality across, Wonderfulness up, colour = price against value, "
             "shape = 3-year trend (▲ rising, ▼ falling, ● steady, ◆ new), size = weight in the fund; the pin marks "
             "the fund's weighted average (its Oracle Score is the average of the two). Zoom or hide a colour and the "
             "table below follows. Click a dot to open the company.").classes("oe-small")
    hm = ui.echart(holdings_map(lt["rows"], bar_q, None, set(), lt["m"]), on_point_click=lambda e: ui.navigate.to(
        f"/stock/{e.name}", new_tab=True) if e.name else None).classes("w-full").style("height:440px")
    for r_, h in zip(rows_, lt["rows"]):
        r_["_q"], r_["_w"], r_["_band"] = h.get("q"), h.get("wscore"), holding_band(h)
    g = ui.aggrid({"columnDefs": cols, "rowData": rows_, "rowHeight": 32,
                   "defaultColDef": {"sortable": True, "resizable": True, "suppressHeaderMenuButton": True},
                   "suppressCellFocus": True}, theme="quartz", auto_size_columns=False).classes("w-full").style(
        f"height:{min(90 + 33 * len(rows_), 560)}px")
    g.on("cellClicked", lambda e: ui.navigate.to(f"/stock/{e.args['data']['ticker']}")
         if e.args["data"].get("link") else None)

    def visible():
        xs = rows_
        if mz["only"]:
            xs = [r for r in xs if r["flag"] in (FLAG["oracle"][0], FLAG["both"][0])]
        if mz["zoom"] or mz["hidden"]:
            (x0, x1), (y0, y1) = (mz["zoom"]["x"], mz["zoom"]["y"]) if mz["zoom"] else ((20, 100), (0, 100))
            xs = [r for r in xs if r["_q"] is not None and r["_w"] is not None and r["_band"] not in mz["hidden"]
                  and x0 - 0.05 <= r["_q"] <= x1 + 0.05 and y0 - 0.05 <= r["_w"] <= y1 + 0.05]
        return xs

    def redraw():
        g.options["rowData"] = visible()
        g.update()

    def toggle(v):
        mz["only"] = v
        redraw()

    async def on_zoom(_e=None):
        mz["seq"] = mz.get("seq", 0) + 1
        me = mz["seq"]
        await asyncio.sleep(0.35)
        if me != mz["seq"]:
            return
        try:
            ext = await ui.run_javascript(
                f"const c = getElement({hm.id}).chart, m = c.getModel();"
                " return [m.getComponent('xAxis', 0).axis.scale.getExtent(),"
                " m.getComponent('yAxis', 0).axis.scale.getExtent()];", timeout=3)
        except Exception:
            return
        mz["zoom"] = _norm_zoom(*(ext or [None, None]), ([20.0, 100.0], [0.0, 100.0]))
        redraw()

    def on_legend(e):
        sel = (e.args or {}).get("selected") or {}
        mz["hidden"] = {k for k in BAND_COL if sel.get(BAND_NAME[k]) is False}
        redraw()

    def on_restore(_e=None):
        mz["zoom"], mz["hidden"] = None, set()
        hm.options.clear()
        hm.options.update(holdings_map(lt["rows"], bar_q, None, set(), lt["m"]))
        hm.update()
        redraw()
    hm.on("chart:datazoom", on_zoom, [])
    hm.on("chart:restore", on_restore, [])
    hm.on("chart:legendselectchanged", on_legend, ["selected"])
    ui.switch("Only the wonderful, quality businesses", on_change=lambda e: toggle(e.value))
    ui.label("Click a company for its page. Not scored: companies outside the app's universe (most foreign listings), "
             "bonds, cash and derivatives.").classes("oe-small")

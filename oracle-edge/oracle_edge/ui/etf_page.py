"""ETFs: the ETF screener (/etfs) and each fund's page (/etf/{ticker}).

Funds are judged the way the app judges companies, through what they own: each holding carries its Quality,
Wonderfulness and value, so a fund gets an Oracle share (wonderful, quality businesses at or below their value), a
look-through value, and a Fund Score that adds its track record and how it is run.
"""

from __future__ import annotations

import html
from datetime import date

from nicegui import app, run, ui

from .. import db, etfs, etfscore
from .growth import PALETTE
from .layout import frame
from .theme import BRAND, NEGATIVE, badge_html, money, pct

GREY = "#6b7280"
PRESETS = {
    "oracle": ("Wonderful at a good price", "Most of the fund in wonderful, quality businesses at or below their value",
               "oracle", True, None),
    "wonderful": ("Most wonderful", "The largest share in wonderful businesses (Wonderfulness 70+)", "wonderful", True,
                  None),
    "quality": ("Highest quality", "The highest weighted Quality of what they hold", "avg_q", True, None),
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
            "oracle": m.get("oracle"), "wonderful": m.get("wonderful"), "quality": m.get("quality"),
            "both": m.get("both"), "both_scored": m.get("both_scored"), "buy_zone": m.get("buy_zone"),
            "fails": m.get("fails"), "avg_q": m.get("avg_q"), "avg_w": m.get("avg_w"), "avg_comb": m.get("avg_comb"),
            "pvv": m.get("price_vs_value"), "lt_er": m.get("lt_er"), "coverage": m.get("coverage"),
            "expense": f["expense"], "aum": f["aum"] or f["net_assets"], "r1": m.get("t_r1"), "r5": r5,
            "ex5": (r5 - b5) if (r5 is not None and b5 is not None) else None, "sharpe": m.get("t_sharpe"),
            "max_dd": m.get("t_max_dd"), "beat": m.get("t_beat_share"), "bench": m.get("t_bench") or f["benchmark"],
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
                 "how much of a fund is in wonderful, quality businesses at a good price (its Oracle share), what its "
                 "holdings are worth against their prices, and a Fund Score that adds its track record and how it is "
                 "run. Click a fund for everything it holds.").classes("oe-muted")
        rs = rows()
        if not any(r["score"] is not None for r in rs):
            _download_card()
            if not rs:
                return
        st = {"preset": preset if preset in PRESETS else store.get("etf_preset", "oracle"),
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
        with ui.card().classes("oe-card w-full"):
            ui.label("The map: how much is in wonderful, quality businesses (across) and how cheap their holdings are "
                     "(up). Bigger = more assets; colour = Fund Score. Click a fund to open it.").classes("oe-small")
            ch = ui.echart({}, on_point_click=lambda e: ui.navigate.to(f"/etf/{e.name}")).classes("w-full").style(
                "height:430px")
        g = ui.aggrid({"columnDefs": _cols(), "rowData": [], "rowHeight": 34,
                       "defaultColDef": {"sortable": True, "resizable": True, "wrapHeaderText": True,
                                         "autoHeaderHeight": True, "suppressHeaderMenuButton": True},
                       "tooltipShowDelay": 300, "suppressCellFocus": True}, theme="quartz",
                      auto_size_columns=False).classes("w-full")
        g.on("cellClicked", lambda e: ui.navigate.to(f"/etf/{e.args['data']['ticker']}"))
        ui.add_css(_BADGE_CSS)
        ui.label("Oracle share, Wonderful, Quality and Buy Zone are shares of the whole fund; Quality, Wonderfulness "
                 "and Combined averages are over the part the app scores (Coverage). Holdings vs value: the "
                 "fund's holdings' prices against their values, weighted (−20% = they trade 20% below what they're "
                 "worth). Returns are yearly, dividends included.").classes("oe-small")

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
            g.options["rowData"] = xs
            g.style(f"height:{min(110 + 35 * len(xs), 700)}px")
            g.update()
            ch.options.clear()
            ch.options.update(_map(xs))
            ch.update()
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
        {"headerName": "Oracle share", "field": "oracle", "width": 90, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in wonderful, quality businesses trading at or below their value",
         "cellStyle": {"fontWeight": 600}},
        {"headerName": "Wonderful", "field": "wonderful", "width": 88, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in businesses with Wonderfulness 70+"},
        {"headerName": "Quality", "field": "quality", "width": 80, "type": "rightAligned", ":valueFormatter": pc,
         "headerTooltip": "Share of the fund in businesses that pass the checklist with Quality at or above the bar"},
        {"headerName": "Both", "field": "both", "width": 72, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "In Buy Zone", "field": "buy_zone", "width": 84, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "Fails checklist", "field": "fails", "width": 84, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "Avg Quality", "field": "avg_q", "width": 84, "type": "rightAligned", ":valueFormatter": n0},
        {"headerName": "Avg Wonder", "field": "avg_w", "width": 84, "type": "rightAligned", ":valueFormatter": n0},
        {"headerName": "Combined", "field": "avg_comb", "width": 84, "type": "rightAligned", ":valueFormatter": n0},
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


def _map(xs: list[dict]) -> dict:
    pts = []
    for r in xs:
        if r["both"] is None or r["pvv"] is None:
            continue
        pts.append({"name": r["ticker"], "value": [round(r["both"] * 100, 1), round(-r["pvv"] * 100, 1),
                                                   r["score"] or 0, r["aum"] or 0],
                    "symbolSize": max(8, min(40, 6 + 4 * ((r["aum"] or 1e7) / 1e9) ** 0.35)),
                    "itemStyle": {"borderColor": "#c9a227", "borderWidth": 2} if r["held"] else {}})
    return {
        "tooltip": {":formatter": "p => `<b>${p.name}</b><br>Wonderful & quality: ${p.value[0]}%<br>Holdings "
                                  "${p.value[1] >= 0 ? p.value[1] + '% below' : -p.value[1] + '% above'} value<br>"
                                  "Fund Score ${Math.round(p.value[2])}`"},
        "grid": {"left": 60, "right": 90, "top": 20, "bottom": 50},
        "xAxis": {"type": "value", "name": "Share in wonderful, quality businesses (%)", "nameLocation": "middle",
                  "nameGap": 30, "min": 0},
        "yAxis": {"type": "value", "name": "Holdings below their value (%)", "nameLocation": "middle", "nameGap": 42},
        "visualMap": {"dimension": 2, "min": 30, "max": 85, "right": 0, "top": "middle", "text": ["Fund Score", ""],
                      "inRange": {"color": ["#A8322D", "#c9a227", "#1f6f4a"]}, "calculable": False},
        "series": [{"type": "scatter", "data": pts, "label": {"show": True, "formatter": "{b}", "position": "right",
                                                              "fontSize": 10, "color": "#4b524f"},
                    "markLine": {"silent": True, "symbol": "none", "lineStyle": {"color": "#c9c6bb", "type": "dashed"},
                                 "data": [{"yAxis": 0}]}}]}


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

FLAG = {"oracle": ("Wonderful at a good price", "#1f6f4a"), "both": ("Wonderful & quality", "#3c8d5f"),
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
            _tile(_p(m.get("oracle")), "Oracle share", "wonderful, quality, at or below value", BRAND,
                  "Share of the fund in wonderful (70+), quality (passes the checklist) businesses trading at or below "
                  "their fair value")
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
    segs = [("Wonderful, quality, at or below value", m.get("oracle") or 0, "#1f6f4a"),
            ("Wonderful & quality, priced above value", (m.get("both") or 0) - (m.get("oracle") or 0), "#7fb89a"),
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
    ui.label(f"Weighted Quality {m.get('avg_q') or 0:.0f} · Wonderfulness {m.get('avg_w') or 0:.0f} · Combined "
             f"{m.get('avg_comb') or 0:.0f} (over the {_p(m.get('coverage'))} the app scores) · top 10 = "
             f"{_p(m.get('top10'))} · acts like {m.get('eff_n') or 0:.0f} equal holdings").classes("oe-small mt-1")
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


def _holdings_table(t: str, f: dict, lt: dict):
    with ui.row().classes("w-full items-center justify-between"):
        ui.label(f"Holdings ({len(lt['rows']):,})").classes("oe-h2")
        ui.label(f"{f.get('holdings_source') or ''} · as of {f.get('holdings_asof') or '—'} · weights moved to today's "
                 "prices").classes("oe-small")
    mode = {"only": False}
    rows_ = []
    for h in lt["rows"]:
        lab, col = FLAG.get(h.get("flag") or "", ("", GREY))
        rows_.append({"w": round(h["w"] * 100, 2), "name": h.get("name_m") or h.get("name") or h.get("title") or "",
                      "ticker": h.get("ticker_m") or h.get("ticker") or "", "q": None if h.get("q") is None
                      else round(h["q"]), "wscore": None if h.get("wscore") is None else round(h["wscore"]),
                      "val": None if not h.get("ratio") else round((1 / h["ratio"] - 1) * 100, 1), "flag": lab,
                      "fcol": col, "link": bool(h.get("ticker_m")), "country": h.get("country") or "",
                      "kind": h.get("asset_cat") or ""})
    cols = [{"headerName": "Weight", "field": "w", "width": 96, "type": "rightAligned", "sort": "desc",
             ":valueFormatter": "p => p.value.toFixed(2) + '%'"},
            {"headerName": "Ticker", "field": "ticker", "width": 80, "cellStyle": {"fontWeight": 700}},
            {"headerName": "Company", "field": "name", "width": 240},
            {"headerName": "Quality", "field": "q", "width": 90, "type": "rightAligned"},
            {"headerName": "Wonderfulness", "field": "wscore", "width": 124, "type": "rightAligned"},
            {"headerName": "Price vs value", "field": "val", "width": 126, "type": "rightAligned",
             ":valueFormatter": "p => p.value == null ? '—' : (p.value >= 0 ? '+' : '') + p.value + '%'",
             ":cellStyle": "p => ({color: p.value == null ? '#6b7280' : p.value <= 0 ? '#1f6f4a' : '#A8322D'})"},
            {"headerName": "", "field": "flag", "width": 210,
             ":cellRenderer": "p => p.value ? `<span style='color:${p.data.fcol};font-weight:600'>${p.value}</span>` "
                              ": ''"},
            {"headerName": "Country", "field": "country", "width": 90},
            {"headerName": "Type", "field": "kind", "width": 76}]
    g = ui.aggrid({"columnDefs": cols, "rowData": rows_, "rowHeight": 32,
                   "defaultColDef": {"sortable": True, "resizable": True, "suppressHeaderMenuButton": True},
                   "suppressCellFocus": True}, theme="quartz", auto_size_columns=False).classes("w-full").style(
        f"height:{min(90 + 33 * len(rows_), 560)}px")
    g.on("cellClicked", lambda e: ui.navigate.to(f"/stock/{e.args['data']['ticker']}")
         if e.args["data"].get("link") else None)

    def toggle(v):
        mode["only"] = v
        g.options["rowData"] = [r for r in rows_ if r["flag"] == FLAG["oracle"][0]] if v else rows_
        g.update()
    ui.switch("Only the wonderful businesses at a good price", on_change=lambda e: toggle(e.value))
    ui.label("Click a company for its page. Not scored: companies outside the app's universe (most foreign listings), "
             "bonds, cash and derivatives.").classes("oe-small")

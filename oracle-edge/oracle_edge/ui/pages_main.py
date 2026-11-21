"""Main screens: Today, Stock report card, Signals, Performance, Market, My holdings."""

from __future__ import annotations

import csv
import io
import json

from nicegui import app, ui

from .. import btcsignal, db, rules, strength, targets
from . import data, quote, wonder
from .charts import zoom_chart
from .layout import empty, frame, kpi
from .theme import BADGES, BRAND, BTC_COL, badge_html, fmt_value, ladder_html, money, num, pct, sparkline


def _run_picker(current: int | None, live_only: bool = True, on_change=None):
    runs = data.live_runs() if live_only else data.all_runs()
    if len(runs) <= 1 and live_only:
        return current
    opts = {r["id"]: (r["pname"] if r["kind"] == "live" else f"Backtest · {r['name']}") for r in runs}
    if current is not None and current not in opts:  # e.g. one portfolio from a holdings test
        r = data.run(current)
        if r:
            opts[current] = r["name"]
        else:
            current = next(iter(opts), None)
    ui.select(opts, value=current, label="Profile" if live_only else "Run",
              on_change=lambda e: on_change(e.value) if on_change else None).props("dense outlined").classes("w-72")
    return current


def _grid(rows, cols, html_cols=(), height=None, click_to_stock=True):
    g = ui.aggrid({
        "columnDefs": cols, "rowData": rows, "domLayout": "autoHeight" if not height else "normal",
        "defaultColDef": {"sortable": True, "resizable": True, "filter": False, "minWidth": 70,
                          "wrapHeaderText": True, "autoHeaderHeight": True},
        "rowHeight": 40, "animateRows": True, "suppressCellFocus": True,
    }, html_columns=list(html_cols), theme="quartz").classes("w-full")
    g.style(f"height:{height}px" if height else "height:auto")
    if click_to_stock:
        g.on("cellClicked", lambda e: ui.navigate.to(f"/stock/{e.args['data']['ticker']}")
             if e.args.get("data", {}).get("ticker") else None)
    return g


# ===================================================================== TODAY
@ui.page("/")
def today_page():
    if not db.cfg("initial_load_done"):
        ui.navigate.to("/setup")
        return
    run_id = app.storage.user.get("run_id") or data.default_run_id()
    with frame("Today"):
        if not run_id:
            empty("No results yet. Run the first-time load from Setup.", ("Open setup", "/setup"))
            return
        d = data.latest_date(run_id)
        r = data.run(run_id)
        mk = data.market_now()
        reg = mk["regime"]
        last = db.cfg("last_daily_run", {}) or {}
        bk = db.cfg("last_backup", {}) or {}
        with ui.row().classes("w-full items-end justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("Today").classes("oe-h1")
                ui.label(f"{r['pname']} · results as of the {d} close · act at the next open").classes("oe-muted")
            with ui.row().classes("items-center gap-3"):
                _run_picker(run_id, True, lambda v: (app.storage.user.update(run_id=v), ui.navigate.reload()))
                ui.button("Run daily update now", icon="refresh",
                          on_click=lambda: _launch("daily", {"force": True})).props("unelevated")

        evs = data.evaluations(run_id, d) if d else []
        buy = sorted([e for e in evs if e["list"] == "buy" and not e["outside_circle"]], key=lambda e: e["rank"] or 999)
        pricey = sorted([e for e in evs if e["list"] == "pricey" and not e["outside_circle"]],
                        key=lambda e: (e["price"] / e["buy_below"]) if e["buy_below"] and e["price"] else 99)
        near = [e for e in evs if e["list"] == "near"]
        pending = [e for e in evs if e["list"] == "pending"]
        hold = data.holdings(run_id)
        st = data.stats(data.equity(run_id))
        # ---- KPI row
        btc = btcsignal.current()
        with ui.grid(columns=6).classes("w-full gap-3 oe-kpis"):
            zone_col = {"Extreme": "#a33a2b", "Significantly overvalued": "#a24d00", "Modestly overvalued": "#9a5b00",
                        "Fair": "#1f6f4a", "Undervalued": "#1f6f4a"}.get(reg.zone, None)
            kpi(pct(reg.ratio) if reg.ratio else "—", "Buffett Indicator (market cap ÷ GDP)",
                f"{reg.zone} · MOS gate {pct(reg.mos_gate)}", zone_col, link="/market")
            if btc:
                kpi(f"{btc['btc_ret'] * 100:+.1f}%", f"Bitcoin, {btc['settings']['lookback']} trading days",
                    btcsignal.ZONE_LABEL[btc["zone"]] + (f" · cut-off {btc['bear_cut'] * 100:+.1f}%"
                                                         if btc["bear_cut"] is not None else ""),
                    BTC_COL.get(btc["zone"]), link="/market#btc")
            else:
                kpi("—", "Bitcoin early warning", "prices arrive with the next daily update", link="/market#btc")
            kpi(str(len(buy)), "In the Buy Zone", "wonderful + fairly priced", link="/list?kind=buy")
            kpi(str(len(pricey)), "Wonderful, but pricey", "watching for a buy price", link="/list?kind=pricey")
            pst = rules.get_profile(r["profile_id"]).settings if r else {}
            kpi(str(len(hold)), "Model holdings",
                f"of {pst.get('max_holdings')} at most" if pst.get("portfolio_mode") == "focused" else "variable count",
                link="/list?kind=holdings")
            if st:
                kpi(pct(st["cagr"], 1), f"Model CAGR since {data.equity(run_id)[0]['date'][:4]}",
                    f"S&P 500: {pct(st['bench_cagr'], 1)}",
                    "#1f6f4a" if st["cagr"] >= (st["bench_cagr"] or 0) else "#a33a2b", link="/performance")
            else:
                kpi("—", "Model CAGR")
        with ui.row().classes("oe-small gap-6"):
            ui.label(f"Last update: {last.get('at', 'never')[:16].replace('T', ' ')}")
            ui.label(f"Last backup: {(bk.get('at') or 'never')[:16].replace('T', ' ')}" +
                     ("" if bk.get("at") else "  ⚠"))
            ui.label(f"30-yr Treasury {pct(reg.t30, 2)}")

        # ---- market warnings (never a hard stop)
        adv = btcsignal.advice(reg, btc)
        if adv:
            with ui.card().classes("oe-card w-full").style("border-left:4px solid #8A5A12"):
                ui.label("Market temperature").classes("oe-h2")
                for a in adv:
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        ui.icon("warning" if a["level"] == "warn" else "info").classes(
                            "text-amber-800" if a["level"] == "warn" else "text-green-800")
                        with ui.column().classes("gap-0"):
                            ui.label(a["title"]).classes("font-semibold")
                            ui.label(a["text"]).classes("text-sm")
                ui.label("Warnings, not hard stops: the model keeps following its rules. Details on the Market page."
                         ).classes("oe-small")

        # ---- actions
        acts = data.actions(run_id, d) if d else []
        al = targets.recent_alerts(7)
        with ui.card().classes("oe-card w-full"):
            ui.label(f"Actions for the next open ({len(acts)})").classes("oe-h2")
            if not acts:
                ui.label("No new buy or sell signals today. The Oracle's most common action is doing nothing.").classes(
                    "oe-muted")
                if al and d:
                    ui.label(f"{len(al)} price alert{'s' if len(al) != 1 else ''} in the last 7 days didn't lead to "
                             "a trade. An alert only means a price touched a level; open an alert below to see "
                             "why.").classes("text-sm mt-1")
            for a in acts:
                with ui.row().classes("oe-action w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-3"):
                        ui.html(badge_html(a["type"]), sanitize=False)
                        ui.link(a["ticker"], f"/stock/{a['ticker']}").classes("text-base font-bold")
                        ui.label(a["name"][:40]).classes("oe-muted")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.html(strength.strength_html(a["type"], a["rank_score"], a["quality"], a["mos"], a["reason"],
                                                       r_mos_gate(run_id)), sanitize=False)
                        ui.label(a["reason"]).classes("text-sm")
                        mt = strength.metrics_text(a["rank"], a["rank_score"], a["quality"], a["value_score"], a["mos"],
                                                   a["exp_return"])
                        if mt:
                            ui.label(mt).classes("oe-small")
                    with ui.row().classes("items-center gap-2"):
                        if a["buy_below"]:
                            ui.label(f"buy below {money(a['buy_below'])}").classes("oe-small")
                        cb = ui.checkbox("Mark done", value=bool(a["acted_on"]))
                        cb.on_value_change(lambda e, a=a: db.get().execute(
                            "UPDATE signals SET acted_on=? WHERE id=?", (int(e.value), a["id"])))

        # ---- price alerts, each with why it did or didn't lead to a trade
        if al or prof_alerts_on(r):
            reasons = {q["cik"]: q for q in data.quiet_day_reasons(run_id, d, al, acts)} if d else {}
            with ui.card().classes("oe-card w-full gap-1"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"Price alerts, last 7 days ({len(al)})").classes("oe-h2")
                    show_all = ui.switch("Show all reasons").props("dense")
                ui.label("Checked after each close against the buy, strong-buy, suggested-limit and trim prices, "
                         "plus your own alerts. 'Approaching' = within about 1.5 average days' move. Open an alert "
                         "to see why it did or didn't lead to a trade.").classes("oe-small")
                if not al:
                    ui.label("No stock is near a target right now.").classes("oe-muted")
                exps = []
                for a in al:
                    reached = a["state"] == "reached"
                    col = "#a24d00" if a["kind"] == "trim" else ("#1f6f4a" if reached else "#9a5b00")
                    q = reasons.get(a["cik"]) or {}
                    with ui.expansion().props("dense switch-toggle-side").classes(
                            "w-full oe-alert-row") as ex:
                        with ex.add_slot("header"):
                            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                                ui.html(f'<span class="oe-pill" style="background:{col};color:#fff">'
                                        f'{"REACHED" if reached else "APPROACHING"}</span>', sanitize=False)
                                ui.link(a["ticker"], f"/stock/{a['ticker']}").classes("font-bold w-16").on(
                                    "click.stop", lambda: None)
                                ui.label(a["message"]).classes("text-sm flex-1")
                                ui.label(a["date"]).classes("oe-small")
                        with ui.row().classes("items-start gap-2 no-wrap pl-12 pb-2"):
                            ui.icon("check_circle" if q.get("acted") else "info").classes(
                                "text-green-800" if q.get("acted") else "text-gray-500")
                            ui.label(q.get("why") or "No result for this stock on the latest close.").classes("text-sm")
                    exps.append(ex)
                show_all.on_value_change(lambda e: [x.set_value(e.value) for x in exps])

        _news_section(run_id)

        # ---- buy zone
        with ui.card().classes("oe-card w-full"):
            ui.label("Buy Zone: ranked best first").classes("oe-h2")
            ui.label("Passes every gate, Quality ≥ 70, price at least the margin of safety below value, and "
                     "expected return ≥ the hurdle.").classes("oe-small")
            if not buy:
                ui.label("Nothing qualifies at today's prices. That's normal in an expensive market; check the "
                         "'pricey' list for buy-below prices.").classes("oe-muted")
            else:
                gate = r_mos_gate(run_id)
                rows = [{"rank": e["rank"], "badge": badge_html(e["badge"] or "WATCH", True), "ticker": e["ticker"],
                         "stars": strength.stars_html(strength.buy_stars(e["rank_score"], e["quality"], e["mos"], gate),
                                                      True),
                         "name": e["name"], "score": round(e["rank_score"] or 0, 1), "quality": round(e["quality"] or 0),
                         "value": round(e["value"] or 0), "mos": round((e["mos"] or 0) * 100, 1),
                         "er": round((e["exp_return"] or 0) * 100, 1), "price": round(e["price"] or 0, 2),
                         "buy_below": round(e["buy_below"] or 0, 2), "strong": round(e["strong_buy_below"] or 0, 2),
                         "iv": round(e["iv"] or 0, 2),
                         "warn": "⚠ " + ", ".join(f["name"] for f in (e["detail"] or {}).get("qflags") or [])
                         if (e["detail"] or {}).get("qflags") else "",
                         "kind": {"bank": "bank", "insurer": "insurer"}.get((e["detail"] or {}).get("category"), "")}
                        for e in buy]
                _grid(rows, [
                    {"field": "rank", "headerName": "#", "width": 60, "pinned": "left"},
                    {"field": "badge", "headerName": "Signal", "width": 140},
                    {"field": "stars", "headerName": "Strength", "width": 110},
                    {"field": "ticker", "headerName": "Ticker", "width": 90, "cellClass": "font-bold"},
                    {"field": "name", "headerName": "Company", "flex": 1, "minWidth": 160},
                    {"field": "score", "headerName": "Rank score", "width": 110},
                    {"field": "quality", "headerName": "Quality", "width": 95},
                    {"field": "value", "headerName": "Value", "width": 85},
                    {"field": "mos", "headerName": "MOS %", "width": 90},
                    {"field": "er", "headerName": "Exp. return %", "width": 125},
                    {"field": "price", "headerName": "Price", "width": 95},
                    {"field": "buy_below", "headerName": "Buy below", "width": 105},
                    {"field": "strong", "headerName": "Strong buy below", "width": 140},
                    {"field": "iv", "headerName": "Value", "width": 95},
                    {"field": "kind", "headerName": "Rulebook", "width": 95},
                    {"field": "warn", "headerName": "10-Q warnings", "width": 200},
                ], html_cols=[1, 2])

        with ui.row().classes("w-full gap-4 no-wrap items-start"):
            with ui.card().classes("oe-card flex-1"):
                ui.label("Wonderful, but pricey").classes("oe-h2")
                ui.label("Great businesses waiting for a better price, closest to buyable first.").classes("oe-small")
                rows = [{"ticker": e["ticker"], "name": e["name"], "quality": round(e["quality"] or 0),
                         "price": round(e["price"] or 0, 2), "buy_below": round(e["buy_below"] or 0, 2),
                         "away": round((e["price"] / e["buy_below"] - 1) * 100, 1) if e["buy_below"] else None,
                         "er": round((e["exp_return"] or 0) * 100, 1)} for e in pricey[:60]]
                if rows:
                    _grid(rows, [
                        {"field": "ticker", "headerName": "Ticker", "width": 85, "cellClass": "font-bold"},
                        {"field": "name", "headerName": "Company", "flex": 1, "minWidth": 140},
                        {"field": "quality", "headerName": "Q", "width": 65},
                        {"field": "price", "headerName": "Price", "width": 90},
                        {"field": "buy_below", "headerName": "Buy below", "width": 100},
                        {"field": "away", "headerName": "% away", "width": 90},
                        {"field": "er", "headerName": "Exp. ret %", "width": 95},
                    ], height=420)
                else:
                    ui.label("None right now.").classes("oe-muted")
            with ui.column().classes("w-[420px] gap-4"):
                with ui.card().classes("oe-card w-full"):
                    ui.label("Near misses").classes("oe-h2")
                    ui.label("Fail exactly one gate by a small margin.").classes("oe-small")
                    for e in near[:12]:
                        nm = (e["detail"] or {}).get("near") or {}
                        with ui.row().classes("w-full justify-between"):
                            ui.link(e["ticker"], f"/stock/{e['ticker']}").classes("font-bold")
                            ui.label(f"{nm.get('name', '?')}: {fmt_value(nm.get('value'), _fmt_for(nm.get('id')))} "
                                     f"vs {fmt_value(nm.get('target'), _fmt_for(nm.get('id')))}").classes("oe-small")
                    if not near:
                        ui.label("None.").classes("oe-muted")
                with ui.card().classes("oe-card w-full"):
                    ui.label("Holdings").classes("oe-h2")
                    for h in hold[:14]:
                        with ui.row().classes("w-full justify-between items-center"):
                            with ui.row().classes("items-center gap-2"):
                                ui.html(badge_html(h["badge"] or "HOLD", True), sanitize=False)
                                ui.link(h["ticker"], f"/stock/{h['ticker']}").classes("font-bold")
                            ui.label(f"{pct(h['weight'])} · {pct(h['gain'], 1)}").classes("oe-small oe-num")
                    if not hold:
                        ui.label("No holdings yet.").classes("oe-muted")
                if pending:
                    with ui.card().classes("oe-card w-full"):
                        ui.label(f"Pending ({len(pending)})").classes("oe-h2")
                        ui.label("Can't be valued yet: usually a foreign filer whose ADR ratio or exchange rate "
                                 "is missing, or no recent price.").classes("oe-small")
                        for e in pending[:10]:
                            with ui.row().classes("w-full justify-between"):
                                ui.link(e["ticker"], f"/stock/{e['ticker']}").classes("font-bold")
                                ui.label((e["detail"] or {}).get("note", "")).classes("oe-small")


# ===================================================================== STOCK LISTS (from the Today cards)
_LISTS = {
    "buy": ("In the Buy Zone", "Wonderful businesses at a fair price: every gate passed, Quality ≥ the bar, price at least the margin "
                               "of safety below value, and expected return ≥ the hurdle."),
    "pricey": ("Wonderful, but pricey", "Great businesses priced above their buy price. '% above buy price' shows how far each "
                                        "has to fall to become buyable."),
    "holdings": ("Model holdings", "What the model portfolio owns now, with the same metrics so you can see which holdings are "
                                   "strongest and which are getting expensive."),
    "near": ("Near misses", "Companies that fail exactly one gate by a small margin."),
    "wonderful": ("All wonderful companies", "Buy Zone and pricey together: every business that passes the quality bar."),
}

_PCT = "p => p.value == null ? '' : (p.value * 100).toFixed(1) + '%'"
_PCT0 = "p => p.value == null ? '' : (p.value * 100).toFixed(0) + '%'"
_USD = "p => p.value == null ? '' : '$' + p.value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})"
_NUM1 = "p => p.value == null ? '' : p.value.toFixed(1)"
_NUM0 = "p => p.value == null ? '' : p.value.toFixed(0)"
_X = "p => p.value == null ? '' : p.value.toFixed(2) + '×'"
_YRS = "p => p.value == null ? '' : p.value.toFixed(1) + ' yrs'"
_BIL = "p => p.value == null ? '' : '$' + (p.value / 1e9).toFixed(1) + 'B'"
_STARS = "p => p.value ? '★'.repeat(p.value) + '☆'.repeat(5 - p.value) : ''"
_FMT_JS = {"pct": _PCT, "ratio": _PCT0, "x": _X, "years": _YRS}


def _col(field, header, fmt=None, width=110, tip=None, **kw):
    width = max(width, 9 * max(len(w) for w in header.split()) + 76)  # room for the longest word + sort/filter icons
    c = {"field": field, "headerName": header, "width": width, "minWidth": width,
         "type": "rightAligned" if fmt else None}
    if fmt:
        c[":valueFormatter"] = fmt
    if tip:
        c["headerTooltip"] = tip
    c.update(kw)
    return {k: v for k, v in c.items() if v is not None}


@ui.page("/list")
def list_page(kind: str = "buy"):
    kind = kind if kind in _LISTS else "buy"
    run_id = app.storage.user.get("run_id") or data.default_run_id()
    title, blurb = _LISTS[kind]
    with frame(title):
        if not run_id:
            empty("No results yet.")
            return
        r = data.run(run_id)
        rows = data.stock_table(run_id, kind)
        gate = r_mos_gate(run_id)
        with ui.row().classes("w-full items-end justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(f"{title} ({len(rows)})").classes("oe-h1")
                ui.label(f"{r['pname']} · as of the {data.latest_date(run_id)} close").classes("oe-muted")
            with ui.row().classes("gap-1"):
                for k, (t, _) in _LISTS.items():
                    ui.button(t, on_click=lambda k=k: ui.navigate.to(f"/list?kind={k}")).props(
                        "unelevated dense" if k == kind else "flat dense no-caps")
        ui.label(blurb + " Click a column header to sort (Shift-click to sort by a second column); hover a header "
                 "for what it means; click a row to open the stock.").classes("oe-small")
        if not rows:
            empty("Nothing on this list right now.")
            return
        for x in rows:
            x["stars"] = strength.buy_stars(x["rank_score"], x["quality"], x["mos"], gate) \
                if x["list"] == "buy" else None
            x["badge_html"] = badge_html(x["badge"], True) if x["badge"] else ""
        metric_cols = [_col(k, h, _FMT_JS[f], 118, tip) for k, h, _, f, tip in data.BUFFETT_METRICS]
        price_cols = [
            _col("price", "Price", _USD, 100),
            _col("iv", "Intrinsic value", _USD, 120, "What the business is worth per share (average of the two models)."),
            _col("buy_below", "Buy below", _USD, 110, "Price that gives the profile's margin of safety."),
            _col("strong", "Strong buy below", _USD, 125, "Price that gives an extra 15% margin of safety."),
            _col("mos", "Margin of safety", _PCT, 120, "How far the price is below value. The Oracle: buy with a big margin."),
            _col("away", "% above buy price", _PCT, 125, "How far the price must fall to reach the buy price (pricey list).",
                 sort="asc" if kind == "pricey" else None),
            _col("er", "Exp. 10-yr return", _PCT, 125,
                 "Expected yearly return over 10 years from today's price (Buffettology method)."),
            _col("ey", "Earnings yield", _PCT, 110, "Earnings ÷ price. The Oracle compares it with the 30-year Treasury."),
            _col("pe", "P/E", _NUM1, 80),
            _col("pe_med", "P/E 10-yr median", _NUM1, 110, "Typical P/E for this company over the lookback."),
            _col("mcap", "Market cap", _BIL, 105),
        ]
        cols = [
            _col("ticker", "Ticker", None, 90, pinned="left", cellClass="font-bold"),
            _col("name", "Company", None, 200, pinned="left"),
        ]
        if kind == "holdings":
            cols += [
                {"field": "badge_html", "headerName": "Badge", "width": 120, "minWidth": 120},
                _col("weight", "Weight", _PCT, 90, "Share of the model portfolio.", sort="desc"),
                _col("gain", "Gain", _PCT, 90, "Price change since the average purchase price."),
                _col("opened", "Owned since", None, 110),
            ]
        if kind == "wonderful":
            cols += [_col("list", "List", "p => ({buy: 'Buy Zone', pricey: 'Pricey'})[p.value] || p.value", 110,
                          "Buy Zone = buyable at today's price; Pricey = waiting for a lower price.")]
        if kind in ("buy", "wonderful"):
            cols += [_col("rank", "Rank", _NUM0, 75, "1 = best idea today (Buy Zone only).",
                          sort="asc" if kind == "buy" else None),
                     _col("stars", "Strength", _STARS, 105, "1–5 stars: Rank score, Quality and margin of safety.")]
        cols += [
            _col("rank_score", "Rank score", _NUM1, 100, "Quality and value combined (0–100). Higher is better.",
                 sort="desc" if kind in ("wonderful", "near") else None),
            _col("quality", "Quality", _NUM0, 85, "How good the business is (0–100) from the 10-year rules."),
            _col("value", "Value score", _NUM0, 95, "How cheap the price is vs value (0–100)."),
        ]
        cols += [{"headerName": "Price and value", "children": price_cols},
                 {"headerName": "The business, 10-year record (what the Oracle checks)", "children": metric_cols},
                 _col("sector", "Industry", None, 200)]
        g = ui.aggrid({
            "columnDefs": cols, "rowData": rows,
            "defaultColDef": {"sortable": True, "resizable": True, "filter": True, "wrapHeaderText": True,
                              "autoHeaderHeight": True},
            "rowHeight": 38, "animateRows": True, "suppressCellFocus": True, "tooltipShowDelay": 300,
            "multiSortKey": "shift",
        }, html_columns=[2] if kind == "holdings" else [], theme="quartz").classes("w-full").style("height:68vh")
        g.on("cellClicked", lambda e: ui.navigate.to(f"/stock/{e.args['data']['ticker']}")
             if e.args.get("data", {}).get("ticker") and e.args.get("colId") != "badge_html" else None)
        with ui.row().classes("items-center gap-3"):
            q = ui.input(placeholder="Filter by ticker, name or industry").props("dense outlined clearable").classes("w-80")
            q.on_value_change(lambda e: g.run_grid_method("setGridOption", "quickFilterText", e.value or ""))
            ui.button("Export CSV", icon="download",
                      on_click=lambda: g.run_grid_method("exportDataAsCsv", {"fileName": f"oracle-edge-{kind}.csv"})
                      ).props("flat dense")
        with ui.expansion("How to compare these like the Oracle", icon="help_outline").classes("w-full oe-card"):
            ui.markdown(
                "- **Start with the business, then the price.** Sort by *Quality* or *ROE*: the Oracle wants high returns on "
                "equity (15%+) with little debt (*Debt payback* under 3–4 years), steady earnings (*Predictability*) and "
                "real cash (*Cash conversion* near 100%).\n"
                "- **Moat clues:** high and stable *Gross margin* and *ROIC*, low *Capex ÷ net income*.\n"
                "- **Shareholder-friendly management:** shrinking *Share count* (buybacks) and a *$1 test* above 1.\n"
                "- **Then the price:** sort by *Margin of safety* or *Exp. 10-yr return*. On the pricey list, *% above buy "
                "price* shows which great businesses are closest to buyable.\n"
                "- **Rank score** already blends quality and price; the columns let you see *why* a stock ranks where it does.\n"
                "- Banks and insurers use their own rulebooks, so some business columns are blank for them.")


_RULE_FMT = {r["id"]: r.get("fmt", "") for r in rules.load_yaml_profile()["rules"]}


def r_mos_gate(run_id) -> float:
    """The buy gate (margin of safety) of the run's profile, for star ratings."""
    try:
        return float(rules.get_profile(data.run(run_id)["profile_id"]).settings.get("mos_gate", 0.25))
    except Exception:
        return 0.25


def prof_alerts_on(r) -> bool:
    try:
        return bool(rules.get_profile(r["profile_id"]).settings.get("price_alerts", True))
    except Exception:
        return True


def _fmt_for(rid):
    return _RULE_FMT.get(rid, "")


def _launch(kind, args=None):
    from .. import jobs

    try:
        jobs.launch(kind, args or {})
        ui.notify("Started. Progress shows in the top bar.", type="info")
    except Exception as e:
        ui.notify(str(e), type="warning")


# ===================================================================== STOCK
@ui.page("/stock/{ticker}")
def stock_page(ticker: str):
    run_id = app.storage.user.get("run_id") or data.default_run_id()
    s = data.stock(ticker)
    with frame(ticker.upper()):
        if not s:
            empty(f"{ticker.upper()} isn't in the universe (check the exchange filters in Settings).")
            return
        r = data.run(run_id) if run_id else None
        prof = rules.get_profile(r["profile_id"]) if r else rules.default_profile()
        snap = data.latest_snapshot(s["cik"], prof.id)
        ev = data.stock_eval(run_id, s["cik"]) if run_id else None
        bd = data.stock_badge(run_id, s["cik"]) if run_id else None
        with ui.row().classes("w-full items-end justify-between"):
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-3"):
                    ui.label(s["ticker"]).classes("oe-h1")
                    if bd:
                        ui.html(badge_html(bd["badge"]), sanitize=False)
                wonder.header_chips(s, prof)
                ui.label(f"{s['name']} · {s['exchange']} · {s['sic_desc'] or s['category']}"
                         f"{' · foreign filer (' + s['currency'] + ')' if s['is_foreign'] else ''}").classes("oe-muted")
                if bd:
                    ui.label(bd["reason"]).classes("text-sm")
            ui.label(f"Profile: {prof.name} · latest annual report filed {snap['filed'] if snap else '—'}").classes(
                "oe-small")
        with ui.tabs().props("dense no-caps align=left active-color=primary indicator-color=primary").classes(
                "w-full border-b border-gray-200") as tabs:
            t_rep = ui.tab("report", label="Report card", icon="fact_check")
            t_w = ui.tab("wonder", label="Wonderful?", icon="workspace_premium")
            t_q = ui.tab("quote", label="Chart & key stats", icon="candlestick_chart")
        with ui.tab_panels(tabs, value=t_rep).props("animated=false").classes("w-full bg-transparent"):
            with ui.tab_panel(t_rep).classes("p-0 gap-4"):
                _report_card(s, r, prof, snap, ev, run_id)
            with ui.tab_panel(t_w).classes("p-0 gap-4") as wpanel:
                pass
            with ui.tab_panel(t_q).classes("p-0 gap-4") as qpanel:
                pass
        drawn = {"q": False, "w": False}

        def lazy(e):
            if e.value == "quote" and not drawn["q"]:  # build the chart once its tab is visible (correct sizing)
                drawn["q"] = True
                with qpanel:
                    quote.render(s, ev)
            if e.value == "wonder" and not drawn["w"]:
                drawn["w"] = True
                with wpanel:
                    wonder.render(s, prof)

        tabs.on_value_change(lazy)


def _report_card(s, r, prof, snap, ev, run_id):
    """The Oracle report card: scores, price ladder, targets, early warnings, every rule and your notes."""
    if not snap:
        empty("No annual-report data for this company yet.")
        return
    if snap["routed"] in ("bank", "insurer"):
        with ui.card().classes("oe-card w-full").style("border-left:4px solid #2c5d8f"):
            ui.label(f"Scored with the {snap['routed']} rulebook.").classes("font-semibold")
            ui.label("Standard rules don't fit financials (no gross margin; deposits or float are the raw "
                     "material), so this company is judged on " +
                     ("return on assets, efficiency, capital, funding and credit losses, and valued on an "
                      "ROE-justified price-to-tangible-book." if snap["routed"] == "bank" else
                      "underwriting profit (combined ratio), float growth, book value growth and reserving, and "
                      "valued on an ROE-justified price-to-book.")).classes("oe-small")
    if s["is_foreign"]:
        _adr_card(s)
    with ui.grid(columns=5).classes("w-full gap-3 oe-kpis"):
        kpi(num(snap["quality"], 0), "Quality", "business, 0–100")
        kpi(num(ev["value"], 0) if ev and ev["value"] is not None else "—", "Value", "price, 0–100")
        kpi(num(ev["rank_score"], 1) if ev and ev["rank_score"] else "—", "Rank score",
            f"#{ev['rank']} in Buy Zone" if ev and ev["rank"] else (ev["list"] if ev else "not evaluated"))
        kpi(pct(ev["mos"]) if ev and ev["mos"] is not None else "—", "Margin of safety",
            f"needs ≥ {pct(prof.settings['mos_gate'])}")
        kpi(pct(ev["exp_return"], 1) if ev and ev["exp_return"] is not None else "—", "Expected 10-yr return",
            f"needs ≥ {pct(prof.settings['hurdle'])}")
    tp, tech = targets.plan_for(run_id, s["cik"], prof.settings) if run_id else (None, None)
    if ev:
        det = ev["detail"]
        with ui.card().classes("oe-card w-full"):
            ui.label("Price ladder").classes("oe-h2")
            ui.html(ladder_html(ev["price"], ev["strong_buy_below"], ev["buy_below"], ev["iv"],
                                det.get("expensive_above"), (tp or {}).get("limit")),
                    sanitize=False).classes("w-full")
            with ui.row().classes("gap-8 oe-small"):
                ui.label(f"Model A ({det.get('model_a') or 'owner-earnings DCF'}): value {money(det.get('iv_a'))}, return "
                         f"{pct(det.get('er_a'), 1)}")
                ui.label(f"Model B (10-yr projection): value {money(det.get('iv_b'))}, return "
                         f"{pct(det.get('er_b'), 1)}")
                ui.label(f"Discount rate {pct(det.get('r'), 1)} "
                         f"({'the higher of the hurdle and the Treasury rule' if det.get('category') in ('bank', 'insurer') else '30-yr Treasury rule'})"
                         f" · growth used "
                         f"{pct(det.get('g'), 1)} · exit P/E {num(det.get('exit_pe'), 1)}")
                ui.label(f"P/E now {num(det.get('pe_now'), 1)} vs 10-yr median {num(det.get('pe_median'), 1)} · "
                         f"owner-earnings yield {pct(det.get('ey'), 1)} · PEPG {num(det.get('pepg'), 2)}")
        owned = bool(db.scalar("SELECT 1 FROM positions p JOIN portfolios f ON f.id=p.portfolio_id "
                               "WHERE f.run_id=? AND p.cik=?", (run_id, s["cik"])) or
                     db.scalar("SELECT 1 FROM real_holdings WHERE cik=?", (s["cik"],)))
        _targets_card(s, tp, tech, owned)
        _price_chart(s["ticker"], run_id, s["cik"], tp, tech)
    else:
        _targets_card(s, tp, tech, False)
    _warnings_card(s["cik"], snap, prof)
    # ---- rules
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full justify-between items-center"):
            ui.label("Oracle checklist").classes("oe-h2")
            cats = snap["category_scores"]
            ui.label(" · ".join(f"{k.title()} {v:.0f}" for k, v in cats.items())).classes("oe-small")
        rules_by_id = {x["id"]: x for x in prof.all_rules()}
        groups = [("Gates (must pass)", lambda x: x["mode"] == "gate"),
                  ("Scored", lambda x: x["mode"] == "scored")]
        for title, pred in groups:
            ui.label(title).classes("text-sm font-semibold mt-2")
            for rr in [x for x in snap["rules_json"] if pred(x)]:
                rd = rules_by_id.get(rr["id"], {})
                icon, cls = ("check_circle", "oe-rule-pass") if rr["passed"] else (
                    ("cancel", "oe-rule-fail") if rr["passed"] is False else ("help", "oe-rule-na"))
                with ui.row().classes("w-full items-center no-wrap gap-3 py-1 border-b border-gray-100"):
                    ui.icon(icon).classes(cls)
                    with ui.column().classes("gap-0 w-64"):
                        ui.label(rr["name"]).classes("font-medium")
                        ui.label(rd.get("formula", "")).classes("oe-small")
                    ui.label(fmt_value(rr["value"], rd.get("fmt", ""))).classes("w-28 oe-num font-semibold")
                    ui.label(f"target {fmt_value(rd.get('target'), rd.get('fmt', ''))}").classes("w-32 oe-small")
                    ui.html(sparkline(rr.get("series")), sanitize=False)
                    ui.label(f"{rr['consistency'] * 100:.0f}% of yrs" if rr.get("consistency") is not None
                             else "").classes("w-24 oe-small")
                    ui.label(f"{rr['score']:.0f} pts" if rr.get("score") is not None else "—").classes(
                        "w-16 oe-num")
                    ui.label(rd.get("why", "")).classes("oe-small flex-1")
    # ---- judgment + signals
    with ui.row().classes("w-full gap-4 no-wrap items-start"):
        with ui.card().classes("oe-card flex-1"):
            ui.label("Your judgment").classes("oe-h2")
            ui.label("The Oracle's first filters can't be automated. Your answers are stored locally.").classes(
                "oe-small")
            n = data.notes(s["cik"])
            c1 = ui.checkbox("I understand how this business makes money", value=bool(n["understand"]))
            c2 = ui.checkbox("I trust management to act like owners", value=bool(n["trust_mgmt"]))
            c3 = ui.checkbox("The moat should last 20 years", value=bool(n["moat_20y"]))
            c4 = ui.checkbox("Outside my circle of competence (hide from my lists)", value=bool(n["outside_circle"]))
            note = ui.textarea("Notes", value=n.get("note") or "").classes("w-full")

            def save():
                data.save_notes({"cik": s["cik"], "understand": c1.value, "trust_mgmt": c2.value,
                                 "moat_20y": c3.value, "outside_circle": c4.value, "note": note.value})
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save).props("unelevated")
        with ui.card().classes("oe-card flex-1"):
            ui.label("Signal history").classes("oe-h2")
            ui.label(f"Live signals and backtests of {prof.name} v{prof.version} only.").classes("oe-small")
            sigs = data.stock_signals(s["cik"], prof, limit=200)
            if not sigs:
                ui.label("No signals for this stock yet.").classes("oe-muted")
            box = ui.column().classes("w-full gap-1")

            def show(n):
                box.clear()
                with box:
                    for g in sigs[:n]:
                        with ui.row().classes("w-full items-center gap-2 no-wrap"):
                            ui.label(g["date"]).classes("oe-small w-24")
                            ui.html(badge_html(g["type"], True), sanitize=False)
                            ui.html(strength.strength_html(g["type"], g["rank_score"], g["quality"], g["mos"],
                                                           g["reason"] or "", small=True), sanitize=False)
                            ui.label(g["reason"] or "").classes("text-sm flex-1")
                            ui.label("live" if g["kind"] == "live" else f"backtest #{g['run_id']}").classes(
                                "oe-small whitespace-nowrap")
                    if len(sigs) > n:
                        lab = f"Show all {len(sigs)}" if len(sigs) < 200 else "Show the 200 most recent"
                        ui.button(lab, on_click=lambda: show(len(sigs))).props("flat dense no-caps")
            show(15)


def _warnings_card(cik, snap, prof):
    q = db.one("SELECT * FROM qsnaps WHERE cik=? ORDER BY filed DESC LIMIT 1", (cik,))
    flags = json.loads(q["flags_json"]) if q else []
    m = json.loads(q["metrics_json"]) if q else {}
    newer = q is not None and q["filed"] > (snap["filed"] or "")
    gm = snap.get("gate_margin")
    gmr = json.loads(snap["gate_margin_rule"]) if snap.get("gate_margin_rule") else None
    near = gm is not None and gm < prof.settings["review_gate_margin"] and gmr
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Early warnings").classes("oe-h2")
            ui.label(f"latest 10-Q filed {q['filed']} (quarter ended {q['period_end']})" if q else
                     "no 10-Q data (annual filer)").classes("oe-small")
        if not flags and not near and (not q or newer):
            ui.label("✓ Nothing flagged. Trailing-12-month results and every gate have room to spare.").classes(
                "text-green-800 text-sm")
        if flags and not newer:
            ui.label(f"{len(flags)} flag(s) from the {q['filed']} 10-Q were superseded by the newer annual report "
                     "and no longer count.").classes("oe-small")
            flags = []
        if not flags and not near and q and not newer:
            ui.label("✓ Nothing current flagged.").classes("text-green-800 text-sm")
        for f in flags:
            with ui.row().classes("items-center gap-2"):
                ui.icon("error" if f["hard"] else "warning").classes(
                    "text-red-700" if f["hard"] else "text-amber-700")
                ui.label(f"{f['name']}: {f['detail']}").classes("text-sm")
                ui.label("hard: blocks new buys; SELL if the next filing confirms" if f["hard"] else
                         "soft: watch item").classes("oe-small")
        if near:
            from .theme import fmt_value as fv

            with ui.row().classes("items-center gap-2"):
                ui.icon("warning").classes("text-amber-700")
                ui.label(f"{gmr['name']} is within {gm:.0%} of its limit ({fv(gmr['value'], gmr.get('fmt', ''))} vs "
                         f"{fv(gmr['target'], gmr.get('fmt', ''))})").classes("text-sm")
                ui.label("soft: watch item").classes("oe-small")
        if m:
            parts = []
            if m.get("ttm_net_income") is not None:
                parts.append(f"TTM net income {m['ttm_net_income'] / 1e6:,.0f}M")
            if m.get("ttm_ocf") is not None:
                parts.append(f"TTM operating cash flow {m['ttm_ocf'] / 1e6:,.0f}M")
            for k, lab in (("ttm_roe", "TTM ROE"), ("ttm_roa", "TTM ROA")):
                if m.get(k) is not None:
                    parts.append(f"{lab} {m[k]:.1%}")
            if parts:
                ui.label(" · ".join(parts)).classes("oe-small")


def _adr_card(s):
    with ui.card().classes("oe-card w-full").style("border-left:4px solid #7a4f9e"):
        ui.label("Foreign filer").classes("font-semibold")
        ui.label(f"Reports in {s['currency']}. Per-share figures are converted to US dollars with the Federal "
                 f"Reserve exchange rate, then multiplied by the ADR ratio (ordinary shares per traded share)."
                 ).classes("oe-small")
        src = s.get("adr_source") or "not checked yet"
        with ui.row().classes("items-end gap-3"):
            r = ui.number("Ordinary shares per ADS", value=s.get("adr_ratio"), format="%.4g").props(
                "outlined dense").classes("w-56")

            def save():
                from ..sources import sec as _sec

                if not r.value or r.value <= 0:
                    ui.notify("Enter a positive number", type="warning")
                    return
                _sec.set_adr_ratio(s["cik"], float(r.value), "manual: entered on the stock page")
                ui.notify("Saved. It applies from the next update (Run daily update now).", type="positive")

            ui.button("Save ratio", on_click=save).props("outline dense")
        ui.label(f"Source: {src}").classes("oe-small")


_TREND_COL = {"up": "#1f6f4a", "down": "#a33a2b", "sideways": "#6b746f"}


def _targets_card(s, tp, t, owned):
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Price targets").classes("oe-h2")
            if t:
                ui.html(f'<span class="oe-pill" style="background:#f0eee7;color:{_TREND_COL[t["trend"]]}">'
                        f'{targets.TREND_TEXT[t["trend"]]}</span>', sanitize=False)
        if tp and tp.get("limit"):
            with ui.grid(columns=5).classes("w-full gap-3 oe-kpis"):
                kpi(money(tp["limit"]), "Suggested add limit" if owned else "Suggested buy limit", tp["limit_label"],
                    BRAND)
                kpi(money(tp["buy_below"]), "Buy below", "margin of safety and 10% return")
                kpi(money(tp["strong"]), "Strong buy below", "extra 15% margin")
                kpi(money(tp["iv"]), "Intrinsic value", "not a sell price")
                kpi(money(tp["trim_above"]), "Trim above", "2× value and P/E 2× its norm" +
                    ("" if owned else " (if owned)"), "#a24d00")
            ui.label(tp["note"]).classes("text-sm")
            if len(tp["tranches"]) > 1:
                with ui.row().classes("gap-6"):
                    for k, (pr, fr, lab) in enumerate(tp["tranches"], 1):
                        ui.label(f"{k}. ⅓ at {money(pr)} · {lab}").classes("text-sm font-semibold")
            ui.label("Sells are business-driven, Oracle style: a SELL or REVIEW fills at the next open whatever the "
                     "chart says. Charts only fine-tune the entry price inside the Buy Zone.").classes("oe-small")
        elif tp:
            ui.label(tp["note"] or "No targets yet.").classes("oe-muted")
        if t:
            lv = targets.support_levels(t)
            res = [(t["hi52"], "52-week high")] + [(v, f"Fibonacci {r * 100:.1f}%") for r, v in t["fibs"].items()
                                                   if v > t["close"]]
            with ui.row().classes("w-full gap-8 items-start"):
                with ui.column().classes("gap-0"):
                    ui.label("Support below today (where buyers stepped in)").classes("oe-small font-semibold")
                    for v, lab in [x for x in lv if x[0] <= t["close"]][:6]:
                        ui.label(f"{money(v)} · {lab} · {pct(v / t['close'] - 1, 1)}").classes("text-sm oe-num")
                with ui.column().classes("gap-0"):
                    ui.label("Resistance above (info only)").classes("oe-small font-semibold")
                    for v, lab in sorted({x for x in res if x[0] > t["close"]}, key=lambda x: x[0])[:4]:
                        ui.label(f"{money(v)} · {lab} · +{pct(v / t['close'] - 1, 1)}").classes("text-sm oe-num")
                with ui.column().classes("gap-0"):
                    ui.label("Context").classes("oe-small font-semibold")
                    ui.label(f"Average daily range (ATR-14) {money(t['atr'])} = {pct(t['atr_pct'], 1)}").classes(
                        "text-sm")
                    if tp and tp.get("band"):
                        ui.label(f"'Approaching' alert within {pct(tp['band'], 1)} of a target").classes("text-sm")
                    if t.get("mom_12_1") is not None:
                        ui.label(f"12-month momentum {pct(t['mom_12_1'], 1)}").classes("text-sm")
                    ui.label(f"Fibonacci levels from the 52-week {'rise' if t['swing'] == 'up' else 'fall'} "
                             f"{money(t['lo52'] if t['swing'] == 'up' else t['hi52'])} → "
                             f"{money(t['hi52'] if t['swing'] == 'up' else t['lo52'])}").classes("oe-small")
        _custom_alerts(s)
        al = targets.recent_alerts(30, s["cik"])
        if al:
            ui.label("Recent price alerts").classes("oe-small font-semibold")
            for a in al[:6]:
                ui.label(f"{a['date']} · {'✔ reached' if a['state'] == 'reached' else '… approaching'} · "
                         f"{a['message']}").classes("text-sm")


def _custom_alerts(s):
    box = ui.row().classes("w-full items-center gap-3")

    def render():
        box.clear()
        with box:
            ui.label("My alerts:").classes("oe-small font-semibold")
            for c in db.rows("SELECT * FROM custom_targets WHERE cik=? AND active=1 ORDER BY price", (s["cik"],)):
                with ui.row().classes("items-center gap-1"):
                    ui.label(f"{'≤' if c['direction'] == 'below' else '≥'} {money(c['price'])}"
                             f"{' · ' + c['note'] if c['note'] else ''}").classes("text-sm")
                    ui.button(icon="close", on_click=lambda c=c: (db.get().execute(
                        "UPDATE custom_targets SET active=0 WHERE id=?", (c["id"],)), render())).props(
                        "flat dense round size=sm")
            pr = ui.number("Price", format="%.2f").props("outlined dense").classes("w-28")
            dr = ui.select({"below": "falls to", "above": "rises to"}, value="below").props(
                "outlined dense").classes("w-28")
            nt = ui.input("Note").props("outlined dense").classes("w-40")

            def add():
                if not pr.value or pr.value <= 0:
                    ui.notify("Enter a price", type="warning")
                    return
                db.get().execute("INSERT INTO custom_targets(cik,price,direction,note,created_at) VALUES(?,?,?,?,?)",
                                 (s["cik"], float(pr.value), dr.value, (nt.value or "").strip(), db.now()))
                ui.notify("Alert saved. It's checked after each daily close.", type="positive")
                render()
            ui.button("Add alert", icon="notifications", on_click=add).props("flat dense")
    render()


def _price_chart(ticker, run_id, cik, tp=None, t=None):
    px = data.stock_prices(ticker)
    vh = data.stock_value_history(run_id, cik)
    if not px:
        return
    sigs = data.stock_signals(cik, run_id=run_id, limit=None)
    marks = [{"name": g["type"], "coord": [g["date"], g["price"]], "value": g["type"].replace("_", " ")[:6],
              "itemStyle": {"color": BADGES.get(g["type"], ("", "#444"))[1] if g["type"] != "BUY" else "#2e8b57"}}
             for g in sigs if g["price"]]
    closes = [p["close"] for p in px]

    def sma(n):
        out, run = [], 0.0
        for i, c in enumerate(closes):
            run += c
            if i >= n:
                run -= closes[i - n]
            out.append([px[i]["date"], round(run / n, 2) if i >= n - 1 else None])
        return out

    lines = []
    if tp and tp.get("limit"):
        lines.append({"yAxis": round(tp["limit"], 2), "name": "Suggested limit",
                      "lineStyle": {"color": BRAND, "type": "solid", "width": 1.5},
                      "label": {"formatter": "limit ${c}".replace("{c}", f"{tp['limit']:,.2f}"), "position": "insideEndTop",
                                "color": BRAND, "fontSize": 10}})
    fib_lines = []
    if t and t.get("fibs") and len(px) > 5:
        start = px[max(0, len(px) - 252)]["date"]
        for r, v in t["fibs"].items():
            fib_lines.append([{"coord": [start, round(v, 2)], "name": f"Fib {r * 100:.1f}%",
                               "label": {"formatter": f"Fib {r * 100:.1f}%  ${v:,.2f}", "position": "insideEndTop",
                                         "fontSize": 9, "color": "#7a4f9e"}},
                              {"coord": [px[-1]["date"], round(v, 2)]}])
    with ui.card().classes("oe-card w-full"):
        ui.label("Price vs value").classes("oe-h2")
        ui.label("Value lines decide what's cheap; the 50/200-day averages and Fibonacci levels (purple, last 52 "
                 "weeks) only refine the entry price.").classes("oe-small")
        zoom_chart({
            "tooltip": {"trigger": "axis"},
            "legend": {"data": ["Price", "Intrinsic value", "Buy below", "50-day avg", "200-day avg"], "top": 0,
                       "selected": {"50-day avg": True, "200-day avg": True}},
            "grid": {"left": 60, "right": 20, "top": 30, "bottom": 40},
            "xAxis": {"type": "time"},
            "yAxis": {"type": "value", "scale": True, "axisLabel": {"formatter": "${value}"}},
            "series": [
                {"name": "Price", "type": "line", "showSymbol": False, "data": [[p["date"], p["close"]] for p in px],
                 "lineStyle": {"width": 1.6, "color": "#1d2321"}, "itemStyle": {"color": "#1d2321"},
                 "markPoint": {"symbol": "pin", "symbolSize": 34, "data": marks[-40:], "label": {"fontSize": 8}},
                 "markLine": {"symbol": "none", "silent": True, "data": lines + fib_lines,
                              "lineStyle": {"color": "#7a4f9e", "type": "dotted", "width": 1}}},
                {"name": "50-day avg", "type": "line", "showSymbol": False, "data": sma(50),
                 "lineStyle": {"width": 1, "color": "#c08a2e"}, "itemStyle": {"color": "#c08a2e"}},
                {"name": "200-day avg", "type": "line", "showSymbol": False, "data": sma(200),
                 "lineStyle": {"width": 1.2, "color": "#8a6d9e"}, "itemStyle": {"color": "#8a6d9e"}},
                {"name": "Intrinsic value", "type": "line", "showSymbol": False, "data": [[v["date"], v["iv"]] for v in vh],
                 "lineStyle": {"type": "dashed", "color": "#2c5d8f"}, "itemStyle": {"color": "#2c5d8f"}},
                {"name": "Buy below", "type": "line", "showSymbol": False,
                 "data": [[v["date"], v["buy_below"]] for v in vh], "lineStyle": {"color": BRAND},
                 "itemStyle": {"color": BRAND}, "areaStyle": {"color": "rgba(31,111,74,0.06)"}},
            ],
        }, height="h-96", default="5Y", name=f"{ticker}-price-vs-value")


# ===================================================================== SIGNALS
@ui.page("/signals")
def signals_page():
    with frame("Signals"):
        runs = data.all_runs()
        sel = {"run": app.storage.user.get("run_id") or data.default_run_id(), "types": []}
        ui.label("Signals log").classes("oe-h1")
        ui.label("Every buy, add, trim, swap and sell with the reason it fired. Click a row to open the stock.").classes(
            "oe-muted")
        holder = ui.column().classes("w-full")

        def render():
            holder.clear()
            q = """SELECT g.date, g.type, s.ticker, s.name, g.reason, g.price, g.fill_price, g.acted_on,
                          e.rank_score, e.quality, e.mos, e.exp_return
                   FROM signals g JOIN securities s ON s.cik=g.cik
                   LEFT JOIN evaluations e ON e.run_id=g.run_id AND e.date=g.date AND e.cik=g.cik
                   WHERE g.run_id=?"""
            params = [sel["run"]]
            if sel["types"]:
                q += f" AND g.type IN ({','.join('?' * len(sel['types']))})"
                params += sel["types"]
            rows = [dict(r) for r in db.rows(q + " ORDER BY g.date DESC, g.id DESC LIMIT 5000", params)]
            gate = r_mos_gate(sel["run"])
            for r in rows:
                r["strength"] = strength.strength_html(r["type"], r["rank_score"], r["quality"], r["mos"], r["reason"],
                                                       gate, small=True)
                r["score"] = round(r["rank_score"], 1) if r["rank_score"] is not None else None
                r["q"] = round(r["quality"]) if r["quality"] is not None else None
                r["mos_pct"] = round(100 * r["mos"], 1) if r["mos"] is not None else None
                r["er_pct"] = round(100 * r["exp_return"], 1) if r["exp_return"] is not None else None
                r["badge"] = badge_html(r["type"], True)
                r["price"] = round(r["price"] or 0, 2)
                r["fill_price"] = round(r["fill_price"], 2) if r["fill_price"] else None
                r["acted_on"] = "✓" if r["acted_on"] else ""
            with holder:
                with ui.row().classes("items-center gap-3"):
                    ui.label(f"{len(rows)} signals").classes("oe-small")
                    ui.button("Export CSV", icon="download", on_click=lambda: _csv(rows)).props("flat dense")
                _grid(rows, [
                    {"field": "date", "headerName": "Date", "width": 110},
                    {"field": "badge", "headerName": "Signal", "width": 140},
                    {"field": "strength", "headerName": "Strength", "width": 190},
                    {"field": "ticker", "headerName": "Ticker", "width": 85, "cellClass": "font-bold"},
                    {"field": "score", "headerName": "Rank score", "width": 105},
                    {"field": "q", "headerName": "Quality", "width": 90},
                    {"field": "mos_pct", "headerName": "MOS %", "width": 85},
                    {"field": "er_pct", "headerName": "Exp. ret %", "width": 100},
                    {"field": "reason", "headerName": "Why", "flex": 1, "minWidth": 360, "wrapText": True,
                     "autoHeight": True},
                    {"field": "name", "headerName": "Company", "width": 170},
                    {"field": "price", "headerName": "Close", "width": 90},
                    {"field": "fill_price", "headerName": "Filled at (adj.)", "width": 130},
                    {"field": "acted_on", "headerName": "Done", "width": 75},
                ], html_cols=[1, 2], height=640)

        with ui.row().classes("items-center gap-3"):
            ui.select({r["id"]: (r["pname"] if r["kind"] == "live" else f"Backtest · {r['name']}") for r in runs},
                      value=sel["run"], label="Run",
                      on_change=lambda e: (sel.update(run=e.value), render())).props("dense outlined").classes("w-80")
            ui.select(["STRONG_BUY", "BUY", "ADD", "SELL", "SWAP", "TRIM"], multiple=True, label="Types",
                      on_change=lambda e: (sel.update(types=e.value or []), render())).props(
                "dense outlined use-chips").classes("w-96")
        render()


def _csv(rows):
    buf = io.StringIO()
    keys = ["date", "type", "ticker", "name", "reason", "price", "fill_price"]
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    ui.download(buf.getvalue().encode(), "oracle_edge_signals.csv")


# ===================================================================== PERFORMANCE
@ui.page("/performance")
def performance_page(run: int | None = None):
    with frame("Performance"):
        run_id = run or app.storage.user.get("run_id") or data.default_run_id()
        if not run_id:
            empty("No runs yet.")
            return
        r = data.run(run_id)
        with ui.row().classes("w-full items-end justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("Performance").classes("oe-h1")
                ui.label(f"{r['name']} · {r['kind']}").classes("oe-muted")
            _run_picker(run_id, False, lambda v: ui.navigate.to(f"/performance?run={v}"))
        curve = data.equity(run_id)
        st = data.stats(curve)
        if not curve:
            empty("No equity history for this run yet.")
            return
        with ui.grid(columns=5).classes("w-full gap-3 oe-kpis"):
            kpi(pct(st.get("cagr"), 1), "Model CAGR", f"S&P 500 {pct(st.get('bench_cagr'), 1)}",
                "#1f6f4a" if (st.get("cagr") or 0) >= (st.get("bench_cagr") or 0) else "#a33a2b")
            kpi(pct(st.get("total"), 0), "Total return", f"S&P 500 {pct(st.get('bench_total'), 0)}")
            kpi(pct(st.get("max_dd"), 1), "Worst drop (max drawdown)",
                f"S&P 500 {pct(st.get('bench_max_dd'), 1)} · worst 12 months {pct(st.get('worst_12m'), 0)} "
                f"(S&P {pct(st.get('bench_worst_12m'), 0)})",
                "#a33a2b" if (st.get("max_dd") or 0) < (st.get("bench_max_dd") or 0) else None)
            kpi(num(st.get("sharpe"), 2), "Sharpe (rf = 0)")
            eps = data.episodes(run_id)
            closed = [e for e in eps if e["status"] == "closed"]
            wins = sum(1 for e in closed if (e["return_pct"] or 0) > 0)
            kpi(f"{wins}/{len(closed)}" if closed else "—", "Winning closed holdings",
                f"{len(eps) - len(closed)} still open")
        money = [c for c in curve if c.get("value") is not None]
        if money and money[-1].get("contributed"):
            last = money[-1]
            res_amt = db.scalar("SELECT reserve FROM portfolios WHERE run_id=? AND kind='model'", (run_id,), 0) or 0
            with ui.card().classes("oe-card w-full"):
                ui.label("Your money").classes("oe-h2")
                ui.html(f'<div style="font-size:16px">You put in <b>${last["contributed"]:,.0f}</b> (starting money plus '
                        f'each week\'s amount). Following every signal it would be worth <b>${last["value"]:,.0f}</b>. '
                        f'The same money put into the S&amp;P 500 on the same days: '
                        f'<b>${(last["bench_value"] or 0):,.0f}</b>. Cash waiting: {pct(last.get("cash"), 0)}'
                        + (f' (of which ${res_amt:,.0f} is the reserve for strong buys)' if res_amt else "")
                        + '.</div>',
                        sanitize=False)
                zoom_chart({
                    "tooltip": {"trigger": "axis"}, "legend": {"top": 0},
                    "grid": {"left": 70, "right": 20, "top": 30, "bottom": 40}, "xAxis": {"type": "time"},
                    "yAxis": {"type": "value", "axisLabel": {"formatter": "${value}"}},
                    "series": [
                        {"name": "Following the signals", "type": "line", "showSymbol": False,
                         "data": [[c["date"], round(c["value"])] for c in money],
                         "lineStyle": {"color": BRAND, "width": 2}, "itemStyle": {"color": BRAND}},
                        {"name": "Same money in the S&P 500", "type": "line", "showSymbol": False,
                         "data": [[c["date"], round(c["bench_value"] or 0)] for c in money],
                         "lineStyle": {"color": "#8a8f8c"}, "itemStyle": {"color": "#8a8f8c"}},
                        {"name": "Money put in", "type": "line", "step": "end", "showSymbol": False,
                         "data": [[c["date"], round(c["contributed"] or 0)] for c in money],
                         "lineStyle": {"color": "#c9c5b9", "type": "dashed"}, "itemStyle": {"color": "#c9c5b9"}},
                    ]}, height="h-80", name="your-money")
        with ui.card().classes("oe-card w-full"):
            ui.label("If you had followed every signal: growth of $1 vs the S&P 500").classes("oe-h2")
            ui.label("Green: the model portfolio, an imaginary account that bought and sold exactly as the signals said "
                     "(at the next day's open). Grey: an S&P 500 index fund with dividends reinvested. Both start at "
                     "$1, and new money doesn't count as growth. Beige (right axis): how many stocks were held.").classes(
                "oe-small")
            zoom_chart({
                "tooltip": {"trigger": "axis"}, "legend": {"top": 0},
                "grid": {"left": 50, "right": 50, "top": 30, "bottom": 40},
                "xAxis": {"type": "time"},
                "yAxis": [{"type": "value", "scale": True, "name": "growth of $1"},
                          {"type": "value", "name": "stocks held", "position": "right",
                                                              "splitLine": {"show": False}}],
                "series": [
                    {"name": "Followed every signal", "type": "line", "showSymbol": False,
                     "data": [[c["date"], round(c["nav"], 4)] for c in curve], "lineStyle": {"color": BRAND, "width": 2},
                     "itemStyle": {"color": BRAND}},
                    {"name": "S&P 500 (SPY)", "type": "line", "showSymbol": False,
                     "data": [[c["date"], round(c["bench_nav"] or 1, 4)] for c in curve],
                     "lineStyle": {"color": "#8a8f8c"}, "itemStyle": {"color": "#8a8f8c"}},
                    {"name": "Stocks held (right axis)", "type": "line", "step": "end", "yAxisIndex": 1, "showSymbol": False,
                     "data": [[c["date"], c["holdings"]] for c in curve], "lineStyle": {"color": "#c9c5b9", "width": 1},
                     "itemStyle": {"color": "#c9c5b9"}},
                ],
            }, height="h-96", name="growth-of-1")
        with ui.column().classes("w-full gap-4"):
            with ui.card().classes("oe-card w-full"):
                ui.label("Is the ranking working?").classes("oe-h2")
                ui.label("Every Buy Zone entry, grouped by its rank on the day it entered, with the stock's next "
                         "year next to the S&P 500's over the same days. Top ranks should beat the S&P by more. "
                         "Compare the 'vs S&P' columns, not the raw return: a rank of 16 or worse only exists when "
                         "many stocks are cheap at once, which is usually a market sell-off, so the raw return "
                         "includes the market's own rebound.").classes("oe-small")
                rb = data.rank_buckets(run_id)
                skipped = rb[-1].get("skipped_start", 0)
                rb = rb[:-1]
                ui.table(rows=[{"bucket": b["bucket"], "n": f"{b['stints']} ({b['stocks']})",
                                "fwd": pct(b["avg_fwd_1y"], 1), "med": pct(b["med_fwd_1y"], 1),
                                "spx": pct(b["avg_bench"], 1), "ex": pct(b["avg_excess_1y"], 1),
                                "mex": pct(b["med_excess_1y"], 1),
                                "beat": "—" if b["beat"] is None else f"{b['beat'] * 100:.0f}%",
                                "eps": b["episodes"], "hex": pct(b["avg_excess"], 1), "hmex": pct(b["med_excess"], 1)}
                               for b in rb],
                         columns=[{"name": "bucket", "label": "Entry rank", "field": "bucket"},
                                  {"name": "n", "label": "Entries (stocks)", "field": "n"},
                                  {"name": "fwd", "label": "1-yr return: avg", "field": "fwd"},
                                  {"name": "med", "label": "median", "field": "med"},
                                  {"name": "spx", "label": "S&P same days", "field": "spx"},
                                  {"name": "ex", "label": "vs S&P: avg", "field": "ex"},
                                  {"name": "mex", "label": "median", "field": "mex"},
                                  {"name": "beat", "label": "Beat S&P", "field": "beat"},
                                  {"name": "eps", "label": "Closed holdings", "field": "eps"},
                                  {"name": "hex", "label": "Holdings vs S&P: avg", "field": "hex"},
                                  {"name": "hmex", "label": "median", "field": "hmex"}],
                         row_key="bucket").props("flat dense").classes("w-full")
                ui.label("A stock that leaves the Buy Zone and comes back within 30 days counts as one entry. "
                         + (f"{skipped} entries on the first day of the history are left out: those stocks were "
                            "already in the Buy Zone when it began. " if skipped else "")
                         + "Medians are less swayed by a few huge winners. Holdings: return vs the S&P while "
                           "actually owned (not annualized).").classes("oe-small")
            with ui.card().classes("oe-card w-full"):
                ui.label("While stocks met the criteria").classes("oe-h2")
                ss = data.stints_summary(run_id)
                ui.label("Every stretch a stock spent in the Buy Zone, owned or not.").classes("oe-small")
                if ss and ss.get("n"):
                    ui.label(f"{ss['n']} completed stretches · average {pct(ss['r'], 1)} vs S&P {pct(ss['b'], 1)} over "
                             f"the same days (median gap {pct(ss['med_ex'], 1)}) · beat the S&P {ss['beat'] or 0} "
                             f"times · 1 year after entry: {pct(ss['f'], 1)}, {pct(ss['fx'], 1)} vs the S&P "
                             f"(median {pct(ss['med_fx'], 1)})").classes("text-sm")
                else:
                    ui.label("No completed stretches yet.").classes("oe-muted")
        open_ret = data.open_episode_returns(run_id)
        rows = [{"ticker": e["ticker"], "name": e["name"], "entry": e["entry_date"], "exit": e["exit_date"] or "open",
                 "days": e["days_held"], "ret": round(((e["return_pct"] if e["status"] == "closed"
                                                        else open_ret.get(e["cik"])) or 0) * 100, 1),
                 "bench": round((e["bench_return_pct"] or 0) * 100, 1) if e["bench_return_pct"] is not None else None,
                 "excess": round((e["excess_pct"] or 0) * 100, 1) if e["excess_pct"] is not None else None,
                 "mdd": round((e["max_dd_pct"] or 0) * 100, 1) if e["max_dd_pct"] is not None else None,
                 "rank": e["entry_rank"], "why": e["exit_reason"] or ""} for e in eps]
        with ui.card().classes("oe-card w-full"):
            ui.label("Holdings history (BUY → SELL)").classes("oe-h2")
            _grid(rows, [
                {"field": "ticker", "headerName": "Ticker", "width": 85, "cellClass": "font-bold"},
                {"field": "name", "headerName": "Company", "flex": 1, "minWidth": 150},
                {"field": "entry", "headerName": "Bought", "width": 105},
                {"field": "exit", "headerName": "Sold", "width": 105},
                {"field": "days", "headerName": "Days", "width": 75},
                {"field": "ret", "headerName": "Return %", "width": 95},
                {"field": "bench", "headerName": "S&P %", "width": 85},
                {"field": "excess", "headerName": "Excess %", "width": 95},
                {"field": "mdd", "headerName": "Max DD %", "width": 95},
                {"field": "rank", "headerName": "Entry rank", "width": 95},
                {"field": "why", "headerName": "Exit reason", "flex": 1, "minWidth": 200},
            ], height=420)


# ===================================================================== MARKET
def _news_section(run_id):
    """Market headlines and headlines on watched stocks, loaded after the page appears (never blocks it)."""
    from .. import news

    with ui.row().classes("w-full gap-4 items-stretch oe-news"):
        mk_card = ui.card().classes("oe-card flex-1 min-w-[320px] gap-1")
        st_card = ui.card().classes("oe-card flex-[1.4] min-w-[360px] gap-1")
    with mk_card:
        ui.label("Market news").classes("oe-h2")
        mk_box = ui.column().classes("w-full gap-1")
        with mk_box:
            ui.spinner(size="sm")
    with st_card:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("News on your stocks").classes("oe-h2")
            flt = ui.toggle({"all": "All", "you hold it": "Held", "model holding": "Model",
                             "price alert": "Alerts"}, value="all").props("dense no-caps size=sm")
        ui.label("Your holdings, the model portfolio's holdings and stocks with price alerts this week.").classes(
            "oe-small")
        st_box = ui.column().classes("w-full gap-2")
        with st_box:
            ui.spinner(size="sm")
    state: dict = {"groups": []}

    def item(x):
        with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
            ui.link(x["title"], x["url"], new_tab=True).classes("text-sm oe-news-link flex-1")
            ui.label(" · ".join(v for v in (x.get("source"), news.ago(x.get("when"))) if v)).classes(
                "oe-small whitespace-nowrap")

    def draw_stocks():
        st_box.clear()
        groups = [g for g in state["groups"] if flt.value == "all" or g["why"] == flt.value]
        with st_box:
            if not groups:
                ui.label("No recent headlines for these stocks." if state["groups"] else
                         "No headlines right now (or the news sites can't be reached).").classes("oe-muted")
            for g in groups:
                with ui.column().classes("w-full gap-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.link(g["ticker"], f"/stock/{g['ticker']}").classes("font-bold")
                        ui.label(g["why"]).classes("oe-small")
                    for x in g["items"]:
                        item(x)

    flt.on_value_change(lambda e: draw_stocks())

    async def load():
        from nicegui import run

        mk = await run.io_bound(news.market, 12)
        mk_box.clear()
        with mk_box:
            if not mk:
                ui.label("No headlines right now (or the news sites can't be reached).").classes("oe-muted")
            for x in mk:
                item(x)
        state["groups"] = await run.io_bound(news.for_watched, run_id)
        draw_stocks()

    ui.timer(0.3, load, once=True)


def _tn(v) -> str:
    """Dollars as $X.XXT / $X.XB."""
    if v is None:
        return "—"
    return f"${v / 1e12:,.2f}T" if abs(v) >= 1e12 else f"${v / 1e9:,.1f}B"


def _gdp_note(us: dict) -> str:
    base = f"{_tn(us['gdp_q'])} for the quarter ending {us['gdp_quarter_end']} (nominal, seasonally adjusted)"
    if us.get("gdp_growth") is None or abs(us["gdp_scale"] - 1) < 1e-9:
        return base
    return (f"{base}, grown {(us['gdp_scale'] - 1) * 100:+.1f}% to today at its past-year rate of "
            f"{us['gdp_growth'] * 100:.1f}%/yr until the next release")


def _gdp_note_world(w: dict) -> str:
    base = f"{_tn(w['gdp_y'])} for {w['gdp_year']} (latest the World Bank has published)"
    if w.get("gdp_growth") is None or abs(w["gdp_scale"] - 1) < 1e-9:
        return base
    return f"{base}, grown {(w['gdp_scale'] - 1) * 100:+.1f}% since at its latest yearly rate of {w['gdp_growth'] * 100:.1f}%"


@ui.page("/market")
def market_page():
    with frame("Market"):
        mk = data.market_now()
        reg = mk["regime"]
        us, world = mk["us"], mk["world"]
        off = [h for h in mk["history"] if not h.get("estimate")]
        est = (off[-1:] + [h for h in mk["history"] if h.get("estimate")]) if off else []
        ui.label("Market temperature").classes("oe-h1")
        ui.label("The Buffett Indicator: the stock market's total value ÷ the economy's yearly output (GDP). It rates "
                 "the whole market, so it can raise the margin-of-safety bar but never forces a sale.").classes(
            "oe-muted")
        with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
            kpi(pct(reg.ratio) if reg.ratio else "—", "US: market value ÷ GDP", reg.zone)
            kpi(_tn(us["equities"]) if us else "—", "US stock market value",
                (f"Fed Z.1 level at {us['quarter_end']} ({_tn(us['equities_q'])}), moved "
                 f"{(us['scale'] - 1) * 100:+.1f}% since by the US total-market ETF (VTI) price") if us else "")
            kpi(_tn(us["gdp"]) if us else "—", "US GDP (yearly rate)", _gdp_note(us) if us else "")
            kpi(pct(reg.mos_gate), "Margin of safety required",
                "raised when expensive (profile setting on)" if rules.default_profile().settings.get("regime_modifier")
                else "fixed (the profile doesn't raise it when expensive)")
        with ui.card().classes("oe-card w-full"):
            ui.label("US Buffett Indicator history").classes("oe-h2")
            ui.label("Solid: official quarter-end figures (market value of US companies' shares ÷ that quarter's "
                     "GDP). Dashed: the weekly estimate since the last official quarter, ending with today's "
                     "reading.").classes("oe-small")
            zoom_chart({
                "tooltip": {"trigger": "axis", ":valueFormatter": "(v) => v + '%'"}, "grid": {"left": 55, "right": 48},
                "xAxis": {"type": "time"}, "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}%"}},
                "visualMap": {"show": False, "dimension": 1, "pieces": [
                    {"lt": 90, "color": "#1f6f4a"}, {"gte": 90, "lt": 120, "color": "#6fb38f"},
                    {"gte": 120, "lt": 150, "color": "#c9a227"}, {"gte": 150, "lt": 200, "color": "#d9822b"},
                    {"gte": 200, "color": "#a33a2b"}]},
                "series": [{"type": "line", "showSymbol": False, "name": "Market value ÷ GDP",
                            "data": [[h["date"], round(h["ratio"] * 100, 1)] for h in off],
                            "markLine": {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed"},
                                         "data": [{"yAxis": 90}, {"yAxis": 120}, {"yAxis": 150}, {"yAxis": 200}]}},
                           {"type": "line", "showSymbol": False, "name": "Estimate",
                            "data": [[h["date"], round(h["ratio"] * 100, 1)] for h in est],
                            "lineStyle": {"type": "dashed", "width": 2}}],
            }, height="h-96", name="us-buffett-indicator")
            ui.label("Zones: <90% undervalued · 90–120% fair · 120–150% modestly overvalued · 150–200% significantly "
                     "overvalued · >200% extreme. Different methods can differ by 20–40 points.").classes("oe-small")
        with ui.card().classes("oe-card w-full"):
            ui.label("US market value and GDP").classes("oe-h2")
            ui.label("The two numbers behind the indicator, in trillions of dollars. The gap between the lines is the "
                     "indicator.").classes("oe-small")
            zoom_chart({
                "tooltip": {"trigger": "axis", ":valueFormatter": "(v) => '$' + v + 'T'"}, "legend": {"top": 0},
                "xAxis": {"type": "time"}, "yAxis": {"type": "value", "name": "$ trillions",
                                                     "axisLabel": {"formatter": "${value}T"}},
                "series": [
                    {"type": "line", "showSymbol": False, "name": "Stock market value (Fed Z.1)",
                     "data": [[h["date"], round(h["equities"] / 1e12, 2)] for h in off],
                     "lineStyle": {"color": "#1f6f4a", "width": 2}, "itemStyle": {"color": "#1f6f4a"}},
                    {"type": "line", "showSymbol": False, "name": "GDP (yearly rate)",
                     "data": [[h["date"], round(h["gdp"] / 1e12, 2)] for h in off],
                     "lineStyle": {"color": "#8A5A12", "width": 2}, "itemStyle": {"color": "#8A5A12"}},
                    {"type": "line", "showSymbol": False, "name": "Market value, estimate",
                     "data": [[h["date"], round(h["equities"] / 1e12, 2)] for h in est],
                     "lineStyle": {"color": "#1f6f4a", "width": 2, "type": "dashed"}, "itemStyle": {"color": "#1f6f4a"}},
                    {"type": "line", "showSymbol": False, "name": "GDP, estimate",
                     "data": [[h["date"], round(h["gdp"] / 1e12, 2)] for h in est],
                     "lineStyle": {"color": "#8A5A12", "width": 2, "type": "dashed"}, "itemStyle": {"color": "#8A5A12"}}],
            }, height="h-80", name="us-market-value-and-gdp")
        ui.label("Global Buffett Indicator").classes("oe-h1 mt-4")
        ui.label("The same idea for the whole world: the value of every listed company ÷ world GDP, from the World "
                 "Bank (World Federation of Exchanges data). Annual, and published with a lag, so today's figure is "
                 "an estimate: the latest year-end market value moved by the world total-market ETF (VT) since, over "
                 "the latest world GDP grown at its latest yearly rate. Official figures replace the estimate as "
                 "the World Bank publishes them.").classes("oe-muted")
        if not world:
            empty("No World Bank data yet. It downloads with the next daily update.")
        else:
            wh = mk["world_history"]
            avg = sum(h["ratio"] for h in wh if not h.get("estimate")) / max(1, len([h for h in wh if not h.get("estimate")]))
            with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
                kpi(pct(world["ratio"]), "World: market value ÷ GDP", f"long-run average {pct(avg)} (since {wh[0]['date'][:4]})")
                kpi(_tn(world["equities"]), "World stock market value",
                    f"{world['year_end'][:4]} year-end {_tn(world['equities_y'])}, "
                    f"moved {(world['scale'] - 1) * 100:+.1f}% since by VT's price")
                kpi(_tn(world["gdp"]), "World GDP", _gdp_note_world(world))
                kpi(pct(reg.ratio) if reg.ratio else "—", "US, for comparison", reg.zone)
            with ui.card().classes("oe-card w-full"):
                ui.label("Global Buffett Indicator history").classes("oe-h2")
                zoom_chart({
                    "tooltip": {"trigger": "axis", ":valueFormatter": "(v) => v + '%'"}, "legend": {"top": 0},
                    "xAxis": {"type": "time"}, "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}%"}},
                    "series": [
                        {"type": "line", "name": "World (year-end; last point = estimate)", "showSymbol": True,
                         "symbolSize": 5, "data": [[h["date"], round(h["ratio"] * 100, 1)] for h in wh],
                         "lineStyle": {"color": "#2c5d8f", "width": 2}, "itemStyle": {"color": "#2c5d8f"},
                         "markLine": {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed"},
                                      "label": {"formatter": "average", "position": "insideEndTop"},
                                      "data": [{"yAxis": round(avg * 100, 1)}]}},
                        {"type": "line", "name": "US (quarterly)", "showSymbol": False,
                         "data": [[h["date"], round(h["ratio"] * 100, 1)] for h in mk["history"]
                                  if h["date"] >= wh[0]["date"]],
                         "lineStyle": {"color": "#9aa5a0", "width": 1}, "itemStyle": {"color": "#9aa5a0"}}],
                }, height="h-80", name="global-buffett-indicator")
                ui.label("The US zones don't apply to the world figure: other markets have far fewer listed companies "
                         "relative to their economies, so compare the world reading with its own average.").classes(
                    "oe-small")
            with ui.card().classes("oe-card w-full"):
                ui.label("World market value and GDP").classes("oe-h2")
                zoom_chart({
                    "tooltip": {"trigger": "axis", ":valueFormatter": "(v) => '$' + v + 'T'"}, "legend": {"top": 0},
                    "xAxis": {"type": "time"}, "yAxis": {"type": "value", "name": "$ trillions",
                                                         "axisLabel": {"formatter": "${value}T"}},
                    "series": [
                        {"type": "line", "name": "World stock market value", "symbolSize": 5,
                         "data": [[h["date"], round(h["equities"] / 1e12, 2)] for h in wh if h.get("equities")],
                         "lineStyle": {"color": "#2c5d8f", "width": 2}, "itemStyle": {"color": "#2c5d8f"}},
                        {"type": "line", "name": "World GDP", "symbolSize": 5,
                         "data": [[h["date"], round(h["gdp"] / 1e12, 2)] for h in wh if h.get("gdp")],
                         "lineStyle": {"color": "#8A5A12", "width": 2}, "itemStyle": {"color": "#8A5A12"}}],
                }, height="h-80", name="world-market-value-and-gdp")
        with ui.grid(columns=4).classes("w-full gap-3 oe-kpis mt-2"):
            kpi(pct(reg.t30, 2), "30-yr Treasury", "The Oracle's discount-rate anchor")
            kpi(pct(reg.t10, 2) if reg.t10 else "—", "10-yr Treasury")
        _btc_section(reg)


def _btc_section(reg):
    s = btcsignal.settings()
    btc = btcsignal.current(s)
    L, H = s["lookback"], s["horizon"]
    ui.element("a").props("id=btc")
    ui.label("Bitcoin early warning").classes("oe-h1 mt-4")
    ui.label(f"Bitcoin's change over the last {L} trading days, compared with its own history. In past data its "
             f"extremes went with the next {H} trading days in tech stocks (QQQ): a drop into Bitcoin's worst "
             f"{s['bear_pct']}% so far was followed by a weak month more often than usual. A warning about the "
             "coming weeks, not a forecast and never a hard stop. Cut-offs use only data known on each day. "
             "Change the window under Settings → Market temperature.").classes("oe-muted")
    if not btc:
        empty("No Bitcoin or QQQ prices yet. They download with the next daily update.")
        return
    st = btc["stats"]
    with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
        kpi(f"{btc['btc_ret'] * 100:+.1f}%", f"Bitcoin, last {L} trading days",
            f"${btc.get('start_price') or 0:,.0f} on {btc.get('start_date', '?')} → ${btc['btc']:,.0f} on {btc['date']}",
            BTC_COL.get(btc["zone"]))
        kpi(btcsignal.ZONE_LABEL[btc["zone"]], "Reading", f"since {btc['since']} ({btc['days_in_zone']} trading days)",
            BTC_COL.get(btc["zone"]))
        kpi(f"{btc['bear_cut'] * 100:+.1f}%" if btc["bear_cut"] is not None else "—",
            f"Warning cut-off (worst {s['bear_pct']}%)",
            f"tailwind above {btc['bull_cut'] * 100:+.1f}%" if btc["bull_cut"] is not None else "")
        b = st["bear"]
        kpi(f"{b['up'] * 100:.0f}%" if b["up"] is not None else "—", f"QQQ up {H} days after a warning",
            (f"avg {b['avg'] * 100:+.1f}% · {b['episodes']} episodes · normal days: "
             f"{st['normal']['up'] * 100:.0f}% up" if b["up"] is not None and st["normal"]["up"] is not None else ""))
    for a in btcsignal.advice(reg, btc):
        ui.label(f"{a['title']}: {a['text']}").classes("text-sm")
    rows = btcsignal.history(s)
    with ui.card().classes("oe-card w-full"):
        ui.label(f"Bitcoin {L}-day change, the cut-offs, and whether each call was right").classes("oe-h2")
        _btc_scorecard(btcsignal.scorecard(rows), H)
        zoom_chart(_btc_chart_options(rows, L, H), height="h-[560px]", default="5Y", name="bitcoin-early-warning")
        ui.label("Top: Bitcoin's change with the cut-offs; shaded = a warning (red) or tailwind (green) was on. Each "
                 "triangle marks the day a call was made: ▼ warning, ▲ tailwind; green = right, red = wrong, grey = "
                 f"the {H} days haven't passed yet. Bottom: QQQ, so you can see what followed. Click a legend item to "
                 "hide it.").classes("oe-small")
    _btc_calls_table(rows, H)
    with ui.card().classes("oe-card w-full"):
        ui.label(f"What QQQ did over the next {H} trading days").classes("oe-h2")
        ui.label("Days overlap within an episode, so the number of separate episodes says more about how much "
                 "evidence there is than the number of days.").classes("oe-small")
        _grid([{"zone": {"bear": "Warning (worst " + str(s["bear_pct"]) + "%)", "normal": "Normal",
                         "bull": "Tailwind (best " + str(100 - s["bull_pct"]) + "%)"}[z],
                "episodes": st[z]["episodes"], "days": st[z]["days"],
                "up": round(st[z]["up"] * 100) if st[z]["up"] is not None else None,
                "avg": round(st[z]["avg"] * 100, 1) if st[z]["avg"] is not None else None,
                "worst": round(st[z]["worst"] * 100, 1) if st[z]["worst"] is not None else None,
                "best": round(st[z]["best"] * 100, 1) if st[z]["best"] is not None else None}
               for z in ("bear", "normal", "bull")],
              [{"field": "zone", "headerName": "Bitcoin reading", "flex": 1, "minWidth": 180},
               {"field": "episodes", "headerName": "Episodes", "width": 100},
               {"field": "days", "headerName": "Days", "width": 90},
               {"field": "up", "headerName": "QQQ up, % of the time", "width": 170},
               {"field": "avg", "headerName": "QQQ average %", "width": 140},
               {"field": "worst", "headerName": "Worst %", "width": 100},
               {"field": "best", "headerName": "Best %", "width": 100}], height=190, click_to_stock=False)
    fr = btcsignal.forward_record()
    with ui.card().classes("oe-card w-full"):
        ui.label("Live record (calls logged since it was switched on)").classes("oe-h2")
        if not fr["calls"]:
            ui.label("Each daily update logs that day's call. Results fill in once the "
                     f"{H} trading days have passed, so the rule is judged on data it has never seen.").classes("oe-muted")
        else:
            parts = []
            for z, lbl in (("bear", "warnings"), ("normal", "normal days"), ("bull", "tailwinds")):
                x = fr["by_zone"][z]
                if x["n"]:
                    parts.append(f"{lbl}: {x['n']} resolved, QQQ up {x['up']:.0%}, average {x['avg']:+.1%}")
            ui.label(f"{fr['calls']} call{'s' if fr['calls'] != 1 else ''} logged since {fr['first']}; "
                     f"{fr['resolved']} resolved. " +
                     ("; ".join(parts) if parts else "None resolved yet.")).classes("text-sm")
    al = btcsignal.recent_alerts(30)
    if al:
        with ui.card().classes("oe-card w-full"):
            ui.label("Market alerts, last 30 days").classes("oe-h2")
            for a in al:
                ui.label(f"{a['date']} · {a['message']}").classes("text-sm")


# ===================================================================== MY HOLDINGS (real)
@ui.page("/holdings")
def holdings_page():
    with frame("My holdings"):
        run_id = app.storage.user.get("run_id") or data.default_run_id()
        ui.label("My holdings").classes("oe-h1")
        ui.label("Optional: enter what you really own to get badges for your own account next to the model "
                 "portfolio. Stored only in your local database.").classes("oe-muted")
        holder = ui.column().classes("w-full")

        def render():
            holder.clear()
            d = data.latest_date(run_id) if run_id else None
            evs = {e["cik"]: e for e in (data.evaluations(run_id, d) if d else [])}
            rows = db.rows("SELECT h.*, s.ticker, s.name FROM real_holdings h JOIN securities s ON s.cik=h.cik ORDER BY s.ticker")
            prof = rules.get_profile(data.run(run_id)["profile_id"]) if run_id else rules.default_profile()
            with holder:
                if not rows:
                    ui.label("No holdings entered.").classes("oe-muted")
                for h in rows:
                    badge, why = real_badge(h["cik"], evs.get(h["cik"]), prof)
                    with ui.row().classes("oe-action w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-3"):
                            ui.html(badge_html(badge), sanitize=False)
                            ui.link(h["ticker"], f"/stock/{h['ticker']}").classes("font-bold text-base")
                            ui.label(h["name"][:40]).classes("oe-muted")
                        ui.label(why).classes("text-sm")
                        ui.label(f"{h['shares']:g} sh @ {money(h['avg_cost'])}").classes("oe-small")
                        ui.button(icon="delete", on_click=lambda h=h: (db.get().execute(
                            "DELETE FROM real_holdings WHERE cik=?", (h["cik"],)), render())).props("flat round dense")

        with ui.card().classes("oe-card w-full"):
            with ui.row().classes("items-end gap-3"):
                t = ui.input("Ticker").props("dense outlined").classes("w-32")
                sh = ui.number("Shares", value=0).props("dense outlined").classes("w-32")
                ac = ui.number("Average cost", value=0).props("dense outlined prefix=$").classes("w-40")

                def add():
                    s = data.stock(t.value or "")
                    if not s:
                        ui.notify("Unknown ticker", type="warning")
                        return
                    db.get().execute("INSERT OR REPLACE INTO real_holdings(cik,shares,avg_cost,added_on) VALUES(?,?,?,?)",
                                     (s["cik"], sh.value or 0, ac.value or 0, db.now()))
                    t.value = ""
                    render()

                ui.button("Add", icon="add", on_click=add).props("unelevated")
        render()


def real_badge(cik, ev, prof) -> tuple[str, str]:
    st = prof.settings
    if ev is None:
        snap = data.latest_snapshot(cik, prof.id)
        if snap and not snap["gates_passed"] and snap["failed_gates"]:
            f = snap["failed_gates"]
            return "SELL", "thesis broken: " + ", ".join(x["name"] for x in f)
        return "HOLD", "not on today's lists (no fresh valuation)"
    if ev["list"] == "pending":
        return "HOLD", (ev["detail"] or {}).get("note", "valuation pending")
    failed = [f for f in (ev["detail"] or {}).get("failed", []) if f.get("id") != "u1"]
    if failed:
        return "SELL", "thesis broken: " + ", ".join(f["name"] for f in failed)
    if ev["quality"] is not None and ev["quality"] < st["sell_quality"]:
        return "SELL", f"Quality {ev['quality']:.0f} < {st['sell_quality']}"
    det = ev["detail"] or {}
    if ev["iv"] and ev["price"] and ev["price"] >= st["nosebleed_iv_mult"] * ev["iv"] and det.get("pe_now") and \
            det.get("pe_median") and det["pe_now"] >= st["nosebleed_pe_mult"] * det["pe_median"]:
        return "TRIM", f"nose-bleed price: {ev['price'] / ev['iv']:.1f}× value"
    qf = det.get("qflags") or []
    if qf:
        hard = [f for f in qf if f.get("hard")]
        return "REVIEW", "; ".join(f"{f['name']}: {f['detail']}" for f in qf) + (
            " · read the latest 10-Q" if hard else " · watch item")
    if det.get("gate_margin") is not None and det["gate_margin"] < st["review_gate_margin"] and det.get(
            "gate_margin_rule"):
        g = det["gate_margin_rule"]
        return "REVIEW", f"{g['name']} close to its limit"
    if ev["list"] == "buy":
        return "ADD", "still in the Buy Zone"
    return "HOLD", "business intact" + (" · expensive: hold, don't add" if det.get("expensive_above") and
                                        ev["price"] and ev["price"] > det["expensive_above"] else "")


def _btc_scorecard(sc: dict, H: int) -> None:
    def line(z, verb, base):
        x = sc[z]
        if not x["judged"]:
            return f"No {('warnings' if z == 'bear' else 'tailwinds')} judged yet."
        return (f"{x['correct']} of {x['judged']} {('warnings' if z == 'bear' else 'tailwinds')} were right "
                f"({x['rate']:.0%}): QQQ {verb} over the next {H} trading days"
                + (f", against {base:.0%} of all {H}-day windows" if base is not None else "")
                + (f"; {x['pending']} still pending" if x["pending"] else "") + ".")

    with ui.row().classes("w-full gap-6"):
        for z, verb, base, col in (("bear", "fell", sc["base_down"], "#A8322D"), ("bull", "rose", sc["base_up"], BRAND)):
            with ui.column().classes("gap-0 flex-1"):
                x = sc[z]
                ui.label(f"{x['rate']:.0%}" if x["rate"] is not None else "—").classes("oe-kpi").style(f"color:{col}")
                ui.label(line(z, verb, base)).classes("text-sm")
    ui.label("A call only adds information if it is right more often than the base rate (how often QQQ fell, or rose, "
             "in any window of the same length).").classes("oe-small")


def _btc_chart_options(rows: list[dict], L: int, H: int) -> dict:
    eps = btcsignal.episodes(rows)
    areas_b = [[{"xAxis": e["start"], "itemStyle": {"color": "rgba(168,50,45,0.10)" if e["zone"] == "bear"
                                                     else "rgba(31,111,74,0.10)"}}, {"xAxis": e["end"]}] for e in eps]

    def marks(zone, verdict):
        out = []
        for e in eps:
            if e["zone"] != zone or e["correct"] is not verdict:
                continue
            q = "pending" if e["qqq_fwd"] is None else f"QQQ next {H} days {e['qqq_fwd'] * 100:+.1f}%"
            tag = {True: "✓ right", False: "✗ wrong", None: "… pending"}[verdict]
            out.append({"value": [e["start"], round(e["btc_ret"] * 100, 1)],
                        "tip": f"{'Warning' if zone == 'bear' else 'Tailwind'} {e['start']}: Bitcoin "
                               f"{e['btc_ret'] * 100:+.1f}% · {q} · {tag}"})
        return out

    def scatter(name, zone, verdict, color):
        # a path (not a rotated triangle) so the legend icon points the same way as the marker
        return {"name": name, "type": "scatter", "z": 5, "symbolSize": 11,
                "symbol": "path://M0,0L10,0L5,9Z" if zone == "bear" else "path://M5,0L10,9L0,9Z",
                "itemStyle": {"color": color, "borderColor": "#fff", "borderWidth": 1},
                "data": marks(zone, verdict)}

    return {
        "animation": False,
        "tooltip": {"trigger": "axis", ":formatter": """params => {
            const d = params.length ? (params[0].axisValueLabel || '') : '';
            const lines = params.filter(p => p.value != null).map(p => p.data && p.data.tip ? p.marker + p.data.tip :
                p.marker + p.seriesName + ': ' + (Array.isArray(p.value) ? p.value[1] : p.value) +
                (p.seriesName === 'QQQ' ? '' : '%'));
            return d + '<br>' + lines.join('<br>');
        }"""},
        "legend": {"top": 0, "left": 0, "type": "scroll", "right": 110},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "grid": [{"left": 60, "right": 24, "top": 40, "height": "50%"}, {"left": 60, "right": 24, "top": "66%",
                                                                          "height": "20%"}],
        "xAxis": [{"type": "time", "axisLabel": {"show": False}}, {"type": "time", "gridIndex": 1}],
        "yAxis": [{"type": "value", "axisLabel": {"formatter": "{value}%"}},
                  {"type": "value", "gridIndex": 1, "name": "QQQ", "nameGap": 8, "axisLabel": {"formatter": "${value}"}}],
        "series": [
            {"type": "line", "name": f"Bitcoin {L}-day change", "showSymbol": False, "lineStyle": {"width": 1},
             "itemStyle": {"color": "#6F582C"}, "data": [[r["date"], round(r["btc_ret"] * 100, 1)] for r in rows],
             "markArea": {"silent": True, "data": areas_b}},
            {"type": "line", "name": "Warning cut-off", "showSymbol": False, "itemStyle": {"color": "#A8322D"},
             "lineStyle": {"type": "dashed", "width": 1.3},
             "data": [[r["date"], round(r["bear_cut"] * 100, 1)] for r in rows if r["bear_cut"] is not None]},
            {"type": "line", "name": "Tailwind cut-off", "showSymbol": False, "itemStyle": {"color": "#1f6f4a"},
             "lineStyle": {"type": "dashed", "width": 1.3},
             "data": [[r["date"], round(r["bull_cut"] * 100, 1)] for r in rows if r["bull_cut"] is not None]},
            scatter("Warning ✓ right", "bear", True, "#1f6f4a"),
            scatter("Warning ✗ wrong", "bear", False, "#A8322D"),
            scatter("Tailwind ✓ right", "bull", True, "#1f6f4a"),
            scatter("Tailwind ✗ wrong", "bull", False, "#A8322D"),
            {**scatter("Pending", "bear", None, "#9aa5a0"), "data": marks("bear", None) + marks("bull", None)},
            {"type": "line", "name": "QQQ", "xAxisIndex": 1, "yAxisIndex": 1, "showSymbol": False,
             "lineStyle": {"width": 1.2, "color": "#1C2420"}, "itemStyle": {"color": "#1C2420"},
             "data": [[r["date"], round(r["qqq"], 2)] for r in rows],
             "markArea": {"silent": True, "data": areas_b}},
        ],
    }


def _btc_calls_table(rows: list[dict], H: int) -> None:
    eps = list(reversed(btcsignal.episodes(rows)))
    if not eps:
        return
    with ui.expansion(f"Every call ({len(eps)} warnings and tailwinds), newest first", icon="list").classes(
            "w-full oe-card"):
        _grid([{"start": e["start"], "end": e["end"], "kind": "Warning ▼" if e["zone"] == "bear" else "Tailwind ▲",
                "btc": round(e["btc_ret"] * 100, 1),
                "qqq": round(e["qqq_fwd"] * 100, 1) if e["qqq_fwd"] is not None else None,
                "verdict": {True: "✓ right", False: "✗ wrong", None: "pending"}[e["correct"]]} for e in eps],
              [{"field": "start", "headerName": "Call made", "width": 120},
               {"field": "end", "headerName": "Zone ended", "width": 120},
               {"field": "kind", "headerName": "Call", "width": 120},
               {"field": "btc", "headerName": "Bitcoin change %", "width": 150},
               {"field": "qqq", "headerName": f"QQQ next {H} days %", "width": 170},
               {"field": "verdict", "headerName": "Result", "flex": 1, "minWidth": 120}],
              height=360, click_to_stock=False)

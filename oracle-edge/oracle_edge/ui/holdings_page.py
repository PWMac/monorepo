"""My holdings: the real portfolio across accounts and baskets, its performance, income, gains, and a plan for cash.

Layout (top to bottom)
- Scope bar: which accounts, then basket chips (All · your baskets · Not in a basket). Everything follows it.
- Summary tiles: value, today, gain, total return a year vs the S&P 500, income and yield, cash.
- Tabs: Overview · Holdings · Performance · Income · Gains & tax · Activity · Invest cash · Accounts & imports.
- Click a holding anywhere for its page: every purchase, dividends, baskets, add or record a sale.
"""

from __future__ import annotations

import html
import json
from datetime import date

from nicegui import app, run, ui

from .. import brokers, db, holdings, orderplan, rules
from ..holdings import PERIODS, Scope
from .growth import PALETTE
from .layout import frame
from .theme import BRAND, NEGATIVE, badge_html, money, pct

GREY = "#6b7280"
TABS = ["Overview", "Holdings", "Performance", "Income", "Gains & tax", "Activity", "Invest cash",
        "Accounts & imports"]


def _col(v) -> str:
    return GREY if v is None else (BRAND if v >= 0 else NEGATIVE)


def _sgn(v, d=0, money_=True) -> str:
    if v is None:
        return "—"
    s = money(abs(v), d) if money_ else pct(abs(v), d)
    return ("+" if v >= 0 else "−") + s


def _store() -> dict:
    s = app.storage.user.get("holdings") or {}
    return {"accounts": s.get("accounts"), "basket": s.get("basket"), "tab": s.get("tab", "Overview"),
            "period": s.get("period", "1Y"), "compare": s.get("compare") or holdings.compare_tickers()}


def _save(**kw) -> None:
    s = dict(app.storage.user.get("holdings") or {})
    s.update(kw)
    app.storage.user["holdings"] = s


def _scope(s: dict) -> Scope:
    accs = s["accounts"] or None
    b = s["basket"]
    lab = "All holdings"
    if b == 0:
        lab = "Not in a basket"
    elif b:
        lab = next((x["name"] for x in holdings.baskets() if x["id"] == b), "Basket")
    return Scope(accs, b, lab)


def _tile(value: str, label: str, sub: str = "", color: str | None = None, tip: str = ""):
    with ui.card().classes("oe-card").style("min-width:150px;flex:1") as c:
        ui.label(value).classes("oe-kpi").style(f"color:{color}" if color else "")
        ui.label(label).classes("oe-kpi-l")
        if sub:
            ui.html(f"<div class='oe-small'>{sub}</div>", sanitize=False)
    if tip:
        c.tooltip(tip)


# ===================================================================== page
@ui.page("/holdings")
def holdings_page(tab: str | None = None):
    with frame("My holdings"):
        s = _store()
        if tab in TABS:
            s["tab"] = tab
        if not holdings.accounts():
            _welcome()
            return
        _scope_bar(s)
        scope = _scope(s)
        hs, px = holdings.holdings(scope)
        sm = holdings.summary(hs, scope)
        _tiles(scope, hs, sm, s)
        for r in holdings.reconcile()[:1]:
            with ui.row().classes("items-center gap-2 w-full").style("background:#fff7e6;border-radius:8px;"
                                                                      "padding:6px 12px"):
                ui.icon("rule").style("color:#9a5b00")
                n = len(holdings.reconcile())
                ui.label(f"{n} holding{'s' if n > 1 else ''} where the broker's share count and the purchases on file "
                         "differ (for example a purchase is missing).").classes("text-sm")
                ui.button("Review", on_click=lambda: _go("Accounts & imports")).props("flat dense no-caps")
        with ui.tabs(value=s["tab"], on_change=lambda e: _save(tab=e.value)).props(
                "dense no-caps align=left active-color=primary indicator-color=primary").classes("w-full") as tabs:
            for t in TABS:
                ui.tab(t)
        with ui.tab_panels(tabs, value=s["tab"]).props("animated=false keep-alive=false").classes(
                "w-full bg-transparent"):
            with ui.tab_panel("Overview"):
                _overview(scope, hs, px, sm, s)
            with ui.tab_panel("Holdings"):
                _holdings_tab(scope, hs, sm)
            with ui.tab_panel("Performance"):
                _performance(scope, hs, s)
            with ui.tab_panel("Income"):
                _income(scope, hs, px, sm)
            with ui.tab_panel("Gains & tax"):
                _gains(scope, hs, sm)
            with ui.tab_panel("Activity"):
                _activity(scope)
            with ui.tab_panel("Invest cash"):
                _invest(scope)
            with ui.tab_panel("Accounts & imports"):
                _accounts_tab()


def _go(tab: str):
    _save(tab=tab)
    ui.navigate.reload()


def _welcome():
    ui.label("My holdings").classes("oe-h1")
    with ui.card().classes("oe-card w-full").style("max-width:900px"):
        ui.label("Bring in what you own").classes("oe-h2")
        ui.label("Three ways; you can mix them. Everything stays in this computer's database, and broker sign-ins are "
                 "kept in Windows Credential Manager.").classes("oe-muted")
        with ui.row().classes("w-full gap-4 mt-2"):
            for icon, title, text in (
                    ("link", "Connect Schwab", "Positions, cash and a year of transactions, refreshed when you sync."),
                    ("hub", "Other brokerages", "Fidelity, Vanguard, Robinhood, E*TRADE and more through SnapTrade "
                                                "(free for personal use)."),
                    ("upload_file", "Import a file", "Schwab's positions, lot-details or history export, or any "
                                                     "broker's CSV."),
                    ("edit", "Enter by hand", "Add each purchase: date, shares and price paid.")):
                with ui.card().classes("oe-card").style("flex:1;min-width:180px"):
                    ui.icon(icon).classes("text-3xl").style(f"color:{BRAND}")
                    ui.label(title).classes("font-semibold")
                    ui.label(text).classes("oe-small")
        with ui.row().classes("gap-2 mt-3"):
            ui.button("Set up accounts and imports", icon="arrow_forward",
                      on_click=lambda: (holdings.ensure_manual_account(), _go("Accounts & imports"))).props("unelevated")
            ui.button("Add a purchase by hand", icon="add",
                      on_click=lambda: (holdings.ensure_manual_account(), _lot_dialog(None, after=ui.navigate.reload))
                      ).props("outline")


def _scope_bar(s: dict):
    accs = holdings.accounts()
    bs = holdings.baskets()
    with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
        with ui.row().classes("items-center gap-3"):
            ui.label("My holdings").classes("oe-h1")
            if len(accs) > 1:
                opts = {a["id"]: a["name"] for a in accs}
                sel = ui.select(opts, multiple=True, value=[a for a in (s["accounts"] or []) if a in opts],
                                label="Accounts (none = all)").props("dense outlined use-chips clearable").classes(
                    "min-w-[220px]")
                sel.on_value_change(lambda e: (_save(accounts=e.value or None), ui.navigate.reload()))
        with ui.row().classes("items-center gap-1"):
            ui.button("Add purchase", icon="add", on_click=lambda: _lot_dialog(None, after=ui.navigate.reload)).props(
                "unelevated dense no-caps")
            ui.button("Record sale", icon="sell", on_click=lambda: _sell_dialog(None, after=ui.navigate.reload)).props(
                "outline dense no-caps")

            async def refresh():
                n = ui.notification("Updating prices…", spinner=True, timeout=None)
                try:
                    from ..pipeline import fetch_prices_for

                    tk = list(dict.fromkeys(holdings.held_tickers() + holdings.compare_tickers() + ["SPY"]))
                    r = await run.io_bound(fetch_prices_for, tk)
                    n.message = f"Prices updated ({r['rows']} rows)" + (f"; none for {', '.join(r['missing'][:5])}"
                                                                         if r["missing"] else "")
                except Exception as e:
                    n.message = f"Couldn't update prices: {e}"
                n.spinner = False
                n.timeout = 4
                ui.timer(1.5, ui.navigate.reload, once=True)

            ui.button(icon="refresh", on_click=refresh).props("flat round dense").tooltip("Update prices")
            if any(a["source"] in ("schwab", "snaptrade") for a in accs):
                ui.button("Sync", icon="sync", on_click=lambda: _sync_all()).props("flat dense no-caps").tooltip(
                    "Download positions, cash and new transactions from connected brokers")
    with ui.row().classes("items-center gap-1 w-full"):
        def chip(label, val, color=None, count=None):
            on = s["basket"] == val
            b = ui.button(label + (f" · {count}" if count is not None else ""),
                          on_click=lambda v=val: (_save(basket=v), ui.navigate.reload())).props(
                f"{'unelevated' if on else 'outline'} rounded dense no-caps size=sm")
            if color and not on:
                b.style(f"color:{color};border-color:{color}")
            return b
        chip("All", None)
        for k, b in enumerate(bs):
            chip(b["name"], b["id"], b.get("color") or PALETTE[k % len(PALETTE)], len(b["tickers"]))
        if bs:
            chip("Not in a basket", 0)
        ui.button("Basket", icon="add", on_click=lambda: _basket_dialog(None)).props("flat dense no-caps size=sm")
        if s["basket"]:
            ui.button(icon="edit", on_click=lambda: _basket_dialog(s["basket"])).props(
                "flat round dense size=sm").tooltip("Rename, choose holdings, or delete this basket")


def _sync_all():
    async def go():
        n = ui.notification("Syncing…", spinner=True, timeout=None)
        msgs = []
        for src, cls in (("schwab", brokers.Schwab), ("snaptrade", brokers.SnapTrade)):
            if not any(a["source"] == src for a in holdings.accounts()):
                continue
            try:
                r = await run.io_bound(lambda c=cls: c().sync())
                msgs += r["messages"]
            except Exception as e:
                msgs.append(f"{src}: {e}")
        try:
            from ..pipeline import fetch_prices_for

            await run.io_bound(fetch_prices_for, holdings.held_tickers())
        except Exception as e:
            msgs.append(f"prices: {e}")
        n.message = " · ".join(msgs)[:400] or "Nothing to sync"
        n.spinner = False
        n.timeout = 8
        ui.timer(2, ui.navigate.reload, once=True)
    ui.timer(0.05, go, once=True)


def _tiles(scope: Scope, hs, sm: dict, s: dict):
    hi = holdings.history(scope, "1Y", ["SPY"])
    st = holdings.stats(hi["dates"], hi["twr"], hi["bench"].get("SPY")) if hi.get("twr") else {}
    with ui.row().classes("w-full gap-3"):
        _tile(money(sm["value"], 0), "Value" if not scope.is_basket else f"{scope.label} value",
              f"{money(sm['invested'], 0)} invested" + (f" · {money(sm['cash'], 0)} cash" if not scope.is_basket
                                                         else ""))
        _tile(_sgn(sm["day"]), "Today", f"<span style='color:{_col(sm['day'])}'>{_sgn(sm['day_pct'], 2, False)}"
              "</span>", _col(sm["day"]), "Change since the previous close")
        _tile(_sgn(sm["gain"]), "Unrealized gain", f"{_sgn(sm['gain_pct'], 1, False)} on {money(sm['cost'], 0)} cost"
              + (f" · {money(sm['divs'], 0)} dividends" if sm["divs"] else ""), _col(sm["gain"]))
        if st:
            ex = st.get("excess")
            _tile(_sgn(st["total"], 1, False), "Return, last 12 months",
                  f"S&P 500 {_sgn(st.get('bench_total'), 1, False)} · "
                  f"<span style='color:{_col(ex)}'>{_sgn(ex, 1, False)} vs the index</span>" if ex is not None else "",
                  _col(st["total"]), "Time-weighted, dividends included (cash excluded), so adding money doesn't "
                                     "move it. The Performance tab has longer periods and more comparisons.")
        _tile(money(sm["income"], 0), "Dividend income a year",
              f"yield {pct(sm['yield'], 2)} · on cost {pct(sm['yoc'], 2)}", None,
              "The dividends paid over the last 12 months at today's share counts")
        if not scope.is_basket:
            _tile(money(sm["cash"], 0), "Cash available", f"{pct(sm['cash_pct'], 1)} of the account"
                  if sm["cash_pct"] is not None else "", None, "Set per account on Accounts & imports; synced "
                                                                "accounts update it")


# ===================================================================== Overview
def _overview(scope, hs, px, sm, s):
    if not hs:
        ui.label("Nothing in this view yet." + (" Add tickers to the basket with its edit button."
                                                if scope.is_basket else "")).classes("oe-muted")
        return
    with ui.row().classes("w-full gap-4 items-stretch"):
        with ui.card().classes("oe-card").style("flex:2;min-width:420px"):
            _growth_chart(scope, s, height="300px", controls=True)
        with ui.card().classes("oe-card").style("flex:1;min-width:300px"):
            ui.label("What it's made of").classes("oe-h2")
            top = [(h.ticker, h.weight or 0) for h in hs[:8]]
            rest = 1 - sum(w for _, w in top)
            data = [{"name": t, "value": round(w * 100, 2)} for t, w in top] + (
                [{"name": "Others", "value": round(rest * 100, 2)}] if rest > 0.001 else [])
            ui.echart({"tooltip": {"trigger": "item", "valueFormatter": "v => v + '%'"},
                       "color": PALETTE,
                       "series": [{"type": "pie", "radius": ["45%", "75%"], "data": data,
                                   "label": {"formatter": "{b}\n{d}%", "fontSize": 11}}]}).classes("w-full").style(
                "height:230px")
            ui.label(f"Top holding {pct(sm['top_w'], 0)} · top 5 {pct(sm['top5_w'], 0)} · acts like "
                     f"{sm['eff_n']:.1f} equal holdings" if sm["eff_n"] else "").classes("oe-small").tooltip(
                "'Acts like N equal holdings' (1 ÷ the sum of squared weights): how spread out the money really is")
    with ui.row().classes("w-full gap-4 items-stretch mt-2"):
        with ui.card().classes("oe-card").style("flex:1;min-width:320px"):
            ui.label("Today's movers").classes("oe-h2")
            mv = sorted([h for h in hs if h.day_pct is not None], key=lambda h: -abs(h.day_change or 0))[:6]
            for h in mv:
                with ui.row().classes("w-full justify-between items-center cursor-pointer").on(
                        "click", lambda h=h: _holding_dialog(h.ticker, scope)):
                    ui.label(h.ticker).classes("font-semibold")
                    ui.label(f"{_sgn(h.day_pct, 2, False)} · {_sgn(h.day_change)}").style(
                        f"color:{_col(h.day_change)}")
        with ui.card().classes("oe-card").style("flex:1;min-width:320px"):
            ui.label("Strategy check").classes("oe-h2")
            _badges_card(hs, scope)
        with ui.card().classes("oe-card").style("flex:1;min-width:320px"):
            ui.label("Sectors").classes("oe-h2")
            for k, w in holdings.sector_mix(hs)[:8]:
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label(k).classes("text-sm").style("width:45%")
                    ui.html(f"<div style='height:8px;width:{max(w * 100, 1):.0f}%;max-width:100%;background:{BRAND};"
                            "border-radius:4px'></div>", sanitize=False).style("flex:1")
                    ui.label(pct(w, 0)).classes("text-sm").style("width:40px;text-align:right")


def _evals_for(hs) -> dict:
    """The primary strategy's latest evaluation of each held company (ticker -> row)."""
    from . import data

    rid = data.default_run_id()
    d = data.latest_date(rid) if rid else None
    if not d:
        return {}
    tk = [h.ticker for h in hs]
    if not tk:
        return {}
    out = {}
    for r in db.rows(f"SELECT e.*, s.ticker FROM evaluations e JOIN securities s ON s.cik=e.cik WHERE e.run_id=? AND "
                     f"e.date=? AND s.ticker IN ({','.join('?' * len(tk))})", (rid, d, *tk)):
        x = dict(r)
        x["detail"] = json.loads(x["detail"] or "{}")
        out[x["ticker"]] = x
    return out


def _badges(hs) -> dict:
    """ticker -> (badge, reason) under the primary strategy."""
    from .pages_main import real_badge

    prof = rules.default_profile()
    evs = _evals_for(hs)
    out = {}
    for h in hs:
        if h.cik is None:
            out[h.ticker] = ("HOLD", "fund or not covered: no strategy signal")
            continue
        try:
            out[h.ticker] = real_badge(h.cik, evs.get(h.ticker), prof)
        except Exception:
            out[h.ticker] = ("HOLD", "")
    return out


def _badges_card(hs, scope):
    b = _badges(hs)
    act = [(t, x) for t, x in b.items() if x[0] in ("SELL", "TRIM", "REVIEW")]
    adds = [t for t, x in b.items() if x[0] == "ADD"]
    if not act:
        ui.label("Nothing to act on under " + rules.default_profile().name + ".").classes("text-sm")
    for t, (bd, why) in act[:6]:
        with ui.row().classes("w-full items-center no-wrap gap-2 cursor-pointer").on(
                "click", lambda t=t: _holding_dialog(t, scope)):
            ui.html(badge_html(bd, small=True), sanitize=False)
            ui.label(t).classes("font-semibold")
            ui.label(why).classes("oe-small").style("overflow:hidden;text-overflow:ellipsis;white-space:nowrap")
    if adds:
        ui.label("Still in the Buy Zone: " + ", ".join(adds[:8])).classes("oe-small mt-1")
    ui.button("Plan orders for my cash", icon="shopping_cart", on_click=lambda: _go("Invest cash")).props(
        "flat dense no-caps")


# ===================================================================== Holdings table
def _holdings_tab(scope, hs, sm):
    if not hs:
        ui.label("Nothing in this view yet.").classes("oe-muted")
        return
    bad = _badges(hs)
    evs = _evals_for(hs)
    w_now = {}
    try:
        p = rules.default_profile()
        for r in db.rows("SELECT w.cik, w.score FROM wonder w WHERE w.profile_id=? AND w.cohort='now'", (p.snap[0],)):
            w_now[r["cik"]] = r["score"]
    except Exception:
        pass
    bnames = {b["id"]: b["name"] for b in holdings.baskets()}
    rows = []
    for h in hs:
        e = evs.get(h.ticker) or {}
        iv = e.get("iv")
        rows.append({
            "ticker": h.ticker, "name": h.name or "", "badge": bad.get(h.ticker, ("", ""))[0],
            "why": bad.get(h.ticker, ("", ""))[1], "shares": h.shares, "avg": h.avg_cost, "price": h.price,
            "value": h.value, "weight": h.weight, "day": h.day_pct, "day_d": h.day_change, "gain": h.gain,
            "gain_pct": h.gain_pct, "total": h.total_ret, "irr": h.irr, "divs": h.divs, "yield": h.div_yield,
            "yoc": h.yield_on_cost, "income": h.income, "quality": e.get("quality"), "wonder": w_now.get(h.cik),
            "iv": iv, "disc": (h.price / iv - 1) if (iv and h.price) else None, "buy_below": e.get("buy_below"),
            "lots": len(h.lots), "since": h.first_date or "", "accounts": ", ".join(h.accounts),
            "baskets": ", ".join(bnames.get(b, "") for b in h.baskets), "realized": h.realized})
    m = "p => p.value == null ? '—' : '$' + p.value.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})"
    m0 = "p => p.value == null ? '—' : '$' + Math.round(p.value).toLocaleString()"
    pc = "p => p.value == null ? '—' : (p.value*100).toFixed(1) + '%'"
    sp = "p => p.value == null ? '—' : (p.value >= 0 ? '+' : '') + (p.value*100).toFixed(1) + '%'"
    sm0 = "p => p.value == null ? '—' : (p.value >= 0 ? '+$' : '−$') + Math.abs(Math.round(p.value)).toLocaleString()"
    colr = {"function": "p => ({color: p.value == null ? '#6b7280' : (p.value >= 0 ? '#1f6f4a' : '#A8322D')})"}
    num1 = "p => p.value == null ? '—' : Math.round(p.value)"
    cols = [
        {"headerName": "Ticker", "field": "ticker", "pinned": "left", "width": 92, "cellStyle": {"fontWeight": 700,
                                                                                                "cursor": "pointer"}},
        {"headerName": "Signal", "field": "badge", "width": 108, "tooltipField": "why",
         ":cellRenderer": "p => p.value ? `<span class='oe-badge-mini oe-b-${p.value}'>${p.value}</span>` : ''"},
        {"headerName": "Name", "field": "name", "width": 170, "hide": False},
        {"headerName": "Shares", "field": "shares", "width": 90, "type": "rightAligned",
         ":valueFormatter": "p => p.value == null ? '' : (+p.value.toFixed(4)).toLocaleString()"},
        {"headerName": "Avg cost", "field": "avg", "width": 96, "type": "rightAligned", ":valueFormatter": m},
        {"headerName": "Price", "field": "price", "width": 92, "type": "rightAligned", ":valueFormatter": m},
        {"headerName": "Value", "field": "value", "width": 110, "type": "rightAligned", ":valueFormatter": m0,
         "sort": "desc"},
        {"headerName": "Weight", "field": "weight", "width": 84, "type": "rightAligned", ":valueFormatter": pc},
        {"headerName": "Today", "field": "day", "width": 84, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr["function"]},
        {"headerName": "Gain $", "field": "gain", "width": 100, "type": "rightAligned", ":valueFormatter": sm0,
         ":cellStyle": colr["function"]},
        {"headerName": "Gain %", "field": "gain_pct", "width": 84, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr["function"]},
        {"headerName": "Total return", "field": "total", "width": 96, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr["function"], "headerTooltip": "Price gain plus dividends received, on what you paid"},
        {"headerName": "A year", "field": "irr", "width": 84, "type": "rightAligned", ":valueFormatter": sp,
         ":cellStyle": colr["function"], "headerTooltip": "Money-weighted yearly return of your purchases (dated "
                                                          "purchases only)"},
        {"headerName": "Div yield", "field": "yield", "width": 84, "type": "rightAligned",
         ":valueFormatter": "p => p.value ? (p.value*100).toFixed(2) + '%' : '—'"},
        {"headerName": "Yield on cost", "field": "yoc", "width": 90, "type": "rightAligned",
         ":valueFormatter": "p => p.value ? (p.value*100).toFixed(2) + '%' : '—'"},
        {"headerName": "Income/yr", "field": "income", "width": 92, "type": "rightAligned", ":valueFormatter": m0},
        {"headerName": "Quality", "field": "quality", "width": 80, "type": "rightAligned", ":valueFormatter": num1},
        {"headerName": "Wonder", "field": "wonder", "width": 80, "type": "rightAligned", ":valueFormatter": num1,
         "headerTooltip": "Wonderfulness"},
        {"headerName": "Value est.", "field": "iv", "width": 96, "type": "rightAligned", ":valueFormatter": m,
         "headerTooltip": "The primary strategy's value per share"},
        {"headerName": "vs value", "field": "disc", "width": 84, "type": "rightAligned", ":valueFormatter": sp,
         "headerTooltip": "Price above (+) or below (−) the value estimate"},
        {"headerName": "Buy below", "field": "buy_below", "width": 92, "type": "rightAligned", ":valueFormatter": m},
        {"headerName": "Purchases", "field": "lots", "width": 90, "type": "rightAligned"},
        {"headerName": "Since", "field": "since", "width": 100},
        {"headerName": "Accounts", "field": "accounts", "width": 140},
        {"headerName": "Baskets", "field": "baskets", "width": 130},
    ]
    with ui.row().classes("w-full items-center justify-between"):
        ui.label(f"{len(hs)} holdings · click a row for every purchase, dividends and baskets").classes("oe-small")
        with ui.row().classes("gap-1"):
            ui.button("Export CSV", icon="download", on_click=lambda: _export(rows)).props("flat dense no-caps")
    g = ui.aggrid({"columnDefs": cols, "rowData": rows, "rowHeight": 34,
                   "defaultColDef": {"sortable": True, "resizable": True, "filter": True, "wrapHeaderText": True,
                                     "autoHeaderHeight": True},
                   "tooltipShowDelay": 300, "suppressCellFocus": True}, theme="quartz",
                  auto_size_columns=False).classes("w-full").style(
        f"height:{min(120 + 35 * len(rows), 640)}px")
    g.on("cellClicked", lambda e: _holding_dialog(e.args["data"]["ticker"], scope))
    ui.add_css(".oe-badge-mini{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px}"
               ".oe-b-SELL{background:#a33a2b;color:#fff}.oe-b-TRIM{background:#7a4f9e;color:#fff}"
               ".oe-b-REVIEW{background:#ffe7cc;color:#a24d00}.oe-b-ADD{background:#e3f1ea;color:#1f6f4a}"
               ".oe-b-HOLD{background:#eceae3;color:#4b524f}")
    t = [h for h in hs if h.value is not None]
    ui.label(f"Totals: {money(sum(h.value for h in t), 0)} value · {_sgn(sum(h.gain or 0 for h in t))} gain · "
             f"{money(sm['income'], 0)} income a year").classes("oe-small")


def _export(rows):
    import csv
    import io

    out = io.StringIO()
    if rows:
        w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    ui.download(out.getvalue().encode(), f"holdings_{date.today().isoformat()}.csv")


# ===================================================================== Performance
def _growth_chart(scope, s, height="380px", controls=True):
    box = ui.column().classes("w-full gap-1")

    def draw():
        box.clear()
        with box:
            if controls:
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    ui.label(f"Growth of $10,000 · {scope.label}").classes("oe-h2")
                    with ui.row().classes("gap-0"):
                        for p in PERIODS:
                            ui.button(p, on_click=lambda p=p: (_save(period=p), s.update(period=p), draw())).props(
                                f"{'unelevated' if s['period'] == p else 'flat'} dense no-caps size=sm")
            hi = holdings.history(scope, s["period"], s["compare"])
            if len(hi.get("twr") or []) < 2:
                ui.label("Not enough history yet: performance starts on the first purchase date.").classes("oe-muted")
                return
            step = max(1, len(hi["dates"]) // 600)
            idx = list(range(0, len(hi["dates"]), step)) + ([len(hi["dates"]) - 1] if (len(hi["dates"]) - 1) % step
                                                             else [])
            ser = [{"type": "line", "name": scope.label, "showSymbol": False, "lineStyle": {"width": 2.6},
                    "itemStyle": {"color": PALETTE[0]}, "z": 3,
                    "data": [[hi["dates"][i], round(hi["twr"][i] * 10000, 2)] for i in idx]}]
            for k, (t, b) in enumerate(hi["bench"].items()):
                ser.append({"type": "line", "name": t, "showSymbol": False, "lineStyle": {"width": 1.6},
                            "itemStyle": {"color": PALETTE[(k + 1) % len(PALETTE)]},
                            "data": [[hi["dates"][i], round(b[i] * 10000, 2)] for i in idx]})
            ui.echart({"tooltip": {"trigger": "axis", ":valueFormatter": "v => '$' + Math.round(v).toLocaleString()"},
                       "legend": {"top": 0, "type": "scroll"}, "grid": {"left": 64, "right": 16, "top": 30,
                                                                         "bottom": 28},
                       "xAxis": {"type": "time"}, "yAxis": {"type": "value", "scale": True,
                                                            "axisLabel": {":formatter": "v => '$' + (v/1000).toFixed(1)"
                                                                                        " + 'k'"}},
                       "series": ser}).classes("w-full").style(f"height:{height}")
            if hi["missing"]:
                ui.label(f"No prices yet for {', '.join(hi['missing'])}: press Update prices (↻) at the top."
                         ).classes("oe-small")
    draw()
    return box


def _performance(scope, hs, s):
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-end gap-2"):
            cmp = ui.input_chips("Compare with (any stock or ETF tickers)", value=s["compare"]).props(
                "dense outlined").classes("min-w-[360px]")

            def set_cmp():
                v = [x.strip().upper() for x in (cmp.value or []) if x.strip()][:8]
                db.set_cfg("holdings_compare", v)
                _save(compare=v)
                new = [t for t in v if not db.scalar("SELECT 1 FROM prices WHERE ticker=? LIMIT 1", (t,))]

                async def go():
                    if new:
                        n = ui.notification(f"Downloading {', '.join(new)}…", spinner=True, timeout=None)
                        try:
                            from ..pipeline import fetch_prices_for

                            await run.io_bound(fetch_prices_for, new)
                        except Exception as e:
                            ui.notify(f"Couldn't download prices: {e}", type="warning")
                        n.dismiss()
                    ui.navigate.reload()
                ui.timer(0.05, go, once=True)
            ui.button("Apply", on_click=set_cmp).props("unelevated dense no-caps")
            ui.label("Saved for next time. The S&P 500 (SPY) is the usual yardstick.").classes("oe-small")
        _growth_chart(scope, s)
    hi = holdings.history(scope, s["period"], list(dict.fromkeys(["SPY"] + s["compare"])))
    if len(hi.get("twr") or []) < 3:
        return
    me = holdings.stats(hi["dates"], hi["twr"], hi["bench"].get("SPY"))
    cols = [(scope.label, me)] + [(t, holdings.stats(hi["dates"], b, hi["bench"].get("SPY")))
                                  for t, b in hi["bench"].items()]
    rows_def = [
        ("Total return", "total", "p1", "From the start of the period, dividends included"),
        ("Return a year", "cagr", "p1", "Compound yearly return (periods of a year or more)"),
        ("Volatility", "vol", "p1", "How much it swings: yearly standard deviation of daily returns"),
        ("Worst drop", "max_dd", "p1", "The biggest fall from a high"),
        ("Below its high now", "dd_now", "p1", "How far it is below its highest point in the period"),
        ("Sharpe ratio", "sharpe", "x2", "Return above cash per unit of swing; above 1 is good"),
        ("Sortino ratio", "sortino", "x2", "Like Sharpe but only counts the falls"),
        ("Beta vs S&P 500", "beta", "x2", "1 = moves with the market; 0.8 = 20% less"),
        ("Correlation with S&P 500", "corr", "x2", "1 = moves in step"),
        ("Alpha a year", "alpha", "p1", "Return beyond what its beta explains"),
        ("Up-market capture", "up_capture", "p0", "Share of the market's up months it captured"),
        ("Down-market capture", "down_capture", "p0", "Share of the market's down months it suffered (lower is "
                                                      "better)"),
        ("Months up", "pos_months", "p0", "Share of months with a gain"),
        ("Best month", "best_month", "m", ""),
        ("Worst month", "worst_month", "m", ""),
    ]

    def f(v, kind):
        if v is None:
            return "—"
        if kind == "m":
            return f"{v[1] * 100:+.1f}% ({v[0]})"
        if kind == "x2":
            return f"{v:.2f}"
        return pct(v, 1 if kind == "p1" else 0)
    head = "".join(f"<th>{html.escape(n)}</th>" for n, _ in cols)
    body = "".join("<tr><td style='text-align:left' title='" + html.escape(tip) + f"'>{lab}</td>" +
                   "".join(f"<td>{f(x.get(k), kind)}</td>" for _, x in cols) + "</tr>" for lab, k, kind, tip in rows_def)
    with ui.row().classes("w-full gap-4 items-start mt-2"):
        with ui.card().classes("oe-card").style("flex:2;min-width:480px"):
            ui.label(f"Statistics · {s['period']} · from {hi['start']}").classes("oe-h2")
            ui.html("<table class='oe-st'><thead><tr><th></th>" + head + "</tr></thead><tbody>" + body +
                    "</tbody></table><style>.oe-st{width:100%;border-collapse:collapse;font-size:13px}"
                    ".oe-st td,.oe-st th{padding:5px 8px;text-align:right;border-bottom:1px solid #eef0ee}"
                    ".oe-st th{color:#6b7280;font-weight:600}</style>", sanitize=False).classes("w-full")
            ui.label(f"Risk-free rate {pct(me.get('rf'), 1)} (3-month Treasury bill, or 4% if not loaded). Returns are "
                     "time-weighted and exclude cash.").classes("oe-small")
        with ui.card().classes("oe-card").style("flex:1;min-width:300px"):
            ui.label("Your money vs the same purchases elsewhere").classes("oe-h2")
            ui.label("Every purchase's cost put into the comparison on the same day (and taken out when you sold).")\
                .classes("oe-small")
            for t in dict.fromkeys(["SPY"] + s["compare"]):
                sm = holdings.same_money(scope, t)
                if not sm:
                    continue
                d = sm["mine"] - sm["theirs"]
                with ui.row().classes("w-full justify-between items-center"):
                    ui.label(f"vs {t}").classes("font-semibold")
                    ui.label(f"{money(sm['mine'], 0)} vs {money(sm['theirs'], 0)}").classes("text-sm")
                ui.html(f"<div class='oe-small'>Put in {money(sm['put_in'], 0)} · you are <b style='color:{_col(d)}'>"
                        f"{_sgn(d)}</b> ahead · a year: you {pct(sm['irr_mine'], 1)}, {t} {pct(sm['irr_theirs'], 1)}"
                        "</div>", sanitize=False)
            _contrib(scope, hs, hi)


def _contrib(scope, hs, hi):
    """Each holding's share of the period's dollar gain."""
    if not hi.get("dates"):
        return
    start = hi["dates"][0]
    px = holdings.Px({h.ticker for h in hs})
    out = []
    for h in hs:
        g = 0.0
        for m in h.lots:
            p0 = m["cost_ps"] if m["open_date"] >= start else (px.close(h.ticker, start) or m["cost_ps"])
            a = m["open_date"] if m["open_date"] >= start else start
            g += m["shares"] * ((h.price or p0) - p0 + px.divs_between(h.ticker, a))
        out.append((h.ticker, g))
    out.sort(key=lambda x: -x[1])
    ui.label("Biggest contributors in the period").classes("oe-h2 mt-3")
    for t, g in out[:5] + ([("…", None)] if len(out) > 8 else []) + out[-3:] if len(out) > 8 else out:
        if g is None:
            ui.label("…").classes("oe-small")
            continue
        with ui.row().classes("w-full justify-between"):
            ui.label(t).classes("text-sm font-semibold")
            ui.label(_sgn(g)).classes("text-sm").style(f"color:{_col(g)}")


# ===================================================================== Income
def _income(scope, hs, px, sm):
    with ui.row().classes("w-full gap-3"):
        _tile(money(sm["income"], 0), "Dividends a year (last 12 months, today's shares)")
        _tile(money(sm["income"] / 12, 0), "A month, on average")
        _tile(pct(sm["yield"], 2), "Yield", "on today's value")
        _tile(pct(sm["yoc"], 2), "Yield on cost", "on what you paid")
        _tile(money(sm["divs"], 0), "Received since purchase", "on the purchases you hold now")
    mo = holdings.income_by_month(scope, px, 24)
    with ui.card().classes("oe-card w-full mt-2"):
        ui.label("Dividends by month (ex-date), last 2 years").classes("oe-h2")
        ui.echart({"tooltip": {"trigger": "axis", ":valueFormatter": "v => '$' + v.toFixed(2)"},
                   "grid": {"left": 60, "right": 16, "top": 16, "bottom": 30},
                   "xAxis": {"type": "category", "data": [k for k, _ in mo]},
                   "yAxis": {"type": "value"}, "series": [{"type": "bar", "data": [round(v, 2) for _, v in mo],
                                                           "itemStyle": {"color": BRAND}}]}).classes("w-full").style(
            "height:240px")
        ui.label("Worked out from each stock's price history (the ex-date drop), at the shares you held then. Your "
                 "broker's own dividend records are on the Activity tab when imported.").classes("oe-small")
    up = holdings.upcoming_income(hs, px)
    with ui.row().classes("w-full gap-4 items-start mt-2"):
        with ui.card().classes("oe-card").style("flex:1;min-width:320px"):
            ui.label("Expected over the next 12 months").classes("oe-h2")
            ui.label("Last year's payouts repeated a year later (an estimate; dividends change).").classes("oe-small")
            ui.table(columns=[{"name": "date", "label": "Around", "field": "date", "align": "left"},
                              {"name": "ticker", "label": "Ticker", "field": "ticker", "align": "left"},
                              {"name": "ps", "label": "Per share", "field": "ps"},
                              {"name": "amt", "label": "Amount", "field": "amt"}],
                     rows=[{"date": x["date"], "ticker": x["ticker"], "ps": f"${x['per_share']:.4f}",
                            "amt": money(x["amount"], 2)} for x in up if x["date"] >= date.today().isoformat()],
                     pagination=12).props("dense flat").classes("w-full")
        with ui.card().classes("oe-card").style("flex:1;min-width:320px"):
            ui.label("By holding").classes("oe-h2")
            rows = sorted([h for h in hs if h.income], key=lambda h: -h.income)
            ui.table(columns=[{"name": "t", "label": "Ticker", "field": "t", "align": "left"},
                              {"name": "inc", "label": "A year", "field": "inc"},
                              {"name": "sh", "label": "Share of income", "field": "sh"},
                              {"name": "y", "label": "Yield", "field": "y"},
                              {"name": "yoc", "label": "On cost", "field": "yoc"}],
                     rows=[{"t": h.ticker, "inc": money(h.income, 0), "sh": pct(h.income / sm["income"], 0)
                            if sm["income"] else "—", "y": pct(h.div_yield, 2), "yoc": pct(h.yield_on_cost, 2)}
                           for h in rows], pagination=12).props("dense flat").classes("w-full")


# ===================================================================== Gains & tax
def _gains(scope, hs, sm):
    with ui.row().classes("w-full gap-3"):
        _tile(_sgn(sm["unreal_long"]), "Unrealized, long-term", "purchases held more than a year",
              _col(sm["unreal_long"]))
        _tile(_sgn(sm["unreal_short"]), "Unrealized, short-term", "held a year or less", _col(sm["unreal_short"]))
        _tile(_sgn(sm["realized_ytd"]), f"Realized in {date.today().year}",
              f"short-term {_sgn(sm['realized_ytd_short'])}", _col(sm["realized_ytd"]))
        _tile(_sgn(sm["harvest"]), "Purchases showing a loss", f"{sm['harvest_n']} purchases: candidates to sell "
              "for a tax loss (mind the 30-day wash-sale rule)", _col(sm["harvest"]))
    soon = sorted([m for h in hs for m in h.lots if m["days_to_long"] and m["days_to_long"] <= 60 and
                   (m["gain"] or 0) > 0], key=lambda m: m["days_to_long"])
    if soon:
        with ui.card().classes("oe-card w-full mt-2").style("border-left:4px solid #C9A227"):
            ui.label("Turning long-term soon").classes("oe-h2")
            ui.label("Waiting these few days before selling moves the gain to the lower long-term rate.").classes(
                "oe-small")
            for m in soon[:10]:
                ui.label(f"{m['ticker']}: {m['shares']:g} shares bought {m['open_date']} · long-term in "
                         f"{m['days_to_long']} days · gain {money(m['gain'], 0)}").classes("text-sm")
    lots_rows = [{"ticker": m["ticker"], "account": m.get("account", ""), "date": m["open_date"] +
                  ("" if m.get("date_known", 1) else " (date unknown)"), "shares": round(m["shares"], 4),
                  "cost_ps": money(m["cost_ps"], 2), "cost": money(m["cost"], 0), "value": money(m["value"], 0),
                  "gain": m["gain"], "gain_s": _sgn(m["gain"]), "gp": pct(m["gain_pct"], 1),
                  "term": "Long" if m["long_term"] else f"Short ({m['days_to_long']} d to long)",
                  "ann": pct(m["ann"], 1)} for h in hs for m in h.lots]
    with ui.card().classes("oe-card w-full mt-2"):
        ui.label("Every open purchase").classes("oe-h2")
        ui.table(columns=[{"name": k, "label": lab, "field": k, "sortable": True, "align": "left" if k in
                           ("ticker", "account", "date", "term") else "right"} for k, lab in
                          (("ticker", "Ticker"), ("account", "Account"), ("date", "Bought"), ("shares", "Shares"),
                           ("cost_ps", "Price paid"), ("cost", "Cost"), ("value", "Value"), ("gain_s", "Gain"),
                           ("gp", "Gain %"), ("ann", "A year"), ("term", "Term"))],
                 rows=lots_rows, pagination=25).props("dense flat").classes("w-full")
    closed = [x for x in holdings.lots(scope.account_ids, closed=True)
              if x["ticker"] in scope.tickers({x["ticker"] for x in holdings.lots(scope.account_ids, closed=True)})]
    if closed:
        with ui.card().classes("oe-card w-full mt-2"):
            ui.label("Sold purchases (realized)").classes("oe-h2")
            ui.table(columns=[{"name": k, "label": lab, "field": k, "sortable": True, "align": "left" if k in
                               ("ticker", "open", "close", "term") else "right"} for k, lab in
                              (("ticker", "Ticker"), ("open", "Bought"), ("close", "Sold"), ("shares", "Shares"),
                               ("cost", "Cost"), ("proceeds", "Proceeds"), ("gain", "Gain"), ("term", "Term"))],
                     rows=[{"ticker": x["ticker"], "open": x["open_date"], "close": x["close_date"],
                            "shares": round(x["shares"], 4), "cost": money(x["shares"] * x["cost_ps"], 0),
                            "proceeds": money(x["shares"] * x["price"], 0),
                            "gain": _sgn(x["shares"] * (x["price"] - x["cost_ps"])),
                            "term": "Long" if (date.fromisoformat(x["close_date"]) - date.fromisoformat(x["open_date"]))
                            .days >= holdings.LONG_TERM_DAYS else "Short"}
                           for x in sorted(closed, key=lambda x: x["close_date"], reverse=True)],
                     pagination=15).props("dense flat").classes("w-full")
    ui.label("Not tax advice: your broker's 1099-B is the official record (wash sales and adjustments).").classes(
        "oe-small")


# ===================================================================== Activity
def _activity(scope):
    ts = holdings.txns(scope.account_ids)
    if scope.is_basket:
        want = scope.tickers({t["ticker"] for t in ts if t["ticker"]})
        ts = [t for t in ts if t["ticker"] in want]
    accn = {a["id"]: a["name"] for a in holdings.accounts()}
    if not ts:
        ui.label("No transactions yet. Import your broker's history file, or sync a connected account.").classes(
            "oe-muted")
        return
    kinds = sorted({t["kind"] for t in ts})
    sel = ui.select({k: holdings.TXN_KINDS.get(k, k) for k in kinds}, multiple=True, label="Show",
                    value=[]).props("dense outlined use-chips clearable").classes("min-w-[260px]")
    tbl = ui.table(columns=[{"name": k, "label": lab, "field": k, "sortable": True,
                             "align": "right" if k in ("shares", "price", "amount", "fees") else "left"}
                            for k, lab in (("date", "Date"), ("kind", "Type"), ("ticker", "Ticker"),
                                           ("shares", "Shares"), ("price", "Price"), ("amount", "Amount"),
                                           ("fees", "Fees"), ("account", "Account"), ("desc", "Description"))],
                   rows=[], pagination=30).props("dense flat").classes("w-full")

    def fill():
        want = set(sel.value or [])
        tbl.rows = [{"date": t["date"], "kind": holdings.TXN_KINDS.get(t["kind"], t["kind"]), "ticker": t["ticker"]
                     or "", "shares": "" if t["shares"] is None else round(t["shares"], 4),
                     "price": money(t["price"], 2) if t["price"] else "", "amount": money(t["amount"], 2)
                     if t["amount"] is not None else "", "fees": money(t["fees"], 2) if t["fees"] else "",
                     "account": accn.get(t["account_id"], ""), "desc": (t["description"] or "")[:60]}
                    for t in ts if not want or t["kind"] in want]
    sel.on_value_change(lambda e: fill())
    fill()
    yr = date.today().year
    dv = sum(t["amount"] or 0 for t in ts if t["kind"] == "div" and t["date"][:4] == str(yr))
    dep = sum(t["amount"] or 0 for t in ts if t["kind"] in ("deposit", "withdraw") and t["date"][:4] == str(yr))
    ui.label(f"{yr}: dividends received {money(dv, 2)} · net deposits {money(dep, 2)}").classes("oe-small")


# ===================================================================== Invest cash
def _invest(scope):
    accs = holdings.accounts()
    if scope.is_basket:
        ui.label("Plans are made per account: choose All in the basket bar first.").classes("oe-muted")
        return
    ids = scope.account_ids or [a["id"] for a in accs]
    acc_opts = {a["id"]: f"{a['name']} · {money(a['cash'] or 0, 0)} cash" for a in accs if a["id"] in ids}
    best = max(ids, key=lambda i: next((a["cash"] or 0 for a in accs if a["id"] == i), 0)) if ids else None
    profs = {p.id: p.name for p in rules.list_profiles(include_hidden=False)}
    st = {"plan": None}
    with ui.card().classes("oe-card w-full"):
        ui.label("Invest my cash").classes("oe-h2")
        ui.label("A suggested set of limit orders for one account: how much to keep in reserve given the market, "
                 "what the strategy would sell or trim, and which Buy Zone stocks to buy at what price. Nothing is "
                 "sent to your broker: copy or download the list and place the orders yourself.").classes("oe-small")
        with ui.row().classes("items-end gap-3 w-full"):
            acc = ui.select(acc_opts, value=best, label="Account").props("dense outlined").classes("min-w-[260px]")
            prof = ui.select(profs, value=rules.default_profile().id, label="Strategy").props(
                "dense outlined").classes("min-w-[260px]")
            names = ui.number("Spread across up to", value=None, min=1, max=20, format="%d",
                              suffix="stocks").props("dense outlined").classes("w-48")
            res = ui.number("Cash to keep, % of account", value=None, min=0, max=100, suffix="%",
                            placeholder="suggested").props("dense outlined").classes("w-52").tooltip(
                "Blank = the suggestion from today's market. The rest of the cash is invested.")
            sales = ui.switch("Use money from sells and trims", value=True)
            go = ui.button("Make the plan", icon="auto_awesome").props("unelevated no-caps")
    mkt = ui.column().classes("w-full")
    out = ui.column().classes("w-full")

    async def make():
        out.clear()
        with out:
            sp = ui.row().classes("items-center gap-2")
            with sp:
                ui.spinner(size="sm")
                ui.label("Evaluating every company under the strategy at today's prices (up to a minute)…").classes(
                    "oe-small")
        try:
            plan = await run.io_bound(orderplan.build, acc.value, prof.value,
                                      (res.value / 100) if res.value is not None else None,
                                      int(names.value) if names.value else None, sales.value)
        except Exception as e:
            out.clear()
            with out:
                ui.label(f"Couldn't make the plan: {e}").style(f"color:{NEGATIVE}")
            return
        st["plan"] = plan
        _render_plan(plan, mkt, out, res)
    go.on_click(make)


def _render_plan(plan, mkt, out, res_input):
    mv = plan["market"]
    mkt.clear()
    with mkt:
        with ui.card().classes("oe-card w-full").style("border-left:4px solid " + {"normal": BRAND, "caution":
                                                                                  "#C9A227", "stress": NEGATIVE}
                                                       .get(mv["state"], BRAND)):
            with ui.row().classes("w-full gap-6 items-start"):
                with ui.column().classes("gap-0"):
                    ui.label("Market today").classes("oe-kpi-l")
                    ui.label(mv["state"].title()).classes("oe-kpi")
                vix = f" · VIX {mv['vix']:.1f}" if mv.get("vix") is not None else ""
                ui.html(f"<div class='text-sm'>S&P 500 {_sgn(mv.get('spy_vs_ma'), 1, False)} vs its 200-day "
                        f"average{vix}</div>", sanitize=False)
                ui.html(f"<div class='text-sm'>Buffett Indicator {pct(mv.get('buffett'), 0)} · "
                        f"{html.escape(mv.get('zone') or '')}</div>", sanitize=False)
                with ui.column().classes("gap-0"):
                    ui.label(f"Suggested reserve {pct(mv['reserve'], 0)}").classes("font-semibold")
                    for w in mv["why"]:
                        ui.label("· " + w).classes("oe-small")
    out.clear()
    rows = plan["rows"]
    with out:
        with ui.row().classes("w-full gap-3"):
            _tile(money(plan["cash"], 0), "Cash now")
            _tile(money(plan["reserve"], 0), "Cash to keep", f"{pct(plan['reserve_pct'], 0)} of the account's "
                  f"{money(plan['total'], 0)}")
            _tile(money(plan["proceeds"], 0), "From sells and trims")
            _tile(money(plan["buys"], 0), "Buys")
            _tile(money(plan["cash_after"], 0), "Cash after the orders")
        if not rows:
            ui.label("No orders: nothing to sell, and " + ("no Buy Zone stocks today under this strategy." if
                                                           not plan["candidates"] else "the cash is already at or "
                                                           "below the amount to keep. Lower it above to invest some."
                                                           )).classes("oe-muted")
        lab_tot = ui.label("").classes("font-semibold")

        def totals():
            b = sum(r["shares"] * r["limit"] for r in rows if r["include"] and r["action"] == "BUY")
            s = sum(r["shares"] * r["limit"] for r in rows if r["include"] and r["action"] != "BUY")
            lab_tot.text = f"Selected: buys {money(b, 0)} · sells {money(s, 0)} · cash after " \
                           f"{money(plan['cash'] + s - b, 0)} (reserve {money(plan['reserve'], 0)})"
        with ui.column().classes("w-full gap-1"):
            for r in rows:
                with ui.row().classes("w-full items-center no-wrap gap-2 oe-action"):
                    ui.checkbox(value=r["include"], on_change=lambda e, r=r: (r.update(include=e.value), totals()))
                    ui.html(badge_html("BUY" if r["action"] == "BUY" else r["action"], small=True), sanitize=False)
                    ui.link(r["ticker"], f"/stock/{r['ticker']}", new_tab=True).classes("font-bold w-16")
                    ui.number(value=r["shares"], min=0, format="%g", on_change=lambda e, r=r: (
                        r.update(shares=float(e.value or 0)), totals())).props("dense outlined suffix=sh").classes(
                        "w-28")
                    ui.number(value=r["limit"], min=0, step=0.01, format="%.2f", on_change=lambda e, r=r: (
                        r.update(limit=float(e.value or 0)), totals())).props("dense outlined prefix=$").classes(
                        "w-32").tooltip("Limit price")
                    ui.label(r["reason"]).classes("oe-small").style("flex:1")
        totals()

        def sync_amounts():
            for r in rows:
                r["amount"] = r["shares"] * r["limit"]
        with ui.row().classes("gap-2 mt-2"):
            ui.button("Copy as text", icon="content_copy", on_click=lambda: (
                sync_amounts(), ui.clipboard.write(orderplan.to_text(plan)), ui.notify("Copied"))).props(
                "outline dense no-caps")
            ui.button("Download CSV", icon="download", on_click=lambda: (
                sync_amounts(), ui.download(orderplan.to_csv(plan).encode(), f"orders_{date.today()}.csv"))).props(
                "outline dense no-caps")
            ui.button("Save plan", icon="bookmark", on_click=lambda: (
                sync_amounts(), orderplan.save(plan), ui.notify("Saved"))).props("flat dense no-caps")
        ui.label("Limit prices are today's close, or the strategy's buy price when that is lower: a buy may not fill "
                 "until the price comes down to it. Schwab trades whole shares in limit orders.").classes("oe-small")
        hv = [h for h in plan["holdings"] if h["verdict"] not in ("SELL", "TRIM")]
        if hv:
            with ui.expansion(f"The rest of the account ({len(hv)} holdings)").classes("w-full"):
                for h in hv:
                    with ui.row().classes("items-center gap-2"):
                        ui.html(badge_html(h["verdict"], small=True), sanitize=False)
                        ui.label(h["ticker"]).classes("font-semibold")
                        ui.label(h["why"]).classes("oe-small")
    saved = orderplan.saved(5)
    if saved:
        with out:
            with ui.expansion("Saved plans").classes("w-full"):
                for sv in saved:
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"{sv['created_at'][:16]} · {sv['name']}").classes("text-sm")
                        ui.button(icon="download", on_click=lambda sv=sv: ui.download(
                            orderplan.to_csv(sv["plan"]).encode(), f"orders_{sv['created_at'][:10]}.csv")).props(
                            "flat round dense size=sm")


# ===================================================================== Accounts & imports
def _accounts_tab():
    accs = holdings.accounts()
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Accounts").classes("oe-h2")
            ui.button("Add account", icon="add", on_click=lambda: _account_dialog(None)).props("flat dense no-caps")
        for a in accs:
            n = len(holdings.lots([a["id"]]))
            with ui.row().classes("w-full items-center gap-3 oe-action no-wrap"):
                ui.icon({"schwab": "link", "snaptrade": "hub", "file": "upload_file"}.get(a["source"], "edit"))
                with ui.column().classes("gap-0").style("flex:1"):
                    ui.label(a["name"]).classes("font-semibold")
                    ui.label(f"{a['broker'] or 'Manual'} · {n} purchases · cash {money(a['cash'] or 0, 2)}"
                             + (f" · synced {a['synced_at'][:16]}" if a.get("synced_at") else "")
                             + (f" · purchases from {a['lot_source']}" if a.get("lot_source") else "")).classes(
                        "oe-small")
                ui.button("Edit", on_click=lambda a=a: _account_dialog(a["id"])).props("flat dense no-caps")
    rec = holdings.reconcile()
    if rec:
        accn = {a["id"]: a["name"] for a in accs}
        with ui.card().classes("oe-card w-full").style("border-left:4px solid #C9A227"):
            ui.label("Share counts to check").classes("oe-h2")
            ui.label("The broker reports a different number of shares than the purchases on file. Import the lots file "
                     "(exact purchase dates and prices), add the missing purchases, or match the broker's count.")\
                .classes("oe-small")
            for r in rec:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{r['ticker']} in {accn.get(r['account_id'], '')}: broker {r['broker']:g} shares, "
                             f"purchases {r['lots']:g}").classes("text-sm")
                    ui.button("Match the broker", on_click=lambda r=r: (holdings.fix_mismatch(r["account_id"],
                                                                                              r["ticker"]),
                                                                        ui.navigate.reload())).props(
                        "flat dense no-caps size=sm")
    with ui.row().classes("w-full gap-4 items-start"):
        with ui.card().classes("oe-card").style("flex:1;min-width:380px"):
            _import_card(accs)
        with ui.card().classes("oe-card").style("flex:1;min-width:380px"):
            _schwab_card()
            ui.separator().classes("my-3")
            _snaptrade_card()


def _import_card(accs):
    ui.label("Import a file").classes("oe-h2")
    ui.html("<div class='oe-small'>Works with: <b>Schwab</b> Positions export (Accounts › Positions › Export), "
            "lot details (Positions › a holding's cost basis › Export, or StreetSmart's <i>Export Positions with "
            "Lots</i>), and History (Accounts › History › Export, CSV or JSON); <b>Fidelity</b> and most other "
            "brokers' positions CSV; or the templates below. The file type is detected.</div>", sanitize=False)
    opts = {0: "Match or create accounts from the file"} | {a["id"]: a["name"] for a in accs}
    tgt = ui.select(opts, value=0, label="Into").props("dense outlined").classes("w-full")
    kind = ui.select({"": "Detect", "positions": "Positions (shares, average cost, cash)",
                      "lots": "Purchases with dates (replaces the account's purchases)",
                      "transactions": "Transactions"}, value="", label="File type").props("dense outlined").classes(
        "w-full")
    rebuild = ui.checkbox("Rebuild the account's purchases from its transactions (when the history goes back to "
                          "the first purchase)", value=False)
    res = ui.column().classes("w-full")

    async def got(e):
        text = await e.file.text()
        try:
            r = brokers.import_file(text, e.file.name, tgt.value or None, "", kind.value or None, rebuild.value)
        except Exception as ex:
            r = {"messages": [f"Couldn't read {e.file.name}: {ex}"], "rows": 0}
        with res:
            for m in r["messages"]:
                ui.label(f"{e.file.name}: {m}").classes("text-sm")
            if r.get("rows"):
                ui.button("Show my holdings", on_click=lambda: _go("Holdings")).props("flat dense no-caps")
    ui.upload(on_upload=got, auto_upload=True, multiple=True, label="Drop files here or click").props(
        "accept=.csv,.json,.txt flat bordered").classes("w-full")
    with ui.row().classes("gap-1"):
        ui.button("Purchases template", icon="download", on_click=lambda: ui.download(
            brokers.TEMPLATE_LOTS.encode(), "purchases_template.csv")).props("flat dense no-caps size=sm")
        ui.button("Transactions template", icon="download", on_click=lambda: ui.download(
            brokers.TEMPLATE_TXNS.encode(), "transactions_template.csv")).props("flat dense no-caps size=sm")


def _schwab_card():
    stt = brokers.Schwab.status()
    with ui.row().classes("w-full items-center justify-between"):
        ui.label("Schwab").classes("oe-h2")
        ui.label(stt["text"]).classes("text-sm").style(f"color:{BRAND if stt['state'] == 'ok' else '#9a5b00'}")
    with ui.expansion("Set up (once)", value=stt["state"] == "no_app").classes("w-full"):
        ui.html("<ol class='oe-small' style='padding-left:18px;list-style:decimal'>"
                "<li>Sign in at <a href='https://developer.schwab.com' target='_blank'>developer.schwab.com</a> with "
                "your Schwab login and create an app: API product <b>Accounts and Trading Production</b> (Trader API – "
                "Individual), callback URL <code>https://127.0.0.1:8182</code>.</li>"
                "<li>Wait until its status says <b>Ready For Use</b> (Schwab approves it, usually in a few days).</li>"
                "<li>Copy its App Key and Secret here. They are saved in Windows Credential Manager.</li></ol>",
                sanitize=False)
        k = ui.input("App key", value=brokers.Schwab.app_key() or "").props("dense outlined").classes("w-full")
        sc = ui.input("Secret", password=True, password_toggle_button=True,
                      placeholder="saved" if brokers.secrets.get("schwab_app_secret") else "").props(
            "dense outlined").classes("w-full")
        cb = ui.input("Callback URL", value=brokers.Schwab.callback()).props("dense outlined").classes("w-full")
        ui.button("Save", on_click=lambda: (brokers.Schwab.save_app(k.value, sc.value, cb.value),
                                            ui.notify("Saved"), ui.navigate.reload())).props("unelevated dense")
    if stt["state"] != "no_app":
        with ui.column().classes("w-full gap-1"):
            ui.label("Sign in (every 7 days)").classes("font-semibold text-sm")
            ui.html("<div class='oe-small'>1. Open Schwab's sign-in and approve the accounts. 2. Schwab sends the "
                    "browser to 127.0.0.1, which shows an error page: that's expected. 3. Copy the whole address from "
                    "the address bar and paste it below.</div>", sanitize=False)
            ui.button("Open Schwab sign-in", icon="open_in_new", on_click=lambda: ui.navigate.to(
                brokers.Schwab().authorize_url(), new_tab=True)).props("outline dense no-caps")
            back = ui.input("Address you landed on (https://127.0.0.1…?code=…)").props("dense outlined").classes(
                "w-full")

            async def finish():
                try:
                    await run.io_bound(brokers.Schwab().finish_login, back.value)
                    ui.notify("Connected to Schwab", type="positive")
                    await _sync(brokers.Schwab, days.value)
                except Exception as e:
                    ui.notify(str(e), type="negative", multi_line=True)
            ui.button("Connect", on_click=finish).props("unelevated dense no-caps")
            with ui.row().classes("items-center gap-2"):
                days = ui.select({30: "last month", 365: "last year", 365 * 2: "2 years", 365 * 5: "5 years"},
                                 value=365, label="Transactions to download").props("dense outlined").classes("w-48")
                if stt["state"] == "ok":
                    ui.button("Sync now", icon="sync", on_click=lambda: _sync(brokers.Schwab, days.value)).props(
                        "outline dense no-caps")
                    ui.button("Disconnect", on_click=lambda: (brokers.Schwab.disconnect(), ui.navigate.reload())
                              ).props("flat dense no-caps")
            ui.label("The API gives each holding's average cost, not its purchase dates: import the lots file once "
                     "for exact purchases (later syncs keep them and flag share counts that change).").classes(
                "oe-small")


async def _sync(cls, days=None):
    n = ui.notification("Syncing…", spinner=True, timeout=None)
    try:
        r = await run.io_bound(lambda: cls().sync(days) if days else cls().sync())
        from ..pipeline import fetch_prices_for

        await run.io_bound(fetch_prices_for, holdings.held_tickers())
        n.message = " · ".join(r["messages"])[:500] or "Done"
    except Exception as e:
        n.message = f"Sync failed: {e}"
        n.type = "negative"
    n.spinner = False
    n.timeout = 10
    ui.timer(2, ui.navigate.reload, once=True)


def _snaptrade_card():
    ok = brokers.SnapTrade.configured()
    with ui.row().classes("w-full items-center justify-between"):
        ui.label("Other brokerages (SnapTrade)").classes("oe-h2")
        ui.label("Set up" if ok else "Not set up").classes("text-sm").style(f"color:{BRAND if ok else '#9a5b00'}")
    with ui.expansion("Set up", value=False).classes("w-full"):
        ui.html("<ol class='oe-small' style='padding-left:18px;list-style:decimal'>"
                "<li>Create a free account at <a href='https://snaptrade.com/personal' target='_blank'>"
                "snaptrade.com/personal</a> and connect your brokerages there (up to 20; Fidelity, Vanguard, "
                "Robinhood, E*TRADE, Schwab and more). Read-only is enough.</li>"
                "<li>In its dashboard create a Personal API key and copy the client ID and consumer key here (saved in "
                "Windows Credential Manager).</li></ol>", sanitize=False)
        cid = ui.input("Client ID", value=brokers.secrets.get("snaptrade_client_id") or "").props(
            "dense outlined").classes("w-full")
        ck = ui.input("Consumer key", password=True, password_toggle_button=True,
                      placeholder="saved" if ok else "").props("dense outlined").classes("w-full")
        ui.button("Save", on_click=lambda: (brokers.SnapTrade.save(cid.value, ck.value), ui.notify("Saved"),
                                            ui.navigate.reload())).props("unelevated dense")
    if ok:
        ui.button("Sync now", icon="sync", on_click=lambda: _sync(brokers.SnapTrade)).props("outline dense no-caps")
        ui.label("Brings in positions with purchase lots where the broker shares them, cash, and 5 years of "
                 "activity.").classes("oe-small")


# ===================================================================== dialogs
def _account_dialog(aid):
    a = holdings.account(aid) if aid else None
    with ui.dialog() as d, ui.card().style("min-width:420px"):
        ui.label("Edit account" if a else "Add account").classes("oe-h2")
        nm = ui.input("Name", value=(a or {}).get("name", "")).props("dense outlined").classes("w-full")
        br = ui.input("Broker", value=(a or {}).get("broker", "")).props("dense outlined").classes("w-full")
        cash = ui.number("Cash available", value=(a or {}).get("cash", 0), format="%.2f").props(
            "dense outlined prefix=$").classes("w-full")
        if a and a["source"] in ("schwab", "snaptrade"):
            ui.label("Cash is replaced at each sync.").classes("oe-small")

        def save():
            if a:
                holdings.update_account(aid, name=nm.value, broker=br.value, cash=cash.value or 0)
            else:
                holdings.add_account(nm.value, br.value, cash=cash.value or 0)
            d.close()
            ui.navigate.reload()

        def rebuild():
            r = holdings.rebuild_lots_from_txns(aid)
            ui.notify(f"{r['open']} open purchases, {r['closed']} sold rebuilt from the history")
            d.close()
            ui.navigate.reload()

        def delete():
            holdings.delete_account(aid)
            d.close()
            ui.navigate.reload()
        with ui.row().classes("w-full justify-between mt-2"):
            if a:
                with ui.row().classes("gap-1"):
                    ui.button("Delete", on_click=lambda: _confirm(f"Delete {a['name']} with its purchases and "
                                                                  "transactions?", delete)).props(
                        "flat dense no-caps color=negative")
                    if holdings.txns([aid], ["buy", "sell", "reinvest"]):
                        ui.button("Rebuild purchases from history", on_click=lambda: _confirm(
                            "Replace this account's purchases with ones rebuilt from its buy and sell history "
                            "(first in, first out)?", rebuild)).props("flat dense no-caps")
            else:
                ui.label("")
            ui.button("Save", on_click=save).props("unelevated")
    d.open()


def _confirm(msg, fn):
    with ui.dialog() as d, ui.card():
        ui.label(msg)
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=d.close).props("flat")
            ui.button("Yes", on_click=lambda: (d.close(), fn())).props("unelevated color=negative")
    d.open()


def _acct_select(value=None):
    accs = holdings.accounts()
    if not accs:
        holdings.ensure_manual_account()
        accs = holdings.accounts()
    return ui.select({a["id"]: a["name"] for a in accs}, value=value or accs[0]["id"], label="Account").props(
        "dense outlined").classes("w-full")


def _lot_dialog(lot: dict | None, ticker: str = "", after=None):
    with ui.dialog() as d, ui.card().style("min-width:440px"):
        ui.label("Edit purchase" if lot else "Add a purchase").classes("oe-h2")
        acc = _acct_select((lot or {}).get("account_id"))
        tk = ui.input("Ticker", value=(lot or {}).get("ticker", ticker)).props("dense outlined").classes("w-full")
        known = (lot or {}).get("date_known", 1)
        dt = ui.input("Date bought", value=(lot or {}).get("open_date", date.today().isoformat()) if known else "",
                      placeholder="YYYY-MM-DD (blank if unknown)").props("dense outlined").classes("w-full")
        with dt.add_slot("append"):
            ic = ui.icon("event").classes("cursor-pointer")
            with ui.menu() as m:
                ui.date(on_change=lambda e: (setattr(dt, "value", e.value), m.close())).props("mask=YYYY-MM-DD")
            ic.on("click", m.open)
        with ui.row().classes("w-full no-wrap gap-2"):
            sh = ui.number("Shares", value=(lot or {}).get("shares"), format="%g").props("dense outlined").classes(
                "flex-1")
            cps = ui.number("Price per share", value=(lot or {}).get("cost_ps"), format="%.4f").props(
                "dense outlined prefix=$").classes("flex-1")
        tot = ui.label("").classes("oe-small")
        for w in (sh, cps):
            w.on_value_change(lambda e: setattr(tot, "text", f"Cost {money((sh.value or 0) * (cps.value or 0), 2)}"))
        note = ui.input("Note", value=(lot or {}).get("note", "")).props("dense outlined").classes("w-full")
        ui.label("Include commissions in the price per share if you want them in the cost.").classes("oe-small")

        def save():
            t = (tk.value or "").strip().upper()
            if not t or not sh.value:
                ui.notify("Enter a ticker and the shares", type="warning")
                return
            if lot:
                holdings.update_lot(lot["id"], account_id=acc.value, ticker=t, open_date=dt.value or None,
                                    shares=float(sh.value), cost_ps=float(cps.value or 0), note=note.value)
            else:
                holdings.add_lot(acc.value, t, dt.value or None, float(sh.value), float(cps.value or 0),
                                 note=note.value)
                if not db.scalar("SELECT 1 FROM prices WHERE ticker=? LIMIT 1", (t,)):
                    try:
                        from ..pipeline import fetch_prices_for

                        fetch_prices_for([t])
                    except Exception:
                        pass
            d.close()
            (after or ui.navigate.reload)()

        def delete():
            holdings.delete_lot(lot["id"])
            d.close()
            (after or ui.navigate.reload)()
        with ui.row().classes("w-full justify-between mt-2"):
            if lot:
                ui.button("Delete", on_click=lambda: _confirm("Delete this purchase?", delete)).props(
                    "flat dense no-caps color=negative")
            else:
                ui.label("")
            ui.button("Save", on_click=save).props("unelevated")
    d.open()


def _sell_dialog(ticker: str | None, after=None):
    with ui.dialog() as d, ui.card().style("min-width:460px"):
        ui.label("Record a sale").classes("oe-h2")
        acc = _acct_select()
        tk = ui.input("Ticker", value=ticker or "").props("dense outlined").classes("w-full")
        dt = ui.input("Date sold", value=date.today().isoformat()).props("dense outlined").classes("w-full")
        with ui.row().classes("w-full no-wrap gap-2"):
            sh = ui.number("Shares", format="%g").props("dense outlined").classes("flex-1")
            px = ui.number("Price per share", format="%.4f").props("dense outlined prefix=$").classes("flex-1")
            fee = ui.number("Fees", value=0, format="%.2f").props("dense outlined prefix=$").classes("w-28")
        meth = ui.select({"fifo": "Oldest purchases first (FIFO)", "hifo": "Highest cost first (smallest gain)",
                          "lifo": "Newest first (LIFO)", "lofo": "Lowest cost first"}, value="fifo",
                         label="Which purchases").props("dense outlined").classes("w-full")
        info = ui.label("").classes("oe-small")

        def show():
            t = (tk.value or "").upper()
            ls = holdings.lots([acc.value], [t]) if t else []
            info.text = f"{sum(x['shares'] for x in ls):g} shares on file in this account across {len(ls)} purchases"\
                if ls else ("No purchases of this ticker in this account" if t else "")
        tk.on_value_change(lambda e: show())
        acc.on_value_change(lambda e: show())
        show()

        def save():
            t = (tk.value or "").upper()
            have = sum(x["shares"] for x in holdings.lots([acc.value], [t]))
            if not t or not sh.value or not px.value:
                ui.notify("Enter the ticker, shares and price", type="warning")
                return
            if sh.value > have + 1e-6:
                ui.notify(f"Only {have:g} shares on file in this account", type="warning")
                return
            r = holdings.sell(acc.value, t, dt.value, float(sh.value), float(px.value), meth.value,
                              fees=float(fee.value or 0))
            ui.notify(f"Sold {r['sold']:g} {t}: gain {money(r['gain'], 2)}" + (
                f" ({money(r['short'], 2)} short-term)" if r["short"] else ""))
            d.close()
            (after or ui.navigate.reload)()
        with ui.row().classes("w-full justify-end mt-2"):
            ui.button("Record", on_click=save).props("unelevated")
    d.open()


def _basket_dialog(bid):
    b = next((x for x in holdings.baskets() if x["id"] == bid), None) if bid else None
    held = holdings.held_tickers()
    with ui.dialog() as d, ui.card().style("min-width:460px"):
        ui.label("Edit basket" if b else "New basket").classes("oe-h2")
        nm = ui.input("Name", value=(b or {}).get("name", ""), placeholder="Core, Income, Speculative…").props(
            "dense outlined").classes("w-full")
        mem = ui.select(sorted(set(held) | set((b or {}).get("tickers", []))), multiple=True,
                        value=(b or {}).get("tickers", []), label="Holdings in it").props(
            "dense outlined use-chips").classes("w-full")
        tgt = ui.number("Target share of the portfolio (optional)", value=((b or {}).get("target") or 0) * 100 or None,
                        suffix="%").props("dense outlined").classes("w-full")
        note = ui.input("Note", value=(b or {}).get("note", "")).props("dense outlined").classes("w-full")
        ui.label("A holding can be in several baskets. Baskets group tickers across accounts.").classes("oe-small")

        def save():
            if not (nm.value or "").strip():
                ui.notify("Name the basket", type="warning")
                return
            i = bid or holdings.add_basket(nm.value)
            holdings.update_basket(i, name=nm.value.strip(), note=note.value,
                                   target=(tgt.value / 100) if tgt.value else None)
            holdings.set_members(i, mem.value or [])
            _save(basket=i)
            d.close()
            ui.navigate.reload()

        def delete():
            holdings.delete_basket(bid)
            _save(basket=None)
            d.close()
            ui.navigate.reload()
        with ui.row().classes("w-full justify-between mt-2"):
            if b:
                ui.button("Delete basket", on_click=lambda: _confirm(f"Delete the basket {b['name']}? (Holdings are "
                                                                     "not touched.)", delete)).props(
                    "flat dense no-caps color=negative")
            else:
                ui.label("")
            ui.button("Save", on_click=save).props("unelevated")
    d.open()


def _holding_dialog(ticker: str, scope: Scope):
    hs, px = holdings.holdings(Scope(scope.account_ids, None))
    h = next((x for x in hs if x.ticker == ticker), None)
    if h is None:
        return
    bs = holdings.baskets()
    with ui.dialog().props("maximized=false") as d, ui.card().style("min-width:min(1000px,95vw);max-width:1100px"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.row().classes("items-baseline gap-3"):
                ui.label(h.ticker).classes("oe-h1")
                ui.label(h.name or "").classes("oe-muted")
                ui.label(f"{money(h.price, 2)}").classes("text-lg font-semibold")
                if h.day_pct is not None:
                    ui.label(_sgn(h.day_pct, 2, False)).style(f"color:{_col(h.day_pct)}")
            with ui.row().classes("gap-1"):
                ui.button("Stock page", icon="open_in_new", on_click=lambda: ui.navigate.to(
                    f"/stock/{h.ticker}", new_tab=True)).props("flat dense no-caps")
                ui.button(icon="close", on_click=d.close).props("flat round dense")
        with ui.row().classes("w-full gap-3"):
            _tile(money(h.value, 0), "Value", f"{h.shares:g} shares · {pct(h.weight, 1)} of holdings")
            _tile(_sgn(h.gain), "Gain", f"{_sgn(h.gain_pct, 1, False)} on {money(h.cost, 0)} · avg "
                                        f"{money(h.avg_cost, 2)}", _col(h.gain))
            _tile(_sgn(h.total_ret, 1, False), "Total return", f"with {money(h.divs, 0)} dividends · a year "
                                                              f"{pct(h.irr, 1)}", _col(h.total_ret))
            _tile(money(h.income, 0), "Income a year", f"yield {pct(h.div_yield, 2)} · on cost "
                                                       f"{pct(h.yield_on_cost, 2)}")
            if h.realized:
                _tile(_sgn(h.realized), "Realized", "from earlier sales", _col(h.realized))
        # price since the first purchase, with purchases marked and the average cost line
        if px.has(h.ticker):
            first = min(m["open_date"] for m in h.lots)
            pts = [(dd, c) for dd, c in zip(px.d[h.ticker], px.c[h.ticker]) if dd >= first]
            step = max(1, len(pts) // 500)
            pts = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
            buys = [{"value": [m["open_date"], m["cost_ps"]], "name": f"{m['shares']:g} @ {m['cost_ps']:.2f}"}
                    for m in h.lots if m.get("date_known", 1)]
            ui.echart({"tooltip": {"trigger": "axis"}, "grid": {"left": 56, "right": 16, "top": 16, "bottom": 28},
                       "xAxis": {"type": "time"}, "yAxis": {"type": "value", "scale": True},
                       "series": [{"type": "line", "name": "Price", "showSymbol": False, "data": [list(p) for p in pts],
                                   "itemStyle": {"color": PALETTE[1]},
                                   "markLine": {"symbol": "none", "data": [{"yAxis": round(h.avg_cost or 0, 2),
                                                                            "name": "Average cost"}],
                                                "lineStyle": {"color": "#C9A227", "type": "dashed"},
                                                "label": {"formatter": "average cost {c}",
                                                          "position": "insideStartTop"}}},
                                  {"type": "scatter", "name": "Purchases", "data": buys, "symbolSize": 10,
                                   "itemStyle": {"color": BRAND}}]}).classes("w-full").style("height:220px")
        ui.label("Purchases").classes("oe-h2")
        cols = [("account", "Account"), ("date", "Bought"), ("shares", "Shares"), ("cps", "Price paid"),
                ("cost", "Cost"), ("value", "Value"), ("gain", "Gain"), ("gp", "Gain %"), ("divs", "Dividends"),
                ("tot", "Total return"), ("ann", "A year"), ("term", "Term")]
        rows = [{"id": m["id"], "account": m.get("account", ""), "date": m["open_date"] if m.get("date_known", 1)
                 else "unknown", "shares": f"{m['shares']:g}", "cps": money(m["cost_ps"], 2),
                 "cost": money(m["cost"], 0), "value": money(m["value"], 0), "gain": _sgn(m["gain"]),
                 "gp": pct(m["gain_pct"], 1), "divs": money(m["divs"], 0), "tot": pct(m["total_ret"], 1),
                 "ann": pct(m["ann"], 1), "term": "Long" if m["long_term"] else f"Short · {m['days_to_long']} d"}
                for m in h.lots]
        t = ui.table(columns=[{"name": k, "label": lab, "field": k, "align": "left" if k in ("account", "date", "term")
                               else "right"} for k, lab in cols], rows=rows, row_key="id").props(
            "dense flat").classes("w-full")
        by_id = {m["id"]: m for m in h.lots}
        t.on("rowClick", lambda e: (d.close(), _lot_dialog(by_id[e.args[1]["id"]])))
        ui.label("Click a purchase to edit it.").classes("oe-small")
        closed = [x for x in holdings.lots(scope.account_ids, [h.ticker], closed=True)]
        if closed:
            with ui.expansion(f"Sold purchases ({len(closed)})").classes("w-full"):
                for x in closed:
                    g = x["shares"] * (x["price"] - x["cost_ps"])
                    ui.label(f"{x['shares']:g} bought {x['open_date']} at {money(x['cost_ps'], 2)}, sold "
                             f"{x['close_date']} at {money(x['price'], 2)}: {_sgn(g)}").classes("text-sm")
        with ui.row().classes("w-full items-center justify-between mt-2"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Baskets:").classes("text-sm")
                for b in bs:
                    ui.checkbox(b["name"], value=h.ticker in b["tickers"],
                                on_change=lambda e, b=b: holdings.toggle_member(b["id"], h.ticker, e.value))
                if not bs:
                    ui.label("none yet").classes("oe-small")
            with ui.row().classes("gap-1"):
                ui.button("Add purchase", icon="add", on_click=lambda: (d.close(), _lot_dialog(None, h.ticker))
                          ).props("outline dense no-caps")
                ui.button("Record sale", icon="sell", on_click=lambda: (d.close(), _sell_dialog(h.ticker))).props(
                    "outline dense no-caps")
    d.on("hide", lambda: None)
    d.open()

"""Strategies (what the app used to call profiles) and the Oracle checklists.

An Oracle checklist is how companies are scored (the house view before 0.21). A strategy is a portfolio plan run
on top of one: which companies it considers (its universe), what price it pays, how many it holds and how it buys, adds, trims
and sells, its risk limits and its money. Every strategy that runs daily keeps its own paper portfolio; the primary
one drives Today, the emails, the alerts and the Screener's buy prices."""

from __future__ import annotations

import copy
import json

from nicegui import run, ui

from .. import db, exits, rules
from . import data
from .layout import frame
from .theme import pct


def _launch(kind, args=None):
    from .pages_admin import _launch as la

    la(kind, args)


def _pct(v):
    return None if v in (None, "") else round(100 * float(v), 4)


def _frac(v, default=None):
    return round(float(v) / 100, 6) if v not in (None, "") else default


def industries() -> list[str]:
    return [r["sic_desc"] for r in db.rows("SELECT DISTINCT sic_desc FROM securities WHERE sic_desc IS NOT NULL AND "
                                           "sic_desc != '' AND COALESCE(active, 1)=1 ORDER BY sic_desc")]


def record(p: rules.Profile) -> dict:
    """The strategy's live paper record: return a year since it started, against the S&P 500, worst drop, holdings."""
    r = db.one("SELECT id, start_date FROM runs WHERE kind='live' AND profile_id=?", (p.id,))
    if not r:
        return {}
    st = data.stats(data.equity(r["id"]))
    n = db.scalar("SELECT COUNT(*) FROM positions ps JOIN portfolios f ON f.id=ps.portfolio_id WHERE f.run_id=? AND "
                  "ps.units > 0", (r["id"],), 0)
    sig = db.scalar("SELECT COUNT(*) FROM signals WHERE run_id=? AND date >= date('now', '-90 days')", (r["id"],), 0)
    return {"run_id": r["id"], "start": r["start_date"], "holdings": n, "signals_90d": sig, **st}


def universe_text(st: dict) -> str:
    bits = [f"Quality {float(st.get('quality_min', 70)):.0f}+ and the checklist's must-pass rules"]
    if st.get("universe_wonder_min"):
        bits.append(f"Wonderfulness {float(st['universe_wonder_min']):.0f}+")
    if st.get("min_market_cap"):
        bits.append(f"market value ${float(st['min_market_cap']) / 1e9:g}B+")
    if st.get("universe_industries"):
        n = len(st["universe_industries"])
        bits.append(f"{n} industr{'y' if n == 1 else 'ies'} only")
    if st.get("universe_exclude_industries"):
        n = len(st["universe_exclude_industries"])
        bits.append(f"{n} industr{'y' if n == 1 else 'ies'} excluded")
    if not st.get("include_foreign", True):
        bits.append("no foreign filers")
    ck = rules.house_view(rules.checklist_id(st)) or {}
    if ck:
        bits.append(f"scored on {ck.get('name')}" + (" (5 years of results)" if ck.get("record") == "short" else ""))
    return "; ".join(bits)


# ===================================================================== the Strategies page
@ui.page("/profiles")
def _old_profiles(pid: int | None = None):
    ui.navigate.to(f"/strategies?pid={pid}" if pid else "/strategies")


@ui.page("/strategies")
def strategies_page(pid: int | None = None, tab: str = "rules", hidden: int = 0):
    with frame("Strategies"):
        allp = rules.list_profiles()
        profs = [p for p in allp if hidden or not rules.is_hidden(p.id)]
        n_hidden = len(allp) - len([p for p in allp if not rules.is_hidden(p.id)])
        prim = rules.primary_id()
        try:
            cur = rules.get_profile(pid) if pid else rules.default_profile()
        except KeyError:
            cur = rules.default_profile()
        tab = tab if tab in ("rules", "results", "tests") else "rules"
        ui.label("Strategies").classes("oe-h1")
        ui.label("A strategy is a portfolio plan: which companies it considers, what price it pays, how many it holds "
                 "and how it buys, adds, trims and sells. Companies are scored by an Oracle checklist the strategy "
                 "picks; strategies on the same checklist share its scores, so changing a strategy's other settings "
                 "never re-scores a company. Strategies that run daily keep "
                 "their own paper portfolio; the primary one (★) drives Today, the emails, the price alerts and the "
                 "Screener's buy prices. Built-in strategies (🔒) are kept as references: edit a copy with "
                 "Save as.").classes("oe-muted")
        hv = rules.house_view() or {}
        n_ck = len(rules.checklists())
        with ui.row().classes("items-center gap-2"):
            ui.icon("rule").classes("text-gray-500")
            ui.label(f"Oracle checklists ({n_ck}): how companies are scored. Used across the app: "
                     f"{hv.get('name', 'Oracle checklist')} v{hv.get('version', 1)}").classes("text-sm")
            ui.button("Open the Oracle checklists", icon="open_in_new", on_click=lambda: ui.navigate.to(
                "/checklists")).props("flat dense no-caps")

        # ---- side by side: live paper record where there is one, otherwise the latest backtest
        from .. import pipeline

        def ranked(st):
            return st.get("buy_rule") == "top_ranked" and st.get("portfolio_mode", "focused") == "focused"

        def size_txt(st):
            if ranked(st):
                return f"top {int(st.get('ranked_buy_count') or 4)} weekly"
            if st.get("portfolio_mode", "focused") != "focused":
                return "variable"
            if st.get("max_holdings") == 0:
                return "no limit" + (f", {int(st['max_buys_per_week'])}/wk" if st.get("max_buys_per_week") else "")
            return str(st.get("max_holdings"))

        def price_txt(st):
            if ranked(st):
                return "any price"
            rule = st.get("buy_price_rule") or "strict"
            if rule == "growth_gap":
                return f"growth gap {pct(st.get('buy_growth_gap') or 0.10, 0)}"
            if rule == "fair":
                return f"≤ {float(st.get('buy_fair_mult') or 1):g}× fair value"
            return f"strict {pct(st.get('hurdle'), 0)} · {pct(st.get('mos_gate'), 0)}"

        def cell(v, digits=1):
            return pct(v, digits) if v is not None else "—"

        rows = []
        for p in profs:
            rec = record(p)
            lb = pipeline.latest_backtest(p)
            bs = data.stats(data.equity(lb["id"])) if lb else {}
            beat = lambda a, b: "" if a is None or b is None else ("color:#1f6f4a" if a >= b else "color:#a33a2b")
            sel_css = "background:#fbf6e6;" if p.id == cur.id else ""
            rows.append(
                f"<tr style='{sel_css}{'font-weight:600;' if p.id == prim else ''}cursor:pointer' "
                f"onclick=\"location.href='/strategies?pid={p.id}&tab={tab}&hidden={hidden}'\">"
                f"<td style='text-align:left'>{'★ ' if p.id == prim else ''}{p.name}{' 🔒' if p.locked else ''}"
                f"{' <span style=color:#9ca3af>(hidden)</span>' if rules.is_hidden(p.id) else ''}</td>"
                f"<td>{size_txt(p.settings)}</td><td>{price_txt(p.settings)}</td>"
                f"<td style='{beat(rec.get('cagr'), rec.get('bench_cagr'))}'>{cell(rec.get('cagr'))}</td>"
                f"<td>{cell(rec.get('bench_cagr'))}</td><td>{(rec.get('start') or '')[:4] or ('—' if not p.active else 'building')}</td>"
                f"<td style='{beat(bs.get('cagr'), bs.get('bench_cagr'))}'>{cell(bs.get('cagr'))}</td>"
                f"<td>{cell(bs.get('bench_cagr'))}</td><td>{cell(bs.get('max_dd'), 0)}</td>"
                f"<td>{(lb['start_date'][:4] if lb else '—')}{' ⚠' if lb and lb['stale'] else ''}</td></tr>")
        with ui.card().classes("oe-card w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Side by side").classes("oe-h2")
                with ui.row().classes("items-end gap-2"):
                    bst = ui.input("Backtest all from", value=db.cfg("backfill_start", "2019-01-01")).props(
                        "outlined dense").classes("w-36")
                    ui.button("Backtest all", icon="play_arrow", on_click=lambda: _launch("backtest_all", {
                        "start": bst.value, "end": __import__("datetime").date.today().isoformat(),
                        "profile_ids": [p.id for p in profs]})).props("unelevated").tooltip(
                        "Backtests every strategy shown here over the same dates; strategies with an up-to-date "
                        "backtest from that date are skipped")
                    if n_hidden:
                        ui.switch(f"Show hidden ({n_hidden})", value=bool(hidden), on_change=lambda e: ui.navigate.to(
                            f"/strategies?pid={cur.id}&tab={tab}&hidden={int(e.value)}"))
            ui.html("<table class='oe-cmp' style='width:100%;border-collapse:collapse;font-size:13px'><thead>"
                    "<tr><th></th><th></th><th></th><th colspan='3' style='text-align:center'>Live paper portfolio</th>"
                    "<th colspan='4' style='text-align:center'>Latest backtest</th></tr>"
                    "<tr><th style='text-align:left'>Strategy</th><th>Holdings</th><th>Buys at</th>"
                    "<th>Return a year</th><th>S&amp;P 500</th><th>Since</th>"
                    "<th>Return a year</th><th>S&amp;P 500</th><th>Worst drop</th><th>From</th></tr></thead><tbody>"
                    + "".join(rows) + "</tbody></table><style>.oe-cmp td,.oe-cmp th{padding:5px 10px;text-align:right;"
                    "border-bottom:1px solid #eef0ee}.oe-cmp th{color:#6b7280;font-size:12px}</style>",
                    sanitize=False).classes("w-full")
            ui.label("Click a strategy to open it below. Live paper portfolio: strategies that run daily, since their "
                     "history starts. Latest backtest: the "
                     "most recent one of each strategy (⚠ = the strategy was edited since; run it again). Backtests "
                     "starting on different dates aren't comparable: use 'Backtest all' to put every strategy on the "
                     "same dates.").classes("oe-small")

        # ---- the selected strategy: its rules, its results, its tests
        from .pages_admin import backtest_cards, backtest_form, experiments_section, holdings_test_section

        par = rules.parent_of(cur.id)
        hid = rules.is_hidden(cur.id)
        with ui.card().classes("oe-card w-full gap-1").style("border-left:4px solid #C9A227"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.row().classes("items-center gap-2"):
                    if cur.id == prim:
                        ui.icon("star").classes("text-amber-600").tooltip("Primary strategy")
                    ui.label(cur.name).classes("oe-h1")
                    if cur.locked:
                        ui.icon("lock").classes("text-gray-500").tooltip("Built-in, locked: edit a copy with Save as")
                    if hid:
                        ui.label("hidden").classes("oe-small")
                with ui.row().classes("items-center gap-1"):
                    sw = ui.switch("Runs daily", value=cur.active)
                    if cur.id == prim:
                        sw.disable()
                        sw.tooltip("The primary strategy always runs daily")
                    sw.on_value_change(lambda e: _toggle_active(cur, e.value))
                    if cur.id != prim and rules.scoring_standard(cur.settings) == "standard":
                        ui.button("Make primary", icon="star_outline", on_click=lambda: _make_primary(cur)).props(
                            "flat dense no-caps")
                    if cur.id != prim:
                        if cur.locked:
                            ui.button("Show" if hid else "Hide", icon="visibility" if hid else "visibility_off",
                                      on_click=lambda: (rules.set_hidden(cur.id, not hid), ui.navigate.to(
                                          f"/strategies?pid={cur.id}&tab={tab}&hidden=1"))).props(
                                "flat dense no-caps").tooltip("Built-in strategies can't be deleted; hiding takes it "
                                                              "off this page and the Screener")
                        else:
                            ui.button("Delete", icon="delete", on_click=lambda: _delete_strategy(cur)).props(
                                "flat dense no-caps color=negative").tooltip(
                                "Delete this strategy with its backtests and paper portfolio")
            if cur.notes:
                ui.label(cur.notes).classes("text-sm")
            if par:
                diffs = rules.differences(cur.body, par.body)
                ui.label(f"Copied from {par.name}: " + ("; ".join(diffs) if diffs else "no changes yet") + ".").classes(
                    "oe-small")
            ui.label("Considers: " + universe_text(cur.settings) + ".").classes("oe-small")
            with ui.expansion("What it does, in full").classes("w-full").props("dense"):
                ui.label(rules.describe(cur.body)).classes("oe-small")
        with ui.tabs(value=tab).classes("w-full") as tabs:
            ui.tab("rules", label="Rules", icon="tune")
            ui.tab("results", label="Results & backtests", icon="query_stats")
            ui.tab("tests", label="Holdings test & experiments", icon="science")
        with ui.tab_panels(tabs, value=tab).classes("w-full").style("background:transparent"):
            with ui.tab_panel("rules").classes("p-0"):
                _editor(cur)
            with ui.tab_panel("results").classes("p-0 gap-3"):
                rec = record(cur)
                with ui.card().classes("oe-card w-full"):
                    ui.label("Live paper portfolio").classes("oe-h2")
                    if rec:
                        ui.label(f"Since {rec.get('start')}: {pct(rec.get('cagr'), 1)} a year vs S&P 500 "
                                 f"{pct(rec.get('bench_cagr'), 1)} · worst drop {pct(rec.get('max_dd'), 0)} · holds "
                                 f"{rec.get('holdings')} · {rec.get('signals_90d')} signals in 90 days").classes("text-sm")
                        ui.button("Open", icon="open_in_new", on_click=lambda: ui.navigate.to(
                            f"/performance?run={rec['run_id']}")).props("flat dense")
                    else:
                        ui.label("It doesn't run daily, so it has no live paper portfolio. Switch on 'Runs daily' on "
                                 "its card to keep one.").classes("text-sm")
                backtest_form(cur.id)
                runs = [r for r in data.all_runs() if r["kind"] == "backtest" and r["profile_id"] == cur.id]
                if runs:
                    ui.label(f"{cur.name}'s backtests").classes("oe-h2")
                    backtest_cards(runs)
                else:
                    ui.label("No backtests of this strategy yet.").classes("oe-muted")
            with ui.tab_panel("tests").classes("p-0 gap-3"):
                holdings_test_section(cur.id)
                experiments_section(cur.id)

        # ---- every backtest, for tidying up
        allruns = [r for r in data.all_runs() if r["kind"] == "backtest"]
        if allruns:
            with ui.expansion(f"All backtests ({len(allruns)}): tidy up", icon="cleaning_services").classes(
                    "w-full oe-card"):
                vers = {p.id: p.version for p in allp}
                trows = []
                for r in allruns:
                    stt = data.stats(data.equity(r["id"]))
                    sj = json.loads(r["settings_json"] or "{}")
                    trows.append({"id": r["id"], "name": r["name"], "strategy": r["pname"],
                                  "period": f"{r['start_date']} → {r['end_date']}",
                                  "ret": pct(stt.get("cagr"), 1) if stt.get("cagr") is not None else "—",
                                  "spx": pct(stt.get("bench_cagr"), 1) if stt.get("bench_cagr") is not None else "—",
                                  "state": ("out of date" if sj.get("profile_version") not in
                                            (None, vers.get(r["profile_id"])) else r["status"])})
                tbl = ui.table(columns=[
                    {"name": "name", "label": "Backtest", "field": "name", "align": "left", "sortable": True},
                    {"name": "strategy", "label": "Strategy", "field": "strategy", "align": "left", "sortable": True},
                    {"name": "period", "label": "Dates", "field": "period", "sortable": True},
                    {"name": "ret", "label": "Return a year", "field": "ret"},
                    {"name": "spx", "label": "S&P 500", "field": "spx"},
                    {"name": "state", "label": "State", "field": "state", "sortable": True}],
                    rows=trows, row_key="id", selection="multiple", pagination=15).classes("w-full").props("dense flat")
                with ui.row().classes("gap-2"):
                    ui.button("Delete selected", icon="delete", on_click=lambda: _confirm_delete(
                        [r["id"] for r in tbl.selected])).props("outline color=negative")
                    ui.button("Select out-of-date ones", on_click=lambda: setattr(
                        tbl, "selected", [r for r in trows if r["state"] == "out of date"]) or tbl.update()).props("flat")
                    ui.button("Select all but each strategy's latest", on_click=lambda: _select_old(tbl, trows,
                                                                                                    allruns)).props(
                        "flat")


def _confirm_delete(ids):
    from .pages_admin import _confirm_delete_runs

    if ids:
        _confirm_delete_runs(ids)
    else:
        ui.notify("Select backtests to delete first")


def _select_old(tbl, trows, allruns):
    newest = {}
    for r in allruns:  # newest first
        newest.setdefault(r["profile_id"], r["id"])
    keep = set(newest.values())
    tbl.selected = [r for r in trows if r["id"] not in keep]
    tbl.update()


def _delete_strategy(p):
    n = db.scalar("SELECT COUNT(*) FROM runs WHERE profile_id=?", (p.id,), 0)
    with ui.dialog() as dlg, ui.card():
        ui.label(f"Delete {p.name}?").classes("oe-h2")
        ui.label(f"Removes the strategy and everything run with it: {n} backtest, holdings-test and paper-portfolio "
                 "run(s). Company scores are kept. This can't be undone (a backup can restore it).").classes("text-sm")

        def go():
            try:
                res = rules.delete_profile(p.id)
            except ValueError as e:
                ui.notify(str(e), type="warning")
                return
            dlg.close()
            ui.notify(f"Deleted {p.name} and {res['runs']} run(s)")
            ui.navigate.to("/strategies")

        with ui.row():
            ui.button("Delete", icon="delete", on_click=go).props("unelevated color=negative")
            ui.button("Cancel", on_click=dlg.close).props("flat")
    dlg.open()


def _toggle_active(p, value):
    db.get().execute("UPDATE profiles SET active=? WHERE id=?", (int(value), p.id))
    if value and not db.one("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (p.id,)):
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Build {p.name}'s paper portfolio now?").classes("oe-h2")
            ui.label("It replays the strategy over the live history, then it runs with every daily update.").classes(
                "oe-small")
            with ui.row():
                ui.button("Build", on_click=lambda: (dlg.close(), _launch("rebuild", {"profile_id": p.id}))).props(
                    "unelevated")
                ui.button("Later", on_click=dlg.close).props("flat")
        dlg.open()


def _make_primary(p):
    with ui.dialog() as dlg, ui.card():
        ui.label(f"Make {p.name} the primary strategy?").classes("oe-h2")
        ui.label("Today's actions, the emails, the price alerts and the Screener's buy prices will follow it. Its "
                 "paper portfolio and history are kept; nothing else changes.").classes("text-sm")

        def ok():
            try:
                rules.set_primary(p.id)
            except ValueError as e:
                ui.notify(str(e), type="warning")
                return
            dlg.close()
            if not db.one("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (p.id,)):
                _launch("rebuild", {"profile_id": p.id})
            ui.navigate.to(f"/strategies?pid={p.id}")
        with ui.row():
            ui.button("Make primary", on_click=ok).props("unelevated")
            ui.button("Cancel", on_click=dlg.close).props("flat")
    dlg.open()


def _num(label, value, fmt="%.4g", w="w-44", **kw):
    return ui.number(label, value=value, format=fmt, **kw).props("outlined dense").classes(w)


def _x_to_ui(v, unit):
    if v is None:
        return None
    return round(float(v) * 100, 4) if unit == "pct" else v


def _x_from_ui(v, unit):
    if v in (None, ""):
        return None
    return round(float(v) / 100, 6) if unit == "pct" else float(v)


def _exit_rules_editor(rs: list[dict], st: dict, locked: bool) -> None:
    """Section 4: the strategy's exit rules as an editable list (exits.py). Edits change `rs` in place."""
    trig = {k: f"{v['group']}: {v['label']}" for k, v in exits.TRIGGERS.items()}

    @ui.refreshable
    def table():
        if not rs:
            ui.label("No exit rules: it never sells, trims or switches on its own.").classes("oe-small")
        for i, r in enumerate(rs):
            t = exits.TRIGGERS[r["when"]]
            with ui.row().classes("gap-2 items-end w-full flex-nowrap"):
                ui.switch(value=r["on"], on_change=lambda ev, r=r: (r.update(on=ev.value), table.refresh())
                          ).props("dense").tooltip("Use this rule")
                ui.select(trig, value=r["when"], label="When",
                          on_change=lambda ev, r=r: (_set_trigger(r, ev.value), table.refresh())).props(
                    "outlined dense options-dense").classes("w-80").tooltip(t["help"])
                if t.get("unit"):
                    ui.number("X " + {"pct": "%", "x": "×", "rank": "#", "pts": "pts"}.get(t["unit"], ""),
                              value=_x_to_ui(r.get("x"), t["unit"]), format="%.4g",
                              on_change=lambda ev, r=r, u=t["unit"]: (r.update(x=_x_from_ui(ev.value, u)),
                                                                     table.refresh())).props(
                        "outlined dense clearable").classes("w-28")
                if t.get("y_label"):
                    yu = "pct" if r["when"] == "better_idea" else t.get("y_unit", "x")
                    ui.number(t["y_label"], value=_x_to_ui(r.get("y"), yu), format="%.4g",
                              on_change=lambda ev, r=r, u=yu: (r.update(y=_x_from_ui(ev.value, u)), table.refresh())
                              ).props("outlined dense clearable").classes("w-56")
                acts = {"switch": exits.ACTIONS["switch"]} if r["when"] == "better_idea" else \
                    {k: v for k, v in exits.ACTIONS.items() if k != "switch"}
                ui.select(acts, value=r["do"] if r["do"] in acts else next(iter(acts)), label="Do",
                          on_change=lambda ev, r=r: (r.update(do=ev.value), table.refresh())).props(
                    "outlined dense").classes("w-52")
                if r["do"] in ("trim", "trim_to"):
                    ui.number("% of holding" if r["do"] == "trim" else "% of portfolio (blank = auto)",
                              value=_x_to_ui(r.get("amount"), "pct"), format="%.0f", min=1, max=100,
                              on_change=lambda ev, r=r: (r.update(amount=_x_from_ui(ev.value, "pct")),
                                                         table.refresh())).props("outlined dense clearable"
                                                                                 ).classes("w-44")
                if r["when"] != "better_idea" and r["do"] != "warn":
                    ui.select(exits.CONFIRMS, value=r["confirm"], label="Confirmed",
                              on_change=lambda ev, r=r: (r.update(confirm=ev.value), table.refresh())).props(
                        "outlined dense").classes("w-56")
                    if r["confirm"] == "checks":
                        ui.number("Checks", value=r.get("checks") or 1, format="%.0f", min=1,
                                  on_change=lambda ev, r=r: (r.update(checks=int(ev.value or 1)), table.refresh())
                                  ).props("outlined dense").classes("w-24")
                if r["do"] != "switch":
                    ui.select({"": "—", **{k: v["label"] for k, v in exits.GUARDS.items()}}, value=r.get("unless") or "",
                              label="Unless",
                              on_change=lambda ev, r=r: (r.update(unless=ev.value or None,
                                                                  ux=exits.GUARDS[ev.value]["x"] if ev.value else None),
                                                         table.refresh())).props("outlined dense").classes("w-64")
                    if r.get("unless"):
                        gu = exits.GUARDS[r["unless"]]["unit"]
                        ui.number("…" + ("%" if gu == "pct" else ""), value=_x_to_ui(r.get("ux"), gu), format="%.4g",
                                  on_change=lambda ev, r=r, u=gu: (r.update(ux=_x_from_ui(ev.value, u)), table.refresh())
                                  ).props("outlined dense").classes("w-24")
                ui.number("Held at least (days)", value=r.get("min_days") or 0, format="%.0f", min=0,
                          on_change=lambda ev, r=r: (r.update(min_days=int(ev.value or 0)), table.refresh())
                          ).props("outlined dense").classes("w-40")
                ui.button(icon="arrow_upward", on_click=lambda i=i: _move(i, -1)).props("flat dense round"
                                                                                         ).tooltip("Earlier")
                ui.button(icon="delete", on_click=lambda i=i: (rs.pop(i), table.refresh())).props(
                    "flat dense round color=negative").tooltip("Remove")
            ui.label(("" if r["on"] else "(off) ") + exits.describe_rule(r)).classes(
                "oe-small pl-12 " + ("" if r["on"] else "line-through"))

    def _set_trigger(r, when):
        t = exits.TRIGGERS[when]
        r.update(when=when, x=t.get("x"), y=None)
        if when == "better_idea":
            r.update(do="switch", y=0.04, confirm="now")
        elif r["do"] == "switch":
            r["do"] = "sell"

    def _move(i, step):
        j = i + step
        if 0 <= j < len(rs):
            rs[i], rs[j] = rs[j], rs[i]
            table.refresh()

    def _preset(name):
        rs[:] = exits.preset(name, st)
        table.refresh()

    ui.label("Each rule: when its condition holds, do the action. Every signal day the sell and switch rules are "
             "checked first (the first that fires sells), then warnings and rules that wait for the next filing "
             "(REVIEW), then trims (one at a time). The new money then goes where the buying rule says.").classes(
        "oe-small")
    table()
    if not locked:
        with ui.row().classes("gap-2 items-center"):
            ui.button("Add rule", icon="add", on_click=lambda: (rs.append(exits.rule("quality_below", x=55)),
                                                                table.refresh())).props("outline dense")
            ui.label("Start from:").classes("oe-small")
            for k, label in exits.PRESETS.items():
                ui.button(label.split(" (")[0], on_click=lambda k=k: _preset(k)).props("flat dense").tooltip(label)


def _editor(cur: rules.Profile) -> None:
    body = copy.deepcopy(cur.body)
    st = body["settings"]
    inds = industries()
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(f"{cur.name} · v{cur.version}{' · locked' if cur.locked else ''}").classes("oe-h2")
            prev = ui.label("").classes("text-sm font-semibold text-green-800")
        desc = ui.input("Description: what this strategy is for (blank = describe it from its settings)",
                        value=cur.notes).props("outlined dense autogrow" + (" readonly" if cur.locked else "")
                                               ).classes("w-full")

        # ---- 1. universe
        ui.label("1 · Which companies it considers").classes("oe-h2 pt-2")
        ui.label("On top of its Oracle checklist's must-pass rules. Wonderfulness is used as it was known on each day, so "
                 "backtests stay honest. Companies you hold are always followed.").classes("oe-small")
        with ui.row().classes("gap-3 items-end"):
            cks = rules.checklists()
            std = ui.select({k: v["name"] + ("" if "record" in v["name"].lower() else (
                " · 10-year record" if v.get("record", "standard") == "standard" else " · 5-year record (research)"))
                for k, v in cks.items()},
                            value=rules.checklist_id(st), label="Oracle checklist (how companies are scored)").props(
                "outlined dense").classes("w-[420px]")
            qmin = _num("Quality at least", st.get("quality_min", 70), "%.0f", "w-40", min=0, max=100)
            wmin = _num("Wonderfulness at least (blank = any)", st.get("universe_wonder_min"), "%.0f", "w-72",
                        min=0, max=100)
            wmin.props("clearable")
            mcap = _num("Market value at least $", st.get("min_market_cap"), "%.0f", "w-52", min=0)
            dvol = _num("Trading at least $ a day", st.get("min_dollar_volume"), "%.0f", "w-52", min=0)
            inf = ui.switch("Include foreign filers", value=st.get("include_foreign", True))
        with ui.row().classes("gap-3 items-end w-full"):
            inc = ui.select(inds, value=[x for x in st.get("universe_industries") or [] if x in inds], multiple=True,
                            with_input=True, label="Only these industries (blank = all)").props(
                "outlined dense use-chips clearable").classes("flex-1")
            exc = ui.select(inds, value=[x for x in st.get("universe_exclude_industries") or [] if x in inds],
                            multiple=True, with_input=True, label="Never these industries").props(
                "outlined dense use-chips clearable").classes("flex-1")

        # ---- 2. price
        ui.label("2 · What price it pays").classes("oe-h2 pt-2")
        with ui.row().classes("gap-3 items-end"):
            hur = _num("Hurdle: expected return a year %", _pct(st.get("hurdle")), "%.4g", "w-60")
            mos = _num("Margin of safety %", _pct(st.get("mos_gate")), "%.4g", "w-48")
            rm = ui.switch("Bigger margin when the market is expensive", value=st.get("regime_modifier", False))
            rvm = ui.select({"classic": "Classic valuation (lower of two models)",
                             "value_driver": "Value-driver (strict buy price at the hurdle)",
                             "enhanced": "Enhanced valuation (bear/base/bull)"},
                            value=st.get("valuation_model", "classic"), label="Valuation").props(
                "outlined dense").classes("w-96")
        with ui.row().classes("gap-3 items-end"):
            ptest = ui.select({"dcf": "Margin of safety + expected return (DCF)",
                               "fair_pe": "Fair price: P/E vs the company's own usual P/E"},
                              value=st.get("price_test", "dcf"), label="Price test").props(
                "outlined dense").classes("w-96")
            fpm = _num("Fair P/E: at most × usual", float(st.get("fair_pe_mult", 1.5)), "%.2f", "w-48", min=0.5,
                       max=3, step=0.1)
            mmo = _num("Too cheap to trust: no buy more than % below value (blank = off)", _pct(st.get("max_mos")),
                       "%.0f", "w-[420px]", min=0, max=95)
            mmo.props("clearable")
            rvr = _num("Value's discount rate % (blank = 9%)", _pct(st.get("vd_required_return")), "%.1f", "w-64",
                       min=4, max=25)
            rvr.props("clearable")
            rvg = _num("Growth cap %", 100 * float(st.get("vd_growth_cap", 0.12)), "%.0f", "w-32", min=0, max=30)
            rpm = ui.switch("Bigger margin for less predictable earnings", value=bool(st.get("predictability_mos")))

        with ui.row().classes("gap-3 items-end"):
            bpr = ui.select({"strict": "Strict: margin of safety below the cautious value at the hurdle (the old rule)",
                             "fair": "Fair value: at most × the fair value",
                             "growth_gap": "Growth gap: the price implies at most X more growth than delivered"},
                            value=st.get("buy_price_rule") or "strict", label="Buy price rule").props(
                "outlined dense").classes("w-[470px]")
            bfm = _num("Fair value: buy at most ×", float(st.get("buy_fair_mult") or 1.0), "%.2f", "w-52", min=0.3,
                       max=3, step=0.05)
            bgg = _num("Growth gap: at most (pts)", _pct(st.get("buy_growth_gap") if st.get("buy_growth_gap") is not None
                                                          else 0.10), "%.0f", "w-52", min=0, max=40)
            bmr = _num("Expected return at least %", _pct(st.get("buy_min_return") if st.get("buy_min_return") is not None
                                                           else 0.05), "%.1f", "w-52", min=0, max=30)
        with ui.row().classes("gap-3 items-end"):
            bsg = _num("Only buy when sales grow at least % (blank = any)", _pct(st.get("buy_min_sales_growth")), "%.0f",
                       "w-[340px]", min=-50, max=100)
            bsg.props("clearable")
            bwm = _num("Only buy when Wonderfulness is at least (blank = any)", st.get("buy_min_wonder"), "%.0f",
                       "w-[360px]", min=0, max=100)
            bwm.props("clearable")
            btr = ui.select({"": "Any price trend", "above_200d": "Only when the price is above its 200-day average",
                             "beating_market_6m": "Only when ahead of the S&P 500 over 6 months",
                             "both": "Both"}, value=st.get("buy_trend") or "", label="Only buy on the rise").props(
                "outlined dense").classes("w-[380px]")
            brk = ui.select({"oracle_rank": "Oracle Rank (Quality and cheapness)",
                             "combined": "Combined score: (Wonderfulness + Quality) ÷ 2"},
                            value=st.get("buy_rank_by") or "oracle_rank", label="Rank buy candidates by").props(
                "outlined dense").classes("w-[380px]")
        ui.label("The strict rule (hurdle, margin of safety, valuation above) asks for prices most good companies never "
                 "reach: in the November 2026 tests stocks at or below it did worst of all. The fair-value and growth-"
                 "gap rules use the fair value instead; see the Guide, 'Buying and selling on data'.").classes("oe-small")

        # ---- 3. portfolio
        ui.label("3 · How the portfolio is run").classes("oe-h2 pt-2")
        with ui.row().classes("gap-3 items-end"):
            brule = ui.select({"buy_zone": "Buy Zone: buy below the buy price (section 2)",
                               "top_ranked": "Top-ranked: every week, the best-scored companies at any price"},
                              value=st.get("buy_rule", "buy_zone"), label="Buying rule").props(
                "outlined dense").classes("w-[430px]")
            rbn = _num("Top-ranked: companies bought each week", st.get("ranked_buy_count", 4), "%.0f", "w-72",
                       min=1, max=20)
            rmw = _num("Top-ranked: no new money above % of portfolio (blank = off)", _pct(st.get("ranked_max_weight")),
                       "%.0f", "w-[400px]", min=5, max=100)
            rmw.props("clearable")
        ui.label("Top-ranked ranks every scored company by (Wonderfulness + Quality) ÷ 2 as known each week, splits the "
                 "week's money (and any sales) equally across the top ones, and ignores the price test, Buy Zone, "
                 "holdings limit, sizing and new-money settings; it sells, trims and warns by the rules in section 4 "
                 "(start from 'Top-ranked' there). It must be Focused.").classes("oe-small")
        with ui.row().classes("gap-3 items-end"):
            pmode = ui.select({"focused": "Focused: hold only the best N", "variable": "Variable (old style)"},
                              value=st.get("portfolio_mode", "focused"), label="Portfolio style").props(
                "outlined dense").classes("w-64")
            pn = _num("Holdings (N, 0 = no limit)", st.get("max_holdings", 15), "%.0f", "w-48", min=0)
            pmb = _num("Spread each week's money across at most (blank = all)", st.get("max_buys_per_week"), "%.0f",
                       "w-[380px]", min=1)
            pmb.props("clearable")
            psz = ui.select({"equal": "Equal slices", "conviction": "Conviction (higher Rank, bigger slice)",
                             "value_quality": "Better business at a better price gets more",
                             "size": "Bigger company gets more (like an index)"},
                            value=st.get("sizing", "equal"), label="Sizing").props("outlined dense").classes("w-72")
            pcap0 = _num("Starting money $", st.get("initial_capital", 10000), "%.0f", "w-40", min=0)
            pwk = _num("Add each week $", st.get("weekly_contribution", 0), "%.0f", "w-40", min=0)
            ppol = ui.select({"best_value": "Best value (highest expected return)",
                              "best_rank": "Best Oracle Rank", "spread": "Spread evenly",
                              "patient": "Patient: wait for strong-buy prices",
                              "reserve": "Sliding reserve: part now, the rest for strong buys"},
                             value=st.get("new_money", "best_value"), label="New money goes to").props(
                "outlined dense").classes("w-80")
        with ui.row().classes("gap-3 items-end"):
            rqw = _num("Quality's weight in the Oracle Rank", st.get("rank_quality_weight", 0.65), "%.2f", "w-60",
                       min=0, max=1, step=0.05)
            pmin = _num("Min Oracle Rank to buy", st.get("min_rank_score", 0), "%.0f", "w-48", min=0)
            pmax = _num("Max weight a buy can reach %", 100 * float(st.get("max_buy_weight", 0.25)), "%.0f", "w-56",
                        min=5, max=100)
            padds = _num("Max dip ADDs (variable style)", st.get("max_adds", 2), "%.0f", "w-48", min=0)
            paq = ui.switch("No ADD while REVIEW or Quality slipped", value=st.get("add_requires_quality", True))
            pswap = _num("Swap only after (days held)", st.get("swap_min_hold_days", 365), "%.0f", "w-52", min=0)
            ppw = _num("Patient: max weeks waiting", st.get("patient_max_weeks", 26), "%.0f", "w-52", min=0)
            prmin = _num("Reserve: spend % at an ordinary buy", 100 * float(st.get("reserve_min_deploy", 0.5)),
                         "%.0f", "w-60", min=0, max=100)
            prmax = _num("Reserve: max % of portfolio", 100 * float(st.get("reserve_max", 0.20)), "%.0f", "w-52",
                         min=0, max=100)
            prwk = _num("Reserve: spend after weeks", st.get("reserve_max_weeks", 26), "%.0f", "w-48", min=0)
        with ui.row().classes("gap-3 items-end"):
            pfund = ui.switch("When all slots are full, trim or sell clearly weaker holdings to buy a strong buy",
                              value=st.get("fund_strong", False))
            pfx = _num("Extra slots for strong buys", st.get("fund_extra_slots", 1), "%.0f", "w-48", min=0)
            pft = _num("Trim at most % of a holding", 100 * float(st.get("fund_trim_max", 0.34)), "%.0f", "w-52",
                       min=0, max=100)
            pfh = _num("Sell whole only after days held", st.get("fund_min_hold_days", 180), "%.0f", "w-56", min=0)

        # ---- 4. exit rules and warnings
        ui.label("4 · When it trims, sells or switches").classes("oe-h2 pt-2")
        ex_rules = exits.copy_rules(exits.rules_of(st))
        _exit_rules_editor(ex_rules, st, cur.locked)
        with ui.row().classes("gap-3 items-end pt-2"):
            stq = _num("Strong buy needs Quality at least", st.get("strong_quality", 80), "%.0f", "w-64", min=0,
                       max=100)
            hc = ui.switch("Business health check: skip companies slipping now; sell when the next filing confirms it",
                           value=bool(st.get("health_check")))
        with ui.row().classes("gap-6 items-center"):
            qw = ui.switch("Quarterly early warnings (10-Q)", value=st.get("quarterly_warnings", True))
            qb = ui.switch("Block new buys while a hard 10-Q warning is open",
                           value=st.get("block_buy_on_quarterly_flag", True))
            rdw = ui.switch("Extra early warnings (ROIC, margins, receivables, inventory, acquisitions, stock pay)",
                            value=st.get("deterioration_warnings", True))

        # ---- 5. risk and timing
        ui.label("5 · Risk limits, timing and alerts").classes("oe-h2 pt-2")
        with ui.row().classes("gap-3 items-end"):
            rmp = _num("Max position % (blank = off)", _pct(st.get("max_position")), "%.0f", "w-56", min=1, max=100)
            rms = _num("Max sector % (blank = off)", _pct(st.get("max_sector")), "%.0f", "w-52", min=1, max=100)
            rmc = _num("Max correlated % (blank = off)", _pct(st.get("max_correlated")), "%.0f", "w-56", min=1,
                       max=100)
            for el in (rmp, rms, rmc):
                el.props("clearable")
            rct = _num("Correlated means ≥", st.get("corr_threshold", 0.7), "%.2f", "w-40", min=0, max=1, step=0.05)
            rtc = _num("Trading cost (bps per trade)", st.get("trade_cost_bps", 0), "%.0f", "w-56", min=0)
        with ui.row().classes("gap-3 items-end"):
            rov = ui.switch("Market overlay: limit new purchases while the S&P 500 is below its 200-day average",
                            value=bool(st.get("market_overlay")))
            rocb = _num("…buy % of normal", 100 * float(st.get("overlay_caution_buy", 0.35)), "%.0f", "w-40", min=0,
                        max=100)
            rosb = _num("…when VIX > 30, buy %", 100 * float(st.get("overlay_stress_buy", 0.10)), "%.0f", "w-44",
                        min=0, max=100)
        with ui.row().classes("gap-6 items-center"):
            tmf = ui.switch("Timing filter: new buys wait while the price is below a falling 50-day average",
                            value=st.get("timing_filter", False))
            tmw = _num("Max wait (signal days)", st.get("timing_max_wait", 60), "%.0f", "w-44")
            stg = ui.switch("Buy in thirds while the price is sliding", value=st.get("stage_in_downtrend", True))
            pal = ui.switch("Price-target alerts", value=st.get("price_alerts", True))
            bnd = _num("Alert band (× average daily range)", st.get("alert_band_atr", 1.5), "%.3g", "w-56")
        ui.label("Tip: Backtest → 'Find the best number of holdings' tests N = 5…30 on your own history and can save "
                 "the winner here.").classes("oe-small")

        def collect() -> dict:
            b = copy.deepcopy(body)
            s = b["settings"]
            s.update({
                "checklist_id": int(std.value) if std.value is not None else None,
                "quality_min": float(qmin.value or 0),
                "universe_wonder_min": float(wmin.value) if wmin.value not in (None, "") else None,
                "min_market_cap": float(mcap.value or 0), "min_dollar_volume": float(dvol.value or 0),
                "include_foreign": inf.value, "universe_industries": list(inc.value or []),
                "universe_exclude_industries": list(exc.value or []),
                "hurdle": _frac(hur.value, s.get("hurdle")), "mos_gate": _frac(mos.value, s.get("mos_gate")),
                "regime_modifier": rm.value, "valuation_model": rvm.value, "price_test": ptest.value,
                "fair_pe_mult": round(float(fpm.value or 1.5), 4), "max_mos": _frac(mmo.value),
                "vd_required_return": _frac(rvr.value), "vd_growth_cap": _frac(rvg.value, 0.12),
                "predictability_mos": rpm.value,
                "portfolio_mode": pmode.value, "max_holdings": max(0, int(pn.value)) if pn.value not in (None, "") else 15, "max_buys_per_week": int(pmb.value) if pmb.value not in (None, "", 0) else None, "sizing": psz.value,
                "initial_capital": float(10000 if pcap0.value in (None, "") else pcap0.value), "weekly_contribution": float(pwk.value or 0),
                "new_money": ppol.value, "buy_rule": brule.value,
                "ranked_buy_count": max(1, int(rbn.value or 4)), "ranked_max_weight": _frac(rmw.value),
                "exit_rules": exits.copy_rules(ex_rules),
                "buy_price_rule": bpr.value, "buy_rank_by": brk.value, "buy_trend": btr.value or None, "buy_fair_mult": round(float(bfm.value or 1.0), 4),
                "buy_growth_gap": _frac(bgg.value, 0.10), "buy_min_return": _frac(bmr.value, 0.0),
                "buy_min_sales_growth": _frac(bsg.value), "buy_min_wonder": float(bwm.value) if bwm.value not in (None, "")
                else None, "rank_quality_weight": round(float(rqw.value if rqw.value is not None else
                                                                            0.65), 4),
                "min_rank_score": float(pmin.value or 0), "max_buy_weight": float(pmax.value or 25) / 100,
                "max_adds": int(padds.value or 0), "add_requires_quality": paq.value,
                "swap_min_hold_days": int(pswap.value or 0), "patient_max_weeks": int(ppw.value or 26),
                "reserve_min_deploy": _frac(prmin.value, 0.5), "reserve_max": _frac(prmax.value, 0.20),
                "reserve_max_weeks": int(prwk.value or 0), "fund_strong": pfund.value,
                "fund_extra_slots": int(pfx.value or 0), "fund_trim_max": _frac(pft.value, 0.34),
                "fund_min_hold_days": int(pfh.value or 0),
                "strong_quality": float(stq.value if stq.value is not None else 80), "health_check": hc.value,
                "quarterly_warnings": qw.value, "block_buy_on_quarterly_flag": qb.value,
                "deterioration_warnings": rdw.value,
                "max_position": _frac(rmp.value), "max_sector": _frac(rms.value), "max_correlated": _frac(rmc.value),
                "corr_threshold": round(float(rct.value or 0.7), 4), "trade_cost_bps": float(rtc.value or 0),
                "market_overlay": rov.value, "overlay_caution_buy": _frac(rocb.value, 0.35),
                "overlay_stress_buy": _frac(rosb.value, 0.10), "timing_filter": tmf.value,
                "timing_max_wait": int(tmw.value or 60), "stage_in_downtrend": stg.value, "price_alerts": pal.value,
                "alert_band_atr": float(bnd.value or 1.5)})
            return rules.apply_house_view(b)

        pv = {"seq": 0}

        async def preview():
            pv["seq"] += 1
            my = pv["seq"]
            b = collect()
            prev.text = "Live preview: counting…"
            try:
                from .pages_admin import live_preview

                res = await run.io_bound(live_preview, cur, b)
            except Exception as ex:
                prev.text = f"Live preview unavailable ({ex})"
                return
            if my != pv["seq"]:
                return
            prev.text = (f"Live preview: {res['gates']} pass the must-pass rules · {res['wonderful']} with Quality "
                         f"{b['settings']['quality_min']:.0f}+ · " + (f"~{res['buy']} in the Buy Zone (estimate)"
                                                                       if res.get("buy") is not None else
                                                                       "Buy Zone: run a backtest for this buy rule"))

        for el in (qmin, hur, mos):
            el.on_value_change(lambda e: preview())
        ui.timer(0.2, preview, once=True)

        def save():
            b = collect()
            if cur.locked:
                ui.notify("Built-in strategies are locked: use Save as", type="warning")
                return
            rescore = rules.scoring_hash(b) != rules.scoring_hash(cur.body)
            rules.update_profile(cur.id, b)
            rules.set_notes(cur.id, desc.value)
            with ui.dialog() as dlg, ui.card():
                ui.label("Saved. Replay this strategy's paper portfolio now?").classes("oe-h2")
                ui.label("It now scores on a different record length, so those scores are built first (can take a "
                         "while)." if rescore else "Company scores are reused, so this is quick.").classes("oe-small")
                with ui.row():
                    ui.button("Replay now", on_click=lambda: (dlg.close(), _launch("rebuild", {"profile_id": cur.id}))
                              ).props("unelevated")
                    ui.button("Later", on_click=dlg.close).props("flat")
            dlg.open()

        def save_as():
            with ui.dialog() as dlg, ui.card():
                nm = ui.input("New strategy name").props("outlined dense")

                def ok():
                    if not nm.value:
                        return
                    try:
                        new = rules.save_as(cur.id, nm.value.strip(), collect())
                        if not cur.locked and (desc.value or "").strip() != (cur.notes or ""):
                            rules.set_notes(new, desc.value)
                    except Exception as e:
                        ui.notify(str(e), type="warning")
                        return
                    dlg.close()
                    ui.navigate.to(f"/strategies?pid={new}")

                ui.button("Create", on_click=ok).props("unelevated")
            dlg.open()

        with ui.row():
            ui.button("Save", on_click=save).props("unelevated" + (" disable" if cur.locked else ""))
            ui.button("Save as…", on_click=save_as).props("outline")
            ui.button("Export JSON", on_click=lambda: ui.download(json.dumps(collect(), indent=1).encode(),
                                                                  f"{cur.name}.json")).props("flat")


# ===================================================================== the Oracle checklists
@ui.page("/house")
def _old_house(cid: int | None = None):
    ui.navigate.to(f"/checklists?cid={cid}" if cid else "/checklists")


@ui.page("/checklists")
def house_page(cid: int | None = None):
    with frame("Strategies"):
        cks = rules.checklists()
        app_cid = rules.app_checklist_id()
        cid = cid if cid in cks else app_cid
        hv = cks[cid]
        users = rules.users_of(cid)
        base = users[0] if users else rules.default_profile()
        body = copy.deepcopy(base.body)
        for k in rules.HOUSE_KEYS:  # the selected checklist's rules, whichever strategy is used for the preview
            body[k] = copy.deepcopy(hv[k])
        ui.label("Oracle checklists: how companies are scored").classes("oe-h1")
        ui.label("An Oracle checklist is everything about how a company is scored: the must-pass and scored rules for "
                 "standard companies, banks and insurers, their weights, the near-miss margin and the record length. "
                 "The Quality score comes from it. Each strategy uses one (Strategies → Rules → section 1); strategies on "
                 "the same checklist share its scores. The checklist used across the app (Screener, stock pages, "
                 "Today) is the primary strategy's (★). Copy a checklist to try a change on one strategy and compare "
                 "backtests before changing the one everything uses.").classes("oe-muted")
        rows = []
        for k, v in cks.items():
            us = rules.users_of(k)
            rows.append(f"<tr style='cursor:pointer;{'background:#fbf6e6;' if k == cid else ''}' "
                        f"onclick=\"location.href='/checklists?cid={k}'\"><td style='text-align:left'>"
                        f"{'★ ' if k == app_cid else ''}{v.get('name')}</td>"
                        f"<td>{'10 years' if v.get('record', 'standard') == 'standard' else '5 years (research)'}</td>"
                        f"<td style='text-align:left'>{', '.join(p.name for p in us) or '—'}</td>"
                        f"<td>v{v.get('version', 1)} · {str(v.get('updated_at', ''))[:10]}</td></tr>")
        with ui.card().classes("oe-card w-full"):
            ui.html("<table class='oe-cmp' style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr>"
                    "<th style='text-align:left'>Oracle checklist</th><th>Record</th><th style='text-align:left'>Used by"
                    "</th><th>Version</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table><style>.oe-cmp td,"
                    ".oe-cmp th{padding:5px 10px;text-align:right;border-bottom:1px solid #eef0ee}.oe-cmp th{color:#6b7280;"
                    "font-size:12px}</style>", sanitize=False).classes("w-full")
            ui.label("★ used across the app (the primary strategy's). Click a checklist to edit it.").classes("oe-small")
        ui.label(hv.get("name", "Oracle checklist")).classes("oe-h2 pt-2")
        ui.label(f"Version {hv.get('version', 1)}, last changed {str(hv.get('updated_at', ''))[:10]}. Used by "
                 f"{len(users)} strateg{'y' if len(users) == 1 else 'ies'}. Saving re-scores every company once for "
                 "them and replays the ones that run daily (allow a while).").classes("oe-small")
        prev = ui.label("").classes("text-sm font-semibold text-green-800")
        rows_ui = []
        with ui.card().classes("oe-card w-full"):
            with ui.tabs().classes("w-full").props("align=left dense") as tabs:
                t_std = ui.tab("Standard companies")
                t_bank = ui.tab("Banks")
                t_ins = ui.tab("Insurers")
            with ui.tab_panels(tabs, value=t_std).classes("w-full"):
                for tab, book in ((t_std, "rules"), (t_bank, "bank_rules"), (t_ins, "insurer_rules")):
                    with ui.tab_panel(tab).classes("p-0"):
                        with ui.row().classes("w-full oe-small font-semibold gap-2 no-wrap border-b"):
                            for w, t in [("w-56", "Rule"), ("w-36", "Mode"), ("w-28", "Floor (0)"),
                                         ("w-28", "Target (70)"), ("w-28", "Excellent (100)"), ("w-24", "Weight"),
                                         ("flex-1", "Formula")]:
                                ui.label(t).classes(w)
                        for idx, r in enumerate(body[book]):
                            with ui.row().classes("w-full items-center gap-2 no-wrap py-1 border-b border-gray-100"):
                                ui.label(r["name"]).classes("w-56 text-sm")
                                mode = ui.select(["gate", "scored", "buy_gate", "info", "off"], value=r["mode"]).props(
                                    "dense outlined").classes("w-36")
                                scale = 100 if r.get("fmt") in ("pct", "ratio") else 1
                                fl = ui.number(value=r["floor"] * scale, format="%.4g").props(
                                    "dense outlined").classes("w-28")
                                tg = ui.number(value=r["target"] * scale, format="%.4g").props(
                                    "dense outlined").classes("w-28")
                                ex = ui.number(value=r["excellent"] * scale, format="%.4g").props(
                                    "dense outlined").classes("w-28")
                                wt = ui.number(value=r.get("weight", 1.0), format="%.3g").props(
                                    "dense outlined").classes("w-24")
                                ui.label(r.get("formula", "") + (" (%)" if scale == 100 else "")).classes(
                                    "oe-small flex-1")
                                rows_ui.append((book, idx, mode, fl, tg, ex, wt, scale))
                                for el in (mode, fl, tg, ex, wt):
                                    el.on_value_change(lambda e: preview())
            r19 = next((r for r in body["rules"] if r.get("id") == "r19"), None)
            with ui.row().classes("gap-3 items-end pt-2"):
                roew = ui.number("ROE gate: no year under its floor in the last N years", format="%.0f", min=1, max=10,
                                 value=int((r19 or {}).get("min_year_window") or rules.ROE_FLOOR_WINDOW)).props(
                    "outlined dense").classes("w-96")
                nmm = ui.number("Near miss: within points of the bar", value=body["settings"].get("near_miss_margin", 5),
                                format="%.3g").props("outlined dense").classes("w-72")
                rec = ui.select({"standard": "10 years of results (7 at least)",
                                 "short": "5 years of results (research: backtests can start earlier)"},
                                value=hv.get("record", "standard"), label="Record length").props(
                    "outlined dense").classes("w-96")

        def collect() -> dict:
            b = copy.deepcopy(body)
            for book, idx, mode, fl, tg, ex, wt, scale in rows_ui:
                rr = b[book][idx]
                rr["mode"] = mode.value
                rr["floor"], rr["target"], rr["excellent"] = (round(float(fl.value or 0) / scale, 10),
                                                              round(float(tg.value or 0) / scale, 10),
                                                              round(float(ex.value or 0) / scale, 10))
                rr["weight"] = float(wt.value or 0)
            for rr in b["rules"]:
                if rr.get("id") == "r19":
                    rr["min_year_window"] = max(1, min(10, int(roew.value or rules.ROE_FLOOR_WINDOW)))
            b["settings"]["near_miss_margin"] = float(nmm.value or 0)
            b["record"] = rec.value
            return b

        pv = {"seq": 0}

        async def preview():
            pv["seq"] += 1
            my = pv["seq"]
            b = collect()
            prev.text = "Live preview: counting…"
            try:
                from .pages_admin import live_preview

                res = await run.io_bound(live_preview, base, b)
            except Exception as ex:
                prev.text = f"Live preview unavailable ({ex})"
                return
            if my != pv["seq"]:
                return
            prev.text = (f"Live preview: {res['gates']} companies pass the must-pass rules · {res['wonderful']} with "
                         f"Quality {base.settings['quality_min']:.0f}+ ({base.name}'s bar)")

        ui.timer(0.2, preview, once=True)

        def save():
            b = collect()
            if not rules.house_changed(b, cid):
                ui.notify("Nothing changed.", type="info")
                return
            if b.get("record") == "short" and any(p.id == rules.primary_id() for p in users):
                ui.notify("The primary strategy uses this checklist, and it can't be on a 5-year record.",
                          type="warning")
                return
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Change {hv.get('name')}?").classes("oe-h2")
                ui.label(f"Every company is re-scored once for the {len(users)} strateg"
                         f"{'y' if len(users) == 1 else 'ies'} on it, and those that run daily are replayed. Backtests "
                         "keep their old results until recalculated.").classes("text-sm")

                def ok():
                    v = rules.set_house_view(b, cid)
                    dlg.close()
                    _launch("rebuild_all")
                    ui.notify(f"{hv.get('name')} v{v} saved; rebuilding.", type="positive")
                with ui.row():
                    ui.button("Save and rebuild", on_click=ok).props("unelevated")
                    ui.button("Cancel", on_click=dlg.close).props("flat")
            dlg.open()

        def restore():
            d = rules.house_defaults()
            if not rules.house_changed(d, cid):
                ui.notify("This checklist already has the shipped rules.", type="info")
                return
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Restore {hv.get('name')} to the shipped rules?").classes("oe-h2")
                ui.label("Every rule, weight, the bank and insurer rules and the near-miss margin go back to the "
                         "defaults the app ships with (rules/oracle_classic.yaml); the record length stays. Every company "
                         "is re-scored once for the strategies on it. Your strategies' own settings don't change.").classes(
                    "text-sm")

                def ok():
                    v = rules.set_house_view(d, cid)
                    dlg.close()
                    _launch("rebuild_all")
                    ui.notify(f"{hv.get('name')} v{v}: defaults restored; rebuilding.", type="positive")
                    ui.navigate.reload()
                with ui.row():
                    ui.button("Restore defaults and rebuild", on_click=ok).props("unelevated")
                    ui.button("Cancel", on_click=dlg.close).props("flat")
            dlg.open()

        def save_copy():
            with ui.dialog() as dlg, ui.card():
                ui.label("Save as a new Oracle checklist").classes("oe-h2")
                nm = ui.input("Name", value=f"{hv.get('name')} (copy)").props("outlined dense").classes("w-96")
                ui.label("The copy gets the rules as they are on screen now. No strategy uses it until you pick it in a "
                         "strategy's section 1, so nothing is re-scored yet.").classes("oe-small")

                def ok():
                    try:
                        nid = rules.copy_checklist(cid, nm.value)
                    except ValueError as e:
                        ui.notify(str(e), type="warning")
                        return
                    b = collect()
                    if rules.house_changed(b, nid):
                        rules.set_house_view(b, nid)
                    dlg.close()
                    ui.navigate.to(f"/checklists?cid={nid}")
                with ui.row():
                    ui.button("Save copy", on_click=ok).props("unelevated")
                    ui.button("Cancel", on_click=dlg.close).props("flat")
            dlg.open()

        def rename():
            with ui.dialog() as dlg, ui.card():
                nm = ui.input("New name", value=hv.get("name")).props("outlined dense").classes("w-96")

                def ok():
                    try:
                        rules.rename_checklist(cid, nm.value)
                    except ValueError as e:
                        ui.notify(str(e), type="warning")
                        return
                    dlg.close()
                    ui.navigate.reload()
                with ui.row():
                    ui.button("Rename", on_click=ok).props("unelevated")
                    ui.button("Cancel", on_click=dlg.close).props("flat")
            dlg.open()

        def delete():
            try:
                rules.delete_checklist(cid)
            except ValueError as e:
                ui.notify(str(e), type="warning")
                return
            ui.navigate.to("/checklists")

        with ui.row().classes("pt-2"):
            ui.button("Save", on_click=save).props("unelevated")
            ui.button("Save as new checklist…", icon="content_copy", on_click=save_copy).props("outline")
            ui.button("Rename", icon="edit", on_click=rename).props("flat")
            ui.button("Restore defaults", icon="restart_alt", on_click=restore).props("flat")
            ui.button("Delete", icon="delete", on_click=delete).props("flat color=negative").tooltip(
                "Only a checklist no strategy uses can be deleted")
            ui.button("Back to strategies", on_click=lambda: ui.navigate.to("/strategies")).props("flat")

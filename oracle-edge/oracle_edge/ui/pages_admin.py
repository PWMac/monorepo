"""Setup wizard, Settings, Data sources, Profiles editor, Backtests."""

from __future__ import annotations

import copy
import json
import re
from datetime import date

from nicegui import ui

from .. import backup, db, emailer, jobs, paths, rules, scheduler, secrets
from ..sources import prices
from ..tradecal import local_run_time
from . import data
from .layout import empty, frame, kpi
from .theme import pct


def _launch(kind, args=None, then=None):
    try:
        jobs.launch(kind, args or {})
        ui.notify("Started. Progress shows in the top bar.", type="info")
        if then:
            then()
    except Exception as e:
        ui.notify(str(e), type="warning")


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ===================================================================== SETUP WIZARD
@ui.page("/setup")
def setup_page():
    with frame("Setup"):
        ui.label("Welcome to Oracle Edge").classes("oe-h1")
        ui.label("Seven quick steps. Everything is saved locally and can be changed later in Settings.").classes(
            "oe-muted")
        st = {"name": "", "email": ""}
        ua = db.cfg("sec_user_agent", "") or ""
        if ua:
            parts = ua.rsplit(" ", 1)
            st["name"], st["email"] = (parts[0], parts[1]) if len(parts) == 2 else (ua, "")
        with ui.stepper().props("vertical animated").classes("w-full oe-card") as stepper:
            with ui.step("What this is"):
                ui.markdown(
                    "Oracle Edge screens U.S.-listed companies the way the Oracle would: **10 years of evidence**, "
                    "**hard gates**, a **Quality** score, two conservative **valuation models**, and a ranked "
                    "**Buy Zone**. It logs BUY/SELL signals for a model portfolio and tracks how they performed.\n\n"
                    "*It's a research and signal-tracking tool, not financial advice. Signals are never sent to a "
                    "broker.*")
                ack = ui.checkbox("I understand")
                with ui.stepper_navigation():
                    ui.button("Next", on_click=lambda: stepper.next() if ack.value else ui.notify(
                        "Please tick the box", type="warning")).props("unelevated")
            with ui.step("SEC identity"):
                ui.label("The SEC requires every program that downloads its data to identify itself with a name "
                         "and email. They're sent only to sec.gov and stored on this PC.").classes("oe-muted")
                n = ui.input("Your name", value=st["name"]).props("outlined dense").classes("w-80")
                e = ui.input("Your email", value=st["email"]).props("outlined dense").classes("w-80")

                def save_sec():
                    if not n.value or not EMAIL_RE.match(e.value or ""):
                        ui.notify("Enter a name and a valid email", type="warning")
                        return
                    db.set_cfg("sec_user_agent", f"{n.value.strip()} {e.value.strip()}")
                    stepper.next()

                with ui.stepper_navigation():
                    ui.button("Next", on_click=save_sec).props("unelevated")
                    ui.button("Back", on_click=stepper.previous).props("flat")
            with ui.step("Price data"):
                ui.label("Starts free. Paid sources are a one-click upgrade later (Data page).").classes("oe-muted")
                order = db.cfg("price_sources", prices.DEFAULT_ORDER)
                for nme in order:
                    ui.label(f"• {prices.REGISTRY[nme].label}").classes("text-sm")
                res = ui.label("").classes("oe-small")

                def test():
                    res.text = "Testing…"
                    out = []
                    for s in prices.ordered_sources():
                        h = s.health_check()
                        out.append(f"{s.label}: {'OK' if h.ok else 'FAILED'} · {h.detail}")
                    res.text = " | ".join(out)

                with ui.stepper_navigation():
                    ui.button("Next", on_click=stepper.next).props("unelevated")
                    ui.button("Test connection", on_click=test).props("outline")
                    ui.button("Back", on_click=stepper.previous).props("flat")
            with ui.step("Universe"):
                ex = ui.select(["Nasdaq", "NYSE", "CBOE", "OTC"], multiple=True,
                               value=db.cfg("exchanges", ["Nasdaq", "NYSE", "CBOE"]), label="Exchanges").props(
                    "outlined dense use-chips").classes("w-96")
                ui.label("Small and foreign companies are included (Oracle-wide). Size floors and other rules "
                         "live in the profile (Profiles page).").classes("oe-small")
                bf = ui.input("Backfill history from", value=db.cfg("backfill_start", "2019-01-01")).props(
                    "outlined dense").classes("w-56")
                with ui.stepper_navigation():
                    ui.button("Next", on_click=lambda: (db.set_cfg("exchanges", ex.value),
                                                        db.set_cfg("backfill_start", bf.value), stepper.next())
                              ).props("unelevated")
                    ui.button("Back", on_click=stepper.previous).props("flat")
            with ui.step("Folders & backup"):
                ui.label(f"App folder: {paths.APP_DIR}").classes("text-sm")
                bdir = ui.input("Daily backup folder", value=str(backup.backup_dir())).props("outlined dense").classes(
                    "w-[520px]")
                bmsg = ui.label("").classes("oe-small")

                def test_backup():
                    db.set_cfg("backup_dir", bdir.value)
                    try:
                        bmsg.text = "Backup written: " + backup.run_backup()
                    except Exception as ex_:
                        bmsg.text = f"Couldn't write: {ex_}"

                with ui.stepper_navigation():
                    ui.button("Next", on_click=lambda: (db.set_cfg("backup_dir", bdir.value), stepper.next())).props(
                        "unelevated")
                    ui.button("Test backup", on_click=test_backup).props("outline")
                    ui.button("Back", on_click=stepper.previous).props("flat")
            with ui.step("Schedule & email"):
                ui.label(f"Daily update after the close: 6:00 PM New York = {local_run_time()} your time today. "
                         "Runs on weekdays; missed days are caught up next time.").classes("text-sm")
                sch = ui.label("").classes("oe-small")

                def reg():
                    try:
                        sch.text = scheduler.register()
                    except Exception as ex_:
                        sch.text = f"Not scheduled: {ex_}"

                ui.button("Create the Windows scheduled task", icon="schedule", on_click=reg).props("outline")
                ui.separator()
                _email_form(compact=True)
                with ui.stepper_navigation():
                    ui.button("Next", on_click=stepper.next).props("unelevated")
                    ui.button("Back", on_click=stepper.previous).props("flat")
            with ui.step("Start"):
                ui.markdown(
                    "The first load downloads the SEC's financial-statement archive (**a few GB, one time**), scores "
                    "every company, downloads prices for the shortlist, and replays **"
                    f"{db.cfg('backfill_start', '2019-01-01')} → today** so you see signals and performance from day "
                    "one. Expect roughly **30–90 minutes** depending on your connection. You can keep using the app "
                    "while it runs; progress shows in the top bar.")
                done = db.cfg("initial_load_done")
                if done:
                    ui.label(f"First load already completed {done[:16]}.").classes("text-green-800")

                def start():
                    if not db.cfg("sec_user_agent"):
                        ui.notify("Enter your SEC identity first", type="warning")
                        return
                    _launch("initial_load", {}, lambda: ui.navigate.to("/data"))

                with ui.stepper_navigation():
                    ui.button("Start first load", icon="rocket_launch", on_click=start).props("unelevated")
                    ui.button("Back", on_click=stepper.previous).props("flat")


def _email_form(compact=False):
    s = emailer.settings()
    ui.label("Email alerts").classes("oe-h2")
    en = ui.switch("Send email alerts", value=s["enabled"])
    with ui.row().classes("gap-3 items-end"):
        preset = ui.select({k: v["label"] for k, v in emailer.PRESETS.items()}, value=s["preset"], label="Provider"
                           ).props("outlined dense").classes("w-64")
        host = ui.input("SMTP server", value=s["host"]).props("outlined dense").classes("w-56")
        port = ui.number("Port", value=s["port"], format="%d").props("outlined dense").classes("w-24")
        sec_ = ui.select({"starttls": "STARTTLS", "ssl": "SSL"}, value=s["security"], label="Security").props(
            "outlined dense").classes("w-32")
    helptext = ui.label(emailer.PRESETS[s["preset"]]["help"]).classes("oe-small")

    def on_preset(e):
        p = emailer.PRESETS[e.value]
        if p["host"]:
            host.value, port.value, sec_.value = p["host"], p["port"], p["security"]
        helptext.text = p["help"]

    preset.on_value_change(on_preset)

    def on_port(e):
        if e.value in (465, 465.0):
            sec_.value = "ssl"
        elif e.value in (25, 587, 2525):
            sec_.value = "starttls"

    port.on_value_change(on_port)
    with ui.row().classes("gap-3 items-end"):
        user = ui.input("Sign-in address (full email)", value=s["username"]).props("outlined dense").classes("w-80")
        pw = ui.input("App password", password=True, password_toggle_button=True,
                      placeholder="•••• (saved)" if secrets.get("smtp_password") else "16 characters").props(
            "outlined dense").classes("w-64")
        fromn = ui.input("From name", value=s["from_name"]).props("outlined dense").classes("w-44")
    to = ui.input("Send to (comma-separated)", value=", ".join(s["to"])).props("outlined dense").classes("w-[560px]")
    with ui.row().classes("gap-4"):
        a = ui.checkbox("Actions (BUY/SELL/…)", value=s["send_actions"])
        w = ui.checkbox("Weekly summary", value=s["send_weekly"])
        f = ui.checkbox("Run failures", value=s["send_failures"])
        pa = ui.checkbox("Price-target alerts", value=s.get("send_price_alerts", True))
    msg = ui.label("").classes("oe-small")

    def save(show=True):
        emailer.save_settings({"enabled": en.value, "preset": preset.value, "host": host.value, "port": int(port.value or 587),
                               "security": sec_.value, "username": (user.value or "").strip(),
                               "from_name": fromn.value or "Oracle Edge",
                               "to": [x.strip() for x in (to.value or "").split(",") if x.strip()],
                               "send_actions": a.value, "send_weekly": w.value, "send_failures": f.value,
                               "send_price_alerts": pa.value},
                              pw.value or None)
        pw.value = ""
        if show:
            ui.notify("Email settings saved", type="positive")

    async def test():
        from nicegui import run

        save(False)
        msg.text = "Sending…"
        try:
            msg.text = await run.io_bound(emailer.send_test)
            msg.classes(replace="oe-small text-green-800")
        except Exception as e:
            msg.text = str(e)
            msg.classes(replace="oe-small text-red-700")

    diag = {}

    async def check():
        from nicegui import run

        save(False)
        box = diag["box"]
        box.clear()
        with box:
            ui.label("Checking each step (up to a minute)…").classes("oe-small")
        steps = await run.io_bound(emailer.diagnose)
        box.clear()
        with box:
            for st_ in steps:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("check_circle" if st_["ok"] else "cancel").classes(
                        "text-green-700" if st_["ok"] else "text-red-700")
                    ui.label(f"{st_['step']}: {st_['detail']}").classes("oe-small")

    with ui.row():
        ui.button("Save email settings", on_click=save).props("unelevated" if not compact else "outline")
        ui.button("Send test email", icon="send", on_click=test).props("outline")
        ui.button("Check connection step by step", icon="troubleshoot", on_click=check).props("flat")
    diag["box"] = ui.column().classes("gap-0 w-full")
    if secrets.uses_fallback():
        ui.label("⚠ No OS keychain found: the password is stored in data/secrets.json on this PC.").classes("oe-small")


# ===================================================================== SETTINGS
def _market_temp_card():
    from .. import btcsignal

    s = btcsignal.settings()
    with ui.card().classes("oe-card w-full"):
        ui.label("Market temperature").classes("oe-h2")
        ui.label("Two readings, tracked daily: the Buffett Indicator (market cap ÷ GDP) and the Bitcoin early "
                 "warning. Neither is a hard stop: when either reaches an extreme, Today shows a recommendation "
                 "and the daily email includes a market alert.").classes("oe-small")
        with ui.row().classes("items-end gap-3"):
            lb = ui.number("Bitcoin window (trading days)", value=s["lookback"], min=5, max=120, format="%.0f").props(
                "outlined dense").classes("w-56")
            hz = ui.number("QQQ horizon judged (trading days)", value=s["horizon"], min=5, max=120,
                           format="%.0f").props("outlined dense").classes("w-60")
            bp = ui.number("Warning: worst % of history", value=s["bear_pct"], min=5, max=40, format="%.0f").props(
                "outlined dense").classes("w-56")
            up = ui.number("Tailwind: best % of history", value=100 - s["bull_pct"], min=5, max=40,
                           format="%.0f").props("outlined dense").classes("w-56")
            on = ui.switch("Market alerts in the daily email", value=bool(s.get("alerts", True)))

        def save():
            btcsignal.save_settings({"lookback": int(lb.value or 20), "horizon": int(hz.value or 20),
                                     "bear_pct": int(bp.value or 20), "bull_pct": 100 - int(up.value or 20),
                                     "alerts": bool(on.value)})
            ui.notify("Saved. The Market page and Today use the new window right away.", type="positive")

        ui.button("Save", on_click=save).props("outline")
        ui.label("The research behind the defaults found similar results for windows of about 15–30 days; the effect "
                 "fades below about 10 and beyond about 40.").classes("oe-small")


@ui.page("/settings")
def settings_page():
    with frame("Settings"):
        ui.label("Settings").classes("oe-h1")
        _market_temp_card()
        with ui.card().classes("oe-card w-full"):
            ui.label("SEC identity").classes("oe-h2")
            ua = ui.input("Name and email (sent to sec.gov)", value=db.cfg("sec_user_agent", "")).props(
                "outlined dense").classes("w-[520px]")
            ui.button("Save", on_click=lambda: (db.set_cfg("sec_user_agent", ua.value), ui.notify("Saved"))).props(
                "outline")
        with ui.card().classes("oe-card w-full"):
            ui.label("Daily schedule").classes("oe-h2")
            t = ui.input("Run time (New York, 24h)", value=db.cfg("run_time_et", "18:00")).props(
                "outlined dense").classes("w-48")
            stl = ui.label("").classes("text-sm")

            def show():
                s = scheduler.status()
                if s["exists"]:
                    stl.text = (f"✔ Windows task '{scheduler.TASK_NAME}' found (Task Scheduler → Task Scheduler "
                                f"Library). Next run: {s.get('next_run') or '?'}."
                                + (" ⚠ The app folder moved: click Save & (re)register." if s.get("moved") else ""))
                    stl.style("color:#1f6f4a")
                else:
                    stl.text = (f"✖ No '{scheduler.TASK_NAME}' task in Windows yet"
                                + (f" (last attempt: {s['error']})" if s.get("error") else "")
                                + f". Today the run would be at {local_run_time()} your time.")
                    stl.style("color:#a33a2b")

            def reg():
                db.set_cfg("run_time_et", t.value)
                try:
                    ui.notify(scheduler.register(), type="positive", multi_line=True, timeout=8000)
                except Exception as e:
                    ui.notify(str(e), type="negative", multi_line=True, timeout=0, close_button=True)
                show()

            def unreg():
                scheduler.unregister()
                ui.notify("Task removed")
                show()

            show()
            with ui.row():
                ui.button("Save & (re)register task", on_click=reg).props("outline")
                ui.button("Open Task Scheduler", icon="open_in_new",
                          on_click=scheduler.open_task_scheduler).props("flat")
                ui.button("Remove task", on_click=unreg).props("flat")
                ui.button("Run daily update now", on_click=lambda: _launch("daily", {"force": True})).props("flat")
        with ui.card().classes("oe-card w-full"):
            _email_form()
        with ui.card().classes("oe-card w-full"):
            ui.label("Model portfolio").classes("oe-h2")
            ui.label("How many stocks to hold, how to size them and how often to add are now part of each profile "
                     "(Profiles → Portfolio), so backtests can test them. Backtest → 'Find the best number of "
                     "holdings' finds the best size on your own history.").classes("text-sm")
            ui.button("Open Profiles", on_click=lambda: ui.navigate.to("/profiles")).props("flat")
        with ui.card().classes("oe-card w-full"):
            ui.label("Backups").classes("oe-h2")
            bdir = ui.input("Backup folder", value=str(backup.backup_dir())).props("outlined dense").classes("w-[560px]")
            r = backup.retention()
            ui.label("One backup a day, stored once. Older ones are thinned out: keep the last N days, the newest of "
                     "each of the last N weeks and months, and one per year (Yearly 0 = keep every year).").classes(
                "oe-small")
            with ui.row():
                rd = ui.number("Daily", value=r["daily"], format="%d", min=1).props("outlined dense").classes("w-24")
                rw = ui.number("Weekly", value=r["weekly"], format="%d", min=0).props("outlined dense").classes("w-24")
                rm = ui.number("Monthly", value=r["monthly"], format="%d", min=0).props("outlined dense").classes("w-24")
                ry = ui.number("Yearly", value=r["yearly"], format="%d", min=0).props("outlined dense").classes("w-24")
            lb = db.cfg("last_backup", {}) or {}
            ui.label(f"Last backup: {lb.get('at', 'never')} {lb.get('path', '')}").classes("oe-small")

            def save_b():
                db.set_cfg("backup_dir", bdir.value)
                db.set_cfg("backup_retention", {"daily": max(int(rd.value or 7), 1), "weekly": int(rw.value or 0),
                                                "monthly": int(rm.value or 0), "yearly": int(ry.value or 0)})
                ui.notify("Saved")

            def restore_dialog():
                items = backup.list_backups()
                with ui.dialog() as dlg, ui.card().classes("w-[640px]"):
                    ui.label("Restore a backup").classes("oe-h2")
                    ui.label("Your current database is kept as a safety copy first. Restart the app afterwards."
                             ).classes("oe-small")
                    if not items:
                        ui.label("No backups found in the folder.").classes("oe-muted")
                    for it in items[:40]:
                        with ui.row().classes("w-full justify-between items-center"):
                            ui.label(f"{it['day']} · {it['name']} · {it['size_mb']} MB"
                                     + (f" · old {it['kind']} folder" if it["kind"] != "backup" else ""))

                            def do(it=it):
                                safety = backup.restore(it["path"])
                                ui.notify(f"Restored. Safety copy: {safety}. Please restart Oracle Edge.",
                                          type="positive", timeout=0, close_button="OK")
                                dlg.close()

                            ui.button("Restore", on_click=do).props("outline dense")
                dlg.open()

            with ui.row():
                ui.button("Save", on_click=save_b).props("outline")
                ui.button("Back up now", icon="backup", on_click=lambda: _launch("backup")).props("unelevated")
                ui.button("Restore…", icon="restore", on_click=restore_dialog).props("flat")
        with ui.card().classes("oe-card w-full"):
            ui.label("Setup").classes("oe-h2")
            ui.button("Open setup wizard", on_click=lambda: ui.navigate.to("/setup")).props("flat")
            ui.label(f"Credentials store: {secrets.backend_name()} · App folder: {paths.APP_DIR} · Database: "
                     f"{paths.DB_PATH}").classes("oe-small")


# ===================================================================== DATA
@ui.page("/data")
def data_page():
    with frame("Data"):
        h = data.data_health()
        ui.label("Data sources & Data Doctor").classes("oe-h1")
        j = jobs.running()
        if j:
            with ui.card().classes("oe-card w-full"):
                ui.label(f"Running: {j['kind']} · {j.get('step') or ''}").classes("font-semibold")
                bar = ui.linear_progress(value=j["progress"], show_value=False).classes("w-full")
                lab = ui.label(j.get("message") or "").classes("oe-small")

                def tick():
                    jj = jobs.running()
                    if jj:
                        bar.value = jj["progress"]
                        lab.text = f"{jj.get('step') or ''} · {jj.get('message') or ''}"

                ui.timer(2, tick)
        last = h["last_daily"] or {}
        with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
            kpi(f"{h['companies']:,}", "Companies in universe", f"{h['with_facts']:,} with SEC financials")
            kpi(f"{h['price_tickers']:,}", "Tickers with prices", f"latest {h['last_price'] or '—'}")
            kpi((last.get("through") or "—"), "Results through", f"{last.get('signals', 0)} new signals last run")
            lb = h["last_backup"] or {}
            kpi((lb.get("at") or "never")[:10], "Last backup", (lb.get("path") or "")[-40:])
        with ui.card().classes("oe-card w-full"):
            ui.label("Price sources (tried in order, automatic fallback)").classes("oe-h2")
            order = list(db.cfg("price_sources", prices.DEFAULT_ORDER))
            box = ui.column().classes("w-full")

            def render():
                box.clear()
                with box:
                    for i, name in enumerate(order):
                        src = prices.REGISTRY[name]
                        with ui.row().classes("w-full items-center gap-3 oe-action"):
                            ui.label(f"{i + 1}.").classes("w-6")
                            ui.label(src.label).classes("flex-1")
                            ui.button(icon="arrow_upward", on_click=lambda i=i: move(i, -1)).props("flat dense round")
                            ui.button(icon="arrow_downward", on_click=lambda i=i: move(i, 1)).props("flat dense round")
                            ui.button(icon="close", on_click=lambda n=name: (order.remove(n), render())).props(
                                "flat dense round")
                    missing = [n for n in prices.REGISTRY if n not in order]
                    if missing:
                        ui.select({n: prices.REGISTRY[n].label for n in missing}, label="Add source",
                                  on_change=lambda e: (order.append(e.value), render())).props(
                            "outlined dense").classes("w-80")

            def move(i, d):
                k = i + d
                if 0 <= k < len(order):
                    order[i], order[k] = order[k], order[i]
                    render()

            render()
            with ui.row().classes("items-end gap-3"):
                tk = ui.input("Tiingo key", password=True, placeholder="saved" if secrets.get("tiingo_key") else "").props(
                    "outlined dense").classes("w-64")
                ek = ui.input("EODHD key", password=True, placeholder="saved" if secrets.get("eodhd_key") else "").props(
                    "outlined dense").classes("w-64")
            out = ui.label("").classes("oe-small")

            def save():
                db.set_cfg("price_sources", order)
                if tk.value:
                    secrets.set("tiingo_key", tk.value.strip())
                if ek.value:
                    secrets.set("eodhd_key", ek.value.strip())
                ui.notify("Saved. New order applies to the next price update.", type="positive")

            def test_all():
                res = []
                for name in order:
                    hh = prices.REGISTRY[name]().health_check()
                    res.append(f"{name}: {'OK' if hh.ok else 'FAILED'} ({hh.detail})")
                out.text = " | ".join(res)

            with ui.row():
                ui.button("Save", on_click=save).props("unelevated")
                ui.button("Test sources", on_click=test_all).props("outline")
            ui.label("Rows by source: " + ", ".join(f"{r['source']}: {r['n']:,} (to {r['d']})" for r in h["sources"])
                     ).classes("oe-small")
        with ui.row().classes("w-full gap-4 no-wrap items-start"):
            with ui.card().classes("oe-card flex-1"):
                ui.label("Recent problems").classes("oe-h2")
                if not h["errors"]:
                    ui.label("None. 👍").classes("oe-muted")
                for e in h["errors"][:20]:
                    ui.label(f"{e['ts'][:16]} · {e['source']} · {e['kind']} · {e['detail'][:160]}").classes("oe-small")
                miss = (h["last_prices"] or {}).get("missing") or []
                if miss:
                    ui.label(f"Tickers with no price from any source ({len(miss)}): {', '.join(miss[:40])}").classes(
                        "oe-small mt-2")
            with ui.card().classes("oe-card flex-1"):
                ui.label("Coverage gaps").classes("oe-h2")
                ui.label("Companies missing 3+ checks the data should support (improve concept_map.yaml to fix). "
                         "Not counted: gross-margin checks for companies with no cost-of-sales line, and the $1 test "
                         "for companies off the shortlist (no prices downloaded).").classes("oe-small")
                gaps = data.coverage_gaps(25)
                for g in gaps:
                    ui.label(f"{g['ticker']} · {g['missing']}").classes("oe-small")
                if not gaps:
                    ui.label("No big gaps.").classes("oe-muted")
        fx = db.rows("""SELECT ticker, name, currency, adr_ratio, adr_source FROM securities
                        WHERE is_foreign=1 AND EXISTS (SELECT 1 FROM snapshots sn WHERE sn.cik=securities.cik AND sn.gates_passed=1)
                        ORDER BY (adr_ratio IS NULL) DESC, ticker""")
        with ui.card().classes("oe-card w-full"):
            ui.label("Foreign filers on the shortlist").classes("oe-h2")
            ui.label("Valued in US$ per traded share: reporting-currency figures × Fed exchange rate × ADR ratio. "
                     "Ratios are read from each company's annual report; fix any wrong one on its stock page."
                     ).classes("oe-small")
            if not fx:
                ui.label("None right now.").classes("oe-muted")
            for r in fx[:40]:
                with ui.row().classes("w-full items-center gap-3"):
                    ui.link(r["ticker"], f"/stock/{r['ticker']}").classes("font-bold w-16")
                    ui.label((r["name"] or "")[:34]).classes("w-64 text-sm")
                    ui.label(r["currency"] or "").classes("w-12 text-sm")
                    ui.label(f"{r['adr_ratio']:g} per ADS" if r["adr_ratio"] else "ratio unknown ⚠").classes(
                        "w-32 text-sm" + ("" if r["adr_ratio"] else " text-amber-700"))
                    ui.label((r["adr_source"] or "not checked")[:110]).classes("oe-small flex-1")
        with ui.card().classes("oe-card w-full"):
            ui.label("Maintenance").classes("oe-h2")
            from ..pipeline import resumable_initial_load

            stopped = None if jobs.running() else resumable_initial_load()
            if stopped:
                ui.label(f"First-time load #{stopped['id']} stopped after the SEC statements were read in. "
                         "Continue picks up from there (scoring, prices, history replay) and skips the long "
                         "download and read.").classes("oe-small text-amber-800")
            with ui.row():
                if stopped:
                    ui.button("Continue first-time load", icon="play_arrow",
                              on_click=lambda: _launch("initial_load", {"resume": True})).props("unelevated")
                ui.button("Re-run first-time load", on_click=lambda: _launch("initial_load")).props("outline")
                ui.button("Run daily update now", on_click=lambda: _launch("daily", {"force": True})).props("outline")
            jl = db.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 8")
            for r in jl:
                ui.label(f"#{r['id']} {r['kind']} · {r['status']} · {(r['started_at'] or '')[:16]} → "
                         f"{(r['finished_at'] or '')[:16]} · {(r['message'] or r['error'] or '')[:160]}").classes(
                    "oe-small")


# ===================================================================== PROFILES
def _pct0(v):
    return None if v in (None, "") else round(100 * float(v), 4)


@ui.page("/profiles")
def profiles_page(pid: int | None = None):
    with frame("Profiles"):
        profs = rules.list_profiles()
        cur = rules.get_profile(pid) if pid else rules.default_profile()
        ui.label("Filter profiles").classes("oe-h1")
        ui.label("A profile is a complete rule set: which businesses qualify, what price to pay, and how the "
                 "portfolio is run. Built-in profiles (🔒) are locked so they always exist as references: edit a "
                 "copy with 'Save as'. Active profiles are scored every day and have their own history on "
                 "Performance. Click a card to open it.").classes("oe-muted")
        with ui.element("div").classes("w-full grid gap-3").style(
                "grid-template-columns: repeat(auto-fill, minmax(320px, 1fr))"):
            for p in profs:
                par = rules.parent_of(p.id)
                diffs = rules.differences(p.body, par.body) if par else []
                sel = p.id == cur.id
                with ui.card().classes("oe-card cursor-pointer gap-1").style(
                        "outline: 2px solid #C9A227" if sel else "").on(
                        "click", lambda p=p: ui.navigate.to(f"/profiles?pid={p.id}")):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.label(p.name).classes("font-semibold")
                        if p.locked:
                            ui.icon("lock", size="xs").classes("text-gray-500").tooltip("Built-in, locked")
                    if p.notes:
                        ui.label(p.notes).classes("text-sm")
                    if par:
                        ui.label(f"Copied from {par.name}: " + ("; ".join(diffs) if diffs else "no changes yet")
                                 + ".").classes("text-sm" + ("" if not p.notes else " oe-small"))
                    ui.label(rules.describe(p.body)).classes("oe-small")
                    sw = ui.switch("Active (scored daily)", value=p.active).on("click.stop", lambda: None)
                    sw.on_value_change(lambda e, p=p: _toggle_active(p, e.value))
        body = copy.deepcopy(cur.body)
        st = body["settings"]
        with ui.card().classes("oe-card w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(f"{cur.name} · v{cur.version}{' · locked' if cur.locked else ''}").classes("oe-h2")
                prev = ui.label("").classes("text-sm font-semibold text-green-800")
            desc = ui.input("Description: what this profile is for (blank = describe it from its settings)",
                            value=cur.notes).props("outlined dense autogrow" + (" readonly" if cur.locked else "")
                                                   ).classes("w-full")
            with ui.row().classes("gap-3 items-end"):
                fields = {}
                for k, label, fmt in [("hurdle", "Hurdle (exp. return)", "%"), ("mos_gate", "Margin of safety", "%"),
                                      ("quality_min", "Quality ≥", ""), ("lookback_years", "Lookback yrs", ""),
                                      ("min_years", "Min yrs", ""), ("rank_quality_weight", "Quality weight in rank", ""),
                                      ("min_market_cap", "Min market cap $", ""),
                                      ("min_dollar_volume", "Min $ volume/day", "")]:
                    v = st[k] * 100 if fmt == "%" else st[k]
                    fmt_s = "%.0f" if k in ("min_market_cap", "min_dollar_volume") else "%.4g"
                    fields[k] = (ui.number(label, value=v, format=fmt_s).props("outlined dense").classes("w-44"), fmt)
                rm = ui.switch("Raise MOS when market is expensive", value=st.get("regime_modifier", False))
                inf = ui.switch("Include foreign filers", value=st.get("include_foreign", True))
            rows_ui = []
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
            with ui.row().classes("gap-6 items-center"):
                qw = ui.switch("Quarterly early warnings (10-Q)", value=st.get("quarterly_warnings", True))
                qb = ui.switch("Block new buys while a hard 10-Q warning is open",
                               value=st.get("block_buy_on_quarterly_flag", True))
            r19 = next((r for r in body["rules"] if r.get("id") == "r19"), None)
            with ui.row().classes("gap-3 items-end"):
                roew = ui.number("ROE gate: no year under its floor in the last N years", format="%.0f", min=1, max=10,
                                 value=int((r19 or {}).get("min_year_window") or rules.ROE_FLOOR_WINDOW)).props(
                    "outlined dense").classes("w-96")
                ui.label(f"Floor {((r19 or {}).get('min_year') or 0.10) * 100:.0f}%. 5 = default (tested: a weak year "
                         "6–10 years back didn't hurt the next 3 years; one in the last 3 did). 10 = the whole "
                         "record.").classes("oe-small")
            ui.label("Value-trap guards and price test").classes("oe-h2 pt-2")
            with ui.row().classes("gap-3 items-end"):
                hc = ui.switch("Business health check: skip companies whose sales, profits or return on equity are "
                               "slipping now; sell when the next filing confirms a bad slide",
                               value=bool(st.get("health_check")))
                ptest = ui.select({"dcf": "Margin of safety + expected return (DCF)",
                                   "fair_pe": "Fair price: P/E vs the company's own usual P/E"},
                                  value=st.get("price_test", "dcf"), label="Price test").props(
                    "outlined dense").classes("w-96")
                fpm = ui.number("Fair P/E: at most x usual", value=float(st.get("fair_pe_mult", 1.5)), format="%.2f",
                                min=0.5, max=3, step=0.1).props("outlined dense").classes("w-48")
                mmo = ui.number("Too cheap to trust: no buy more than % below value (blank = off)",
                                value=_pct0(st.get("max_mos")), format="%.0f", min=0, max=95).props(
                    "outlined dense clearable").classes("w-[420px]")
            ui.label("Portfolio").classes("oe-h2 pt-2")
            with ui.row().classes("gap-3 items-end"):
                pmode = ui.select({"focused": "Focused: hold only the best N", "variable": "Variable (old style)"},
                                  value=st.get("portfolio_mode", "focused"), label="Portfolio style").props(
                    "outlined dense").classes("w-64")
                pn = ui.number("Holdings (N)", value=st.get("max_holdings", 15), format="%.0f", min=1).props(
                    "outlined dense").classes("w-32")
                psz = ui.select({"equal": "Equal slices", "conviction": "Conviction (higher Rank, bigger slice)",
                                 "value_quality": "Better business at a better price gets more",
                                 "size": "Bigger company gets more (like an index)"},
                                value=st.get("sizing", "equal"), label="Sizing").props("outlined dense").classes("w-72")
                padds = ui.number("Max dip ADDs (variable style)", value=st.get("max_adds", 2), format="%.0f", min=0).props(
                    "outlined dense").classes("w-40")
                pswap = ui.number("Swap only after (days held)", value=st.get("swap_min_hold_days", 365),
                                  format="%.0f", min=0).props("outlined dense").classes("w-52")
                pcap0 = ui.number("Starting money $", value=st.get("initial_capital", 10000), format="%.0f",
                                  min=0).props("outlined dense").classes("w-40")
                pwk = ui.number("Add each week $", value=st.get("weekly_contribution", 0), format="%.0f", min=0).props(
                    "outlined dense").classes("w-40")
                ppol = ui.select({"best_value": "Best value (highest expected return)",
                                  "best_rank": "Best Oracle Rank", "spread": "Spread evenly",
                                  "patient": "Patient: wait for strong-buy prices",
                                  "reserve": "Sliding reserve: part now, the rest for strong buys"},
                                 value=st.get("new_money", "best_value"), label="New money goes to").props(
                    "outlined dense").classes("w-80")
                pmax = ui.number("Max weight a buy can reach %", value=100 * float(st.get("max_buy_weight", 0.25)),
                                 format="%.0f", min=5, max=100).props("outlined dense").classes("w-56")
                ppw = ui.number("Patient: max weeks waiting", value=st.get("patient_max_weeks", 26), format="%.0f",
                                min=0).props("outlined dense").classes("w-52")
                pmin = ui.number("Min Oracle Rank to buy", value=st.get("min_rank_score", 0), format="%.0f",
                                 min=0).props("outlined dense").classes("w-48")
                prmin = ui.number("Reserve: spend % at an ordinary buy", value=100 * float(st.get("reserve_min_deploy", 0.5)),
                                  format="%.0f", min=0, max=100).props("outlined dense").classes("w-60")
                prmax = ui.number("Reserve: max % of portfolio", value=100 * float(st.get("reserve_max", 0.20)),
                                  format="%.0f", min=0, max=100).props("outlined dense").classes("w-52")
                prwk = ui.number("Reserve: spend after weeks", value=st.get("reserve_max_weeks", 26), format="%.0f",
                                 min=0).props("outlined dense").classes("w-48")
                paq = ui.switch("No ADD while REVIEW or Quality slipped", value=st.get("add_requires_quality", True))
            ui.label("Risk controls").classes("oe-h2 pt-2")

            def _pct(v):
                return None if v in (None, "") else round(100 * float(v), 4)

            with ui.row().classes("gap-3 items-end"):
                rmp = ui.number("Max position % (blank = off)", value=_pct(st.get("max_position")), format="%.0f",
                                min=1, max=100).props("outlined dense clearable").classes("w-56")
                rms = ui.number("Max sector % (blank = off)", value=_pct(st.get("max_sector")), format="%.0f",
                                min=1, max=100).props("outlined dense clearable").classes("w-52")
                rmc = ui.number("Max correlated % (blank = off)", value=_pct(st.get("max_correlated")), format="%.0f",
                                min=1, max=100).props("outlined dense clearable").classes("w-56")
                rct = ui.number("Correlated means ≥", value=st.get("corr_threshold", 0.7), format="%.2f", min=0,
                                max=1, step=0.05).props("outlined dense").classes("w-40")
                rtc = ui.number("Trading cost (bps per trade)", value=st.get("trade_cost_bps", 0), format="%.0f",
                                min=0).props("outlined dense").classes("w-56")
            with ui.row().classes("gap-3 items-end"):
                rov = ui.switch("Market overlay: limit new purchases while the S&P 500 is below its 200-day average",
                                value=bool(st.get("market_overlay")))
                rocb = ui.number("…buy % of normal", value=100 * float(st.get("overlay_caution_buy", 0.35)),
                                 format="%.0f", min=0, max=100).props("outlined dense").classes("w-40")
                rosb = ui.number("…when VIX > 30, buy %", value=100 * float(st.get("overlay_stress_buy", 0.10)),
                                 format="%.0f", min=0, max=100).props("outlined dense").classes("w-44")
            with ui.row().classes("gap-3 items-end"):
                rvm = ui.select({"classic": "Classic valuation", "enhanced": "Enhanced valuation (incremental ROIC, "
                                 "normalized earnings, dilution, maintenance capex, bear/base/bull)"},
                                value=st.get("valuation_model", "classic"), label="Valuation").props(
                    "outlined dense").classes("w-[520px]")
                rpm = ui.switch("Bigger margin of safety for less predictable earnings",
                                value=bool(st.get("predictability_mos")))
                rdw = ui.switch("Extra early warnings (ROIC, margins, receivables, inventory, acquisitions, stock pay)",
                                value=st.get("deterioration_warnings", True))
            with ui.row().classes("gap-3 items-end"):
                pfund = ui.switch("When all slots are full, trim or sell clearly weaker holdings to buy a strong buy",
                                  value=st.get("fund_strong", False))
                pfx = ui.number("Extra slots for strong buys", value=st.get("fund_extra_slots", 1), format="%.0f",
                                min=0).props("outlined dense").classes("w-48")
                pft = ui.number("Trim at most % of a holding", value=100 * float(st.get("fund_trim_max", 0.34)),
                                format="%.0f", min=0, max=100).props("outlined dense").classes("w-52")
                pfh = ui.number("Sell whole only after days held", value=st.get("fund_min_hold_days", 180),
                                format="%.0f", min=0).props("outlined dense").classes("w-56")
            ui.label("Tip: Backtest → 'Find the best number of holdings' tests N = 5…30 on your own history and can "
                     "save the winner here.").classes("oe-small")
            with ui.row().classes("gap-6 items-center"):
                pal = ui.switch("Price-target alerts", value=st.get("price_alerts", True))
                stg = ui.switch("Buy in thirds while the price is sliding", value=st.get("stage_in_downtrend", True))
                tmf = ui.switch("Timing filter: new buys wait while price is below a falling 50-day average",
                                value=st.get("timing_filter", False))
                tmw = ui.number("Max wait (signal days)", value=st.get("timing_max_wait", 60), format="%.0f").props(
                    "outlined dense").classes("w-44")
                bnd = ui.number("Alert band (× average daily range)", value=st.get("alert_band_atr", 1.5),
                                format="%.3g").props("outlined dense").classes("w-56")

            def collect() -> dict:
                b = copy.deepcopy(body)
                for k, (el, fmt) in fields.items():
                    v = float(el.value or 0)
                    b["settings"][k] = round(v / 100, 10) if fmt == "%" else (
                        int(v) if k in ("lookback_years", "min_years") else v)
                b["settings"]["regime_modifier"] = rm.value
                b["settings"]["include_foreign"] = inf.value
                b["settings"]["quarterly_warnings"] = qw.value
                b["settings"]["block_buy_on_quarterly_flag"] = qb.value
                for rr in b["rules"]:
                    if rr.get("id") == "r19":
                        rr["min_year_window"] = max(1, min(10, int(roew.value or rules.ROE_FLOOR_WINDOW)))
                b["settings"]["health_check"] = hc.value
                b["settings"]["price_test"] = ptest.value
                b["settings"]["fair_pe_mult"] = round(float(fpm.value or 1.5), 4)
                b["settings"]["max_mos"] = round(float(mmo.value) / 100, 6) if mmo.value not in (None, "") else None
                b["settings"]["price_alerts"] = pal.value
                b["settings"]["stage_in_downtrend"] = stg.value
                b["settings"]["timing_filter"] = tmf.value
                b["settings"]["timing_max_wait"] = int(tmw.value or 60)
                b["settings"]["alert_band_atr"] = float(bnd.value or 1.5)
                b["settings"].update({
                    "portfolio_mode": pmode.value, "max_holdings": max(1, int(pn.value or 15)), "sizing": psz.value,
                    "max_adds": int(padds.value or 0), "swap_min_hold_days": int(pswap.value or 0),
                    "initial_capital": float(pcap0.value or 10000), "weekly_contribution": float(pwk.value or 0),
                    "new_money": ppol.value, "max_buy_weight": float(pmax.value or 25) / 100,
                    "patient_max_weeks": int(ppw.value or 26), "min_rank_score": float(pmin.value or 0),
                    "add_requires_quality": paq.value,
                    "reserve_min_deploy": round(float(prmin.value if prmin.value is not None else 50) / 100, 6),
                    "reserve_max": round(float(prmax.value if prmax.value is not None else 20) / 100, 6),
                    "reserve_max_weeks": int(prwk.value or 0), "fund_strong": pfund.value,
                    "fund_extra_slots": int(pfx.value or 0),
                    "fund_trim_max": round(float(pft.value if pft.value is not None else 34) / 100, 6),
                    "fund_min_hold_days": int(pfh.value or 0),
                    "max_position": round(float(rmp.value) / 100, 6) if rmp.value not in (None, "") else None,
                    "max_sector": round(float(rms.value) / 100, 6) if rms.value not in (None, "") else None,
                    "max_correlated": round(float(rmc.value) / 100, 6) if rmc.value not in (None, "") else None,
                    "corr_threshold": round(float(rct.value or 0.7), 4), "trade_cost_bps": float(rtc.value or 0),
                    "market_overlay": rov.value,
                    "overlay_caution_buy": round(float(rocb.value if rocb.value is not None else 35) / 100, 6),
                    "overlay_stress_buy": round(float(rosb.value if rosb.value is not None else 10) / 100, 6),
                    "valuation_model": rvm.value, "predictability_mos": rpm.value,
                    "deterioration_warnings": rdw.value})
                for book, idx, mode, fl, tg, ex, wt, scale in rows_ui:
                    rr = b[book][idx]
                    rr["mode"] = mode.value
                    rr["floor"], rr["target"], rr["excellent"] = (round(float(fl.value or 0) / scale, 10),
                                                                  round(float(tg.value or 0) / scale, 10),
                                                                  round(float(ex.value or 0) / scale, 10))
                    rr["weight"] = float(wt.value or 0)
                return b

            pv = {"seq": 0}

            async def preview():
                """Runs in a worker thread and only the latest edit is shown, so typing never blocks the app."""
                from nicegui import run

                pv["seq"] += 1
                my = pv["seq"]
                b = collect()
                prev.text = "Live preview: counting…"
                try:
                    res = await run.io_bound(live_preview, cur, b)
                except Exception as ex:  # never break the editor over a preview
                    prev.text = f"Live preview unavailable ({ex})"
                    return
                if my != pv["seq"]:
                    return  # a newer edit is already being counted
                prev.text = (f"Live preview: {res['gates']} pass gates · {res['wonderful']} wonderful (Q ≥ "
                             f"{b['settings']['quality_min']:.0f}) · ~{res['buy']} in Buy Zone (estimate)")

            for el, _ in fields.values():
                el.on_value_change(lambda e: preview())
            ui.timer(0.2, preview, once=True)

            def save():
                b = collect()
                if cur.locked:
                    ui.notify("Locked: use Save as", type="warning")
                    return
                rescore = rules.scoring_hash(b) != rules.scoring_hash(cur.body)
                rules.update_profile(cur.id, b)
                rules.set_notes(cur.id, desc.value)
                with ui.dialog() as dlg, ui.card():
                    ui.label("Saved. Rebuild this profile's live history now?").classes("oe-h2")
                    ui.label("Scoring rules changed, so every company is rescored first (can take a while)." if rescore
                             else "Only portfolio or signal settings changed: company scores are reused, so this is "
                                  "quick.").classes("oe-small")
                    with ui.row():
                        ui.button("Rebuild now", on_click=lambda: (dlg.close(), _launch("rebuild",
                                                                                         {"profile_id": cur.id}))
                                  ).props("unelevated")
                        ui.button("Later", on_click=dlg.close).props("flat")
                dlg.open()

            def save_as():
                with ui.dialog() as dlg, ui.card():
                    nm = ui.input("New profile name").props("outlined dense")

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
                        ui.navigate.to(f"/profiles?pid={new}")

                    ui.button("Create", on_click=ok).props("unelevated")
                dlg.open()

            with ui.row():
                ui.button("Save", on_click=save).props("unelevated" + (" disable" if cur.locked else ""))
                ui.button("Save as…", on_click=save_as).props("outline")
                ui.button("Export JSON", on_click=lambda: ui.download(json.dumps(collect(), indent=1).encode(),
                                                                      f"{cur.name}.json")).props("flat")


def _toggle_active(p, value):
    db.get().execute("UPDATE profiles SET active=? WHERE id=?", (int(value), p.id))
    if value and not db.one("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (p.id,)):
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Build history for {p.name} now?").classes("oe-h2")
            with ui.row():
                ui.button("Build", on_click=lambda: (dlg.close(), _launch("rebuild", {"profile_id": p.id}))).props(
                    "unelevated")
                ui.button("Later", on_click=dlg.close).props("flat")
        dlg.open()


_PREVIEW_CACHE: dict = {}


def _preview_rows(snap) -> list:
    """Latest stored rule values per company for one set of scores, parsed once and cached."""
    key = tuple(snap)
    stamp = db.scalar("SELECT MAX(filed) FROM snapshots WHERE profile_id=? AND profile_version=?", key)
    hit = _PREVIEW_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    con = db.connect()  # own connection: this runs in a worker thread
    try:
        rows = con.execute(
            """SELECT sn.cik, sn.rules_json FROM snapshots sn JOIN (SELECT cik, MAX(filed) m FROM snapshots
               WHERE profile_id=? AND profile_version=? GROUP BY cik) x ON x.cik=sn.cik AND x.m=sn.filed
               WHERE sn.profile_id=? AND sn.profile_version=? AND sn.quality IS NOT NULL""", (*key, *key)).fetchall()
    finally:
        con.close()
    parsed = [{"cik": r["cik"], "rules_json": json.loads(r["rules_json"] or "[]")} for r in rows]
    _PREVIEW_CACHE.clear()
    _PREVIEW_CACHE[key] = (stamp, parsed)
    return parsed


def live_preview(base: rules.Profile, body: dict) -> dict:
    """Re-score stored rule values with edited thresholds: fast what-if counts, no recomputation."""
    import logging
    import time

    t0 = time.monotonic()
    rows = _preview_rows(base.snap)
    t1 = time.monotonic()
    rmap = {r["id"]: r for r in body["rules"] + body.get("bank_rules", []) + body.get("insurer_rules", [])}
    st = body["settings"]
    gates = wonderful = 0
    wonderful_ciks = []
    for row in rows:
        results = row["rules_json"]
        res_objs = []
        ok = True
        for rr in results:
            rd = rmap.get(rr["id"])
            if not rd or rd["mode"] == "off":
                continue
            v = rr["value"]
            lvl = rules.level_score(v, rd)
            cons = rr.get("consistency")
            score = None if lvl is None else (lvl if cons is None else 0.6 * lvl + 0.4 * 100 * cons)
            passed = rules.meets_target(v, rd)
            if rd["mode"] == "gate" and passed is not True:
                ok = False
            res_objs.append(rules.RuleResult(rr["id"], rr["name"], rd["category"], rd["mode"], v, score, passed, cons))
        q, _ = rules.quality_from(res_objs, body["category_weights"], rmap)
        if ok:
            gates += 1
            if q is not None and q >= st["quality_min"]:
                wonderful += 1
                wonderful_ciks.append(row["cik"])
    buy = 0
    rid = db.scalar("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (base.id,))
    if rid and wonderful_ciks:
        d = db.scalar("SELECT MAX(date) FROM evaluations WHERE run_id=?", (rid,))
        ev = {r["cik"]: r for r in db.rows("SELECT cik, mos, exp_return FROM evaluations WHERE run_id=? AND date=?",
                                           (rid, d))}
        for c in wonderful_ciks:
            e = ev.get(c)
            if e and e["mos"] is not None and e["mos"] >= st["mos_gate"] and (e["exp_return"] or 0) >= st["hurdle"]:
                buy += 1
    dt = time.monotonic() - t0
    if dt > 1:
        logging.getLogger("oracle_edge").info("profile preview took %.1fs (load %.1fs, %d companies)", dt, t1 - t0,
                                              len(rows))
    return {"gates": gates, "wonderful": wonderful, "buy": buy}


# ===================================================================== BACKTEST
@ui.page("/backtest")
def backtest_page():
    from .. import pipeline

    with frame("Backtest"):
        ui.label("Backtests").classes("oe-h1")
        ui.label("Replays a profile over past dates using only data that was public on each day (point-in-time). "
                 "Results go into their own run and never mix with your live history.").classes("oe-muted")
        profs = {p.id: p for p in rules.list_profiles()}

        # ---------------------------------------------------------- holdings test
        with ui.card().classes("oe-card w-full").style("border-left:4px solid #1f6f4a"):
            ui.label("Find the best number of holdings, and where new money should go").classes("oe-h2")
            ui.label("Runs focused portfolios of the best 5, 6, 8, 10 and 12 stocks under each new-money rule (best "
                     "value, best Rank, spread evenly, patient, sliding reserve, and spread or reserve with weaker "
                     "holdings sold to fund strong buys), starting with the profile's starting money and adding "
                     "its weekly amount, all in one pass, plus the old 'variable' style for comparison. The winner beat "
                     "the S&P 500 most reliably in each third of the period, not just once. Dollar results are compared "
                     "with putting the same money into the S&P 500 on the same days.").classes("oe-small")
            with ui.row().classes("items-end gap-3"):
                sprof = ui.select({k: v.name for k, v in profs.items()}, value=rules.default_profile().id,
                                  label="Profile").props("outlined dense").classes("w-64")
                sstart = ui.input("Start", value=db.cfg("backfill_start", "2019-01-01")).props(
                    "outlined dense").classes("w-36")
                send = ui.input("End", value=date.today().isoformat()).props("outlined dense").classes("w-36")
                ssizes = ui.input("Holdings to test", value=", ".join(str(x) for x in pipeline.SWEEP_SIZES)).props(
                    "outlined dense").classes("w-56")
                ui.button("Run holdings test", icon="tune", on_click=lambda: _launch("sweep", {
                    "profile_id": sprof.value, "start": sstart.value, "end": send.value,
                    "sizes": [int(x) for x in re.findall(r"\d+", ssizes.value or "")] or None})).props("unelevated")
            for sw in db.rows("SELECT * FROM sweeps WHERE COALESCE(kind,'holdings')='holdings' ORDER BY id DESC LIMIT 3"):
                _sweep_card(dict(sw), profs)

        # ---------------------------------------------------------- experiments
        with ui.card().classes("oe-card w-full").style("border-left:4px solid #3d405b"):
            ui.label("Experiments: find the best risk-adjusted setup").classes("oe-h2")
            ui.label("Runs your live setup, the Risk Optimized defaults and experiments A–F (holdings, 20% cap, sector and "
                     "correlation limits, market overlay) in one replay, then G: the most robust of A–F with the enhanced "
                     "valuation (and G+ with a bigger margin of safety for less predictable earnings). Ranked by a "
                     "robustness score: 30% median CAGR across three independent periods, 25% maximum drawdown, 20% "
                     "worst 12 months, 15% consistency, 10% turnover. Trading costs come from the profile "
                     "(Risk Optimized: 0.10% per trade).").classes("oe-small")
            with ui.row().classes("items-end gap-3"):
                risk_id = db.scalar("SELECT id FROM profiles WHERE name=?", (rules.RISK_NAME,))
                eprof = ui.select({k: v.name for k, v in profs.items()}, value=risk_id or rules.default_profile().id,
                                  label="Profile").props("outlined dense").classes("w-72")
                estart = ui.input("Start", value=db.cfg("backfill_start", "2019-01-01")).props(
                    "outlined dense").classes("w-36")
                eend = ui.input("End", value=date.today().isoformat()).props("outlined dense").classes("w-36")
                ui.button("Run experiments A–G", icon="science", on_click=lambda: _launch("experiments", {
                    "profile_id": eprof.value, "start": estart.value, "end": eend.value})).props("unelevated")
            for sw in db.rows("SELECT * FROM sweeps WHERE kind='experiments' ORDER BY id DESC LIMIT 2"):
                _experiment_card(dict(sw), profs)

        # ---------------------------------------------------------- single backtest
        with ui.card().classes("oe-card w-full"):
            ui.label("Run one backtest").classes("oe-h2")
            with ui.row().classes("items-end gap-3"):
                prof = ui.select({k: v.name for k, v in profs.items()}, value=rules.default_profile().id,
                                 label="Profile").props("outlined dense").classes("w-64")
                start = ui.input("Start", value=db.cfg("backfill_start", "2019-01-01")).props("outlined dense").classes(
                    "w-36")
                end = ui.input("End", value=date.today().isoformat()).props("outlined dense").classes("w-36")
                cad = ui.select({"weekly": "Weekly signals (fast)", "daily": "Daily signals"}, value="weekly",
                                label="Cadence").props("outlined dense").classes("w-48")
                hold = ui.number("Holdings (blank = profile's)", format="%.0f", min=0).props(
                    "outlined dense clearable").classes("w-52")
                hint = ui.label("").classes("oe-small")

                def show_hint():
                    st = profs[prof.value].settings
                    hint.text = (f"Profile: best {st.get('max_holdings')} · "
                                 f"{pipeline.policy_label(st.get('new_money', 'best_value') + ('+fund' if st.get('fund_strong') else ''))} · "
                                 f"${st.get('initial_capital', 0):,.0f} + ${st.get('weekly_contribution', 0):,.0f}/week"
                                 if st.get("portfolio_mode") == "focused" else "Profile: variable holdings")
                prof.on_value_change(lambda e: show_hint())
                show_hint()
                ui.button("Run backtest", icon="play_arrow", on_click=lambda: _launch(
                    "backtest", {"profile_id": prof.value, "start": start.value, "end": end.value,
                                 "cadence": cad.value,
                                 **({"max_holdings": int(hold.value), "mode": "focused"} if hold.value else {})})
                          ).props("unelevated")
            src = db.cfg("price_sources", prices.DEFAULT_ORDER)
            if all(prices.REGISTRY[s].free for s in src if s in prices.REGISTRY):
                ui.label("⚠ Free price sources don't include delisted stocks, so results are survivorship-biased "
                         "(likely optimistic). A paid source + 'Re-sync' removes this.").classes("oe-small")
            ui.label("Note: SEC data starts in 2009, so strict 10-year rules can only judge larger companies before "
                     "~2021. Use the 'Relaxed backfill (5y)' profile for earlier tests.").classes("oe-small")
        runs = [r for r in data.all_runs() if r["kind"] == "backtest"]
        if not runs:
            empty("No backtests yet.")
            return
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Your backtests").classes("oe-h2")
            ui.button("Recalculate all with current profile settings", icon="refresh",
                      on_click=lambda: _launch("recalc_all")).props("flat")
        for r in runs:
            curve = data.equity(r["id"])
            stt = data.stats(curve)
            sj = json.loads(r["settings_json"] or "{}")
            with ui.card().classes("oe-card w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(r["name"]).classes("font-semibold")
                        ui.label(f"{r['pname']} · {r['start_date']} → {r['end_date']} · "
                                 f"{pipeline._portfolio_label(sj.get('mode', 'variable'), sj.get('max_holdings'), sj.get('sizing', 'equal'))}"
                                 f" · {r['status']}").classes("oe-small")
                    with ui.row().classes("gap-6 items-center"):
                        beat = (stt.get("cagr") or 0) >= (stt.get("bench_cagr") or 0)
                        ui.label(f"CAGR {pct(stt.get('cagr'), 1)} vs S&P {pct(stt.get('bench_cagr'), 1)}").classes(
                            "text-sm font-semibold").style(f"color:{'#1f6f4a' if beat else '#a33a2b'}")
                        ui.label(f"Worst drop {pct(stt.get('max_dd'), 0)} (S&P {pct(stt.get('bench_max_dd'), 0)})").classes(
                            "text-sm").style("color:#a33a2b" if (stt.get("max_dd") or 0) < (stt.get("bench_max_dd") or 0)
                                             else "")
                        ui.label(f"avg {sum(c.get('holdings') or 0 for c in curve) / max(len(curve), 1):.0f} "
                                 f"holdings").classes("text-sm")
                        ui.button("Open", on_click=lambda r=r: ui.navigate.to(f"/performance?run={r['id']}")).props(
                            "outline dense")
                        ui.button("Recalculate", icon="refresh", on_click=lambda r=r: _launch(
                            "recalc", {"run_id": r["id"]})).props("flat dense").tooltip(
                            "Re-run with the profile's current settings (holdings, sizing, add limits)")
                        ui.button(icon="delete", on_click=lambda r=r: _delete_run(r["id"])).props("flat round dense")


def _sweep_card(sw: dict, profs: dict):
    from .. import pipeline

    p = profs.get(sw["profile_id"])
    res = json.loads(sw["results_json"] or "[]")
    rec = json.loads(sw["recommended_json"] or "null")
    if sw["status"] == "done" and any("worst_12m" not in r and r.get("run_id") for r in res):
        # tests from before v0.6 lack the worst-12-month figures: work them out once from the saved curves
        for r in res:
            if "worst_12m" not in r and r.get("run_id"):
                m = pipeline.sweep_metrics(r["run_id"])
                for k in ("worst_12m", "bench_worst_12m", "bench_max_dd"):
                    r.setdefault(k, m.get(k))
        db.get().execute("UPDATE sweeps SET results_json=? WHERE id=?", (json.dumps(res), sw["id"]))
    with ui.card().classes("w-full").style("background:#fbfaf7"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(f"Holdings test #{sw['id']} · {p.name if p else '?'} · {sw['start_date']} → {sw['end_date']} · "
                     f"{sw['status']}").classes("font-semibold")
            ui.button(icon="delete", on_click=lambda: (pipeline.clear_sweep(sw["id"]), ui.navigate.reload())).props(
                "flat round dense")
        if not res:
            ui.label("Running… progress shows in the top bar." if sw["status"] == "running" else "No results.").classes(
                "oe-muted")
            return
        if rec:
            dollars = ""
            if rec.get("end_value") and rec.get("contributed"):
                dollars = (f'<br>Put in <b>${rec["contributed"]:,.0f}</b> → worth <b>${rec["end_value"]:,.0f}</b> '
                           f'(same money in the S&amp;P 500: ${(rec.get("bench_end_value") or 0):,.0f})')
            bdd = next((r.get("bench_max_dd") for r in res if r.get("bench_max_dd") is not None), None)
            b12 = next((r.get("bench_worst_12m") for r in res if r.get("bench_worst_12m") is not None), None)
            rbest = next((r for r in res if r.get("run_id") == rec.get("run_id")), {})
            ui.html(f'<div style="font-size:15px">Best: <b>{rec["label"]}</b> · CAGR <b>{rec["cagr"]:.1%}</b> vs '
                    f'S&amp;P {rec["bench_cagr"]:.1%} · worst drop <b>{rec["max_dd"]:.0%}</b>'
                    + (f' (S&amp;P {bdd:.0%})' if bdd is not None else "")
                    + (f' · worst 12 months {rbest["worst_12m"]:.0%}' if rbest.get("worst_12m") is not None else "")
                    + (f' (S&amp;P {b12:.0%})' if b12 is not None else "")
                    + (f' · {rec["avg_cash"]:.0%} cash on average' if rec.get("avg_cash") is not None else "")
                    + dollars + "</div>", sanitize=False)
        foc = [r for r in res if r.get("mode") == "focused" and r.get("cagr") is not None]
        var = next((r for r in res if r.get("mode") == "variable"), None)
        sizes = sorted({r["n"] for r in foc})
        series = []
        groups = ([(pol, col, nm)
                   for pol, col, nm in (("best_value", "#1f6f4a", "Best value"), ("best_rank", "#2c5d8f", "Best Rank"),
                                        ("spread", "#c08a2e", "Spread evenly"), ("patient", "#7a4f9e", "Patient"),
                                        ("reserve", "#2a9d8f", "Sliding reserve"),
                                        ("spread+fund", "#e07a5f", "Spread + fund strong"),
                                        ("reserve+fund", "#3d405b", "Reserve + fund strong"))]
                  if any(r.get("policy") for r in foc) else
                  [(sz, col, f"{sz.title()} sizing") for sz, col in (("equal", "#1f6f4a"), ("conviction", "#7a4f9e"))])
        for key, col, name in groups:
            pts = {r["n"]: r for r in foc if (r.get("policy") or r["sizing"]) == key}
            if pts:
                series.append({"name": name, "type": "line", "symbolSize": 7,
                               "data": [round(100 * pts[n]["cagr"], 2) if n in pts else None for n in sizes],
                               "lineStyle": {"color": col}, "itemStyle": {"color": col}})
        bench = next((r["bench_cagr"] for r in res if r.get("bench_cagr") is not None), None)
        if bench is not None:
            series.append({"name": "S&P 500", "type": "line", "symbol": "none", "data": [round(100 * bench, 2)] * len(sizes),
                           "lineStyle": {"type": "dashed", "color": "#6b746f"}, "itemStyle": {"color": "#6b746f"}})
        if var and var.get("cagr") is not None:
            series.append({"name": "Old variable style", "type": "line", "symbol": "none",
                           "data": [round(100 * var["cagr"], 2)] * len(sizes),
                           "lineStyle": {"type": "dotted", "color": "#a33a2b"}, "itemStyle": {"color": "#a33a2b"}})
        ui.echart({"tooltip": {"trigger": "axis", "valueFormatter": "function(v){return v+'%'}"},
                   "legend": {"top": 0}, "grid": {"left": 50, "right": 20, "top": 34, "bottom": 36},
                   "xAxis": {"type": "category", "data": [str(n) for n in sizes], "name": "holdings",
                             "nameLocation": "middle", "nameGap": 24},
                   "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}%"}, "scale": True},
                   "series": series}).classes("w-full h-64")
        rows = []
        order = {"best_value": 0, "best_rank": 1, "spread": 2, "patient": 3, "reserve": 4, "spread+fund": 5,
                 "reserve+fund": 6}
        for r in sorted(res, key=lambda r: (r.get("mode") != "focused", order.get(r.get("policy"), 9),
                                            r.get("sizing") != "equal", r.get("n") or 999)):
            if r.get("cagr") is None:
                continue
            th = r.get("thirds_excess") or []
            rows.append({"run_id": r["run_id"], "portfolio": r["label"] + ("  ★ best" if rec and r["run_id"] == rec["run_id"] else ""),
                         "cagr": round(100 * r["cagr"], 1), "vs": round(100 * (r["cagr"] - (r["bench_cagr"] or 0)), 1),
                         "dd": round(100 * r["max_dd"], 1), "sharpe": round(r["sharpe"] or 0, 2),
                         "w12": round(100 * r["worst_12m"], 1) if r.get("worst_12m") is not None else None,
                         "hold": round(r.get("avg_holdings") or 0, 1),
                         "cash": round(100 * r["avg_cash"], 0) if r.get("avg_cash") is not None else None,
                         "thirds": " / ".join(f"{100 * x:+.1f}" for x in th if x is not None),
                         "end": f"${r['end_value']:,.0f}" if r.get("end_value") else "",
                         "spend": f"${r['bench_end_value']:,.0f}" if r.get("bench_end_value") else "",
                         "trades": r.get("trades")})
        grid = ui.aggrid({"columnDefs": [
            {"field": "portfolio", "headerName": "Portfolio", "minWidth": 330, "pinned": "left"},
            {"field": "cagr", "headerName": "CAGR %", "width": 95},
            {"field": "vs", "headerName": "vs S&P (pts)", "width": 115},
            {"field": "end", "headerName": "Ends with", "width": 115},
            {"field": "spend", "headerName": "Same $ in S&P", "width": 125},
            {"field": "dd", "headerName": "Worst drop %", "width": 115},
            {"field": "w12", "headerName": "Worst 12 mo %", "width": 125},
            {"field": "sharpe", "headerName": "Sharpe", "width": 90},
            {"field": "thirds", "headerName": "vs S&P in each third (pts)", "width": 210},
            {"field": "hold", "headerName": "Avg held", "width": 95},
            {"field": "cash", "headerName": "Avg cash %", "width": 105},
            {"field": "trades", "headerName": "Trades", "width": 90}],
            "rowData": rows}).classes("w-full").style(f"height:{min(46 + 42 * len(rows), 640)}px")
        grid.on("cellClicked", lambda e: ui.navigate.to(f"/performance?run={e.args['data']['run_id']}"))
        bdd = next((r.get("bench_max_dd") for r in res if r.get("bench_max_dd") is not None), None)
        b12 = next((r.get("bench_worst_12m") for r in res if r.get("bench_worst_12m") is not None), None)
        if bdd is not None:
            ui.label(f"For comparison, the S&P 500 over the same period: worst drop {bdd:.0%}"
                     + (f", worst 12 months {b12:.0%}" if b12 is not None else "")
                     + ". Click a row to open that portfolio's full results.").classes("oe-small")
        else:
            ui.label("Click a row to open that portfolio's full results.").classes("oe-small")
        if p:
            with ui.row().classes("items-center gap-3"):
                opts = {f"{r['n']}|{r['sizing']}|{r.get('policy') or ''}": r["label"] for r in foc}
                pick = ui.select(opts, value=f"{rec['n']}|{rec['sizing']}|{rec.get('policy') or ''}" if rec else None,
                                 label="Use for this profile").props("outlined dense").classes("w-96")

                def apply():
                    if not pick.value:
                        return
                    n, sz, pol = pick.value.split("|")
                    try:
                        pid, msg = pipeline.apply_holdings(p.id, int(n), sz, pol or None)
                    except Exception as ex:
                        ui.notify(str(ex), type="warning")
                        return
                    _after_apply(pid, msg)
                ui.button("Save to profile", icon="check", on_click=apply).props("unelevated")


def _experiment_card(sw: dict, profs: dict):
    from .. import pipeline

    p = profs.get(sw["profile_id"])
    res = json.loads(sw["results_json"] or "[]")
    rec = json.loads(sw["recommended_json"] or "null") or {}
    with ui.card().classes("w-full").style("background:#fbfaf7"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(f"Experiments #{sw['id']} · {p.name if p else '?'} · {sw['start_date']} → {sw['end_date']} · "
                     f"{sw['status']}").classes("font-semibold")
            ui.button(icon="delete", on_click=lambda: (pipeline.clear_sweep(sw["id"]), ui.navigate.reload())).props(
                "flat round dense")
        if not res:
            ui.label("Running… progress shows in the top bar." if sw["status"] == "running" else "No results.").classes(
                "oe-muted")
            return
        if rec.get("label"):
            ui.html(f'<div style="font-size:15px">Most robust: <b>{rec["label"]}</b></div>', sanitize=False)
        if rec.get("note"):
            ui.label(rec["note"]).classes("oe-small").style("color:#a24d00")
        bdd = next((r.get("bench_max_dd") for r in res if r.get("bench_max_dd") is not None), None)
        b12 = next((r.get("bench_worst_12m") for r in res if r.get("bench_worst_12m") is not None), None)
        bc = next((r.get("bench_cagr") for r in res if r.get("bench_cagr") is not None), None)
        rows = []
        for r in res:
            if r.get("cagr") is None:
                continue
            rb = r.get("robustness") or {}
            pc = r.get("period_cagr") or []
            rows.append({
                "run_id": r["run_id"], "exp": r["code"], "what": r["label"].split(" · ", 1)[-1],
                "score": rb.get("score"), "cagr": round(100 * r["cagr"], 1), "dd": round(100 * r["max_dd"], 1),
                "calmar": round(r["calmar"], 2) if r.get("calmar") is not None else None,
                "w12": round(100 * r["worst_12m"], 1) if r.get("worst_12m") is not None else None,
                "periods": " / ".join(f"{100 * x:.0f}%" for x in pc if x is not None),
                "beat": sum(1 for x in (r.get("thirds_excess") or []) if x is not None and x > 0),
                "turn": round(100 * r["turnover"], 0) if r.get("turnover") is not None else None,
                "costs": f"${r['costs']:,.0f}" if r.get("costs") is not None else "",
                "end": f"${r['end_value']:,.0f}" if r.get("end_value") else "",
                "vs_live": "✓" if r.get("beats_live") else ""})
        rows.sort(key=lambda x: -(x["score"] or 0))
        grid = ui.aggrid({"columnDefs": [
            {"field": "exp", "headerName": "Exp.", "width": 95, "minWidth": 95, "pinned": "left"},
            {"field": "what", "headerName": "Configuration", "width": 330, "minWidth": 330, "pinned": "left", "tooltipField": "what"},
            {"field": "score", "headerName": "Robustness", "width": 110, "minWidth": 110, "sort": "desc",
             "headerTooltip": "0-100: 30% median period CAGR, 25% max drawdown, 20% worst 12 months, 15% consistency, "
                              "10% turnover"},
            {"field": "cagr", "headerName": "CAGR %", "width": 95, "minWidth": 95},
            {"field": "dd", "headerName": "Max drawdown %", "width": 125, "minWidth": 125},
            {"field": "calmar", "headerName": "Calmar", "width": 95, "minWidth": 95, "headerTooltip": "CAGR ÷ |max drawdown|"},
            {"field": "w12", "headerName": "Worst 12 mo %", "width": 120, "minWidth": 120},
            {"field": "periods", "headerName": "CAGR in each period", "width": 170, "minWidth": 170},
            {"field": "beat", "headerName": "Periods beating S&P (of 3)", "width": 130, "minWidth": 130},
            {"field": "turn", "headerName": "Turnover %/yr", "width": 115, "minWidth": 115},
            {"field": "costs", "headerName": "Trading costs", "width": 115, "minWidth": 115},
            {"field": "end", "headerName": "Ends with", "width": 115, "minWidth": 115},
            {"field": "vs_live", "headerName": "Higher CAGR, same or lower drawdown than live", "width": 160, "minWidth": 160}],
            "rowData": rows, "defaultColDef": {"sortable": True, "resizable": True, "wrapHeaderText": True,
                                               "autoHeaderHeight": True},
            "tooltipShowDelay": 300}).classes("w-full").style(f"height:{140 + 42 * len(rows)}px")
        grid.on("cellClicked", lambda e: ui.navigate.to(f"/performance?run={e.args['data']['run_id']}"))
        if bdd is not None:
            ui.label(f"S&P 500 over the same period: CAGR {bc:.1%}, worst drop {bdd:.0%}"
                     + (f", worst 12 months {b12:.0%}" if b12 is not None else "")
                     + ". Periods are the three equal thirds of the test. Click a row to open its full results.").classes(
                "oe-small")


def _after_apply(pid: int, msg: str):
    from ..pipeline import backfill_start

    with ui.dialog() as dlg, ui.card().classes("w-[520px]"):
        ui.label(msg).classes("oe-h2")
        ui.label("Next: recalculate so results use the new size. Company scores are reused, so it's quick.").classes(
            "text-sm")
        with ui.column().classes("gap-2"):
            ui.button("Run a backtest with it now", icon="play_arrow", on_click=lambda: (dlg.close(), _launch(
                "backtest", {"profile_id": pid, "start": db.cfg("backfill_start", backfill_start()),
                             "end": date.today().isoformat(), "cadence": "weekly"}, then=ui.navigate.reload))
                      ).props("unelevated")
            ui.button("Rebuild this profile's live signals with it", icon="sync", on_click=lambda: (
                dlg.close(), db.get().execute("UPDATE profiles SET active=1 WHERE id=?", (pid,)),
                _launch("rebuild", {"profile_id": pid}))).props("outline")
            ui.label("Rebuilding replays your live history from the start date with the new size, and makes the "
                     "profile active so it gets daily signals.").classes("oe-small")
            ui.button("Later", on_click=dlg.close).props("flat")
    dlg.open()


def _delete_run(run_id):
    from ..pipeline import clear_run

    clear_run(run_id)
    ui.navigate.reload()

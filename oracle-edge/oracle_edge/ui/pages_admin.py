"""Setup wizard, Settings, Data sources, Backtests (and the live preview the Strategies editor uses)."""

from __future__ import annotations

import json
import re
from datetime import date

from nicegui import ui

from .. import backup, db, emailer, jobs, paths, rules, scheduler, secrets
from ..sources import prices
from ..tradecal import local_run_time
from . import data
from .layout import frame, kpi
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
                         "live in each strategy (Strategies page).").classes("oe-small")
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
        ca = ui.checkbox("The month ahead (economic calendar, daily)", value=s.get("send_calendar", True)).tooltip(
            "Fed decisions, major economic releases, market holidays and options expiration, and the earnings and "
            "ex-dividend dates of what you own. Sent every day the update runs, even with no actions.")
    msg = ui.label("").classes("oe-small")

    def save(show=True):
        emailer.save_settings({"enabled": en.value, "preset": preset.value, "host": host.value, "port": int(port.value or 587),
                               "security": sec_.value, "username": (user.value or "").strip(),
                               "from_name": fromn.value or "Oracle Edge",
                               "to": [x.strip() for x in (to.value or "").split(",") if x.strip()],
                               "send_actions": a.value, "send_weekly": w.value, "send_failures": f.value,
                               "send_price_alerts": pa.value, "send_calendar": ca.value},
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
            ui.label("How many stocks to hold, how to size them and how often to add are now part of each strategy "
                     "(Strategies → How the portfolio is run), so backtests can test them. Backtest → 'Find the best number of "
                     "holdings' finds the best size on your own history.").classes("text-sm")
            ui.button("Open Strategies", on_click=lambda: ui.navigate.to("/strategies")).props("flat")
        with ui.card().classes("oe-card w-full"):
            ui.label("Database").classes("oe-h2")
            from .. import pipeline as _pl

            sz = _pl.db_size()
            ui.label(f"The database file is {sz['bytes'] / 1e9:.2f} GB. Backtests, holdings tests and experiments used to "
                     f"keep every day's evaluations; {sz['removable_rows']:,} of those daily rows can go (each test keeps "
                     "its results, trades and charts, and its last day). New tests clean up after themselves. Cleaning "
                     "then compacts the file, which needs free disk space about the size of the database and can take "
                     "several minutes.").classes("text-sm")
            ui.button("Clean up and compact", icon="cleaning_services",
                      on_click=lambda: _launch("db_cleanup")).props("flat")
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
                fk = ui.input("FRED API key (economic calendar)", password=True,
                              placeholder="saved" if secrets.get("fred_api_key") else "free at fredaccount.stlouisfed.org"
                              ).props("outlined dense").classes("w-80").tooltip(
                    "Free: sign in at fredaccount.stlouisfed.org → API Keys → Request API Key. Used only for the "
                    "release dates of jobs, inflation, GDP and other reports in the month-ahead calendar.")
            out = ui.label("").classes("oe-small")

            def save():
                db.set_cfg("price_sources", order)
                if tk.value:
                    secrets.set("tiingo_key", tk.value.strip())
                if ek.value:
                    secrets.set("eodhd_key", ek.value.strip())
                if fk.value:
                    secrets.set("fred_api_key", fk.value.strip())
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
            with ui.row().classes("items-center gap-4 pt-2"):
                ui.switch("Prices for every company (not just the shortlist)", value=bool(db.cfg("prices_all", True)),
                          on_change=lambda e: db.set_cfg("prices_all", bool(e.value)))
                ui.button("Download missing price histories now", icon="download",
                          on_click=lambda: _launch("prices_all")).props("flat")
                ui.switch("Analyst estimates for holdings and the shortlist (daily)",
                          value=bool(db.cfg("analyst_estimates", True)),
                          on_change=lambda e: db.set_cfg("analyst_estimates", bool(e.value)))
                ui.button("Rate CEOs now (Wonderfulness 60+)", icon="badge",
                          on_click=lambda: _launch("ceo_grades")).props("flat")
            ui.label("Every company's prices make research and backtests complete and give new candidates a full "
                     "history; the first download is a few thousand tickers (the daily update does it on its own if "
                     "you don't). Estimates come from Yahoo Finance, one copy a day, so the app builds its own record "
                     "of how they change. CEO grades for the Screener are worked out as you view it and 150 a day in the daily update; "
                     "'Rate CEOs now' does them all (each reads the company's SEC filings plus Wikidata and Yahoo, so allow a while)."
                     ).classes("oe-small")
            with ui.row().classes("items-center gap-4 pt-2"):
                ui.button("Re-read every company's SEC statements", icon="sync",
                          on_click=lambda: _launch("restatements")).props(
                    "outline" if db.cfg("statements_v16_all") else "unelevated")
                ui.button("Re-value every company now", icon="calculate",
                          on_click=lambda: _launch("values")).props("flat")
                ui.button("Check the value's accuracy", icon="fact_check",
                          on_click=lambda: _launch("value_check")).props("flat")
            ui.label(("Every company's statements were re-read on " + str(db.cfg("statements_v16_all"))[:10] + ". "
                      if db.cfg("statements_v16_all") else
                      "Recommended once after installing 0.16 or later: the rebuild re-read only the shortlist's "
                      "statements, so companies off it lack the lease, pension and minority-interest figures the "
                      "value-driver model subtracts, and their values on the Screener run high until they next file. ")
                     + "It downloads the SEC's bulk file (about 1.3 GB), reads every company again and re-scores them: "
                       "allow 30–60 minutes. Prices, live histories and backtests are untouched.").classes("oe-small")
            from .. import valuecheck

            vc = db.cfg("value_check")
            ui.label("Value accuracy" + (f" (checked {str(vc.get('at'))[:10]}): " if vc else ": ") +
                     (valuecheck.summary(vc) if vc else "not checked yet. 'Check the value's accuracy' values every "
                      "company's 2013–2019 annual reports as of their filing day and compares each value with what the "
                      "company turned out to be worth (a few minutes; best after downloading every company's prices).")
                     ).classes("oe-small")
            jl = db.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 8")
            for r in jl:
                ui.label(f"#{r['id']} {r['kind']} · {r['status']} · {(r['started_at'] or '')[:16]} → "
                         f"{(r['finished_at'] or '')[:16]} · {(r['message'] or r['error'] or '')[:160]}").classes(
                    "oe-small")


# ===================================================================== PROFILES
def _pct0(v):
    return None if v in (None, "") else round(100 * float(v), 4)


# the Profiles page became Strategies (ui/strategies.py, 0.18); the live preview below is shared


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
    buy = 0 if (st.get("buy_price_rule") or "strict") == "strict" else None  # the new rules need a backtest
    rid = db.scalar("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (base.id,))
    if rid and wonderful_ciks and buy is not None:
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
    """0.19: backtests live with their strategy (Strategies → Results / Tests)."""
    ui.navigate.to("/strategies?tab=results")


def holdings_test_section(pid: int) -> None:
    from .. import pipeline

    profs = {p.id: p for p in rules.list_profiles()}
    with ui.card().classes("oe-card w-full").style("border-left:4px solid #1f6f4a"):
        ui.label("Find the best number of holdings, and where new money should go").classes("oe-h2")
        ui.label("Runs focused portfolios of the best 5, 6, 8, 10 and 12 stocks under each new-money rule (best "
                 "value, best Rank, spread evenly, patient, sliding reserve, and spread or reserve with weaker "
                 "holdings sold to fund strong buys), starting with the strategy's starting money and adding "
                 "its weekly amount, all in one pass, plus the old 'variable' style for comparison. The winner beat "
                 "the S&P 500 most reliably in each third of the period, not just once. Dollar results are compared "
                 "with putting the same money into the S&P 500 on the same days.").classes("oe-small")
        with ui.row().classes("items-end gap-3"):
            sstart = ui.input("Start", value=db.cfg("backfill_start", "2019-01-01")).props(
                "outlined dense").classes("w-36")
            send = ui.input("End", value=date.today().isoformat()).props("outlined dense").classes("w-36")
            ssizes = ui.input("Holdings to test", value=", ".join(str(x) for x in pipeline.SWEEP_SIZES)).props(
                "outlined dense").classes("w-56")
            ui.button("Run holdings test", icon="tune", on_click=lambda: _launch("sweep", {
                "profile_id": pid, "start": sstart.value, "end": send.value,
                "sizes": [int(x) for x in re.findall(r"\d+", ssizes.value or "")] or None})).props("unelevated")
        for sw in db.rows("SELECT * FROM sweeps WHERE COALESCE(kind,'holdings')='holdings' AND profile_id=? "
                          "ORDER BY id DESC LIMIT 3", (pid,)):
            _sweep_card(dict(sw), profs)


def experiments_section(pid: int) -> None:
    profs = {p.id: p for p in rules.list_profiles()}
    with ui.card().classes("oe-card w-full").style("border-left:4px solid #3d405b"):
        ui.label("Experiments: find the best risk-adjusted setup").classes("oe-h2")
        ui.label("Runs this setup, the Risk Optimized defaults and experiments A–F (holdings, 20% cap, sector and "
                 "correlation limits, market overlay) in one replay, then G: the most robust of A–F with the enhanced "
                 "valuation (and G+ with a bigger margin of safety for less predictable earnings). Ranked by a "
                 "robustness score: 30% median CAGR across three independent periods, 25% maximum drawdown, 20% "
                 "worst 12 months, 15% consistency, 10% turnover. Trading costs come from the strategy.").classes(
            "oe-small")
        with ui.row().classes("items-end gap-3"):
            estart = ui.input("Start", value=db.cfg("backfill_start", "2019-01-01")).props(
                "outlined dense").classes("w-36")
            eend = ui.input("End", value=date.today().isoformat()).props("outlined dense").classes("w-36")
            ui.button("Run experiments A–G", icon="science", on_click=lambda: _launch("experiments", {
                "profile_id": pid, "start": estart.value, "end": eend.value})).props("unelevated")
        for sw in db.rows("SELECT * FROM sweeps WHERE kind='experiments' AND profile_id=? ORDER BY id DESC LIMIT 2",
                          (pid,)):
            _experiment_card(dict(sw), profs)


def backtest_form(pid: int) -> None:
    """Run one backtest of a strategy."""
    from .. import pipeline

    p = rules.get_profile(pid)
    st = p.settings
    with ui.card().classes("oe-card w-full"):
        ui.label("Run a backtest").classes("oe-h2")
        ui.label("Replays the strategy over past dates using only data that was public on each day (point-in-time). "
                 "Results go into their own run and never mix with the live history.").classes("oe-small")
        with ui.row().classes("items-end gap-3"):
            start = ui.input("Start", value=db.cfg("backfill_start", "2019-01-01")).props("outlined dense").classes(
                "w-36")
            end = ui.input("End", value=date.today().isoformat()).props("outlined dense").classes("w-36")
            cad = ui.select({"weekly": "Weekly signals (fast)", "daily": "Daily signals"}, value="weekly",
                            label="Cadence").props("outlined dense").classes("w-48")
            hold = ui.number("Holdings (blank = strategy's)", format="%.0f", min=0).props(
                "outlined dense clearable").classes("w-52")
            ui.button("Run backtest", icon="play_arrow", on_click=lambda: _launch(
                "backtest", {"profile_id": pid, "start": start.value, "end": end.value, "cadence": cad.value,
                             **({"max_holdings": int(hold.value), "mode": "focused"} if hold.value else {})})
                      ).props("unelevated")
        if st.get("buy_rule") == "top_ranked" and st.get("portfolio_mode") == "focused":
            ui.label(f"Money: ${float(st.get('initial_capital') or 0):,.0f} to start + "
                     f"${float(st.get('weekly_contribution') or 0):,.0f} a week · top-ranked "
                     f"{int(st.get('ranked_buy_count') or 4)} every week").classes("oe-small")
        elif st.get("portfolio_mode") == "focused":
            ui.label(f"Money: ${float(st.get('initial_capital') or 0):,.0f} to start + "
                     f"${float(st.get('weekly_contribution') or 0):,.0f} a week · "
                     f"{pipeline._portfolio_label('focused', 0 if st.get('max_holdings') == 0 else st.get('max_holdings'), st.get('sizing', 'equal'), st.get('new_money', 'best_value'))}").classes(
                "oe-small")
        src = db.cfg("price_sources", prices.DEFAULT_ORDER)
        if all(prices.REGISTRY[s].free for s in src if s in prices.REGISTRY):
            ui.label("⚠ Free price sources don't include delisted stocks, so results are survivorship-biased "
                     "(likely optimistic). A paid source + 'Re-sync' removes this.").classes("oe-small")


def backtest_cards(runs: list[dict], show_strategy: bool = False) -> None:
    """One card per backtest: its result against the S&P 500, open, recalculate, delete."""
    from .. import pipeline

    vers = {p.id: p.version for p in rules.list_profiles()}
    for r in runs:
        curve = data.equity(r["id"])
        stt = data.stats(curve)
        sj = json.loads(r["settings_json"] or "{}")
        stale = sj.get("profile_version") not in (None, vers.get(r["profile_id"]))
        with ui.card().classes("oe-card w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label(r["name"]).classes("font-semibold")
                    ui.label((f"{r['pname']} · " if show_strategy else "") + f"{r['start_date']} → {r['end_date']} · "
                             f"{pipeline._portfolio_label(sj.get('mode', 'variable'), sj.get('max_holdings'), sj.get('sizing', 'equal'))}"
                             f" · {r['status']}" + (" · ⚠ strategy edited since: recalculate" if stale else "")).classes(
                        "oe-small")
                with ui.row().classes("gap-6 items-center"):
                    beat = (stt.get("cagr") or 0) >= (stt.get("bench_cagr") or 0)
                    ui.label(f"{pct(stt.get('cagr'), 1)} a year vs S&P {pct(stt.get('bench_cagr'), 1)}").classes(
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
                        "Re-run with the strategy's current settings")
                    ui.button(icon="delete", on_click=lambda r=r: _confirm_delete_runs([r["id"]])).props(
                        "flat round dense").tooltip("Delete this backtest")


def _confirm_delete_runs(ids: list[int]) -> None:
    from ..pipeline import clear_run

    with ui.dialog() as dlg, ui.card():
        ui.label(f"Delete {len(ids)} backtest{'s' if len(ids) != 1 else ''}?").classes("oe-h2")
        ui.label("Their results, trades and charts are removed. The strategies stay.").classes("text-sm")

        def go():
            for i in ids:
                clear_run(i)
            dlg.close()
            ui.navigate.reload()

        with ui.row():
            ui.button("Delete", icon="delete", on_click=go).props("unelevated color=negative")
            ui.button("Cancel", on_click=dlg.close).props("flat")
    dlg.open()


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
                                 label="Use for this strategy").props("outlined dense").classes("w-96")

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
                ui.button("Save to strategy", icon="check", on_click=apply).props("unelevated")


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
            ui.button("Rebuild this strategy's paper portfolio with it", icon="sync", on_click=lambda: (
                dlg.close(), db.get().execute("UPDATE profiles SET active=1 WHERE id=?", (pid,)),
                _launch("rebuild", {"profile_id": pid}))).props("outline")
            ui.label("Rebuilding replays your live history from the start date with the new size, and makes the "
                     "strategy run daily so it gets daily signals.").classes("oe-small")
            ui.button("Later", on_click=dlg.close).props("flat")
    dlg.open()


def _delete_run(run_id):
    from ..pipeline import clear_run

    clear_run(run_id)
    ui.navigate.reload()

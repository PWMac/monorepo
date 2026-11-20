"""Shared page frame: header (search, job progress, dark mode) and navigation drawer."""

from __future__ import annotations

from contextlib import contextmanager

from nicegui import app, ui

from .. import APP_NAME, __version__, jobs
from . import data
from .theme import BRAND, CSS

NAV = [
    ("Today", "/", "today"),
    ("My holdings", "/holdings", "account_balance_wallet"),
    ("Signals", "/signals", "notifications_active"),
    ("Performance", "/performance", "show_chart"),
    ("Market", "/market", "thermostat"),
    ("Profiles", "/profiles", "tune"),
    ("Backtest", "/backtest", "history"),
    ("Data", "/data", "storage"),
    ("Settings", "/settings", "settings"),
    ("Guide", "/guide", "menu_book"),
]


def _job_bar():
    box = ui.row().classes("items-center gap-2")
    with box:
        lbl = ui.label("").classes("text-xs")
        bar = ui.linear_progress(value=0, show_value=False).props("rounded color=white track-color=green-9").style(
            "width:180px")
    box.set_visibility(False)
    state = {"was": None}

    def tick():
        j = jobs.running()
        if j:
            box.set_visibility(True)
            lbl.text = f"{j.get('step') or j['kind']} · {j['progress'] * 100:.0f}%"
            bar.value = j["progress"]
            state["was"] = j["id"]
        else:
            box.set_visibility(False)
            if state["was"]:
                from .. import db

                r = db.one("SELECT status, message, error FROM jobs WHERE id=?", (state["was"],))
                if r and r["status"] == "done":
                    ui.notify(r["message"] or "Done", type="positive", close_button="OK", timeout=0)
                elif r:
                    ui.notify(f"Job failed: {(r['error'] or '')[:200]}", type="negative", close_button="OK", timeout=0)
                state["was"] = None

    ui.timer(2.0, tick)


@contextmanager
def frame(title: str):
    ui.add_head_html(CSS)
    ui.colors(primary=BRAND, secondary="#2c5d8f", accent="#7a4f9e", positive="#1f6f4a", negative="#a33a2b",
              warning="#9a5b00")
    dark = ui.dark_mode(value=app.storage.user.get("dark", False))
    with ui.header(elevated=False).classes("items-center justify-between px-4").style(f"background:{BRAND}"):
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round color=white dense")
            ui.label(APP_NAME).classes("text-lg font-bold text-white")
            ui.label(title).classes("text-sm text-green-100")
        with ui.row().classes("items-center gap-3"):
            _job_bar()
            search = ui.input(placeholder="Search ticker or name  ( / )").props(
                "dense outlined dark color=white input-class=text-white").classes("w-64")
            results = ui.menu().props("no-focus")

            def do_search(e=None):
                results.clear()
                hits = data.search(search.value or "")
                with results:
                    for h in hits:
                        ui.menu_item(f"{h['ticker']} · {h['name'][:40]}",
                                     on_click=lambda h=h: ui.navigate.to(f"/stock/{h['ticker']}"))
                if hits:
                    results.open()

            search.on("keydown.enter", do_search)
            search.on_value_change(lambda e: do_search() if len(e.value or "") >= 2 else None)

            def toggle_dark():
                dark.value = not dark.value
                app.storage.user["dark"] = dark.value

            ui.button(icon="dark_mode", on_click=toggle_dark).props("flat round color=white dense").tooltip("Dark mode")
    ui.keyboard(on_key=lambda e: search.run_method("focus") if (e.key == "/" and e.action.keydown and not e.modifiers.ctrl)
                else None, ignore=["input", "select", "textarea"])
    with ui.left_drawer(value=True, bordered=True).props("width=210") as drawer:
        with ui.column().classes("oe-nav gap-0 w-full"):
            for label, path, icon in NAV:
                with ui.link(target=path, new_tab=path == "/guide").classes("w-full"):
                    with ui.row().classes("items-center gap-3 px-3 py-2 rounded-lg hover:bg-green-50 w-full"):
                        ui.icon(icon).classes("text-green-800")
                        ui.label(label).classes("text-[14px] text-gray-900")
            ui.label(f"v{__version__} · research tool, not advice").classes("oe-small px-3 pt-6")
    with ui.column().classes("w-full max-w-[1280px] mx-auto p-5 gap-4"):
        yield


def kpi(value: str, label: str, sub: str | None = None, color: str | None = None, link: str | None = None):
    card = ui.card().classes("oe-card" + (" oe-kpi-link cursor-pointer" if link else ""))
    if link:
        card.on("click", lambda: ui.navigate.to(link))
        card.tooltip("Click to see the list")
    with card:
        with ui.row().classes("w-full items-start justify-between no-wrap"):
            ui.label(value).classes("oe-kpi").style(f"color:{color}" if color else "")
            if link:
                ui.icon("open_in_new").classes("text-gray-400 text-base")
        ui.label(label).classes("oe-kpi-l")
        if sub:
            ui.label(sub).classes("oe-small")


def empty(msg: str, action: tuple[str, str] | None = None):
    with ui.card().classes("oe-card items-center w-full py-8"):
        ui.icon("inbox").classes("text-4xl text-gray-400")
        ui.label(msg).classes("oe-muted text-center")
        if action:
            ui.button(action[0], on_click=lambda: ui.navigate.to(action[1])).props("unelevated")

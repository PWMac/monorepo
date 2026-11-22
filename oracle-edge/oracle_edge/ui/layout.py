"""Shared page frame: header (search, job progress, dark mode) and navigation drawer."""

from __future__ import annotations

from contextlib import contextmanager

from nicegui import app, ui

from .. import APP_NAME, __version__, jobs
from . import data
from .theme import BRAND, CSS, HEAD, NEGATIVE

NAV_GROUPS = [  # small groups (best practice: about seven items at most per group)
    ("Daily", [("Today", "/", "today"), ("My holdings", "/holdings", "account_balance_wallet"),
               ("Signals", "/signals", "notifications_active")]),
    ("Analysis", [("Screener", "/screener", "manage_search"), ("ETFs", "/etfs", "donut_small"),
                  ("Performance", "/performance", "show_chart"),
                  ("Market", "/market", "thermostat"), ("Strategies", "/strategies", "tune")]),
    ("Setup", [("Data", "/data", "storage"), ("Settings", "/settings", "settings"), ("Guide", "/guide", "menu_book")]),
]
NAV = [item for _, items in NAV_GROUPS for item in items]


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
    ui.add_head_html(HEAD + CSS)
    ui.colors(primary=BRAND, secondary="#2c5d8f", accent="#7a4f9e", positive="#1f6f4a", negative=NEGATIVE,
              warning="#8A5A12")
    dark = ui.dark_mode(value=app.storage.user.get("dark", False))
    with ui.header(elevated=False).classes("oe-topbar items-center justify-between"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            async def toggle_nav():
                # wide screens: full menu <-> icon rail (remembered); narrow screens: open/close the menu
                w = await ui.run_javascript("window.innerWidth")
                if w and w < 1024:
                    drawer.toggle()
                    return
                mini = not app.storage.user.get("nav_mini", False)
                app.storage.user["nav_mini"] = mini
                if mini:
                    drawer.props("mini")
                else:
                    drawer.props(remove="mini")
                for tip in tips:  # labels on hover only where they are hidden
                    tip.props(remove="disable") if mini else tip.props("disable")

            ui.button(icon="menu", on_click=toggle_nav).props("flat round color=white dense").tooltip(
                "Collapse or expand the menu")
            ui.html('<a class="oe-brand" href="/" title="Today">'
                    '<img class="oe-brand-full" src="/static/brand/logo/oracle-edge-header-tagline.png" '
                    'srcset="/static/brand/logo/oracle-edge-header-tagline@2x.png 2x" '
                    f'alt="{APP_NAME}: Screen, Rank, Signal">'
                    '<img class="oe-brand-mark" src="/static/brand/logo/oracle-edge-mark.svg" '
                    f'alt="{APP_NAME}"></a>', sanitize=False)
            ui.element("span").classes("oe-topbar-divider")
            ui.label(title).classes("oe-page-label")
        with ui.row().classes("items-center gap-3"):
            _job_bar()
            search = ui.input(placeholder="Search ticker or name  ( / )").props(
                "dense outlined dark color=white input-class=text-white debounce=200").classes("w-64")
            results = ui.menu().props("no-focus no-parent-event")
            found: list[dict] = []

            def do_search(e=None):
                results.clear()
                found[:] = data.search(search.value or "")
                with results:
                    for h in found:
                        with ui.menu_item(on_click=lambda h=h: ui.navigate.to(f"/stock/{h['ticker']}")):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.label(h["ticker"]).classes("font-bold w-16")
                                ui.label(h["name"][:44]).classes("text-sm")
                                if not h["active"]:
                                    ui.label("delisted").classes("oe-small")
                    if not found and len(search.value or "") >= 2:
                        ui.menu_item("No company matches").props("disable")
                if found or len(search.value or "") >= 2:
                    results.open()
                else:
                    results.close()

            def go_top():
                if not found:
                    do_search()
                if found:
                    ui.navigate.to(f"/stock/{found[0]['ticker']}")

            search.on("keydown.enter", go_top)
            search.on_value_change(lambda e: do_search() if len(e.value or "") >= 1 else results.close())

            def toggle_dark():
                dark.value = not dark.value
                app.storage.user["dark"] = dark.value

            ui.button(icon="dark_mode", on_click=toggle_dark).props("flat round color=white dense").tooltip("Dark mode")
    ui.keyboard(on_key=lambda e: search.run_method("focus") if (e.key == "/" and e.action.keydown and not e.modifiers.ctrl)
                else None, ignore=["input", "select", "textarea"])
    mini = app.storage.user.get("nav_mini", False)
    tips = []
    with ui.left_drawer(value=True, bordered=True).props("width=212 mini-width=60 breakpoint=1023"
                                                         + (" mini" if mini else "")) as drawer:
        with ui.column().classes("oe-nav gap-0 w-full pt-1"):
            for group, items in NAV_GROUPS:
                ui.label(group).classes("oe-nav-group q-mini-drawer-hide")
                ui.element("div").classes("oe-nav-sep q-mini-drawer-only")
                for label, path, icon in items:
                    active = label == title
                    with ui.link(target=path, new_tab=path == "/guide").classes("w-full").props(
                            'aria-current="page"' if active else f'aria-label="{label}"'):
                        with ui.row().classes("oe-nav-item items-center gap-3 no-wrap w-full"
                                              + (" oe-nav-active" if active else "")):
                            ui.icon(icon).classes("oe-nav-icon")
                            ui.label(label).classes("oe-nav-label q-mini-drawer-hide")
                        tips.append(ui.tooltip(label).props("anchor='center right' self='center left' :delay=200"
                                                            + ("" if mini else " disable")))
            ui.label(f"v{__version__} · research tool, not advice").classes("oe-small px-3 pt-6 q-mini-drawer-hide")
            ui.label(f"v{__version__}").classes("oe-small pt-6 text-center w-full q-mini-drawer-only")
    with ui.column().classes("oe-main w-full mx-auto gap-4"):
        _calc_fix_notice()
        yield


def _calc_fix_notice():
    """Shown on every page until the one-time rebuild after a calculation fix has run."""
    from .. import pipeline

    try:
        pending = pipeline.calc_fixes_pending()
    except Exception:
        return
    if not pending:
        return
    with ui.card().classes("oe-card w-full").style("border-left:4px solid #A8741F"):
        with ui.row().classes("w-full items-center justify-between no-wrap gap-4"):
            with ui.column().classes("gap-1"):
                ui.label("Calculation fixes are installed: rebuild your results").classes("font-semibold")
                ui.label("This version corrects how stock splits, share-count units, fiscal years and a few other "
                         "figures are handled, adds the business-health checks, applies the ROE gate's yearly floor to the last 5 "
                         "years, and (0.16) re-reads the shortlist's statements for leases, pensions and minority "
                         "interests so the new value-driver valuation has its inputs (see Guide → Accuracy audit and "
                         "Valuation). "
                         "Until the rebuild runs, the history "
                         "on screen was calculated the old way. It re-downloads price histories with their split "
                         "dates, re-scores every company and replays the live history: allow an hour or more. The "
                         "next daily update does it automatically if you don't.").classes("text-sm")
            running = jobs.running()

            def go():
                if jobs.running():
                    ui.notify("Another job is running; try again when it finishes.", type="warning")
                    return
                jobs.launch("calc_fixes", {})
                ui.notify("Rebuild started. Progress shows in the top bar.", type="positive")

            ui.button("Rebuild now", icon="build", on_click=go).props(
                "unelevated no-wrap" + (" disable" if running else "")).classes("shrink-0")


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

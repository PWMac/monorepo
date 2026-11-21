"""One way to zoom every time chart in the app.

Each chart gets: range buttons (1M ... Max) that jump to a timeframe instantly in the browser, a slider under the
chart to drag either end or slide the window, mouse-wheel / pinch zoom and drag-to-pan, box zoom with undo,
reset, save as image, and a crosshair tooltip. The y-axis rescales to whatever is in view.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from nicegui import ui

PRESETS = [("1M", 31), ("3M", 92), ("6M", 183), ("YTD", None), ("1Y", 365), ("3Y", 1096), ("5Y", 1827),
           ("10Y", 3653), ("Max", 0)]


def _ms(d: str) -> int:
    return int(datetime.fromisoformat(d[:10]).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _first_date(options: dict) -> tuple[str | None, str | None]:
    """Earliest and latest date in a time chart's series (data points are [date, value])."""
    lo = hi = None
    for s in options.get("series", []):
        for p in s.get("data") or []:
            d = p[0] if isinstance(p, (list, tuple)) and p and isinstance(p[0], str) else None
            if d:
                lo = d if lo is None or d < lo else lo
                hi = d if hi is None or d > hi else hi
    return lo, hi


def range_start(last: str, days: int | None) -> str | None:
    """First date of a preset ending at `last` (None = year to date, 0 = everything)."""
    if days == 0:
        return None
    end = date.fromisoformat(last[:10])
    return (date(end.year, 1, 1) if days is None else end - timedelta(days=days)).isoformat()


def presets_for(first: str, last: str) -> list[tuple[str, int | None]]:
    span = (date.fromisoformat(last[:10]) - date.fromisoformat(first[:10])).days
    out = [(lab, d) for lab, d in PRESETS if d == 0 or (d is None and span > 60) or (d and d < span * 0.95)]
    return out


def decorate(options: dict, name: str = "chart", slider: bool = True) -> dict:
    """Add the shared zoom, toolbox and tooltip settings to an ECharts option dict (in place)."""
    xs = options.get("xAxis")
    n_x = len(xs) if isinstance(xs, list) else 1
    idx = list(range(n_x))
    options["dataZoom"] = [
        {"type": "inside", "xAxisIndex": idx, "zoomOnMouseWheel": True, "moveOnMouseMove": True, "filterMode": "filter"},
    ]
    if slider:
        options["dataZoom"].append({"type": "slider", "xAxisIndex": idx, "height": 22, "bottom": 6,
                                    "brushSelect": True, "filterMode": "filter", "showDetail": True,
                                    "borderColor": "#E4E2DA", "fillerColor": "rgba(214,168,64,0.18)",
                                    "handleStyle": {"color": "#D6A840", "borderColor": "#A8741F"},
                                    "moveHandleStyle": {"color": "#D6A840"},
                                    "dataBackground": {"lineStyle": {"color": "#9aa5a0"}, "areaStyle": {"color": "#E4E2DA"}}})
        grids = options.get("grid")
        for g in grids if isinstance(grids, list) else [grids] if grids else []:
            if isinstance(g.get("bottom"), (int, float)) and g["bottom"] < 64:
                g["bottom"] = 64  # room for the axis labels above the slider
        if not grids:
            options["grid"] = {"left": 60, "right": 30, "top": 44, "bottom": 64}
    ys = options.get("yAxis")
    named = any(isinstance(y, dict) and y.get("name") for y in (ys if isinstance(ys, list) else [ys]))
    top_min = 66 if named else 44  # a y-axis title sits above the plot: keep it below the legend and toolbox
    grids = options.get("grid")
    if not grids:
        options["grid"] = grids = {"left": 60, "right": 30, "top": top_min, "bottom": 30}
    for i, g in enumerate(grids if isinstance(grids, list) else [grids]):
        if i == 0 and isinstance(g.get("top"), (int, float)) and g["top"] < top_min:
            g["top"] = top_min
    options["toolbox"] = {"right": 6, "top": 0, "itemSize": 14, "feature": {
        "dataZoom": {"yAxisIndex": "none", "title": {"zoom": "Box zoom: drag a rectangle", "back": "Undo zoom"}},
        "restore": {"title": "Reset"},
        "saveAsImage": {"title": "Save as image", "name": name, "pixelRatio": 2}}}
    tt = options.setdefault("tooltip", {"trigger": "axis"})
    tt.setdefault("axisPointer", {"type": "cross", "label": {"backgroundColor": "#0D341A"}})
    for y in options["yAxis"] if isinstance(options.get("yAxis"), list) else [options.get("yAxis") or {}]:
        y.setdefault("scale", True)
    leg = options.get("legend")
    if isinstance(leg, dict):  # legend on the left, toolbox on the right: they never overlap
        leg.pop("right", None)
        leg["left"] = 0
        leg.setdefault("type", "scroll")
        leg.setdefault("width", "75%")
    return options


def zoom_chart(options: dict, *, height: str = "h-80", default: str = "Max", dates: list[str] | None = None,
               name: str = "chart", on_range=None, controls=None) -> ui.echart:
    """A time chart with range buttons. `dates`: the category values when the x-axis is a category axis (index-based
    zoom); otherwise the axis is a time axis and dates are read from the series. `on_range(label, start)`: optional
    server-side hook (e.g. to re-base a comparison) called before the zoom is applied."""
    category = dates is not None
    first, last = (dates[0], dates[-1]) if category and dates else _first_date(options)
    decorate(options, name)
    if not first:
        return ui.echart(options).classes(f"w-full {height}")

    def window(days):
        start = range_start(last, days)
        if category:
            i = 0
            if start:
                i = next((k for k, d in enumerate(dates) if d >= start), 0)
            return {"startValue": i, "endValue": len(dates) - 1}, start
        return {"startValue": _ms(start) if start else _ms(first), "endValue": _ms(last)}, start

    presets = presets_for(first, last)
    labels = [p[0] for p in presets]
    default = default if default in labels else labels[-1]
    init, _ = window(dict(presets)[default])
    for dz in options["dataZoom"]:
        dz.update(init)
    bar = ui.row().classes("w-full items-center justify-between no-wrap")
    chart = ui.echart(options).classes(f"w-full {height}")
    buttons = {}

    def pick(lab):
        w, start = window(dict(presets)[lab])
        if on_range:
            on_range(lab, start)
        for dz in chart.options["dataZoom"]:  # keep the server copy in step for later redraws
            dz.update(w)
        chart.run_chart_method("dispatchAction", {"type": "dataZoom", **w})
        for k, b in buttons.items():
            b.props(f"color={'primary' if k == lab else 'grey-8'}")
        state["label"] = lab

    state = {"label": default}
    with bar:
        with ui.row().classes("items-center gap-0 no-wrap"):
            for lab in labels:
                buttons[lab] = ui.button(lab, on_click=lambda lab=lab: pick(lab)).props(
                    f"flat dense no-caps size=sm color={'primary' if lab == default else 'grey-8'}").classes("px-2")
            ui.icon("help_outline").classes("text-gray-400 ml-1").tooltip(
                "Pick a timeframe, scroll to zoom, drag to pan, drag the slider ends, or use the box-zoom tool "
                "(top right). Reset returns to the full view.")
        if controls:
            with ui.row().classes("items-center gap-3 no-wrap"):
                controls()
    chart.range_state = state  # type: ignore[attr-defined]
    chart.pick_range = pick  # type: ignore[attr-defined]
    return chart

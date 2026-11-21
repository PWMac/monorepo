"""The Screener (/screener; /wonderful redirects here): screen and rank every business on Wonderfulness, Quality and
value.

- The map: Quality across, Wonderfulness up. Colour = group, shape = direction of Wonderfulness (▲ rising,
  ▼ falling, ● steady, ◆ new), size = years in a row at 70+. Zoom with the sliders or the box tool; a click opens
  the stock in a new tab.
- The table: columns in collapsible groups (the Columns menu shows or hides whole groups and is remembered);
  small arrows in a cell show its direction over the last few years, and hovering shows the yearly history.
- Filters: "Add filter" offers every column, with a control that suits its values (min / max for numbers, a
  searchable pick-list for industries, tiers, CEO grades); active filters show as removable chips and are
  remembered.
"""

from __future__ import annotations

from collections import Counter

from nicegui import app, ui

from .. import db, rules, wonderful
from .layout import empty, frame
from .wonder import (_BIL, _NUM0, _SPARK, _X, _TREND_TIP, _Y70, _c, _compare, history_chart,
                     ranking_rows, trend_facts)

VIEWS = {  # key -> (button label, one-line explanation, filter(row), sort field)
    "both": ("Best of both", "Wonderful or better (70+) AND passes the Oracle checklist; sorted by the average of the two "
             "scores.", lambda r: r["score"] >= 70 and r["passes"], "combined"),
    "wonderful": ("Most wonderful", "Every company, best Wonderfulness first.", lambda r: True, "score"),
    "quality": ("Highest Quality", "Every company, best Quality (checklist score) first.", lambda r: r["quality"] is not None,
                "quality"),
    "nocheck": ("Wonderful, fails checklist", "Wonderful or better, but failing a checklist rule: see why in the "
                "Checklist column (often heavy R&D or capital spending, or one weak year).",
                lambda r: r["score"] >= 70 and not r["passes"], "score"),
    "notwonder": ("Passes checklist, not wonderful", "Pass the Oracle's rules but rank below 70 on Wonderfulness: "
                  "solid, but slower or less profitable than the best.", lambda r: r["passes"] and r["score"] < 70,
                  "quality"),
}
QUAD_COL = {"both": "#1f6f4a", "nocheck": "#2c5d8f", "notwonder": "#c9a227", "neither": "#c7ccc9"}
QUAD_NAME = {"both": "Best of both", "nocheck": "Wonderful, fails checklist", "notwonder": "Passes checklist, not wonderful",
             "neither": "Neither"}

# ---------------------------------------------------------------- the map's encoding
SHAPE = {"up": ("triangle", 0), "down": ("triangle", 180), "flat": ("circle", 0), "new": ("diamond", 0)}


def dot_size(streak: int) -> int:
    """Years in a row at 70+ -> dot size: the longer at the top, the bigger."""
    return 6 if streak <= 0 else 7 if streak == 1 else 9 if streak == 2 else 11 if streak <= 4 else 14 if streak <= 6 else 17


def _quad(r) -> str:
    w, q = r["score"] >= 70, r["passes"]
    return "both" if w and q else "nocheck" if w else "notwonder" if q else "neither"


def map_options(rows: list[dict], inview: set[str], bar: float) -> dict:
    groups: dict[str, list] = {k: [] for k in QUAD_COL}
    for r in rows:
        if r["quality"] is not None:
            groups[_quad(r)].append(r)
    series = []
    for g, xs in groups.items():
        data = []
        for r in xs:
            sym, rot = SHAPE.get(r.get("trend_dir") or "new", SHAPE["new"])
            data.append({"name": r["ticker"], "symbol": sym, "symbolRotate": rot, "symbolSize": dot_size(r["streak70"]),
                         "value": [round(r["quality"], 1), round(r["score"], 1), r["name"], r.get("trend_dir") or "new",
                                   r["streak70"], None if r.get("trend") is None else round(r["trend"], 0)],
                         "itemStyle": {"opacity": 0.92 if r["ticker"] in inview else 0.15}})
        series.append({"type": "scatter", "name": f"{QUAD_NAME[g]} ({len(xs)})", "itemStyle": {"color": QUAD_COL[g]},
                       "data": data, "emphasis": {"focus": "none", "scale": 1.6}})
    series[0]["markLine"] = {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed", "color": "#6b7280"},
                             "label": {"show": False}, "data": [{"xAxis": bar}, {"yAxis": 70}]}
    fmt = ("p => { const v = p.value, d = {up: '▲ rising', down: '▼ falling', flat: '● steady', new: '◆ new'}[v[3]];"
           " const c = v[5] == null ? '' : ` (${v[5] >= 0 ? '+' : ''}${v[5]} in 3 yrs)`;"
           " return `<b>${p.name}</b> ${v[2]}<br>Wonderfulness <b>${v[1]}</b> · Quality <b>${v[0]}</b>"
           "<br>${d}${c}<br>${v[4] ? v[4] + (v[4] === 1 ? ' year' : ' years') + ' in a row at 70+' : 'not at 70+ now'}"
           "<br><i>click to open in a new tab</i>`; }")
    return {"tooltip": {":formatter": fmt},
            "legend": {"top": 0, "icon": "circle"},
            "grid": {"left": 50, "right": 56, "top": 40, "bottom": 70},
            "toolbox": {"right": 8, "top": 0, "feature": {"dataZoom": {"title": {"zoom": "Drag a box to zoom",
                                                                                 "back": "Undo zoom"}},
                                                          "restore": {"title": "Reset"}}},
            "dataZoom": [{"type": "slider", "xAxisIndex": 0, "bottom": 8, "height": 16, "filterMode": "none",
                          "showDataShadow": False},
                         {"type": "slider", "yAxisIndex": 0, "right": 8, "width": 16, "filterMode": "none",
                          "showDataShadow": False},
                         {"type": "inside", "xAxisIndex": 0, "filterMode": "none", "zoomOnMouseWheel": "shift",
                          "moveOnMouseMove": False},
                         {"type": "inside", "yAxisIndex": 0, "filterMode": "none", "zoomOnMouseWheel": "shift",
                          "moveOnMouseMove": False}],
            "xAxis": {"name": "Quality (Oracle checklist)", "nameLocation": "middle", "nameGap": 26, "min": 20,
                      "max": 100},
            "yAxis": {"name": "Wonderfulness", "nameLocation": "middle", "nameGap": 34, "min": 0, "max": 100},
            "series": series}


MAP_KEY = ("<div style='display:flex;gap:22px;flex-wrap:wrap;font-size:12px;color:#4b5563;align-items:center'>"
           "<span><b>Shape</b>: Wonderfulness over 3 years</span>"
           "<span><span style='color:#374151'>▲</span> rising</span><span><span style='color:#374151'>▼</span> falling"
           "</span><span>● steady</span><span>◆ new</span>"
           "<span style='margin-left:10px'><b>Size</b>: years in a row at 70+</span>"
           + "".join(f"<span style='display:inline-flex;align-items:center;gap:4px'><span style='display:inline-block;"
                     f"width:{s}px;height:{s}px;border-radius:50%;background:#6b7280'></span>{lab}</span>"
                     for s, lab in ((6, "0"), (9, "2"), (11, "3–4"), (14, "5–6"), (17, "7+")))
           + "<span style='margin-left:10px'><b>Colour</b>: group (legend above)</span></div>")

# ---------------------------------------------------------------- table cells
_LINK = """p => {
  if (!p.data || p.value == null) return '';
  const e = String(p.value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  return `<a href="/stock/${encodeURIComponent(p.data.ticker)}" target="_blank" rel="noopener"
    title="Open ${p.data.ticker} in a new tab" style="color:inherit;text-decoration:none;${p.colDef.field === 'ticker' ?
    'font-weight:700;color:#1f6f4a' : ''}">${e}</a>`;
}"""


def _tcell(fmt: str, better: int) -> str:
    """A number with a small arrow for its direction over recent years: green when that's good, red when bad,
    nothing when steady (or when arrows are switched off)."""
    return ("p => { const f = " + fmt + "; const v = p.value == null ? '' : f(p.value);"
            " const d = (p.context && p.context.arrows && p.data && p.data.tr) ? p.data.tr[p.colDef.field] : null;"
            " if (!d || d === 'flat') return v;"
            f" const good = (d === 'up') === ({better} > 0);"
            " return `${v} <span style=\"color:${good ? '#1f6f4a' : '#a33a2b'};font-size:10px\">${d === 'up' ? '▲' : '▼'}</span>`; }")


F_NUM0 = "v => v.toFixed(0)"
F_PCT = "v => (v * 100).toFixed(1) + '%'"
F_X = "v => v.toFixed(2) + '×'"
F_YRS = "v => v.toFixed(1) + ' yrs'"

# hovering a trend-capable cell: that column year by year
_HIST_TIP = """class {
  init(p) {
    const r = p.data || {}, f = p.colDef.field, cp = p.colDef.tooltipComponentParams || {};
    const vals = (r.hist || {})[f] || [], y0 = r.hy0;
    const e = document.createElement('div');
    e.style.cssText = 'background:#fff;border:1px solid #d5dbd7;border-radius:8px;padding:10px 12px;' +
      'box-shadow:0 6px 18px rgba(0,0,0,.14);font-size:12px;color:#1f2937;width:300px;line-height:1.45';
    const fmt = {pct: v => (Math.abs(v) < 0.1 ? (v * 100).toFixed(1) : (v * 100).toFixed(0)), num: v => v.toFixed(0),
                 x: v => v.toFixed(1), yrs: v => v.toFixed(1)}[cp.fmt || 'num'];
    const unit = {pct: ' (%)', x: ' (×)', yrs: ' (years)'}[cp.fmt] || '';
    const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
    let h = `<div style="font-weight:600">${esc(r.ticker)} · ${esc(cp.title || p.colDef.headerName)}${unit}</div>`;
    const pts = vals.map((v, i) => [i, v]).filter(x => x[1] != null);
    if (pts.length >= 2) {
      const W = 276, H = 70, ys = pts.map(x => x[1]);
      let lo = Math.min(...ys), hi = Math.max(...ys);
      if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.15; lo -= pad; hi += pad;
      const x = i => (i * (W - 16) / Math.max(vals.length - 1, 1) + 8).toFixed(1);
      const y = v => (H - 8 - (v - lo) / (hi - lo) * (H - 16)).toFixed(1);
      const d = (r.tr || {})[f], good = d && d !== 'flat' && ((d === 'up') === ((cp.better || 1) > 0));
      const col = !d || d === 'flat' ? '#6b7280' : good ? '#1f6f4a' : '#a33a2b';
      h += `<svg width="${W}" height="${H}" style="margin-top:4px"><polyline points="${pts.map(q => x(q[0]) + ',' + y(q[1])).join(' ')}"
        fill="none" stroke="${col}" stroke-width="2"/>` + pts.map(q => `<circle cx="${x(q[0])}" cy="${y(q[1])}" r="2.2" fill="${col}"/>`).join('') + '</svg>';
      h += '<table style="font-size:11px;border-collapse:collapse;width:100%"><tr>' +
        vals.map((v, i) => `<td style="text-align:center;color:#6b7280">'${String(y0 + i).slice(2)}</td>`).join('') + '</tr><tr>' +
        vals.map(v => `<td style="text-align:center;font-weight:600">${v == null ? '–' : fmt(v)}</td>`).join('') + '</tr></table>';
      const word = {up: 'Rising', down: 'Falling', flat: 'Steady'}[d] || '';
      if (word) h += `<div style="margin-top:4px;color:${col}">${word}: the last two years against the three before` +
        (d === 'flat' ? '' : good ? ' (an improvement)' : ' (a deterioration)') + '</div>';
      if (cp.note) h += `<div style="color:#6b7280">${cp.note}</div>`;
    } else {
      h += '<div style="color:#6b7280">Not enough years to show a trend.</div>';
    }
    e.innerHTML = h;
    this.e = e;
  }
  getGui() { return this.e; }
}"""

_PILLARS_MINI = """p => {
  const r = p.data || {}; const ks = [['moat', 'M', 'Moat'], ['growth', 'G', 'Growth'], ['strength', 'S', 'Strength'],
                                      ['allocation', 'A', 'Capital allocation']];
  return '<span style="display:inline-flex;gap:5px;align-items:flex-end;height:30px;padding-top:3px">' + ks.map(([k, l, n]) => {
    const v = r[k]; const h = v == null ? 2 : Math.max(2, Math.round(v / 100 * 16));
    const c = v == null ? '#d5dbd7' : v >= 80 ? '#1f6f4a' : v >= 60 ? '#3c8d5f' : v >= 40 ? '#c9a227' : '#d9822b';
    return `<span title="${n}: ${v == null ? '—' : v.toFixed(0)}" style="display:inline-flex;flex-direction:column;
      align-items:center;line-height:1"><span style="width:11px;height:${h}px;background:${c};border-radius:2px"></span>
      <span style="font-size:8px;color:#6b7280;margin-top:2px">${l}</span></span>`; }).join('') + '</span>';
}"""

_CEO = """p => {
  const r = p.data || {};
  if (!r.ceo_grade) return `<span style="color:#9aa5a0;font-size:12px">${r.ceo_note ? '—' : ''}</span>`;
  const c = {A: '#1f6f4a', B: '#3c8d5f', C: '#8A5A12', D: '#d9822b', F: '#a33a2b'}[r.ceo_grade] || '#6b7280';
  return `<span style="display:inline-block;min-width:20px;text-align:center;background:${c};color:#fff;border-radius:4px;
    font-weight:700;padding:0 5px">${r.ceo_grade}</span> <span style="font-size:12px;color:#4b5563">${p.value == null ? '' : p.value.toFixed(0)}</span>`;
}"""
_CEO_TIP = """p => { const r = p.data || {}; if (!r.ceo_name) return r.ceo_note || 'Not rated yet';
  return `${r.ceo_name}${r.ceo_since ? ', CEO since ' + r.ceo_since : ''}` +
    (r.ceo_grade ? ` · grade ${r.ceo_grade} (${(p.value || 0).toFixed(0)}/100, ${r.ceo_basis === 'career' ? 'career' : 'this company'})` :
     ` · ${r.ceo_note || 'not rated'}`); }"""


_USD = "p => p.value == null ? '' : '$' + p.value.toLocaleString(undefined, {maximumFractionDigits: p.value >= 100 ? 0 : 2})"
_MOS = """p => { if (p.value == null) return '';
  const g = (p.context && p.context.mos) || 0.25, v = p.value;
  const c = v >= g ? '#1f6f4a' : v < 0 ? '#a33a2b' : '#374151';
  const t = v <= -9.99 ? '< −999%' : (v * 100).toFixed(0) + '%';
  return `<span style="color:${c};font-weight:${v >= g ? 700 : 400}">${t}</span>`; }"""
_ER = """p => { if (p.value == null) return ''; const ok = p.data && p.data.v_required != null && p.value >= p.data.v_required;
  return `<span style="color:${ok ? '#1f6f4a' : '#374151'};font-weight:${ok ? 700 : 400}">${(p.value * 100).toFixed(1)}%</span>`; }"""
_RANGE = """p => { const r = p.data || {}; if (r.v_low == null) return '';
  const f = v => '$' + v.toLocaleString(undefined, {maximumFractionDigits: v >= 100 ? 0 : 2});
  return `${f(r.v_low)} – ${f(r.v_high)}`; }"""
_IMPLIED = """p => { const r = p.data || {}; if (p.value == null) return '';
  const gap = r.v_gap, c = gap == null ? '#374151' : gap > 0.03 ? '#A8741F' : gap < -0.03 ? '#1f6f4a' : '#374151';
  const t = p.value >= 0.595 ? '60%+' : (p.value * 100).toFixed(0) + '%';
  return `<span style="color:${c};font-weight:600">${t}</span> <span style="color:#6b7280;font-size:11px">record ` +
    `${r.v_supports == null ? '—' : (r.v_supports * 100).toFixed(0) + '%'}</span>`; }"""
_IMPLIED_TIP = """p => { const r = p.data || {}; if (r.v_implied == null) return null;
  return `Today's price assumes about ${(r.v_implied * 100).toFixed(0)}% a year growth for five years (then fading) at a ` +
    `${(r.v_required * 100).toFixed(0)}% required return; the record supports ${(r.v_supports * 100).toFixed(0)}%. ` +
    (r.v_gap > 0.03 ? 'The price expects more than the record supports.' : r.v_gap < -0.03 ?
     'The price expects less than the record supports.' : 'In line with the record.'); }"""
_BUY = """p => p.value == null ? '' : p.value ? '<span style="color:#1f6f4a;font-weight:700">✓ below</span>' :
  '<span style="color:#6b7280">above</span>'"""


def _tc(field, header, fmt, better, width, tip, title, kind, note=None, **kw):
    """A column that shows its direction (arrow) and its yearly history on hover."""
    c = _c(field, header, None, width, tip, **kw)
    c.update({":cellRenderer": _tcell(fmt, better), "type": "rightAligned", ":tooltipComponent": _HIST_TIP,
              ":tooltipValueGetter": "p => (p.data && p.data.hist && p.data.hist[p.colDef.field]) ? 'h' : null",
              "tooltipComponentParams": {"title": title, "fmt": kind, "better": better, **({"note": note} if note else {})},
              "filter": "agNumberColumnFilter"})
    return c


def columns(bar: float) -> tuple[list[dict], dict[str, list[str]]]:
    """Column groups (collapsible) and, for the Columns menu, each group's column ids."""
    G = {}
    scores = [
        _tc("score", "Wonderfulness", F_NUM0, 1, 128, "How it ranks among all businesses (0–100).",
            "Wonderfulness each year", "num"),
        _c("tier", "Tier", None, 105, columnGroupShow="open"),
        {"field": "trend", "headerName": "Trend (3 yrs)", "width": 128, "minWidth": 128, ":cellRenderer": _SPARK,
         "columnGroupShow": "open",
         "headerTooltip": "Up to 10 years of Wonderfulness, and the change over the last 3 fiscal years (▲ +5 or "
                          "more, ▼ −5 or more, ▬ in between). Hover for the yearly scores; click for the full chart.",
         ":tooltipValueGetter": "p => p.data ? 'trend' : null", ":tooltipComponent": _TREND_TIP,
         "filter": "agNumberColumnFilter"},
        _c("y70", "Years at 70+", _Y70, 96, "How many of the last 5 fiscal years scored 70+ (wonderful or better). "
                                            "Consistency predicted staying on top better than a rising score.",
           columnGroupShow="open"),
        _tc("quality", "Quality", F_NUM0, 1, 104, f"The Oracle checklist score; the model needs {bar:.0f}+ and every "
                                                  "must-pass rule to buy.", "Quality (checklist) each year", "num"),
        _c("checklist", "Checklist", None, 190, "Passes, or the rule it fails.", columnGroupShow="open"),
        _tc("combined", "Average of both", F_NUM0, 1, 112, "(Wonderfulness + Quality) ÷ 2: the sort order of Best of "
                                                          "both.", "Average of both each year", "num"),
    ]
    mgmt = [
        {"field": "ceo_score", "headerName": "Grade", "width": 100, "minWidth": 100, ":cellRenderer": _CEO,
         "headerTooltip": "The CEO's track record grade (A–F; career, weighted by years at each company). Hover for "
                          "the name. Graded in the background, 40 a day, best companies first; opening a stock's "
                          "Wonderful? tab grades it at once.", ":tooltipValueGetter": _CEO_TIP,
         "filter": "agNumberColumnFilter", "type": "rightAligned"},
        _c("ceo_name", "Name", None, 170, columnGroupShow="open"),
        _c("ceo_since", "Since", None, 80, columnGroupShow="open"),
    ]
    pillars = [
        {"colId": "pillars_mini", "headerName": "M G S A", "width": 110, "minWidth": 110, "columnGroupShow": "closed",
         ":cellRenderer": _PILLARS_MINI, "sortable": False, "filter": False,
         "headerTooltip": "Moat, Growth, Strength, Allocation (100 = best). Open the group for the numbers."},
        _tc("moat", "Moat", F_NUM0, 1, 86, None, "Moat pillar each year", "num", columnGroupShow="open"),
        _tc("growth", "Growth", F_NUM0, 1, 90, None, "Growth pillar each year", "num", columnGroupShow="open"),
        _tc("strength", "Strength", F_NUM0, 1, 96, None, "Strength pillar each year", "num", columnGroupShow="open"),
        _tc("allocation", "Capital allocation", F_NUM0, 1, 110, None, "Capital-allocation pillar each year", "num",
            columnGroupShow="open"),
    ]
    measures = [
        _tc("roic_med", "ROIC 10-yr median", F_PCT, 1, 116, None, "Return on invested capital each year", "pct",
            "The column is the 10-year median; the arrow and chart follow each year's ROIC."),
        _tc("gm_med", "Gross margin", F_PCT, 1, 104, None, "Gross margin each year", "pct",
            "The column is the 10-year median; the arrow and chart follow each year's margin."),
        _tc("rev_g5", "Sales growth 5 yrs", F_PCT, 1, 112, None, "Sales growth each year", "pct",
            "The column is 5-year growth a year; the arrow and chart follow each year's growth.",
            columnGroupShow="open"),
        _c("rev_up", "Years sales grew", None, 95, columnGroupShow="open"),
        _tc("debt_payback", "Debt payback", F_YRS, -1, 104, None, "Years of earnings to repay debt", "yrs",
            "Lower is better.", columnGroupShow="open"),
        _tc("dollar_test", "$1 test", F_X, 1, 90, None, "$1 test as known each year", "x", columnGroupShow="open"),
    ]
    value = [
        {**_c("v_mos", "Margin of safety", None, 104, "1 − price ÷ likely value (value-driver model, at your required "
                                                     "return). Green = at least the profile's margin of safety."),
         ":cellRenderer": _MOS, "type": "rightAligned", "filter": "agNumberColumnFilter"},
        {**_c("v_er", "Expected return", None, 100, "The yearly return at which the value equals today's price. Green "
                                                    "= at least your required return."),
         ":cellRenderer": _ER, "type": "rightAligned", "filter": "agNumberColumnFilter"},
        {**_c("v_price", "Price", _USD, 90, "Latest close.", columnGroupShow="open")},
        {**_c("v_iv", "Likely value", _USD, 100, "What the business is worth per share at your required return "
                                                "(value-driver model).", columnGroupShow="open")},
        {**_c("v_low", "Value range", None, 140, "Low: growth 3 points lower and the required return 1 point higher; "
                                                "high: the reverse.", columnGroupShow="open"),
         ":cellRenderer": _RANGE},
        {**_c("v_implied", "Price assumes", None, 130, "The growth today's price assumes (reverse DCF), and the growth the "
                                                      "record supports. Amber = the price expects more.",
              columnGroupShow="open"),
         ":cellRenderer": _IMPLIED, ":tooltipValueGetter": _IMPLIED_TIP, "filter": "agNumberColumnFilter"},
        {**_c("v_pe_rel", "P/E vs its usual", _X, 96, "P/E on last year's EPS ÷ the company's own 10-year median P/E "
                                                     "(1.00× = its usual).", columnGroupShow="open")},
        {**_c("v_buy", "Below buy price", None, 96, "Price at or below the value-driver buy-below price (likely value "
                                                    "less the margin of safety).", columnGroupShow="open"),
         ":cellRenderer": _BUY},
        _c("price_status", "Live profile", None, 150, "Buy Zone, or how far the price is above the buy-below price, "
                                                      "for the live profile's own valuation model.",
           columnGroupShow="open"),
    ]
    info = [
        _c("mcap", "Market value", _BIL, 105),
        _c("warnings", "Latest 10-Q", None, 200, "Business-health or hard warnings in the latest quarterly report."),
        _c("industry", "Industry", None, 200),
    ]
    groups = [("scores", "Scores", scores, True), ("mgmt", "CEO", mgmt, False),
              ("pillars", "Pillars", pillars, False), ("measures", "Key measures", measures, False),
              ("value", "Value", value, True), ("info", "Company info", info, True)]
    cols = [
        _c("pos", "#", _NUM0, 64, "Position in this view.", pinned="left"),
        {**_c("ticker", "Ticker", None, 90, "Click to open the stock in a new tab.", pinned="left"),
         ":cellRenderer": _LINK},
        {**_c("name", "Company", None, 190, pinned="left"), ":cellRenderer": _LINK},
    ]
    for gid, label, children, open_ in groups:
        cols.append({"groupId": gid, "headerName": label, "children": children, "openByDefault": open_})
        G[gid] = [c.get("colId") or c["field"] for c in children]
    return cols, G


GROUP_LABELS = {"scores": "Scores", "mgmt": "CEO", "pillars": "Pillars", "measures": "Key measures",
                "value": "Value (price vs worth)", "info": "Company info (size, 10-Q, industry)"}

# ---------------------------------------------------------------- filters
TIERS = ["Exceptional", "Wonderful", "Good", "Average", "Weak"]
# key -> (label, kind, field, unit) ; kind: multi | range ; unit: num | pct | yrs | x | bil
FILTERS = {
    "industry": ("Industry", "multi", "industry", None),
    "tier": ("Tier", "multi", "tier", None),
    "ceo_grade": ("CEO grade", "multi", "ceo_grade", None),
    "checklist": ("Checklist", "multi", "passes", None),
    "wtrend": ("Wonderfulness trend", "multi", "trend_dir", None),
    "score": ("Wonderfulness", "range", "score", "num"),
    "quality": ("Quality", "range", "quality", "num"),
    "combined": ("Average of both", "range", "combined", "num"),
    "y70": ("Years at 70+ (last 5)", "range", "y70", "num"),
    "streak70": ("Years in a row at 70+", "range", "streak70", "num"),
    "trend": ("Wonderfulness change, 3 yrs", "range", "trend", "num"),
    "ceo_score": ("CEO score", "range", "ceo_score", "num"),
    "moat": ("Moat pillar", "range", "moat", "num"),
    "growth": ("Growth pillar", "range", "growth", "num"),
    "strength": ("Strength pillar", "range", "strength", "num"),
    "allocation": ("Capital-allocation pillar", "range", "allocation", "num"),
    "roic_med": ("ROIC 10-yr median", "range", "roic_med", "pct"),
    "gm_med": ("Gross margin", "range", "gm_med", "pct"),
    "rev_g5": ("Sales growth 5 yrs", "range", "rev_g5", "pct"),
    "debt_payback": ("Debt payback (years)", "range", "debt_payback", "yrs"),
    "dollar_test": ("$1 test", "range", "dollar_test", "x"),
    "mcap": ("Market value ($B)", "range", "mcap", "bil"),
    "v_mos": ("Margin of safety", "range", "v_mos", "pct"),
    "v_er": ("Expected return", "range", "v_er", "pct"),
    "v_implied": ("Growth the price assumes", "range", "v_implied", "pct"),
    "v_gap": ("Price assumes minus record (points)", "range", "v_gap", "pct"),
    "v_pe_rel": ("P/E vs its usual (×)", "range", "v_pe_rel", "x"),
    "v_buy": ("Below the buy price", "multi", "v_buy", None),
}
FILTER_GROUPS = [("Company", ["industry", "tier", "checklist", "mcap"]),
                 ("Scores", ["score", "quality", "combined", "wtrend", "trend", "y70", "streak70"]),
                 ("Management", ["ceo_grade", "ceo_score"]),
                 ("Pillars", ["moat", "growth", "strength", "allocation"]),
                 ("Key measures", ["roic_med", "gm_med", "rev_g5", "debt_payback", "dollar_test"]),
                 ("Value", ["v_mos", "v_er", "v_buy", "v_implied", "v_gap", "v_pe_rel"])]
SCALE = {"num": 1.0, "pct": 0.01, "yrs": 1.0, "x": 1.0, "bil": 1e9}
SUFFIX = {"num": "", "pct": "%", "yrs": " yrs", "x": "×", "bil": " $B"}
TREND_OPTS = {"up": "Rising ▲", "flat": "Steady ▬", "down": "Falling ▼", "new": "New"}


def multi_options(key: str, rows: list[dict]) -> dict:
    if key == "industry":
        n = Counter(r["industry"] for r in rows if r["industry"])
        return {k: f"{k} ({c})" for k, c in sorted(n.items(), key=lambda x: (-x[1], x[0]))}
    if key == "tier":
        return {t: t for t in TIERS}
    if key == "ceo_grade":
        return {**{g: g for g in "ABCDF"}, "none": "Not rated"}
    if key == "checklist":
        return {"pass": "Passes", "fail": "Fails"}
    if key == "wtrend":
        return TREND_OPTS
    if key == "v_buy":
        return {"yes": "Below the buy price", "no": "Above it", "none": "Not valued"}
    return {}


def passes(r: dict, flt: dict) -> bool:
    """Does row r pass every active filter? A blank min or max is open-ended; a missing value fails a number filter."""
    for k, v in flt.items():
        if k not in FILTERS:
            continue
        label, kind, field, unit = FILTERS[k]
        if kind == "multi":
            if not v:
                continue
            x = r.get(field)
            if k == "checklist":
                x = "pass" if x else "fail"
            elif k == "ceo_grade":
                x = x or "none"
            elif k == "wtrend":
                x = x or "new"
            elif k == "v_buy":
                x = "none" if x is None else "yes" if x else "no"
            if x not in v:
                return False
        else:
            lo, hi = (v or {}).get("min"), (v or {}).get("max")
            if lo is None and hi is None:
                continue
            x = r.get(field)
            if x is None:
                return False
            sc = SCALE[unit]
            if lo is not None and x < lo * sc - 1e-12:
                return False
            if hi is not None and x > hi * sc + 1e-12:
                return False
    return True


def chip_text(k: str, v, opts: dict | None = None) -> str:
    label, kind, field, unit = FILTERS[k]
    if kind == "multi":
        names = [(opts or {}).get(x, x).split(" (")[0] for x in v]
        return f"{label}: " + (", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "") if names else "any")
    lo, hi = (v or {}).get("min"), (v or {}).get("max")
    sfx = SUFFIX[unit]
    if lo is not None and hi is not None:
        return f"{label}: {lo:g}–{hi:g}{sfx}"
    if lo is not None:
        return f"{label} ≥ {lo:g}{sfx}"
    if hi is not None:
        return f"{label} ≤ {hi:g}{sfx}"
    return f"{label}: any"


# ---------------------------------------------------------------- the page
@ui.page("/wonderful")
def _old_address(compare: str = "", view: str = "both"):
    """The page's name before 0.17: keep old links and bookmarks working."""
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    q = {k: v for k, v in (("view", view), ("compare", compare)) if v}
    return RedirectResponse("/screener" + ("?" + urlencode(q) if q else ""))


@ui.page("/screener")
def screener_page(compare: str = "", view: str = "both"):
    prof = rules.default_profile()
    view = view if view in VIEWS else "both"
    store = app.storage.user
    with frame("Screener"):
        ui.label("Screener: every business, screened and ranked").classes("oe-h1")
        ui.label("Screen and rank every company on three things. Wonderfulness: how it ranks against all the others on "
                 "what predicted lasting high returns and growth. Quality: does it pass the Oracle checklist (the bar the "
                 "model needs before it may buy)? Value: what it's worth at your required return against today's price. "
                 "The views below start from the best businesses; Add filter narrows by any column.").classes("oe-muted")
        from .. import jobs

        if not db.scalar("SELECT COUNT(*) FROM vd_values", default=0):
            if not jobs.running():
                try:
                    jobs.launch("values", {})
                except Exception:
                    pass
            ui.label("The Value columns are being worked out for every company (about half a minute); refresh the page "
                     "when the job in the top bar finishes.").classes("oe-small text-amber-800")
        rows = ranking_rows(prof)
        if not rows:
            empty("No scores yet. They are computed from the annual-report scores after the first data load.")
            return
        bar = float(prof.settings.get("quality_min", 70))
        st = {"view": view, "filters": dict(store.get("wonder_filters") or {})}
        st["filters"] = {k: v for k, v in st["filters"].items() if k in FILTERS}
        # ---- controls
        with ui.row().classes("items-center gap-2 w-full"):
            btns = {}
            for k, (label, _tip, _f, _s) in VIEWS.items():
                btns[k] = ui.button(label, on_click=lambda k=k: set_view(k)).props("no-caps")
        tip = ui.label("").classes("oe-small")
        with ui.row().classes("items-center gap-3 w-full"):
            q = ui.input(placeholder="Search ticker, name or industry").props("dense outlined clearable").classes("w-64")
            size = ui.select({0: "Any size", 2e9: "$2B+", 10e9: "$10B+", 50e9: "$50B+"}, value=2e9,
                             label="Market value").props("dense outlined").classes("w-36")
            healthy = ui.switch("Hide companies slipping now", value=False)
            buyable = ui.switch("Only in the Buy Zone today", value=False)
            add_btn = ui.button("Add filter", icon="filter_alt").props("outline no-caps")
            with add_btn:
                with ui.menu().props("auto-close"):
                    for gname, keys in FILTER_GROUPS:
                        ui.item_label(gname).props("header").classes("text-xs uppercase text-gray-500")
                        for k in keys:
                            ui.menu_item(FILTERS[k][0], on_click=lambda k=k: add_filter(k)).classes("pl-6")
        chips = ui.row().classes("items-center gap-2 w-full")
        editor = ui.column().classes("w-full gap-1")

        def base():
            return [r for r in rows if ((r["mcap"] or 0) >= size.value if size.value else True)
                    and not (healthy.value and r["warnings"]) and not (buyable.value and not r["buyable"])
                    and passes(r, st["filters"])]

        def current():
            label, t, f, key = VIEWS[st["view"]]
            xs = [r for r in base() if f(r)]
            xs.sort(key=lambda r: -(r.get(key) if r.get(key) is not None else -1))
            for i, r in enumerate(xs):
                r["pos"] = i + 1
            return xs

        # ---- filters: chips (summary) + one editable row per active filter
        def save_filters():
            store["wonder_filters"] = st["filters"]

        def add_filter(k):
            if k not in st["filters"]:
                st["filters"][k] = [] if FILTERS[k][1] == "multi" else {"min": None, "max": None}
            st["open"] = k
            draw_filters()

        def remove_filter(k):
            st["filters"].pop(k, None)
            save_filters()
            draw_filters()
            refresh()

        def clear_filters():
            st["filters"] = {}
            save_filters()
            draw_filters()
            refresh()

        def set_multi(k, v):
            st["filters"][k] = list(v or [])
            save_filters()
            draw_chips()
            refresh()

        def set_range(k, which, v):
            cur = dict(st["filters"].get(k) or {})
            cur[which] = None if v in (None, "") else float(v)
            st["filters"][k] = cur
            save_filters()
            draw_chips()
            refresh()

        def draw_chips():
            chips.clear()
            with chips:
                if not st["filters"]:
                    ui.label("No filters. Use Add filter to narrow the list by any column.").classes("oe-small")
                    return
                for k, v in st["filters"].items():
                    opts = multi_options(k, rows) if FILTERS[k][1] == "multi" else None
                    ui.chip(chip_text(k, v, opts), removable=True, icon="filter_alt",
                            on_value_change=lambda e, k=k: None if e.value else remove_filter(k)).props(
                        "outline color=primary dense")
                ui.button("Clear all", on_click=clear_filters).props("flat dense no-caps")

        def draw_filters():
            draw_chips()
            editor.clear()
            if not st["filters"]:
                return
            with editor, ui.card().classes("oe-card w-full").style("padding:8px 14px"):
                for k, v in st["filters"].items():
                    label, kind, field, unit = FILTERS[k]
                    with ui.row().classes("items-center gap-3 w-full no-wrap"):
                        ui.label(label).classes("w-56 text-sm font-medium")
                        if kind == "multi":
                            opts = multi_options(k, rows)
                            ui.select(opts, multiple=True, value=list(v or []), with_input=k == "industry",
                                      on_change=lambda e, k=k: set_multi(k, e.value),
                                      label="Pick one or more" + (" (type to search)" if k == "industry" else "")
                                      ).props("dense outlined use-chips clearable").classes("flex-1")
                        else:
                            vals = [r.get(field) for r in rows if r.get(field) is not None]
                            sc = SCALE[unit]
                            lo_d = (min(vals) / sc) if vals else None
                            hi_d = (max(vals) / sc) if vals else None
                            ui.number("at least", value=(v or {}).get("min"), format="%g",
                                      on_change=lambda e, k=k: set_range(k, "min", e.value)).props(
                                f"dense outlined clearable debounce=500 suffix=\"{SUFFIX[unit].strip()}\"").classes("w-36")
                            ui.number("at most", value=(v or {}).get("max"), format="%g",
                                      on_change=lambda e, k=k: set_range(k, "max", e.value)).props(
                                f"dense outlined clearable debounce=500 suffix=\"{SUFFIX[unit].strip()}\"").classes("w-36")
                            if lo_d is not None:
                                ui.label(f"companies range {lo_d:,.3g} to {hi_d:,.3g}{SUFFIX[unit]}").classes("oe-small")
                        ui.space()
                        ui.button(icon="close", on_click=lambda k=k: remove_filter(k)).props(
                            "flat dense round size=sm").tooltip("Remove this filter")
                ui.label("Filters apply to the map and the table, and are remembered next time. Blank = no limit."
                         ).classes("oe-small")

        # ---- the map
        with ui.card().classes("oe-card w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Quality vs Wonderfulness").classes("oe-h2")
                counts = ui.label("").classes("oe-small")
            ui.label(f"Each dot is a company. Right of the dashed line = passes the Oracle checklist (Quality {bar:.0f}+ and "
                     "every must-pass rule); above it = wonderful (70+). Zoom with the sliders (bottom and right), the "
                     "box tool (top right; ⟲ resets) or Shift + mouse wheel. Click a dot to open the company in a new "
                     "tab; click a legend entry to hide a group.").classes("oe-small")
            chart = ui.echart({}, on_point_click=lambda e: ui.navigate.to(f"/stock/{e.name}", new_tab=True)
                              if e.name else None).classes("w-full").style("height:480px")
            ui.html(MAP_KEY, sanitize=False).classes("w-full")

        # ---- the table
        hidden = set(store.get("wonder_hidden") or [])
        cols, G = columns(bar)
        for c in cols:
            for ch in c.get("children", []):
                if c.get("groupId") in hidden:
                    ch["hide"] = True
        arrows = {"on": bool(store.get("wonder_arrows", True))}
        ctx = {"arrows": arrows["on"], "mos": float(prof.settings.get("mos_gate", 0.25))}
        with ui.row().classes("w-full items-center justify-between pt-2"):
            ui.label("").classes("oe-small")
            with ui.row().classes("items-center gap-1"):
                ui.button("Expand groups", icon="unfold_more", on_click=lambda: set_groups(True)).props(
                    "flat dense no-caps")
                ui.button("Collapse groups", icon="unfold_less", on_click=lambda: set_groups(False)).props(
                    "flat dense no-caps")
                with ui.button("Columns", icon="view_column").props("outline dense no-caps"):
                    with ui.menu():
                        ui.label("Show column groups").classes("text-xs uppercase text-gray-500 px-4 pt-2")
                        for gid, lab in GROUP_LABELS.items():
                            ui.checkbox(lab, value=gid not in hidden,
                                        on_change=lambda e, gid=gid: show_group(gid, e.value)).classes("px-3")
                        ui.separator()
                        ui.switch("Trend arrows in cells", value=arrows["on"],
                                  on_change=lambda e: set_arrows(e.value)).classes("px-3 pb-2")
        g = ui.aggrid({"columnDefs": cols, "rowData": [], "context": ctx,
                       "defaultColDef": {"sortable": True, "resizable": True, "filter": True, "wrapHeaderText": True,
                                         "autoHeaderHeight": True},
                       "rowHeight": 36, "suppressCellFocus": True, "tooltipShowDelay": 300, "multiSortKey": "shift",
                       "rowSelection": {"mode": "multiRow", "checkboxes": True, "headerCheckbox": False},
                       "selectionColumnDef": {"pinned": "left", "width": 48, "maxWidth": 48,
                                              "suppressHeaderMenuButton": True, "sortable": False, "filter": False}},
                      theme="quartz").classes("w-full").style("height:62vh")
        ui.label("▲ / ▼ in a cell: that measure's direction over recent years (the last two years against the three "
                 "before), green when it's an improvement, red when it isn't; no arrow = steady. Hover a cell for its "
                 "yearly history. Click a ticker or name to open the stock in a new tab. Group headers with ‹ › "
                 "open and close; the Columns menu hides whole groups.").classes("oe-small")

        def show_group(gid, visible):
            h = set(store.get("wonder_hidden") or [])
            (h.discard if visible else h.add)(gid)
            store["wonder_hidden"] = sorted(h)
            g.run_grid_method("setColumnsVisible", G[gid], bool(visible))

        def set_groups(opened):
            for gid in G:
                g.run_grid_method("setColumnGroupOpened", gid, opened)

        def set_arrows(on):
            store["wonder_arrows"] = bool(on)
            ctx["arrows"] = bool(on)
            g.run_grid_method("setGridOption", "context", ctx)
            g.run_grid_method("refreshCells", {"force": True})

        def refresh():
            xs = current()
            for k, b in btns.items():
                b.props("unelevated" if k == st["view"] else "outline")
            label, t, _f, _k = VIEWS[st["view"]]
            tip.text = f"{label}: {t} {len(xs):,} companies."
            allx = base()
            n = {k: sum(1 for r in allx if r["quality"] is not None and _quad(r) == k) for k in QUAD_COL}
            counts.text = (f"{n['both']:,} best of both · {n['nocheck']:,} wonderful but fail the checklist · "
                           f"{n['notwonder']:,} pass the checklist but aren't wonderful")
            g.options["rowData"] = xs
            g.run_grid_method("setGridOption", "rowData", xs)
            chart.options.clear()
            chart.options.update(map_options(allx, {r["ticker"] for r in xs}, bar))
            chart.update()

        def set_view(k):
            st["view"] = k
            refresh()
        for el in (size, healthy, buyable):
            el.on_value_change(lambda e: refresh())
        q.on_value_change(lambda e: g.run_grid_method("setGridOption", "quickFilterText", e.value or ""))

        # the Trend cell opens the full history; ticker and name are links (new tab)
        dlg = ui.dialog()

        def show_history(row):
            dlg.clear()
            h = wonderful.history(row["cik"], prof)
            with dlg, ui.card().classes("oe-card").style("width:min(900px,95vw);max-width:95vw"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    ui.label(f"{row['ticker']} · {row['name']}: score history").classes("oe-h2")
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.button("Open stock page", icon="open_in_new",
                                  on_click=lambda: ui.navigate.to(f"/stock/{row['ticker']}", new_tab=True)).props(
                            "flat dense no-caps")
                        ui.button(icon="close", on_click=dlg.close).props("flat dense round")
                if len(h) < 2:
                    ui.label("Only one year scored so far.").classes("oe-muted")
                else:
                    with ui.row().classes("gap-2"):
                        for f in trend_facts(h):
                            ui.label(f).classes("oe-small").style("background:#eef3f0;border-radius:10px;padding:1px 9px")
                    ui.echart(history_chart(h)).classes("w-full").style("height:340px")
            dlg.open()

        g.on("cellClicked", lambda e: show_history(e.args["data"]) if e.args.get("colId") == "trend"
             and e.args.get("data") else None)
        comp = ui.column().classes("w-full gap-3")

        async def do_compare():
            sel = await g.get_selected_rows()
            tickers = [r["ticker"] for r in sel][:5]
            if len(tickers) < 2:
                ui.notify("Tick 2 to 5 companies to compare.", type="info")
                return
            comp.clear()
            with comp:
                _compare(tickers, prof)
        with ui.row().classes("items-center gap-3"):
            ui.button("Compare ticked companies", icon="compare_arrows", on_click=do_compare).props("unelevated")
            ui.button("Export CSV", icon="download",
                      on_click=lambda: g.run_grid_method("exportDataAsCsv", {"fileName": f"screener-{st['view']}.csv"})
                      ).props("flat dense")
        draw_filters()
        ui.timer(0.1, refresh, once=True)
        if compare:
            with comp:
                _compare([t.strip().upper() for t in compare.split(",") if t.strip()][:5], prof)

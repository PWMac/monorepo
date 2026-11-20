"""Visual system: colors, CSS, badges, formatters, sparklines and the price ladder."""

from __future__ import annotations

import html

# Brand pack colors (brand/theme/tokens.json). Cherry red is reserved for the logo's glint: never use it in the UI.
BRAND = "#1f6f4a"        # green-700: actions, links, positive
HEADER = "#0D341A"       # green-900: header bar
GOLD = "#D6A840"         # gold-500: header rule, accents
CREAM = "#F7F2E3"        # text on dark green
NEGATIVE = "#A8322D"
INK = "#1C2420"
MUTED = "#5B655F"
LINE = "#E4E2DA"
BG = "#F7F6F2"
BTC_COL = {"bear": "#A8322D", "bull": "#1f6f4a", "normal": None, "warming": None}

BADGES = {
    "STRONG_BUY": ("▲▲ STRONG BUY", "#1f6f4a", "#ffffff"),
    "BUY": ("▲ BUY", "#e3f1ea", "#1f6f4a"),
    "ADD": ("＋ ADD", "#ffffff", "#1f6f4a"),
    "HOLD": ("● HOLD", "#eceae3", "#4b524f"),
    "WATCH": ("◎ WATCH", "#fbf0dc", "#9a5b00"),
    "REVIEW": ("⚠ REVIEW", "#ffe7cc", "#a24d00"),
    "SELL": ("▼ SELL", "#a33a2b", "#ffffff"),
    "SWAP": ("⇄ SWAP", "#2c5d8f", "#ffffff"),
    "TRIM": ("✂ TRIM", "#7a4f9e", "#ffffff"),
    "PENDING": ("… PENDING", "#eceae3", "#4b524f"),
    "NEAR": ("◌ NEAR MISS", "#f0eee7", "#5d6763"),
}

HEAD = """
<meta name="theme-color" content="#0D341A">
<link rel="icon" href="/static/brand/favicon/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/brand/favicon/apple-touch-icon.png">
<link rel="manifest" href="/static/brand/favicon/site.webmanifest">
<meta property="og:title" content="Oracle Edge">
<meta property="og:description" content="Screen · Rank · Signal: discover value.">
<meta property="og:image" content="/static/brand/social/og-image.png">
<link rel="stylesheet" href="/static/brand/theme/oracle-edge-theme.css">
"""

CSS = f"""
<style>
:root {{ --brand:{BRAND}; --ink:{INK}; --muted:{MUTED}; --line:{LINE}; --bg:{BG}; }}
body {{ background: var(--bg); color: var(--ink);
  font-family: "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif; }}
.body--dark {{ --oe-bg:#0A1710; --oe-surface:#10241A; --oe-text:#ECE8DC; --oe-text-muted:#A3B0A7; --oe-border:#1E3A2A;
  --oe-accent:var(--oe-gold-300); --oe-header-bg:var(--oe-green-950); --oe-negative:#E0645C; --oe-warning:#E0A84A; }}
.q-header.oe-topbar {{ height:74px; padding:0 16px; background:var(--oe-header-bg, {HEADER}) !important;
  border-bottom:2px solid var(--oe-header-rule, {GOLD}); }}
.oe-topbar .oe-brand img {{ height:52px; }}
.oe-topbar .oe-page-label {{ color:{CREAM}; }}
.oe-guide-hero {{ font-family: var(--oe-font-display); letter-spacing:.04em; }}
.body--dark {{ --bg:#0A1710; --ink:#ECE8DC; --muted:#A3B0A7; --line:#1E3A2A; }}
.body--dark .oe-card {{ background:#10241A; }}
.oe-card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px 18px;
  box-shadow:0 1px 2px rgba(7,31,15,.06), 0 4px 14px rgba(7,31,15,.06); }}
.oe-kpi {{ font-size:28px; font-weight:700; line-height:1.1; font-variant-numeric: tabular-nums; }}
.oe-kpi-l {{ font-size:12.5px; color:var(--muted); }}
.oe-kpi-link {{ transition:border-color .15s, box-shadow .15s; }}
.oe-kpi-link:hover {{ border-color:#1f6f4a; box-shadow:0 2px 10px rgba(31,111,74,.15); }}
.oe-h1 {{ font-size:26px; font-weight:700; letter-spacing:-.01em; }}
.oe-h2 {{ font-size:17px; font-weight:650; margin:4px 0 6px; }}
.oe-muted {{ color: var(--muted); }}
.oe-small {{ font-size:12.5px; color: var(--muted); }}
.oe-pill {{ display:inline-block; font-size:11.5px; font-weight:700; padding:2px 9px; border-radius:999px;
  white-space:nowrap; border:1.5px solid transparent; }}
.oe-num {{ font-variant-numeric: tabular-nums; }}
.oe-action {{ border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:#fff; }}
.body--dark .oe-action {{ background:#10241A; }}
.oe-nav a {{ text-decoration:none; }}
.q-drawer {{ background:#fbfaf7 !important; }}
.body--dark .q-drawer {{ background:#0D1F15 !important; }}
.ag-theme-quartz {{ --ag-font-family: inherit; --ag-font-size: 13.5px; --ag-header-background-color:#f0eee7;
  --ag-row-hover-color:#eef6f1; --ag-selected-row-background-color:#e3f1ea; }}
.ag-cell {{ display:flex; align-items:center; }}
.ag-cell .oe-pill {{ line-height:1.5; padding:1px 8px; }}
.oe-rule-pass {{ color:#1f6f4a; }} .oe-rule-fail {{ color:#a33a2b; }} .oe-rule-na {{ color:#9aa5a0; }}
.oe-ladder {{ position:relative; height:64px; margin:8px 4px 24px; }}
.oe-ladder .seg {{ position:absolute; top:22px; height:22px; font-size:11px; font-weight:700; display:flex;
  align-items:center; justify-content:center; overflow:hidden; white-space:nowrap; }}
.oe-ladder .tick {{ position:absolute; top:48px; font-size:11px; color:var(--muted); transform:translateX(-50%);
  white-space:nowrap; text-align:center; line-height:1.15; }}
.oe-ladder .tick.lim {{ top:0; color:{BRAND}; font-weight:700; }}
.oe-ladder .now {{ position:absolute; top:0; transform:translateX(-50%); font-size:11.5px; font-weight:700;
  white-space:nowrap; }}
</style>
"""


def badge_html(code: str, small: bool = False) -> str:
    label, bg, fg = BADGES.get(code, (code, "#eee", "#333"))
    border = f"border-color:{fg};" if code == "ADD" else ""
    size = "font-size:10.5px;" if small else ""
    return f'<span class="oe-pill" style="background:{bg};color:{fg};{border}{size}">{html.escape(label)}</span>'


def money(v, d=2) -> str:
    if v is None:
        return "—"
    return f"${v:,.{d}f}"


def pct(v, d=0) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{d}f}%"


def num(v, d=1) -> str:
    if v is None:
        return "—"
    return f"{v:,.{d}f}"


def fmt_value(v, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return pct(v, 1)
    if kind == "years":
        return f"{v:.1f} yrs" if v < 90 else "never (losses)"
    if kind == "x":
        return f"{v:.2f}×"
    if kind == "ratio":
        return f"{v * 100:.0f}%"
    if kind == "flag":
        return "yes" if v else "none"
    return f"{v:.2f}"


def sparkline(series, width=110, height=26, color=BRAND) -> str:
    pts = [(i, v) for i, v in enumerate(series or []) if v is not None]
    if len(pts) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y1 = y0 + 1
    path = " ".join(
        f"{'M' if k == 0 else 'L'}{(x - x0) / max(x1 - x0, 1) * (width - 4) + 2:.1f},"
        f"{height - 3 - (y - y0) / (y1 - y0) * (height - 6):.1f}" for k, (x, y) in enumerate(pts))
    lx = (pts[-1][0] - x0) / max(x1 - x0, 1) * (width - 4) + 2
    ly = height - 3 - (pts[-1][1] - y0) / (y1 - y0) * (height - 6)
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.2" fill="{color}"/></svg>')


def ladder_html(price, strong, buy, iv, expensive, limit=None) -> str:
    """Horizontal price ladder: STRONG BUY | BUY | HOLD/WATCH | EXPENSIVE with today's price marker."""
    if not all(v for v in (strong, buy, iv, expensive)) or price is None:
        return '<div class="oe-small">Price levels appear once the stock can be valued.</div>'
    lo = min(strong * 0.75, price * 0.9)
    hi = max(expensive * 1.25, price * 1.1)

    def x(v):
        return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))

    segs = [(lo, strong, "#1f6f4a", "#fff", "STRONG BUY"), (strong, buy, "#6fb38f", "#0f3a26", "BUY"),
            (buy, expensive, "#d9d6cc", "#3a423f", "HOLD · WATCH"), (expensive, hi, "#e8c48a", "#6a3d00", "EXPENSIVE")]
    parts = []
    for a, b, bg, fg, label in segs:
        parts.append(f'<div class="seg" style="left:{x(a)}%;width:{x(b) - x(a)}%;background:{bg};color:{fg}">{label}</div>')
    rows = 1
    for grp in _tick_groups([(x(v), v, cap) for v, cap in ((strong, "strong buy"), (buy, "buy below"), (iv, "value"),
                                                             (expensive, "1.5× value"))]):
        if len({f"{v:,.0f}" for _, v, _ in grp}) == 1:  # same price: one label ("$116 strong buy · buy below")
            xs = sum(t[0] for t in grp) / len(grp)
            parts.append(f'<div class="tick" style="left:{xs}%">${grp[0][1]:,.0f}<br>'
                         f'{" · ".join(t[2] for t in grp)}</div>')
            continue
        for k, (xx, v, cap) in enumerate(grp):  # close but different: stagger onto separate rows
            rows = max(rows, k + 1)
            parts.append(f'<div class="tick" style="left:{xx}%;top:{48 + 28 * k}px">${v:,.0f}<br>{cap}</div>')
    if limit and abs(x(limit) - x(price)) > 9 and abs(limit / buy - 1) > 0.01:
        parts.append(f'<div class="tick lim" style="left:{x(limit)}%">limit ${limit:,.2f}<br>◆</div>')
    parts.append(f'<div class="now" style="left:{x(price)}%">Today ${price:,.2f}<br>▼</div>')
    return f'<div class="oe-ladder" style="height:{64 + 28 * (rows - 1)}px">{"".join(parts)}</div>'


def _tick_groups(ticks: list[tuple], gap: float = 8.0) -> list[list[tuple]]:
    """Group ladder ticks whose labels would overlap (closer than `gap` percent of the width)."""
    groups: list[list[tuple]] = []
    for t in sorted(ticks, key=lambda t: t[0]):
        if groups and t[0] - groups[-1][-1][0] < gap:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups

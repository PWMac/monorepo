"""Visual system: colors, CSS, badges, formatters, sparklines and the price ladder."""

from __future__ import annotations

import html

BRAND = "#1f6f4a"
INK = "#1d2321"
MUTED = "#5d6763"
LINE = "#e2e0d8"
BG = "#f7f6f2"

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

CSS = f"""
<style>
:root {{ --brand:{BRAND}; --ink:{INK}; --muted:{MUTED}; --line:{LINE}; --bg:{BG}; }}
body {{ background: var(--bg); color: var(--ink);
  font-family: "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif; }}
.body--dark {{ --bg:#141816; --ink:#e7ece9; --muted:#9aa5a0; --line:#2a312e; }}
.body--dark .oe-card {{ background:#1b201e; }}
.oe-card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:16px 18px; box-shadow:none; }}
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
.body--dark .oe-action {{ background:#1b201e; }}
.oe-nav a {{ text-decoration:none; }}
.q-drawer {{ background:#fbfaf7 !important; }}
.body--dark .q-drawer {{ background:#161a18 !important; }}
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
    for v, cap in ((strong, "strong buy"), (buy, "buy below"), (iv, "value"), (expensive, "1.5× value")):
        parts.append(f'<div class="tick" style="left:{x(v)}%">${v:,.0f}<br>{cap}</div>')
    if limit and abs(x(limit) - x(price)) > 9 and abs(limit / buy - 1) > 0.01:
        parts.append(f'<div class="tick lim" style="left:{x(limit)}%">limit ${limit:,.2f}<br>◆</div>')
    parts.append(f'<div class="now" style="left:{x(price)}%">Today ${price:,.2f}<br>▼</div>')
    return f'<div class="oe-ladder">{"".join(parts)}</div>'

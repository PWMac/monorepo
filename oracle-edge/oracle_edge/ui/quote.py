"""Stock page, "Chart & key stats" tab: the price chart, quote statistics and financial statements a Yahoo Finance
or Google Finance quote page shows, built from the app's own data (end-of-day prices, SEC filings).

Layout follows the common quote-page pattern: a price chart with range buttons and period returns on top, a
two-column key-statistics table, then financial statements with an Annual/Quarterly switch, a revenue and
net income bar chart and a table with year-over-year change.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

from nicegui import ui

from .. import db

RANGES = [("1M", 31), ("6M", 183), ("YTD", None), ("1Y", 365), ("5Y", 1827), ("Max", 0)]
UP, DOWN, INK, GRID = "#1f6f4a", "#A8322D", "#1C2420", "#E4E2DA"


# ------------------------------------------------------------------ data
def ohlcv(ticker: str) -> list[dict]:
    return [dict(r) for r in db.rows(
        "SELECT date, open, high, low, close, COALESCE(adj_close, close) adj, volume FROM prices "
        "WHERE ticker=? AND close > 0 ORDER BY date", (ticker,))]


def _since(bars: list[dict], days: int | None) -> int:
    """Index of the first bar in a range ending at the last bar (days=None: year to date, 0: everything)."""
    if not bars or days == 0:
        return 0
    last = date.fromisoformat(bars[-1]["date"])
    start = date(last.year, 1, 1) if days is None else last - timedelta(days=days)
    for i, b in enumerate(bars):
        if b["date"] >= start.isoformat():
            return max(0, i - 1) if days is None else i
    return 0


def period_returns(bars: list[dict]) -> list[tuple[str, float | None]]:
    out = []
    if len(bars) < 2:
        return out
    for lab, days in [("1D", 1), ("5D", 7), ("1M", 31), ("6M", 183), ("YTD", None), ("1Y", 365), ("5Y", 1827)]:
        i = len(bars) - 2 if lab == "1D" else _since(bars, days)
        out.append((lab, bars[-1]["adj"] / bars[i]["adj"] - 1 if bars[i]["adj"] and i < len(bars) - 1 else None))
    return out


def _monthly(bars: list[dict]) -> dict[str, float]:
    m = {}
    for b in bars:
        m[b["date"][:7]] = b["adj"]
    return m


def beta(bars: list[dict], bench: list[dict], months: int = 60) -> float | None:
    """Beta from monthly returns over the last five years, the way Yahoo labels it ("5Y Monthly")."""
    a, b = _monthly(bars), _monthly(bench)
    keys = sorted(set(a) & set(b))[-(months + 1):]
    ra = [a[keys[i]] / a[keys[i - 1]] - 1 for i in range(1, len(keys))]
    rb = [b[keys[i]] / b[keys[i - 1]] - 1 for i in range(1, len(keys))]
    if len(ra) < 24:
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    var = sum((x - mb) ** 2 for x in rb)
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / var if var else None


def annual(cik: int) -> dict[str, dict[str, float]]:
    """item -> fiscal-year end -> value, from the latest filing for each year."""
    out: dict[str, dict[str, float]] = {}
    for r in db.rows("SELECT item, fy_end, value FROM facts WHERE cik=? ORDER BY filed", (cik,)):
        out.setdefault(r["item"], {})[r["fy_end"]] = r["value"]
    return out


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def quarterly(cik: int, ann: dict | None = None) -> dict[str, dict[str, float]]:
    """item -> quarter end -> the three-month value. 10-Qs often report year-to-date figures (cash flow always
    does) and the fourth quarter only appears in the annual report, so quarters are rebuilt from year-to-date
    differences and Q4 = full year - nine months. Balance-sheet items are the quarter-end balances."""
    ann = ann if ann is not None else annual(cik)
    dur: dict[str, dict[tuple[str, str], float]] = {}
    inst: dict[str, dict[str, float]] = {}
    for r in db.rows("SELECT item, start, end, value FROM qfacts WHERE cik=? ORDER BY filed", (cik,)):
        if r["start"]:
            dur.setdefault(r["item"], {})[(r["start"], r["end"])] = r["value"]
        else:
            inst.setdefault(r["item"], {})[r["end"]] = r["value"]
    out: dict[str, dict[str, float]] = {}
    for item, vals in dur.items():
        q: dict[str, float] = {}
        by_start: dict[str, list[tuple[str, float]]] = {}
        for (s0, e), v in vals.items():
            n = _days(s0, e)
            if 80 <= n <= 100:
                q[e] = v
            by_start.setdefault(s0, []).append((e, v))
        for s0, pts in by_start.items():  # year-to-date runs: 3, 6, 9 months from the same fiscal-year start
            pts = sorted(pts)
            prev = None
            for e, v in pts:
                n = _days(s0, e)
                if prev is not None and 150 <= n <= 300 and e not in q:
                    q[e] = v - prev[1]
                prev = (e, v)
            nine = [(e, v) for e, v in pts if 240 <= _days(s0, e) <= 300]
            for fe, fv in (ann.get(item) or {}).items():  # Q4 = full year - nine months
                if nine and 330 <= _days(s0, fe) <= 380 and fe not in q:
                    q[fe] = fv - nine[-1][1]
        out[item] = q
    for item, vals in inst.items():
        q = dict(vals)
        for fe, fv in (ann.get(item) or {}).items():  # year-end balance from the annual report
            q.setdefault(fe, fv)
        out[item] = q
    return out


def statements(cik: int, period: str = "annual", n: int = 6) -> tuple[list[str], dict[str, list]]:
    ann = annual(cik)
    src = ann if period == "annual" else quarterly(cik, ann)
    keys = set(src.get("revenue", {})) | set(src.get("net_income", {}))
    if period == "annual":  # one column per calendar year of the fiscal-year end
        seen = {}
        for k in sorted(keys):
            seen[k[:4]] = k
        ends = sorted(seen.values())[-n:]
    else:
        ends = sorted(k for k in keys if src.get("net_income", {}).get(k) is not None
                      or src.get("revenue", {}).get(k) is not None)[-n:]

    def col(item):
        return [src.get(item, {}).get(e) for e in ends]

    s = {k: col(k) for k in ("revenue", "gross_profit", "cogs", "operating_income", "net_income", "eps_diluted",
                             "da", "pretax_income", "income_tax", "cash", "total_assets", "total_liabilities",
                             "equity", "lt_debt", "st_debt", "current_assets", "current_liabilities", "ocf",
                             "capex", "dividends_paid", "buybacks", "sbc", "shares_diluted")}
    for i in range(len(ends)):
        if s["gross_profit"][i] is None and s["revenue"][i] is not None and s["cogs"][i] is not None:
            s["gross_profit"][i] = s["revenue"][i] - s["cogs"][i]
    return ends, s


def _latest(m: dict, before: str | None = None):
    ks = sorted(k for k in (m or {}) if before is None or k <= before)
    return (ks[-1], m[ks[-1]]) if ks else (None, None)


def key_stats(s: dict, ev: dict | None, bars: list[dict], spy: list[dict]) -> dict:
    """The numbers a quote page shows, from end-of-day prices and SEC filings."""
    cik = s["cik"]
    st: dict = {}
    if bars:
        last = bars[-1]
        st.update(close=last["close"], date=last["date"], open=last["open"], day_low=last["low"],
                  day_high=last["high"], volume=last["volume"])
        st["prev_close"] = bars[-2]["close"] if len(bars) > 1 else None
        yr = bars[_since(bars, 365):]
        st["wk52_low"] = min(b["low"] or b["close"] for b in yr)
        st["wk52_high"] = max(b["high"] or b["close"] for b in yr)
        vols = [b["volume"] for b in bars[-63:] if b["volume"]]
        st["avg_volume"] = sum(vols) / len(vols) if vols else None
        st["beta"] = beta(bars, spy)
        st["returns"] = period_returns(bars)
        st["spy_1y"] = dict(period_returns(spy)).get("1Y") if spy else None
    ratio = (s.get("adr_ratio") or 1.0) if s.get("is_foreign") else 1.0
    sh = db.one("SELECT value, as_of FROM shares_out WHERE cik=? ORDER BY as_of DESC, filed DESC LIMIT 1", (cik,))
    st["shares"] = sh["value"] / ratio if sh else None
    st["shares_as_of"] = sh["as_of"] if sh else None
    if st.get("close") and st["shares"]:
        st["market_cap"] = st["close"] * st["shares"]
    ann = annual(cik)
    det = (ev or {}).get("detail") or {}
    if s.get("is_foreign"):  # local-currency filings: use the valuation's converted P/E
        pe = det.get("pe_now")
        st["pe"], st["pe_basis"] = pe, "last fiscal year, converted to US$"
        st["eps"] = st["close"] / pe if pe and st.get("close") else None
    else:
        q = db.one("SELECT metrics_json, period_end, filed FROM qsnaps WHERE cik=? ORDER BY filed DESC LIMIT 1", (cik,))
        m = json.loads(q["metrics_json"]) if q else {}
        fe, eps_fy = _latest(ann.get("eps_diluted"))
        if m.get("ttm_net_income") is not None and m.get("shares"):
            st["eps"], st["pe_basis"] = m["ttm_net_income"] / m["shares"], f"trailing 12 months to {m.get('period_end') or q['period_end']}"
        else:
            st["eps"], st["pe_basis"] = eps_fy, f"fiscal year ended {fe}" if fe else ""
        st["pe"] = st["close"] / st["eps"] if st.get("close") and st.get("eps") and st["eps"] > 0 else None
        _, div = _latest(ann.get("dividends_paid"))
        _, shd = _latest(ann.get("shares_diluted"))
        if div and shd:
            st["dividend_ps"] = abs(div) / shd
            st["dividend_yield"] = st["dividend_ps"] / st["close"] if st.get("close") else None
        fe, rev = _latest(ann.get("revenue"))
        _, ni = _latest(ann.get("net_income"))
        st["revenue"], st["net_income"], st["fy_end"] = rev, ni, fe
        if rev and ni is not None:
            st["net_margin"] = ni / rev
        _, eq = _latest(ann.get("equity"))
        if eq and ni is not None and eq > 0:
            st["roe"] = ni / eq
    st["last_annual"] = s.get("last_filed")
    q = db.one("SELECT period_end, filed FROM qsnaps WHERE cik=? ORDER BY filed DESC LIMIT 1", (cik,))
    if q and q["period_end"] and q["filed"]:
        lag = _days(q["period_end"], q["filed"])
        st["last_quarter"] = f"{q['period_end']} (filed {q['filed']})"
        nxt = date.fromisoformat(q["period_end"]) + timedelta(days=91 + lag)
        while nxt < date.today():  # a missed or late filing: roll forward to the next quarter
            nxt += timedelta(days=91)
        st["next_report_est"] = nxt.isoformat()
    return st


# ------------------------------------------------------------------ formatting
def big(v, cur="$") -> str:
    if v is None:
        return "—"
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{'-' if v < 0 else ''}{cur}{a / div:,.2f}{suf}"
    return f"{'-' if v < 0 else ''}{cur}{a:,.0f}"


def _p(v, d=2) -> str:
    return "—" if v is None else f"${v:,.{d}f}"


def _pc(v, d=2) -> str:
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def stat_rows(st: dict, cur: str = "USD") -> list[tuple[str, str, str]]:
    """(label, value, tooltip) in the order quote pages use."""
    return [
        ("Previous close", _p(st.get("prev_close")), "Close of the trading day before the latest one"),
        ("Open", _p(st.get("open")), "First trade of the latest day"),
        ("Day's range", f"{_p(st.get('day_low'))} – {_p(st.get('day_high'))}", "Low and high of the latest day"),
        ("52-week range", f"{_p(st.get('wk52_low'))} – {_p(st.get('wk52_high'))}", "Lowest low and highest high, last 52 weeks"),
        ("Volume", big(st.get("volume"), ""), "Shares traded on the latest day"),
        ("Avg. volume (3 mo)", big(st.get("avg_volume"), ""), "Average daily shares traded, last 63 trading days"),
        ("Market cap", big(st.get("market_cap")), "Price × shares outstanding (latest SEC cover page)"),
        ("Shares outstanding", big(st.get("shares"), ""), f"As of {st.get('shares_as_of') or '—'}"),
        ("Beta (5Y monthly)", "—" if st.get("beta") is None else f"{st['beta'] + 0.0:.2f}".replace("-0.00", "0.00"),
         "How much the stock moved with the S&P 500: 1 = in step, above 1 = bigger swings"),
        ("P/E", "—" if st.get("pe") is None else f"{st['pe']:.1f}", f"Price ÷ earnings per share ({st.get('pe_basis') or '—'})"),
        ("EPS", _p(st.get("eps")), f"Diluted earnings per share ({st.get('pe_basis') or '—'})"),
        ("Dividend & yield", "—" if not st.get("dividend_ps") else
         f"{_p(st['dividend_ps'])} ({st['dividend_yield'] * 100:.2f}%)" if st.get("dividend_yield") else _p(st["dividend_ps"]),
         "Dividends paid per share in the last fiscal year, and as a % of today's price"),
        ("Revenue", big(st.get("revenue")) + (f" ({cur})" if cur != "USD" else ""), f"Fiscal year ended {st.get('fy_end') or '—'}"),
        ("Net margin", "—" if st.get("net_margin") is None else f"{st['net_margin'] * 100:.1f}%", "Net income ÷ revenue, last fiscal year"),
        ("Return on equity", "—" if st.get("roe") is None else f"{st['roe'] * 100:.1f}%", "Net income ÷ shareholders' equity, last fiscal year"),
        ("1-year change", _pc(dict(st.get("returns") or []).get("1Y")) +
         (f" (S&P 500 {_pc(st['spy_1y'])})" if st.get("spy_1y") is not None else ""), "Total return incl. dividends"),
        ("Last quarterly report", st.get("last_quarter") or "—", "Quarter end and SEC filing date of the latest 10-Q/10-K"),
        ("Next report (estimate)", st.get("next_report_est") or "—",
         "Estimated from the company's usual filing lag; not an announced date"),
    ]


# ------------------------------------------------------------------ chart
def chart_options(bars: list[dict], spy: list[dict], days: int | None, kind: str = "candle",
                  compare: bool = False) -> dict:
    i0 = _since(bars, days)
    win = bars[i0:]
    dates = [b["date"] for b in win]
    closes_all = [b["close"] for b in bars]

    def sma(n):
        out, run = [], 0.0
        for k, c in enumerate(closes_all):
            run += c
            if k >= n:
                run -= closes_all[k - n]
            if k >= i0:
                out.append(round(run / n, 2) if k >= n - 1 else None)
        return out

    up = win[-1]["close"] >= win[0]["close"] if win else True
    col = UP if up else DOWN
    series = []
    if compare:
        base = win[0]["adj"] if win else 1
        sp = {b["date"]: b["adj"] for b in spy}
        sp_dates = sorted(sp)
        import bisect

        def sp_at(d):
            k = bisect.bisect_right(sp_dates, d) - 1
            return sp[sp_dates[k]] if k >= 0 else None

        s0 = sp_at(dates[0]) if dates else None
        series.append({"name": "This stock", "type": "line", "showSymbol": False, "data":
                       [round((b["adj"] / base - 1) * 100, 2) for b in win], "lineStyle": {"color": col, "width": 1.6},
                       "itemStyle": {"color": col}})
        series.append({"name": "S&P 500", "type": "line", "showSymbol": False, "data":
                       [round((sp_at(d) / s0 - 1) * 100, 2) if s0 and sp_at(d) else None for d in dates],
                       "lineStyle": {"color": "#8A5A12", "width": 1.2}, "itemStyle": {"color": "#8A5A12"}})
        y_fmt = "{value}%"
    else:
        if kind == "candle":
            series.append({"name": "Price", "type": "candlestick",
                           "data": [[b["open"] or b["close"], b["close"], b["low"] or b["close"], b["high"] or b["close"]]
                                    for b in win],
                           "itemStyle": {"color": UP, "color0": DOWN, "borderColor": UP, "borderColor0": DOWN}})
        else:
            series.append({"name": "Price", "type": "line", "showSymbol": False, "data": [b["close"] for b in win],
                           "lineStyle": {"color": col, "width": 1.6}, "itemStyle": {"color": col},
                           "areaStyle": {"color": {"type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1, "colorStops": [
                               {"offset": 0, "color": col + "33"}, {"offset": 1, "color": col + "00"}]}}})
        series += [{"name": "50-day avg", "type": "line", "showSymbol": False, "data": sma(50),
                    "lineStyle": {"width": 1, "color": "#D6A840"}, "itemStyle": {"color": "#D6A840"}},
                   {"name": "200-day avg", "type": "line", "showSymbol": False, "data": sma(200),
                    "lineStyle": {"width": 1.2, "color": "#6F582C"}, "itemStyle": {"color": "#6F582C"}}]
        y_fmt = "${value}"
    series.append({"name": "Volume", "type": "bar", "xAxisIndex": 1, "yAxisIndex": 1,
                   "data": [{"value": b["volume"] or 0, "itemStyle": {"color": (UP if b["close"] >= (b["open"] or b["close"])
                                                                                else DOWN) + "88"}} for b in win]})
    return {
        "animation": False,
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": {"top": 0, "data": [x["name"] for x in series if x["name"] != "Volume"]},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "grid": [{"left": 64, "right": 24, "top": 30, "height": "62%"}, {"left": 64, "right": 24, "top": "78%", "height": "14%"}],
        "xAxis": [{"type": "category", "data": dates, "boundaryGap": True, "axisLine": {"onZero": False},
                   "axisLabel": {"show": False}},
                  {"type": "category", "gridIndex": 1, "data": dates, "boundaryGap": True}],
        "yAxis": [{"scale": True, "axisLabel": {"formatter": y_fmt}, "splitLine": {"lineStyle": {"color": GRID}}},
                  {"gridIndex": 1, "scale": True, "axisLabel": {"show": False}, "splitLine": {"show": False}}],
        "dataZoom": [{"type": "inside", "xAxisIndex": [0, 1]}],
        "series": series,
    }


def fin_options(ends: list[str], s: dict, period: str) -> dict:
    lab = [e[:7] if period == "quarterly" else e[:4] for e in ends]
    return {
        "animation": False, "tooltip": {"trigger": "axis"}, "legend": {"top": 0},
        "grid": {"left": 70, "right": 20, "top": 30, "bottom": 30},
        "xAxis": {"type": "category", "data": lab},
        "yAxis": {"type": "value", "axisLabel": {"formatter": "{value}"}},
        "series": [
            {"name": "Revenue", "type": "bar", "data": [round(v / 1e6) if v is not None else None for v in s["revenue"]],
             "itemStyle": {"color": "#1f6f4a"}},
            {"name": "Net income", "type": "bar", "data": [round(v / 1e6) if v is not None else None for v in s["net_income"]],
             "itemStyle": {"color": "#D6A840"}},
        ],
    }


def _ratio(a, b):
    return a / b if a is not None and b else None


def fin_rows(s: dict, which: str) -> list[tuple[str, list, str]]:
    """(label, values, kind) per statement. kind: money | pct | ps."""
    debt = [(a or 0) + (b or 0) if a is not None or b is not None else None for a, b in zip(s["lt_debt"], s["st_debt"])]
    ebitda = [(o + (d or 0)) if o is not None else None for o, d in zip(s["operating_income"], s["da"])]
    fcf = [(o - abs(c or 0)) if o is not None else None for o, c in zip(s["ocf"], s["capex"])]
    if which == "income":
        return [("Revenue", s["revenue"], "money"), ("Gross profit", s["gross_profit"], "money"),
                ("Operating income", s["operating_income"], "money"), ("Net income", s["net_income"], "money"),
                ("Net profit margin", [_ratio(a, b) for a, b in zip(s["net_income"], s["revenue"])], "pct"),
                ("Earnings per share (diluted)", s["eps_diluted"], "ps"), ("EBITDA", ebitda, "money"),
                ("Effective tax rate", [_ratio(t, p) if p and p > 0 else None for t, p in zip(s["income_tax"], s["pretax_income"])], "pct")]
    if which == "balance":
        return [("Cash and equivalents", s["cash"], "money"), ("Total assets", s["total_assets"], "money"),
                ("Total liabilities", s["total_liabilities"], "money"), ("Total debt", debt, "money"),
                ("Shareholders' equity", s["equity"], "money"),
                ("Debt ÷ equity", [_ratio(d, e) if e and e > 0 else None for d, e in zip(debt, s["equity"])], "x"),
                ("Current ratio", [_ratio(a, b) for a, b in zip(s["current_assets"], s["current_liabilities"])], "x")]
    return [("Operating cash flow", s["ocf"], "money"), ("Capital spending", [-abs(v) if v is not None else None for v in s["capex"]], "money"),
            ("Free cash flow", fcf, "money"), ("Dividends paid", [-abs(v) if v is not None else None for v in s["dividends_paid"]], "money"),
            ("Buybacks", [-abs(v) if v is not None else None for v in s["buybacks"]], "money"),
            ("Stock-based pay", s["sbc"], "money")]


def _fmt(v, kind, cur):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "x":
        return f"{v:.2f}×"
    if kind == "ps":
        return f"{v:,.2f}"
    return big(v, "" if cur != "USD" else "$")


# ------------------------------------------------------------------ the tab
def render(s: dict, ev: dict | None) -> None:
    bars = ohlcv(s["ticker"])
    spy = ohlcv("SPY")
    if not bars:
        ui.label("No price history for this stock yet. Prices are downloaded for the shortlist and anything you hold "
                 "or watch; the next daily update adds it once it qualifies.").classes("oe-muted")
    st = key_stats(s, ev, bars, spy)
    cur = s.get("currency") or "USD"
    if bars:
        last, prev = st["close"], st.get("prev_close")
        chg = (last / prev - 1) if prev else None
        with ui.row().classes("items-baseline gap-3"):
            ui.label(f"${last:,.2f}").classes("text-3xl font-bold oe-num")
            if chg is not None:
                ui.label(f"{last - prev:+,.2f} ({chg * 100:+.2f}%)").classes("text-lg font-semibold").style(
                    f"color:{UP if chg >= 0 else DOWN}")
            ui.label(f"close of {st['date']} · end-of-day prices").classes("oe-small")
        state = {"days": 365, "kind": "candle", "compare": False}
        with ui.card().classes("oe-card w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                btns = ui.row().classes("gap-1")
                with ui.row().classes("items-center gap-3"):
                    kind = ui.toggle({"candle": "Candles", "line": "Line"}, value="candle").props("dense no-caps")
                    cmp_ = ui.switch("Compare with S&P 500")
            chart = ui.echart(chart_options(bars, spy, 365)).classes("w-full").style("height:430px")
            rets = ui.row().classes("gap-5 oe-small")

            def redraw():
                chart.options.clear()
                chart.options.update(chart_options(bars, spy, state["days"], state["kind"], state["compare"]))
                chart.update()
                for lab, b in buttons.items():
                    b.props(f"color={'primary' if lab == state['label'] else 'grey-7'}")

            def pick(lab, days):
                state.update(days=days, label=lab)
                redraw()

            buttons = {}
            with btns:
                for lab, days in RANGES:
                    buttons[lab] = ui.button(lab, on_click=lambda lab=lab, days=days: pick(lab, days)).props(
                        f"flat dense no-caps color={'primary' if lab == '1Y' else 'grey-7'}")
            state["label"] = "1Y"
            kind.on_value_change(lambda e: (state.update(kind=e.value), redraw()))
            cmp_.on_value_change(lambda e: (state.update(compare=e.value), redraw()))
            with rets:
                for lab, r in st["returns"]:
                    ui.html(f"{lab} <b style='color:{UP if (r or 0) >= 0 else DOWN}'>{_pc(r)}</b>", sanitize=False)
    with ui.card().classes("oe-card w-full"):
        ui.label("Key statistics").classes("oe-h2")
        rows = stat_rows(st, cur)
        half = (len(rows) + 1) // 2
        with ui.row().classes("w-full gap-8 no-wrap items-start"):
            for part in (rows[:half], rows[half:]):
                with ui.column().classes("flex-1 gap-0"):
                    for lab, val, tip in part:
                        with ui.row().classes("w-full justify-between py-1 border-b border-gray-100 no-wrap"):
                            ui.label(lab).classes("text-sm oe-muted").tooltip(tip)
                            ui.label(val).classes("text-sm font-semibold oe-num text-right")
        ui.label("From your price source's end-of-day data and the company's SEC filings. No live quotes, analyst "
                 "estimates or news." + (f" Foreign filer: statements below are in {cur}." if cur != "USD" else "")
                 ).classes("oe-small mt-1")
    with ui.card().classes("oe-card w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Financials").classes("oe-h2")
            with ui.row().classes("items-center gap-3"):
                stmt = ui.toggle({"income": "Income statement", "balance": "Balance sheet", "cash": "Cash flow"},
                                 value="income").props("dense no-caps")
                per = ui.toggle({"annual": "Annual", "quarterly": "Quarterly"}, value="annual").props("dense no-caps")
        box = ui.column().classes("w-full")

        def show():
            box.clear()
            ends, sd = statements(s["cik"], per.value)
            with box:
                if not ends:
                    ui.label("No statements for this company yet." if per.value == "annual" else
                             "No quarterly figures (foreign filers report once a year).").classes("oe-muted")
                    return
                ui.echart(fin_options(ends, sd, per.value)).classes("w-full").style("height:240px")
                ui.label(f"Revenue and net income, millions of {cur}").classes("oe-small")
                hdr = [e[:7] if per.value == "quarterly" else "FY " + e[:4] for e in ends]
                lines = fin_rows(sd, stmt.value)
                cols = [{"field": "item", "headerName": f"{cur}", "minWidth": 210, "flex": 2, "pinned": "left"}] + [
                    {"field": f"c{i}", "headerName": h, "minWidth": 100, "flex": 1, "type": "rightAligned"}
                    for i, h in enumerate(hdr)] + [{"field": "yoy", "headerName": "Y/Y", "minWidth": 90, "flex": 1,
                                                     "type": "rightAligned"}]
                rows_ = []
                for lab, vals, kind_ in lines:
                    row = {"item": lab}
                    for i, v in enumerate(vals):
                        row[f"c{i}"] = _fmt(v, kind_, cur)
                    back = 4 if per.value == "quarterly" else 1
                    a, b = (vals[-1], vals[-1 - back]) if len(vals) > back else (None, None)
                    row["yoy"] = ("—" if kind_ in ("pct", "x") or a is None or not b or b < 0
                                  else f"{(a / b - 1) * 100:+.1f}%")
                    rows_.append(row)
                ui.aggrid({"columnDefs": cols, "rowData": rows_, "domLayout": "autoHeight",
                           "defaultColDef": {"sortable": False, "resizable": True}, "suppressCellFocus": True},
                          theme="quartz").classes("w-full")
                ui.label(("Y/Y compares the latest quarter with the same quarter a year earlier. Q4 = full year minus "
                          "the first nine months." if per.value == "quarterly" else
                          "Y/Y compares the latest fiscal year with the one before.") +
                         " Cash flows are shown as money in (+) or out (−).").classes("oe-small")

        stmt.on_value_change(lambda e: show())
        per.on_value_change(lambda e: show())
        show()

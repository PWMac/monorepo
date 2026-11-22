"""Wonderfulness: the stock page's "Wonderful?" tab and the data behind the Screener (/screener)."""

from __future__ import annotations

import html
import json

from nicegui import run, ui

from .. import ceo, db, rules, wonderful
from ..sources import company
from .layout import empty, kpi
from .theme import num, pct

TIER_COL = {"Exceptional": "#1f6f4a", "Wonderful": "#3c8d5f", "Good": "#8A5A12", "Average": "#6b7280",
            "Weak": "#a33a2b", "Not scored": "#6b7280"}
PILLAR_ICON = {"moat": "castle", "growth": "trending_up", "strength": "shield", "allocation": "account_balance"}


def _bar(p: float | None, color: str = "#1f6f4a") -> str:
    """A thin percentile bar (0-1)."""
    if p is None:
        return "<span class='oe-small'>—</span>"
    w = max(2, min(100, round(p * 100)))
    return (f"<div style='display:flex;align-items:center;gap:8px;min-width:160px'>"
            f"<div style='flex:1;height:8px;background:#e8ece9;border-radius:4px'>"
            f"<div style='width:{w}%;height:8px;background:{color};border-radius:4px'></div></div>"
            f"<span style='font-size:12px;color:#4b5563;width:34px;text-align:right'>{w}</span></div>")


def _pcol(p):
    if p is None:
        return "#9aa5a0"
    return "#1f6f4a" if p >= 0.8 else "#3c8d5f" if p >= 0.6 else "#c9a227" if p >= 0.4 else "#d9822b" if p >= 0.2 else "#a33a2b"


def _bign(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    return f"${v / 1e12:,.2f}T" if a >= 1e12 else f"${v / 1e9:,.1f}B" if a >= 1e9 else f"${v / 1e6:,.0f}M"


def render(s: dict, prof) -> None:
    """The "Wonderful?" tab: the score, how it compares, what sets the company apart, red flags with what would
    need to change, its leaders and their record with the money."""
    cik = s["cik"]
    if cik in wonderful.financial_ciks():
        with ui.card().classes("oe-card w-full"):
            ui.label("Not scored for wonderfulness").classes("oe-h2")
            ui.label("Banks, lenders, brokers and insurers borrow or hold other people's money as their raw material, "
                     "so their returns on capital and debt can't be compared with an operating business's. The Report "
                     "card judges them with the bank or insurer rulebook instead.").classes("oe-muted")
        _management(s)
        _ceo_card(s)
        return
    wonderful.ensure(prof)
    w = wonderful.get(cik, prof)
    if not w:
        empty("Not enough annual reports yet to score this company (it needs several years of results).")
        _management(s)
        return
    col = TIER_COL.get(w["tier"])
    top = w["rank"] / w["n"]
    with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
        kpi(num(w["score"], 0), "Wonderfulness (0–100)", w["tier"], col)
        kpi(f"#{w['rank']:,}", f"of {w['n']:,} companies scored", f"top {max(top * 100, 0.1):.1f}%" if top < 0.5
            else f"bottom {max((1 - top) * 100, 0.1):.0f}%")
        snap = db.one("SELECT quality, gates_passed FROM snapshots WHERE cik=? AND profile_id=? AND profile_version=? "
                      "ORDER BY filed DESC LIMIT 1", (cik, *prof.snap))
        q_ok = bool(snap and snap["gates_passed"] and (snap["quality"] or 0) >= prof.settings.get("quality_min", 70))
        kpi("Passes" if q_ok else "Fails", "Oracle checklist (Quality)",
            (f"Quality {snap['quality']:.0f} vs the bar of {prof.settings.get('quality_min', 70):.0f}"
             + ("" if not snap or snap["gates_passed"] else ", a must-pass rule failed")) if snap and snap["quality"]
            is not None else "not scored", "#1f6f4a" if q_ok else "#a33a2b")
        kpi(_bign(w["f"].get("mcap")), "Market value", f"latest annual report: fiscal {w['as_of'][:4]}")
    with ui.card().classes("oe-card w-full"):
        ui.label("The four pillars").classes("oe-h2")
        ui.label("Each measure is ranked against every scored company (100 = the best). Pillars are averages of their "
                 "measures; the score weighs moat 40%, growth 30%, strength 20%, capital allocation 10%. These weights "
                 "come from testing which traits best predicted that a company kept high returns and kept growing "
                 "(Guide → Wonderfulness).").classes("oe-small")
        with ui.grid(columns=2).classes("w-full gap-4"):
            for p, items in wonderful.PILLARS.items():
                ps = w["pillars"].get(p)
                with ui.column().classes("gap-1"):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon(PILLAR_ICON[p]).classes("text-lg").style(f"color:{_pcol((ps or 50) / 100)}")
                        ui.label(wonderful.PILLAR_NAMES[p]).classes("font-semibold")
                        ui.label(f"{ps:.0f}" if ps is not None else "no data (counted as 50)").classes(
                            "text-lg font-bold").style(f"color:{_pcol((ps or 50) / 100)}")
                    rows = []
                    for k, _ in items:
                        label, fmt, target = wonderful.FEATURES[k]
                        v = w["f"].get(k)
                        val = wonderful._fmt(v, fmt) if v is not None else "—"
                        if k == "rev_up_share" and w["f"].get("rev_years"):
                            val = f"{w['f']['rev_up']} of {w['f']['rev_years']}"
                        p_ = w["pct"].get(k)
                        rows.append(f"<tr><td style='padding:2px 8px 2px 0'>{html.escape(label)}</td>"
                                    f"<td style='padding:2px 8px;text-align:right;white-space:nowrap'><b>{val}</b></td>"
                                    f"<td style='padding:2px 0;width:45%'>{_bar(p_, _pcol(p_))}</td></tr>")
                    ui.html("<table style='width:100%;font-size:13px'>" + "".join(rows) + "</table>",
                            sanitize=False).classes("w-full")
    strengths, flags = wonderful.explain(w)
    qflags = _quarter_flags(cik)
    with ui.grid(columns=2).classes("w-full gap-3"):
        with ui.card().classes("oe-card").style("border-left:4px solid #1f6f4a"):
            ui.label("What sets it apart").classes("oe-h2")
            if not strengths:
                ui.label("Nothing stands out: no measure is in the top 10% or at a wonderful level.").classes("oe-muted")
            for x in strengths[:8]:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("check_circle").classes("text-green-700 mt-0.5")
                    ui.label(x["text"]).classes("text-sm")
        with ui.card().classes("oe-card").style("border-left:4px solid #a33a2b"):
            ui.label("Red flags and what would need to change").classes("oe-h2")
            if not flags and not qflags:
                ui.label("No red flags on the 10-year record or in the latest quarterly report.").classes("oe-muted")
            for x in flags:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("flag").classes("text-red-700 mt-0.5")
                    with ui.column().classes("gap-0"):
                        ui.label(x["text"]).classes("text-sm font-semibold")
                        ui.label(x["change"]).classes("oe-small")
            if qflags:
                ui.label(f"Latest quarterly report ({qflags[0]})").classes("text-sm font-semibold pt-2")
                for f in qflags[1]:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.icon("warning" if f.get("hard") or f.get("severe") else "info").classes(
                            "text-amber-700 mt-0.5")
                        ui.label(f"{f['name']}: {f['detail']}").classes("text-sm")
            if w["f"].get("missing"):
                ui.label("Not enough data for: " + ", ".join(wonderful.PILLAR_NAMES[m] for m in w["f"]["missing"]) +
                         " (counted as average).").classes("oe-small pt-2")
    _history(cik, prof)
    _capital(cik, w)
    _management(s)
    _ceo_card(s)
    with ui.expansion("How to read this", icon="help_outline").classes("w-full oe-card"):
        ui.markdown(
            "- **Wonderfulness ignores the price.** It asks only how good the business is. Whether it's worth buying "
            "today is the Report card's job.\n"
            "- **Tiers:** 80+ exceptional (about the top 1–2% of companies), 70–80 wonderful (roughly the next 6%), "
            "60–70 good, 45–60 average, under 45 weak. No company is the best at everything, so even the very best "
            "score in the 80s.\n"
            "- **What it predicted** (every company and fiscal year 2012–2022, followed for three more years): of "
            "companies rated exceptional, 47% went on to grow sales and profits 7%+ a year while earning 15%+ on "
            "capital, and their median return on capital stayed 34%; wonderful 29%; good 16%; average 7%; weak 1%. "
            "Across all companies it was 9%. A much better bet than average, not a guarantee.\n"
            "- **What the numbers can't see:** the durability of the moat, the honesty of management, technology "
            "change. The management section and the red flags are where to start your own judgment.")


def header_chips(s: dict, prof) -> None:
    """Wonderfulness and the Oracle checklist at a glance, under the ticker on every stock page."""
    try:
        w = wonderful.get(s["cik"], prof)
        q = db.one("SELECT quality, gates_passed FROM snapshots WHERE cik=? AND profile_id=? AND profile_version=? "
                   "ORDER BY filed DESC LIMIT 1", (s["cik"], *prof.snap))
    except Exception:
        return
    with ui.row().classes("items-center gap-2"):
        if w:
            h = wonderful.history(s["cik"], prof)
            t = wonderful.summarize([(x["fy"], x["score"]) for x in h])
            arrow = ""
            if t.get("chg") is not None:
                sym, word, _ = TREND_WORD[t["dir"]]
                arrow = f" {sym} {t['chg']:+.0f}"
            ui.html(f"<span title='Change over the last 3 fiscal years: see the Wonderful? tab' style='background:"
                    f"{TIER_COL.get(w['tier'])};color:#fff;border-radius:10px;padding:1px 9px;font-size:12px'>"
                    f"Wonderfulness {w['score']:.0f} · {w['tier']}{arrow}</span>", sanitize=False)
        if q and q["quality"] is not None:
            ok = bool(q["gates_passed"]) and q["quality"] >= float(prof.settings.get("quality_min", 70))
            ui.html(f"<span style='border:1px solid {'#1f6f4a' if ok else '#a33a2b'};color:{'#1f6f4a' if ok else '#a33a2b'};"
                    f"border-radius:10px;padding:0 9px;font-size:12px'>Checklist {'✓' if ok else '✗'} · Quality "
                    f"{q['quality']:.0f}</span>", sanitize=False)


def _quarter_flags(cik: int):
    r = db.one("SELECT filed, flags_json FROM qsnaps WHERE cik=? ORDER BY filed DESC LIMIT 1", (cik,))
    if not r:
        return None
    fl = json.loads(r["flags_json"] or "[]")
    return (r["filed"], fl) if fl else None


TREND_WORD = {"up": ("▲", "rising", "#1f6f4a"), "down": ("▼", "falling", "#a33a2b"), "flat": ("▬", "steady", "#6b7280"),
              "new": ("•", "new", "#9aa5a0")}


def history_chart(h: list[dict], *, pillars: bool = True, compact: bool = False) -> dict:
    """Wonderfulness, Quality and their average by fiscal year, on the tier bands; pillars hidden until clicked."""
    labs = [x["fy"] for x in h]

    def ser(name, key, color, width=2, dashed=False, shown=True, dec=1):
        return {"type": "line", "name": name, "data": [None if x.get(key) is None else round(x[key], dec) for x in h],
                "connectNulls": True, "showSymbol": not compact, "symbolSize": 6, "z": 3 if width > 2 else 2,
                "lineStyle": {"width": width, "color": color, "type": "dashed" if dashed else "solid"},
                "itemStyle": {"color": color}}
    series = [ser("Wonderfulness", "score", "#1f6f4a", 3), ser("Quality", "quality", "#2c5d8f"),
              ser("Average of both", "combined", "#c9a227", 2, dashed=True)]
    series[0]["markArea"] = {"silent": True, "label": {"show": not compact,
                             "position": "insideTopLeft", "fontSize": 10, "color": "#6b7280"},
                             "data": [[{"name": "Exceptional 80+", "yAxis": 80, "itemStyle": {"color": "rgba(31,111,74,0.10)"}},
                                       {"yAxis": 100}],
                                      [{"name": "Wonderful 70–80", "yAxis": 70, "itemStyle": {"color": "rgba(201,162,39,0.08)"}},
                                       {"yAxis": 80}]]}
    selected = {"Wonderfulness": True, "Quality": True, "Average of both": True}
    if pillars:
        for p, c in (("moat", "#2c5d8f"), ("growth", "#c9a227"), ("strength", "#7a4f9e"), ("allocation", "#9aa5a0")):
            name = "Pillar: " + wonderful.PILLAR_NAMES[p].split(":")[0]
            series.append(ser(name, p, c, 1, dec=0))
            selected[name] = False
    ranks = [f"#{x['rank']:,} of {x['n']:,}" + (" (today)" if x.get("now") else "") if x.get("rank") else "" for x in h]
    lo = min([x["score"] for x in h] + [x["quality"] for x in h if x.get("quality") is not None] + [70])
    return {"tooltip": {"trigger": "axis", ":formatter":
                        "ps => { const r = " + json.dumps(ranks) + "; let s = `<b>${ps[0].axisValue}</b>`"
                        " + (r[ps[0].dataIndex] ? ` · rank ${r[ps[0].dataIndex]}` : ''); "
                        "ps.forEach(p => { if (p.value != null) s += `<br>${p.marker}${p.seriesName}: <b>${p.value}</b>`; });"
                        " return s; }"},
            "legend": {"top": 0, "selected": selected, "type": "scroll"},
            "grid": {"left": 40, "right": 16, "top": 36 if not compact else 30, "bottom": 28},
            "xAxis": {"type": "category", "data": labs, "boundaryGap": False},
            "yAxis": {"type": "value", "min": max(0, int(lo // 10 * 10) - 10), "max": 100},
            "series": series}


def trend_facts(h: list[dict]) -> list[str]:
    """One-line facts under the history chart."""
    if not h:
        return []
    t = wonderful.summarize([(x["fy"], x["score"]) for x in h])
    out = [f"Wonderful or better (70+) in {t['years_70']} of {t['years']} years"]
    last5 = [x["score"] for x in h[-5:]]
    out.append(f"{sum(v >= 70 for v in last5)} of the last {len(last5)}")
    out.append(f"first Exceptional: {t['first_80']}" if t["first_80"] else "never Exceptional (80+)")
    if t.get("chg") is not None:
        sym, word, _ = TREND_WORD[t["dir"]]
        out.append(f"{sym} {word}: {t['chg']:+.0f} since {t['since']}")
    qs = [(x["fy"], x["quality"]) for x in h if x.get("quality") is not None]
    qt = wonderful.summarize(qs)
    if qt.get("chg") is not None:
        out.append(f"Quality {qt['chg']:+.0f} since {qt['since']}")
    return out


def _history(cik: int, prof) -> None:
    h = wonderful.history(cik, prof)
    if len(h) < 2:
        return
    with ui.card().classes("oe-card w-full"):
        ui.label("Score history").classes("oe-h2")
        ui.label("Each fiscal year scored against every company reporting that year; the last point is today's score. "
                 "Quality is the Oracle checklist score from that year's annual report. Click a legend entry to show the "
                 "four pillars.").classes("oe-small")
        with ui.row().classes("gap-2 pt-1"):
            for f in trend_facts(h):
                ui.label(f).classes("oe-small").style("background:#eef3f0;border-radius:10px;padding:1px 9px")
        ui.echart(history_chart(h)).classes("w-full").style("height:320px")
        ui.label("What it predicted (companies at 70+, fiscal years 2016–2022): of those that had also been 70+ in each of "
                 "the four years before, 77% were still 70+ three years later; of those reaching 70+ for the first time, "
                 "47%. Consistency mattered more than direction: a jump from lower down held only about 60% of the time.").classes("oe-small")


def _capital(cik: int, w: dict) -> None:
    c = wonderful.capital_record(cik)
    if not c:
        return
    with ui.card().classes("oe-card w-full"):
        ui.label(f"What management did with the money ({c['from']}–{c['to']}, {c['years']} annual reports)").classes(
            "oe-h2")
        ni, fcf = c.get("net_income"), c.get("fcf")
        base = fcf if fcf and fcf > 0 else ni

        def share(v):
            return f" ({v / base:.0%} of {'free cash flow' if base is fcf else 'profit'})" if v and base and base > 0 else ""
        with ui.grid(columns=4).classes("w-full gap-3 oe-kpis"):
            kpi(_bign(ni), "Total profit", f"free cash flow {_bign(fcf)}")
            kpi(_bign(c.get("dividends_paid")), "Paid as dividends", share(c.get("dividends_paid")).strip(" ()"))
            kpi(_bign(c.get("buybacks")), "Spent on buybacks", share(c.get("buybacks")).strip(" ()"))
            kpi(_bign(c.get("acquisitions")), "Spent on acquisitions",
                f"sales added {_bign(c.get('revenue_added'))}" if c.get("revenue_added") is not None else None)
        notes = []
        f = w.get("f", {})
        if f.get("dollar_test") is not None:
            notes.append(f"Each $1 kept in the business became ${f['dollar_test']:.2f} of market value (the $1 test; "
                         "the Oracle wants at least $1).")
        if f.get("share_change") is not None:
            notes.append(f"Share count changed {f['share_change'] * 100:+.1f}% a year "
                         f"({'shrinking: owners own more each year' if f['share_change'] < -0.002 else 'growing: owners are diluted' if f['share_change'] > 0.002 else 'about flat'}).")
        if c.get("sbc") and fcf and fcf > 0:
            notes.append(f"Stock-based pay totalled {_bign(c['sbc'])}, {c['sbc'] / fcf:.0%} of free cash flow.")
        if f.get("roic_incr") is not None:
            notes.append(f"The capital added over the last years earned {f['roic_incr'] * 100:.0f}% (incremental ROIC).")
        if c.get("acquisitions") and c.get("revenue_added") and c["revenue_added"] > 0 and \
                c["acquisitions"] >= 1.5 * c["revenue_added"]:
            notes.append("Acquisition spending was large next to the sales added: some growth was bought, not built.")
        for n in notes:
            ui.label("• " + n).classes("text-sm")


def _management(s: dict) -> None:
    box = ui.card().classes("oe-card w-full")
    with box:
        ui.label("Leaders and ownership").classes("oe-h2")
        msg = ui.label("Loading from Yahoo Finance…").classes("oe-muted")

    async def load():
        info = await run.io_bound(company.get, s["ticker"])
        msg.delete()
        with box:
            if not info:
                ui.label("No company profile available (Yahoo Finance had nothing, or the app is offline).").classes(
                    "oe-muted")
                return
            if info.get("summary"):
                ui.label(info["summary"]).classes("text-sm").style("max-width:140ch")
            facts = [x for x in (info.get("sector"), info.get("industry"),
                                 f"{info['employees']:,} employees" if info.get("employees") else None,
                                 info.get("country"), info.get("website")) if x]
            if facts:
                ui.label(" · ".join(facts)).classes("oe-small")
            with ui.grid(columns=2).classes("w-full gap-4 pt-2"):
                with ui.column().classes("gap-1"):
                    ui.label("Leaders").classes("font-semibold")
                    offs = info.get("officers") or []
                    if not offs:
                        ui.label("No officers listed.").classes("oe-muted")
                    rows = "".join(
                        f"<tr><td style='padding:2px 10px 2px 0'><b>{html.escape(o['name'] or '')}</b><br>"
                        f"<span style='color:#6b7280'>{html.escape(o.get('title') or '')}</span></td>"
                        f"<td style='padding:2px 10px;text-align:right'>{o['age'] or '—'}</td>"
                        f"<td style='padding:2px 0;text-align:right'>{_bign(o['pay']) if o.get('pay') else '—'}</td></tr>"
                        for o in offs[:10])
                    if rows:
                        ui.html("<table style='width:100%;font-size:13px'><tr style='color:#6b7280'><th style='text-align:"
                                "left'>Name and role</th><th style='text-align:right'>Age</th><th style='text-align:"
                                "right'>Pay</th></tr>" + rows + "</table>", sanitize=False).classes("w-full")
                with ui.column().classes("gap-1"):
                    ui.label("Ownership and governance").classes("font-semibold")
                    if info.get("insiders") is not None:
                        ui.label(f"Insiders own {pct(info['insiders'], 1)} of the shares"
                                 + (" (owners running the business: a good sign)" if info["insiders"] >= 0.05 else "")
                                 + ".").classes("text-sm")
                    if info.get("institutions") is not None:
                        ui.label(f"Institutions own {pct(info['institutions'], 0)}.").classes("text-sm")
                    for a in info.get("insider_activity") or []:
                        if a.get("label") and ("Net" in a["label"] or "Purchases" in a["label"] or "Sales" in a["label"]):
                            ui.label(f"{a['label']} (last 6 months): {num(a['shares'], 0)} shares"
                                     + (f", {a['trades']} trades" if a.get("trades") else "")).classes("text-sm")
                    gov = info.get("governance") or {}
                    if gov:
                        ui.label("Governance risk (ISS, 1 = lowest risk, 10 = highest): " +
                                 " · ".join(f"{k} {int(v)}" for k, v in gov.items())).classes("text-sm")
                        if gov.get("Overall", 0) >= 8:
                            ui.label("High overall governance risk: read the proxy statement before trusting "
                                     "management.").classes("text-sm text-red-800")
                    pays = [o["pay"] for o in info.get("officers") or [] if o.get("pay")]
                    if pays:
                        ui.label(f"Listed leaders' pay: {_bign(sum(pays))} in total.").classes("oe-small")
            ui.label("Source: Yahoo Finance (free and unofficial; refreshed weekly). Tenure, track record and integrity "
                     "aren't in any data feed: read the latest proxy statement (DEF 14A) and shareholder letters on "
                     "SEC EDGAR.").classes("oe-small pt-1")

    ui.timer(0.2, load, once=True)


GRADE_COL = {"A": "#1f6f4a", "B": "#3c8d5f", "C": "#8A5A12", "D": "#d9822b", "F": "#a33a2b"}
PART_NAMES = {"owners": "Owners' return vs the S&P 500", "growth": "Growth of sales and profit per share",
              "returns": "Returns on capital (level and change)", "capital": "Capital allocation"}


def _rec_lines(m: dict) -> list[str]:
    out = []
    if m.get("excess") is not None:
        out.append(f"Stock {m['tsr'] * 100:+.1f}% a year vs S&P 500 {m['spy'] * 100:+.1f}% "
                   f"({m['price_from']} → {m['price_to']})")
    if m.get("rev_cagr") is not None:
        out.append(f"Sales {m['rev_cagr'] * 100:+.1f}% a year" + (
            f", profit per share {m['nips_cagr'] * 100:+.1f}% a year" if m.get("nips_cagr") is not None else ""))
    if m.get("roic_during") is not None:
        out.append(f"ROIC averaged {m['roic_during'] * 100:.0f}% in office" + (
            f" vs {m['roic_before'] * 100:.0f}% in the 3 years before" if m.get("roic_before") is not None else ""))
    if m.get("dollar_test") is not None:
        out.append(f"Each $1 kept became ${m['dollar_test']:.2f} of market value")
    if m.get("incr_return") is not None:
        out.append(f"Return on the capital added: {m['incr_return'] * 100:.0f}%")
    if m.get("share_cagr") is not None:
        out.append(f"Share count {m['share_cagr'] * 100:+.1f}% a year")
    if m.get("acquisitions") and m.get("revenue_added") is not None:
        out.append(f"Acquisitions {_bign(m['acquisitions'])} vs sales added {_bign(m['revenue_added'])}")
    return out


def ceo_data(s: dict) -> dict:
    """Everything the CEO card shows (network and database work; run off the UI thread). Also saves the grade
    for the ranking page."""
    res = ceo.assess(s["cik"], s["ticker"])
    cur = res.get("current")
    hist = [h for h in res.get("history") or [] if h.get("start")]
    prev = []
    for i, h in enumerate(hist):
        end = h.get("end") or (hist[i + 1]["start"] if i + 1 < len(hist) else None)
        if not end or (cur and h["name"] == cur.get("name") and not h.get("end")):
            continue
        if end > "2010-06-30":
            prev.append((h, end, ceo.record(s["cik"], s["ticker"], h["start"], end).get("rating")))
    res["prev"] = prev[-4:]
    return res


def _ceo_card(s: dict) -> None:
    box = ui.card().classes("oe-card w-full")
    with box:
        ui.label("CEO track record").classes("oe-h2")
        msg = ui.label("Looking up who runs the company (SEC filings, Wikidata) and computing the record…").classes("oe-muted")

    async def load():
        res = await run.io_bound(ceo_data, s)
        msg.delete()
        with box:
            draw(res)

    def draw(res):
        cur = res.get("current")
        ch = db.rows("SELECT * FROM ceo_changes WHERE cik=? ORDER BY date DESC LIMIT 3", (s["cik"],))
        for c in ch[:1]:
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.html("<span style='background:#2c5d8f;color:#fff;border-radius:4px;font-size:11px;padding:1px 6px'>"
                        "CEO CHANGE</span>", sanitize=False)
                ui.label(f"{c['new_name']} replaced {c['old_name']}" + (f" from {c['date']}" if c["date"] else "")
                         + (f" (found via {c['source']})" if c["source"] else "") + ". A new CEO is graded once they "
                         "have two fiscal years in the role; their record at other companies counts toward the career "
                         "grade straight away.").classes("text-sm")
        if res.get("conflict"):
            ui.label(res["conflict"]).classes("text-sm text-amber-800")
        m = res.get("record") or {}
        if not cur or not cur.get("start"):
            ui.label((f"The CEO is {cur['name']}, but the start date isn't known. " if cur else
                      "No CEO found: " + ("no readable CEO certification in the SEC filings" if res.get("sec_reached")
                                          else "the SEC couldn't be reached") + ", " +
                      ("nothing on Wikidata" if res.get("wikidata_ok") else "Wikidata unreachable") + ". ") + "Enter the start date below to rate their record.").classes("oe-muted")
        else:
            r = m.get("rating")
            car = res.get("career")
            ten = res.get("tenures") or []
            multi = car and car["n"] >= 2
            head = car if car else r
            with ui.row().classes("items-center gap-4 w-full no-wrap"):
                ui.label(head["grade"] if head else "—").classes("text-5xl font-bold").style(
                    f"color:{GRADE_COL.get(head['grade']) if head else '#6b7280'}")
                with ui.column().classes("gap-0"):
                    since = ("at least since " if cur.get("at_least") else "since about " if cur.get("approx")
                             else "since ") + cur["start"]
                    ui.label(f"{cur['name']}, CEO {since}").classes("text-lg font-semibold")
                    if multi:
                        ui.label(f"Career grade {car['score']:.0f}/100: {car['n']} companies, {car['years']:.1f} years of "
                                 "records, each weighted by its length").classes("text-sm")
                        ui.label("Here: " + (f"{r['grade']} ({r['score']:.0f})" if r else "not rated yet")).classes(
                            "text-sm")
                    elif r:
                        ui.label(f"{r['score']:.0f} out of 100" + (" (no other CEO role on record)" if not
                                 [t for t in ten if not t["current"]] else "")).classes("text-sm")
                    ui.label(f"Source: {cur['source']}" + (f" · record here {m['fy0'][:4]}–{m['fy1'][:4]} "
                             f"({m['years']:.1f} fiscal years)" if m.get("fy1") else "")).classes("oe-small")
            others = [t for t in ten if not t["current"]]
            if others:
                wts = {id(t): w for t, w in zip([t for t in ten if t.get("rating") and t.get("years")],
                                                (car or {}).get("weights") or [])}
                rows_ = "".join(
                    f"<tr><td style='padding:2px 10px 2px 0'>{html.escape(t['label'] if not t['current'] else 'This company')}"
                    f"{' (' + t['ticker'] + ')' if t.get('ticker') and not t['current'] else ''}</td>"
                    f"<td style='padding:2px 10px'>{t['start'][:4]}–{(t.get('end') or 'now')[:4]}</td>"
                    f"<td style='padding:2px 10px;text-align:right'>"
                    f"{(t['rating']['grade'] + ' (' + format(t['rating']['score'], '.0f') + ')') if t.get('rating') else '—'}</td>"
                    f"<td style='padding:2px 10px;text-align:right'>{(format(t['years'], '.1f') + ' yrs') if t.get('years') else ''}</td>"
                    f"<td style='padding:2px 0;text-align:right;color:#6b7280'>"
                    f"{format(wts[id(t)] * 100, '.0f') + '% of grade' if id(t) in wts else html.escape(t.get('why') or '')}</td></tr>"
                    for t in ten)
                ui.html("<table style='width:100%;font-size:13px'><tr style='color:#6b7280'><th style='text-align:left'>"
                        "CEO role</th><th style='text-align:left'>Years</th><th style='text-align:right'>Grade</th><th "
                        "style='text-align:right'>Record</th><th style='text-align:right'>Weight</th></tr>" + rows_ +
                        "</table>", sanitize=False).classes("w-full")
            if not r:
                ui.label("Not rated here yet: " + (m.get("note") or "fewer than two fiscal years in office, so the "
                         "record would be mostly luck.")).classes("oe-muted")
            if head:
                rows = "".join(f"<tr><td style='padding:2px 10px 2px 0'>{PART_NAMES[k]}"
                               f"{' (career)' if multi else ''}</td>"
                               f"<td style='width:55%'>{_bar(v / 100, _pcol(v / 100))}</td></tr>"
                               for k, v in head["parts"].items())
                ui.html("<table style='width:100%;font-size:13px'>" + rows + "</table>", sanitize=False).classes("w-full")
            if m:
                ui.label("The record here").classes("font-semibold pt-1")
            for line in _rec_lines(m):
                ui.label("• " + line).classes("text-sm")
            if m.get("note"):
                ui.label(m["note"]).classes("oe-small")
        if res.get("prev"):
            ui.label("Earlier CEOs (from 2010, as far as the data go)").classes("font-semibold pt-2")
            for h, end, r in res["prev"]:
                ui.label(f"{h['name']}, {h['start'][:4]}–{end[:4]}: " + (f"{r['grade']} ({r['score']:.0f})" if r else
                         "not rated (under two fiscal years of data)")).classes("text-sm")
        with ui.expansion("Correct the CEO or start date", icon="edit").classes("w-full"):
            mm = ceo.manual(s["cik"]) or {}
            nm = ui.input("CEO name", value=mm.get("name") or (cur or {}).get("name") or "").props("dense outlined")
            st = ui.input("Start date (YYYY-MM-DD)", value=mm.get("start") or (cur or {}).get("start") or "").props(
                "dense outlined")

            def save(clear=False):
                try:
                    ceo.set_manual(s["cik"], nm.value, None if clear else (st.value or "").strip())
                except ValueError:
                    ui.notify("Use a date like 2021-02-15.", type="warning")
                    return
                ui.notify("Saved. Reopen the tab to see the new record.", type="positive")
            with ui.row().classes("gap-2"):
                ui.button("Save", on_click=lambda: save()).props("unelevated dense")
                ui.button("Use the automatic look-up again", on_click=lambda: save(True)).props("flat dense")
        ui.label("How it's rated: four parts on fixed scales (owners' return vs the S&P 500, growth, returns on "
                 "capital, capital allocation), from the last annual report before the CEO started. A CEO inherits a "
                 "business, an industry and a market, so read the grade as how owners fared under this CEO, not as "
                 "proof of skill; under about five years it is partly luck. See Guide → CEO track record.").classes(
            "oe-small pt-1")

    ui.timer(0.3, load, once=True)


# ===================================================================== ranking page
_PCT = "p => p.value == null ? '' : (p.value * 100).toFixed(1) + '%'"
_PCT0 = "p => p.value == null ? '' : (p.value * 100).toFixed(0) + '%'"
_NUM0 = "p => p.value == null ? '' : p.value.toFixed(0)"
_X = "p => p.value == null ? '' : p.value.toFixed(2) + '×'"
_YRS = "p => p.value == null ? '' : p.value.toFixed(1) + ' yrs'"
_BIL = "p => p.value == null ? '' : (p.value >= 1e12 ? '$' + (p.value / 1e12).toFixed(2) + 'T' : '$' + (p.value / 1e9).toFixed(1) + 'B')"
_TREND_COLS = "{up: '#1f6f4a', down: '#a33a2b', flat: '#6b7280', new: '#9aa5a0'}"
_TREND_SYMS = "{up: '▲', down: '▼', flat: '▬', new: '•'}"
# the Trend cell: a sparkline of up to 10 years of Wonderfulness and the change over the last 3 fiscal years
_SPARK = """p => {
  const r = p.data || {}, d = r.spark || [], dir = r.trend_dir || 'new';
  const col = """ + _TREND_COLS + """[dir], sym = """ + _TREND_SYMS + """[dir];
  if (d.length < 2) return '<span style="color:#9aa5a0;font-size:12px">new</span>';
  const W = 64, H = 22, lo0 = Math.min(...d), hi0 = Math.max(...d), mid = (lo0 + hi0) / 2, span = Math.max(hi0 - lo0, 16);
  const lo = mid - span / 2, hi = mid + span / 2;
  const x = i => (i * (W - 6) / (d.length - 1) + 3).toFixed(1), y = v => (H - 3 - (v - lo) / (hi - lo) * (H - 6)).toFixed(1);
  const g70 = (70 > lo && 70 < hi) ? `<line x1="0" x2="${W}" y1="${y(70)}" y2="${y(70)}" stroke="#c7ccc9" stroke-dasharray="2,2"/>` : '';
  const ch = p.value == null ? '' : (p.value >= 0 ? '+' : '−') + Math.abs(p.value).toFixed(0);
  return `<span style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><svg width="${W}" height="${H}">${g70}` +
    `<polyline points="${d.map((v, i) => x(i) + ',' + y(v)).join(' ')}" fill="none" stroke="#1f6f4a" stroke-width="1.6"/>` +
    `<circle cx="${x(d.length - 1)}" cy="${y(d[d.length - 1])}" r="2.6" fill="${col}"/></svg>` +
    `<span style="color:${col};font-size:12px;min-width:38px;text-align:right">${sym} ${ch}</span></span>`;
}"""
# hovering the Trend cell: the yearly history of both scores
_TREND_TIP = """class {
  init(p) {
    const r = p.data || {}, d = r.spark || [], q = r.spark_q || [], lab = r.spark_lab || [];
    const e = document.createElement('div');
    e.style.cssText = 'background:#fff;border:1px solid #d5dbd7;border-radius:8px;padding:10px 12px;' +
      'box-shadow:0 6px 18px rgba(0,0,0,.14);font-size:12px;color:#1f2937;width:320px;line-height:1.45';
    const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
    let h = `<div style="font-weight:600;font-size:13px">${esc(r.ticker)} · ${esc(r.name)}</div>`;
    if (d.length >= 2) {
      const W = 296, H = 110, all = d.concat(q.filter(v => v != null));
      const lo = Math.max(0, Math.min(65, Math.min(...all) - 8)), hi = 100;
      const x = i => (i * (W - 16) / (d.length - 1) + 8).toFixed(1), y = v => (H - 14 - (v - lo) / (hi - lo) * (H - 22)).toFixed(1);
      const line = (vs, c, w, dash) => {
        const pts = vs.map((v, i) => v == null ? null : x(i) + ',' + y(v)).filter(Boolean).join(' ');
        return pts ? `<polyline points="${pts}" fill="none" stroke="${c}" stroke-width="${w}"${dash ? ' stroke-dasharray="4,3"' : ''}/>` : '';
      };
      const guide = (v, t) => `<line x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="#d5dbd7" stroke-dasharray="2,3"/>` +
        `<text x="${W}" y="${+y(v) - 2}" text-anchor="end" font-size="9" fill="#9aa5a0">${t}</text>`;
      h += `<svg width="${W}" height="${H}" style="margin-top:6px">${guide(80, '80 exceptional')}${guide(70, '70 wonderful')}` +
        line(q, '#2c5d8f', 1.4, true) + line(d, '#1f6f4a', 2.4, false) +
        d.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="2.2" fill="${v >= 80 ? '#1f6f4a' : v >= 70 ? '#3c8d5f' : '#c9a227'}"/>`).join('') +
        `<text x="8" y="${H - 2}" font-size="9" fill="#6b7280">${lab[0]}</text>` +
        `<text x="${W - 8}" y="${H - 2}" text-anchor="end" font-size="9" fill="#6b7280">${lab[lab.length - 1]}</text></svg>`;
      h += `<div style="font-size:11px;color:#6b7280"><span style="color:#1f6f4a">━</span> Wonderfulness &nbsp; ` +
        `<span style="color:#2c5d8f">╍</span> Quality</div>`;
      h += '<table style="font-size:11px;border-collapse:collapse;margin:4px 0;width:100%"><tr>' +
        lab.map(l => `<td style="text-align:center;color:#6b7280">'${l.slice(4)}</td>`).join('') + '</tr><tr>' +
        d.map(v => `<td style="text-align:center;font-weight:600;color:${v >= 80 ? '#1f6f4a' : v >= 70 ? '#3c8d5f' : '#8A5A12'}">${v.toFixed(0)}</td>`).join('') +
        '</tr><tr>' + q.map(v => `<td style="text-align:center;color:#2c5d8f">${v == null ? '–' : v.toFixed(0)}</td>`).join('') + '</tr></table>';
    }
    const ch = (v, since) => v == null ? '' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(0)}${since ? ' since ' + since : ''}`;
    if (r.years) h += `<div>Wonderful or better (70+) in <b>${r.years_70} of ${r.years}</b> years · last ${r.y70n}: <b>${r.y70} of ${r.y70n}</b></div>`;
    h += `<div>${r.first_80 ? 'First exceptional (80+): <b>' + r.first_80 + '</b>' : 'Never exceptional (80+)'}</div>`;
    if (r.trend != null) h += `<div>3-year change: <b style="color:${""" + _TREND_COLS + """[r.trend_dir]}">` +
      `${ch(r.trend, r.trend_since)}</b>${r.q_trend != null ? ' · Quality ' + ch(r.q_trend) : ''}</div>`;
    h += '<div style="color:#6b7280;margin-top:4px">Click for the full chart.</div>';
    e.innerHTML = h;
    this.e = e;
  }
  getGui() { return this.e; }
}"""
_Y70 = "p => (p.data && p.data.y70n) ? p.value + ' of ' + p.data.y70n : ''"

def _c(field, header, fmt=None, width=110, tip=None, **kw):
    width = max(width, 9 * max(len(x) for x in header.split()) + 76)
    c = {"field": field, "headerName": header, "width": width, "minWidth": width, "type": "rightAligned" if fmt else None}
    if fmt:
        c[":valueFormatter"] = fmt
    if tip:
        c["headerTooltip"] = tip
    c.update(kw)
    return {k: v for k, v in c.items() if v is not None}


def checklist(prof) -> dict[int, dict]:
    """The Oracle checklist (Quality) from each company's latest annual score: quality, passes, and why not."""
    bar = float(prof.settings.get("quality_min", 70))
    fmts = {r["id"]: r.get("fmt", "") for r in prof.all_rules()}
    mins = {r["id"]: r["min_year"] for r in prof.all_rules() if r.get("min_year") is not None}
    wins = {r["id"]: int(r.get("min_year_window") or rules.ROE_FLOOR_WINDOW) for r in prof.all_rules()
            if r.get("min_year") is not None}
    out = {}
    for r in db.rows("SELECT s.cik, s.quality, s.gates_passed, s.failed_gates FROM snapshots s JOIN (SELECT cik, MAX(filed) f "
                     "FROM snapshots WHERE profile_id=? AND profile_version=? GROUP BY cik) m ON s.cik=m.cik AND "
                     "s.filed=m.f WHERE s.profile_id=? AND s.profile_version=?", (*prof.snap, *prof.snap)):
        if r["quality"] is None:
            continue
        failed = []
        for f in json.loads(r["failed_gates"] or "[]"):
            if f.get("id") == "u1" or not f.get("name"):
                continue
            v, fmt_ = f.get("value"), fmts.get(f.get("id"), "")
            val = ("" if v is None else f" {v * 100:.0f}%" if fmt_ in ("pct", "ratio") else
                   f" {v:.1f} yrs" if fmt_ == "years" and v < 90 else " (losses)" if fmt_ == "years" else f" {v:.2f}")
            my = mins.get(f.get("id"))
            if my is not None and v is not None and v >= my:  # the average is fine; one year fell below the floor
                failed.append(f"{f['name']}: a year under {my * 100:.0f}% in the last {wins.get(f['id'], 5)} "
                              f"(average{val})")
            else:
                failed.append(f"{f['name']}{val}")
        why = ", ".join(failed)
        if not why and r["quality"] < bar:
            why = f"Quality {r['quality']:.0f} < {bar:.0f}"
        out[r["cik"]] = {"quality": r["quality"], "passes": bool(r["gates_passed"]) and r["quality"] >= bar, "why": why}
    return out


def price_status(prof) -> dict[int, dict]:
    """Where today's price stands for a strategy: Buy Zone, how far above its buy price, or outside its universe.
    From its live paper portfolio when it runs daily, otherwise worked out now (pipeline.evaluate_now)."""
    rid = db.scalar("SELECT id FROM runs WHERE kind='live' AND profile_id=? ORDER BY id DESC LIMIT 1", (prof.id,))
    rows, outside = [], set()
    if rid and db.scalar("SELECT 1 FROM evaluations WHERE run_id=? LIMIT 1", (rid,)):
        d = db.scalar("SELECT MAX(date) FROM evaluations WHERE run_id=?", (rid,))
        rows = [dict(r) for r in db.rows("SELECT cik, list, price, buy_below FROM evaluations WHERE run_id=? AND "
                                          "date=?", (rid, d))]
    else:
        from .. import pipeline

        now = pipeline.evaluate_now(prof)
        rows = [{"cik": c, **v} for c, v in now["evals"].items()]
        outside = now["outside"]
    out = {c: {"status": "outside this strategy's universe", "buyable": False} for c in outside}
    for r in rows:
        if r["list"] == "buy":
            out[r["cik"]] = {"status": "Buy Zone", "buyable": True}
        elif r["price"] and r["buy_below"] and r["buy_below"] > 0:
            out[r["cik"]] = {"status": f"{r['price'] / r['buy_below'] - 1:+.0%} above buy price", "buyable": False}
    return out


def _aligned(hist: dict[str, list]) -> tuple[dict[str, list], int | None]:
    """Each column's yearly values on one year axis (gaps as None), so a row carries one start year."""
    years = sorted({int(lab[2:]) for ser in hist.values() for lab, _ in ser})
    if not years:
        return {}, None
    y0, y1 = years[0], years[-1]
    out = {}
    for f, ser in hist.items():
        d = {int(lab[2:]): v for lab, v in ser}
        out[f] = [None if d.get(y) is None else float(f"{d[y]:.4g}") for y in range(y0, y1 + 1)]
    return out, y0


def ranking_rows(prof, evs: dict | None = None, strat=None) -> list[dict]:
    """Rows for the Screener. Scores come from `prof` (the app's Oracle checklist); the Quality bar and the
    buy-price status from `strat` (the strategy picked on the page, default the primary)."""
    strat = strat or prof
    wonderful.ensure(prof)
    sec = {r["cik"]: r for r in db.rows("SELECT cik, ticker, name, sic_desc, is_foreign FROM securities")}
    flags = {}
    for r in db.rows("SELECT q.cik, q.flags_json FROM qsnaps q JOIN (SELECT cik, MAX(filed) f FROM qsnaps GROUP BY cik) m "
                     "ON q.cik=m.cik AND q.filed=m.f"):
        fl = json.loads(r["flags_json"] or "[]")
        flags[r["cik"]] = fl
    checks = checklist(strat)
    evs = price_status(strat) if evs is None else evs
    trs = wonderful.trends(prof)
    mh = wonderful.metric_histories(prof)
    cg = ceo.grades()
    cch = ceo.recent_changes(365)
    from .. import grossfill

    gmk = grossfill.kinds()
    from .. import values

    values.ensure(prof)  # after an upgrade that changed the model: value everything once (~30 s)
    vals = values.all_values()
    out = []
    for r in db.rows("SELECT cik, score, rank, n, as_of, pillars_json, feats_json FROM wonder WHERE profile_id=? AND "
                     "cohort='now' ORDER BY score DESC", (prof.snap[0],)):
        s = sec.get(r["cik"])
        if not s:
            continue
        p = json.loads(r["pillars_json"])
        f = json.loads(r["feats_json"])["f"]
        fl = flags.get(r["cik"], [])
        q = checks.get(r["cik"], {})
        e = evs.get(r["cik"], {})
        t = trs.get(r["cik"], {})
        h = mh.get(r["cik"], {})
        hist, hy0 = _aligned(h.get("hist") or {})
        g = cg.get(r["cik"], {})
        v = vals.get(r["cik"], {})
        w10 = (t.get("w") or [])[-10:]
        qd = dict(t.get("q") or [])
        last5 = [v for _, v in w10[-5:]]
        out.append({"cik": r["cik"], "trend": t.get("chg"), "trend_dir": t.get("dir", "new"), "trend_since": t.get("since"),
                    "spark": [round(v, 1) for _, v in w10], "spark_lab": [k for k, _ in w10],
                    "spark_q": [None if qd.get(k) is None else round(qd[k], 1) for k, _ in w10],
                    "y70": sum(v >= 70 for v in last5), "y70n": len(last5), "years_70": t.get("years_70"),
                    "years": t.get("years"), "first_80": t.get("first_80") or "", "q_trend": t.get("q_chg"),
                    "streak70": t.get("streak_70", 0), "hist": hist, "hy0": hy0, "tr": h.get("tr") or {},
                    "ceo_grade": g.get("grade"), "ceo_score": g.get("score"), "ceo_name": g.get("name") or "",
                    "ceo_since": (g.get("start") or "")[:4], "ceo_basis": g.get("basis") or "",
                    "ceo_note": g.get("note") or ("" if g else "not rated yet"), "ceo_tried": bool(g), "ceo_updated": g.get("updated_at") or "",
                    "gm_kind": gmk.get(r["cik"], "none"),
                    "ceo_changed": (cch.get(r["cik"]) or {}).get("date") or "",
                    "ceo_prev": (cch.get(r["cik"]) or {}).get("old_name") or "",
                    "v_price": v.get("price"), "v_iv": v.get("iv"), "v_low": v.get("low"), "v_high": v.get("high"),
                    "v_er": v.get("er"), "v_mos": v.get("mos"), "v_required": v.get("required"),
                    "v_buy": (v["price"] <= v["buy_below"]) if v.get("buy_below") else None,
                    "v_implied": v.get("implied"), "v_supports": v.get("supports"),
                    "v_gap": (v["implied"] - v["supports"]) if v.get("implied") is not None and v.get("supports") is not None
                    else None,
                    "v_pe_rel": (v["pe_now"] / v["pe_med"]) if v.get("pe_now") and v.get("pe_med") else None,
                    "ticker": s["ticker"], "name": s["name"], "industry": s["sic_desc"], "score": r["score"],
                    "quality": q.get("quality"), "passes": q.get("passes", False), "why_fail": q.get("why", ""),
                    "checklist": ("✓ passes" if q.get("passes") else ("✗ " + q["why"] if q.get("why") else "✗"))
                    if q else "—",
                    "combined": ((r["score"] + q["quality"]) / 2) if q.get("quality") is not None else None,
                    "price_status": e.get("status", "—"), "buyable": e.get("buyable", False),
                    "tier": wonderful.tier(r["score"]), "rank": r["rank"], "as_of": r["as_of"],
                    "moat": p.get("moat"), "growth": p.get("growth"), "strength": p.get("strength"),
                    "allocation": p.get("allocation"), "mcap": f.get("mcap"), "roic_med": f.get("roic_med"),
                    "roic_last": f.get("roic_last"), "gm_med": f.get("gm_med"), "rev_g5": f.get("rev_g5"),
                    "om_med": f.get("om_med"), "nm_med": f.get("nm_med"), "ocfm_med": f.get("ocfm_med"),
                    "rev_up": (f"{f['rev_up']}/{f['rev_years']}" if f.get("rev_years") else None),
                    "debt_payback": f.get("debt_payback"), "dollar_test": f.get("dollar_test"),
                    "share_change": f.get("share_change"),
                    "warnings": "; ".join(x["name"] for x in fl if x.get("health") or x.get("hard")) or ""})
    return out


def _compare(tickers: list[str], prof) -> None:
    ws = []
    for t in tickers:
        c = db.scalar("SELECT cik FROM securities WHERE ticker=?", (t,))
        w = wonderful.get(c, prof) if c else None
        if w:
            ws.append((t, w))
    if len(ws) < 2:
        ui.label("Need at least two scored companies to compare.").classes("oe-muted")
        return
    with ui.card().classes("oe-card w-full"):
        ui.label("Side by side").classes("oe-h2")
        cols = ["#1f6f4a", "#2c5d8f", "#c9a227", "#7a4f9e", "#a33a2b"]
        ind = [{"name": wonderful.PILLAR_NAMES[p].split(":")[0], "max": 100} for p in wonderful.PILLARS]
        ui.echart({"tooltip": {}, "legend": {"top": 0}, "radar": {"indicator": ind, "radius": "65%"},
                   "series": [{"type": "radar", "data": [
                       {"name": t, "value": [round(w["pillars"].get(p, 50)) for p in wonderful.PILLARS],
                        "lineStyle": {"color": cols[i]}, "itemStyle": {"color": cols[i]}, "areaStyle": {"opacity": 0.08}}
                       for i, (t, w) in enumerate(ws)]}]}).classes("w-full h-80")
        hs = {t: {x["fy"]: x["score"] for x in wonderful.history(w["cik"], prof)} for t, w in ws}
        labs = sorted({k for h in hs.values() for k in h})
        if len(labs) >= 2:
            ui.label("Wonderfulness over time").classes("font-semibold pt-2")
            ui.echart({"tooltip": {"trigger": "axis"}, "legend": {"top": 0},
                       "grid": {"left": 40, "right": 16, "top": 32, "bottom": 28},
                       "xAxis": {"type": "category", "data": labs, "boundaryGap": False},
                       "yAxis": {"type": "value", "min": max(0, int(min(v for h in hs.values() for v in h.values()) // 10
                                                                    * 10) - 10), "max": 100},
                       "series": [{"type": "line", "name": t, "connectNulls": True, "symbolSize": 5,
                                   "data": [None if hs[t].get(k) is None else round(hs[t][k], 1) for k in labs],
                                   "lineStyle": {"color": cols[i], "width": 2}, "itemStyle": {"color": cols[i]},
                                   **({"markLine": {"silent": True, "symbol": "none", "label": {"show": False},
                                                    "lineStyle": {"type": "dashed", "color": "#9aa5a0"},
                                                    "data": [{"yAxis": 70}, {"yAxis": 80}]}} if i == 0 else {})}
                                  for i, (t, _w) in enumerate(ws)]}).classes("w-full").style("height:300px")
        head = "".join(f"<th style='text-align:right;padding:4px 10px'><a href='/stock/{t}'>{t}</a></th>" for t, _ in ws)
        body = ["<tr><td style='padding:4px 10px 4px 0'><b>Wonderfulness</b></td>" + "".join(
            f"<td style='text-align:right;padding:4px 10px'><b>{w['score']:.0f}</b> ({w['tier']})</td>" for _, w in ws)
            + "</tr>", "<tr><td style='padding:4px 10px 4px 0'>Rank</td>" + "".join(
            f"<td style='text-align:right;padding:4px 10px'>#{w['rank']:,} of {w['n']:,}</td>" for _, w in ws) + "</tr>"]
        chk = checklist(prof)
        body.append("<tr><td style='padding:4px 10px 4px 0'><b>Quality (Oracle checklist)</b></td>" + "".join(
            "<td style='text-align:right;padding:4px 10px'>"
            + (f"<b>{chk[w['cik']]['quality']:.0f}</b> " + ("✓ passes" if chk[w['cik']]['passes'] else
                                                             "✗ " + html.escape(chk[w['cik']]['why'])) if w["cik"] in chk else "—")
            + "</td>" for _, w in ws) + "</tr>")
        for p, items in wonderful.PILLARS.items():
            body.append(f"<tr><td colspan='{len(ws) + 1}' style='padding:8px 0 2px;font-weight:600'>"
                        f"{wonderful.PILLAR_NAMES[p]}</td></tr>")
            for k, _ in items:
                label, fmt, _t = wonderful.FEATURES[k]
                vals = [w["f"].get(k) for _, w in ws]
                pcts = [w["pct"].get(k) for _, w in ws]
                best = max((x for x in pcts if x is not None), default=None)
                cells = "".join(
                    f"<td style='text-align:right;padding:2px 10px;{'font-weight:700;color:#1f6f4a' if p_ is not None and p_ == best else ''}'>"
                    f"{wonderful._fmt(v, fmt) if v is not None else '—'}</td>" for v, p_ in zip(vals, pcts))
                body.append(f"<tr><td style='padding:2px 10px 2px 12px;color:#374151'>{html.escape(label)}</td>{cells}</tr>")
        ui.html(f"<table style='width:100%;font-size:13px;border-collapse:collapse'><tr><th></th>{head}</tr>"
                + "".join(body) + "</table>", sanitize=False).classes("w-full")
        ui.label("Bold green = the best of the group on that measure.").classes("oe-small")

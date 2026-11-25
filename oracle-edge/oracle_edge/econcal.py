"""The month ahead: an economic and market calendar for the daily email and the Market page.

Four kinds of events, each with how much it tends to move markets (3 = market-moving, 2 = notable, 1 = minor):
- Fed: rate decisions (2 pm ET; with economic projections at four meetings a year) and the minutes three weeks later.
  The Fed's published schedule is kept below (it changes rarely; the guide says where to check).
- Economy: the major US data releases (jobs, inflation, growth, consumer, housing) with their official release dates
  from FRED's release calendar (needs a free FRED API key) and the last reading of each from FRED's public data.
- Market: days the NYSE is closed or closes early (from the exchange calendar), monthly options expiration and the
  quarterly "triple witching" / S&P index rebalance (third Friday).
- Yours: earnings dates and ex-dividend dates of what you own, including companies that are 1%+ of your money through
  your funds (Yahoo Finance; estimated dates are marked).
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from . import db, secrets

FRED_API = "https://api.stlouisfed.org/fred/releases/dates"
WINDOW_DAYS = 31

# (match in FRED's release name, key, title, category, importance, usual time ET, why it matters)
RELEASES = [
    ("employment situation", "jobs", "Jobs report: payrolls and unemployment", "Jobs", 3, "8:30 am",
     "The broadest monthly read on the economy; it moves rate expectations and stocks."),
    ("consumer price index", "cpi", "Consumer prices (CPI)", "Inflation", 3, "8:30 am",
     "Inflation decides what the Fed does with rates, and rates decide what stocks are worth."),
    ("personal income and outlays", "pce", "PCE inflation, income and spending", "Inflation", 3, "8:30 am",
     "The Fed's own preferred inflation measure."),
    ("gross domestic product", "gdp", "GDP", "Growth", 3, "8:30 am",
     "How fast the whole economy is growing; earnings follow it."),
    ("producer price index", "ppi", "Producer prices (PPI)", "Inflation", 2, "8:30 am",
     "Prices businesses pay: an early sign of where consumer inflation is heading."),
    ("advance monthly sales for retail", "retail", "Retail sales", "Growth", 2, "8:30 am",
     "Consumers are about two thirds of the economy."),
    ("job openings and labor turnover", "jolts", "Job openings (JOLTS)", "Jobs", 2, "10:00 am",
     "How tight the job market is: fewer openings usually means cooling wages."),
    ("employment cost index", "eci", "Employment cost index", "Inflation", 2, "8:30 am",
     "Wage growth, the stickiest part of inflation."),
    ("surveys of consumers", "umich", "Consumer sentiment (U. Michigan)", "Consumer", 1, "10:00 am",
     "How households feel, and what inflation they expect."),
    ("industrial production", "ip", "Industrial production", "Growth", 1, "9:15 am",
     "Factories, mines and utilities: the cyclical part of the economy."),
    ("durable goods", "durables", "Durable goods orders", "Growth", 1, "8:30 am",
     "Business spending on equipment: a lead on investment."),
    ("new residential construction", "starts", "Housing starts", "Housing", 1, "8:30 am",
     "Housing is very sensitive to interest rates."),
    ("new residential sales", "newhome", "New home sales", "Housing", 1, "10:00 am",
     "Demand for new homes at today's mortgage rates."),
    ("unemployment insurance weekly claims", "claims", "Jobless claims", "Jobs", 1, "8:30 am",
     "The quickest weekly sign of layoffs."),
]

# FOMC schedule (federalreserve.gov/monetarypolicy/fomccalendars.htm). (first day, decision day, projections,
# decision): projections = Summary of Economic Projections ("dot plot"); decision False = a strategy meeting.
FOMC = [
    ("2026-01-27", "2026-01-28", False, True), ("2026-03-17", "2026-03-18", True, True),
    ("2026-04-28", "2026-04-29", False, True), ("2026-06-16", "2026-06-17", True, True),
    ("2026-07-28", "2026-07-29", False, True), ("2026-09-15", "2026-09-16", True, True),
    ("2026-10-27", "2026-10-28", False, True), ("2026-12-08", "2026-12-09", True, True),
    ("2027-01-26", "2027-01-27", False, True), ("2027-03-16", "2027-03-17", True, True),
    ("2027-04-27", "2027-04-28", False, True), ("2027-06-08", "2027-06-09", True, True),
    ("2027-07-27", "2027-07-28", False, True), ("2027-09-14", "2027-09-15", True, True),
    ("2027-10-26", "2027-10-27", False, False), ("2027-11-16", "2027-11-17", True, True),
    ("2028-01-25", "2028-01-26", False, True),
]

# FRED series for each release's last reading (public CSV, no key)
PREV_SERIES = {"cpi": ["CPIAUCSL", "CPILFESL"], "jobs": ["PAYEMS", "UNRATE"], "pce": ["PCEPILFE"],
               "gdp": ["A191RL1Q225SBEA"], "ppi": ["PPIFIS"], "retail": ["RSAFS"], "jolts": ["JTSJOL"],
               "umich": ["UMCSENT"], "ip": ["INDPRO"], "durables": ["DGORDER"], "starts": ["HOUST"],
               "newhome": ["HSN1F"], "claims": ["ICSA"], "fed": ["DFEDTARL", "DFEDTARU"]}

CAT_COL = {"Fed": "#2c5d8f", "Inflation": "#a33a2b", "Jobs": "#1f6f4a", "Growth": "#7b5ea7", "Consumer": "#c9822b",
           "Housing": "#8c564b", "Market": "#5b6b7a", "Earnings": "#9a5b00", "Dividend": "#3a8f8f"}


# ===================================================================== building the events
def fomc_events(start: date, end: date) -> list[dict]:
    out = []
    for d0, d1, sep, decision in FOMC:
        dd = date.fromisoformat(d1)
        if decision and start <= dd <= end:
            out.append({"date": d1, "time": "2:00 pm", "cat": "Fed", "imp": 3, "key": "fed",
                        "title": "Fed rate decision" + (" + economic projections" if sep else ""),
                        "detail": "Press conference 2:30 pm", "why": "Sets short-term rates; the projections show "
                        "where Fed officials expect rates, inflation and growth to go." if sep else
                        "Sets short-term rates; the press conference hints at what's next."})
        elif not decision and start <= dd <= end:
            out.append({"date": d1, "time": "", "cat": "Fed", "imp": 1, "key": "fed_strategy",
                        "title": "Fed meeting on policy strategy (no rate decision)", "detail": "", "why": ""})
        mins = dd + timedelta(days=21)
        if decision and start <= mins <= end:
            out.append({"date": mins.isoformat(), "time": "2:00 pm", "cat": "Fed", "imp": 2, "key": "minutes",
                        "title": f"Fed minutes (from the {dd.strftime('%b')} {dd.day} meeting)", "detail": "",
                        "why": "How divided officials were, and what would make them move."})
    return out


def market_events(start: date, end: date) -> list[dict]:
    """NYSE holidays and early closes, monthly options expiration, quarterly triple witching / index rebalance."""
    out = []
    try:
        import exchange_calendars as xcals

        cal = xcals.get_calendar("XNYS")
        hol = cal.regular_holidays.holidays(start.isoformat(), end.isoformat(), return_name=True)
        for d, name in hol.items():
            out.append({"date": d.date().isoformat(), "time": "", "cat": "Market", "imp": 2, "key": "closed",
                        "title": f"Market closed: {name}", "detail": "", "why": ""})
        for d in cal.adhoc_holidays:
            if start <= d.date() <= end:
                out.append({"date": d.date().isoformat(), "time": "", "cat": "Market", "imp": 2, "key": "closed",
                            "title": "Market closed", "detail": "", "why": ""})
        for d in cal.early_closes:
            if start <= d.date() <= end:
                out.append({"date": d.date().isoformat(), "time": "1:00 pm", "cat": "Market", "imp": 1,
                            "key": "early", "title": "Early close (1 pm ET)", "detail": "",
                            "why": "Thin trading; bond markets close at 2 pm."})
        sessions = {d.date() for d in cal.sessions_in_range(start.isoformat(), (end + timedelta(days=7)).isoformat())}
    except Exception:
        sessions = None
    m = date(start.year, start.month, 1)
    while m <= end:
        fri = m + timedelta(days=(4 - m.weekday()) % 7 + 14)  # third Friday
        d = fri
        if sessions is not None and d not in sessions:
            d -= timedelta(days=1)  # a holiday: expiration moves to Thursday
        if start <= d <= end:
            q = m.month in (3, 6, 9, 12)
            out.append({"date": d.isoformat(), "time": "", "cat": "Market", "imp": 2 if q else 1,
                        "key": "opex", "title": ("Quarterly options expiration + S&P index rebalance (\"triple "
                                                 "witching\")") if q else "Monthly options expiration",
                        "detail": "", "why": "Heavy volume and sometimes sharp moves near the close." if q else ""})
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def fred_key() -> str | None:
    return secrets.get("fred_api_key")


def fred_release_dates(start: date, end: date, key: str, http=None) -> list[dict]:
    """Every release FRED lists between the dates (future ones included)."""
    from .sources.fred import _http as fred_http

    h = http or fred_http
    out, offset = [], 0
    while True:
        r = h.get(FRED_API, params={"api_key": key, "file_type": "json", "realtime_start": start.isoformat(),
                                    "realtime_end": end.isoformat(), "include_release_dates_with_no_data": "true",
                                    "sort_order": "asc", "limit": 1000, "offset": offset})
        if r.status_code != 200:
            raise RuntimeError(f"FRED release calendar: HTTP {r.status_code} {r.text[:150]}")
        js = r.json()
        rows = js.get("release_dates") or []
        out += rows
        offset += len(rows)
        if not rows or offset >= int(js.get("count", 0)):
            break
    return out


def release_events(rows: list[dict], start: date, end: date) -> list[dict]:
    """The releases worth knowing about, from FRED's calendar rows ({release_name, date})."""
    out, seen = [], set()
    for r in rows:
        name = (r.get("release_name") or "").lower()
        d = r.get("date")
        if not d or not (start.isoformat() <= d <= end.isoformat()):
            continue
        for match, key, title, cat, imp, tm, why in RELEASES:
            if match in name and (key, d) not in seen:
                if key == "durables" and "advance" not in name and "durable goods" not in name:
                    continue
                seen.add((key, d))
                out.append({"date": d, "time": tm, "cat": cat, "imp": imp, "key": key, "title": title, "detail": "",
                            "why": why})
                break
    return out


# ---------------------------------------------------------------- last readings
def _series(s: str) -> list[tuple[str, float]]:
    return [(r["date"], r["value"]) for r in db.rows("SELECT date, value FROM macro WHERE series=? ORDER BY date",
                                                     (s,))]


def _yoy(xs) -> float | None:
    if len(xs) < 13 or not xs[-13][1]:
        return None
    return xs[-1][1] / xs[-13][1] - 1


def _mom(xs) -> float | None:
    return xs[-1][1] / xs[-2][1] - 1 if len(xs) >= 2 and xs[-2][1] else None


def _mon(d: str) -> str:
    return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%b")


def last_reading(key: str) -> str:
    """The latest published value, in words people use ("3.0% a year", "+119k jobs")."""
    try:
        if key == "cpi":
            a, c = _series("CPIAUCSL"), _series("CPILFESL")
            y, yc = _yoy(a), _yoy(c)
            return f"last ({_mon(a[-1][0])}): {y * 100:.1f}% a year, core {yc * 100:.1f}%" if y is not None else ""
        if key == "jobs":
            p, u = _series("PAYEMS"), _series("UNRATE")
            if len(p) >= 2 and u:
                return f"last ({_mon(p[-1][0])}): {p[-1][1] - p[-2][1]:+,.0f}k jobs, unemployment {u[-1][1]:.1f}%"
        if key == "pce":
            x = _series("PCEPILFE")
            y = _yoy(x)
            return f"last ({_mon(x[-1][0])}): core PCE {y * 100:.1f}% a year" if y is not None else ""
        if key == "gdp":
            x = _series("A191RL1Q225SBEA")
            return f"last quarter: {x[-1][1]:+.1f}% annualized" if x else ""
        if key == "ppi":
            x = _series("PPIFIS")
            y = _yoy(x)
            return f"last ({_mon(x[-1][0])}): {y * 100:.1f}% a year" if y is not None else ""
        if key in ("retail", "ip", "durables"):
            x = _series({"retail": "RSAFS", "ip": "INDPRO", "durables": "DGORDER"}[key])
            m = _mom(x)
            return f"last ({_mon(x[-1][0])}): {m * 100:+.1f}% on the month" if m is not None else ""
        if key == "jolts":
            x = _series("JTSJOL")
            return f"last ({_mon(x[-1][0])}): {x[-1][1] / 1000:.1f}M openings" if x else ""
        if key == "umich":
            x = _series("UMCSENT")
            return f"last ({_mon(x[-1][0])}): {x[-1][1]:.1f}" if x else ""
        if key in ("starts", "newhome"):
            x = _series("HOUST" if key == "starts" else "HSN1F")
            return f"last ({_mon(x[-1][0])}): {x[-1][1] / 1000:.2f}M a year" if x and key == "starts" else (
                f"last ({_mon(x[-1][0])}): {x[-1][1]:,.0f}k a year" if x else "")
        if key == "claims":
            x = _series("ICSA")
            return f"last week: {x[-1][1] / 1000:,.0f}k" if x else ""
        if key == "fed":
            lo, hi = _series("DFEDTARL"), _series("DFEDTARU")
            return f"now {lo[-1][1]:.2f}–{hi[-1][1]:.2f}%" if lo and hi else ""
    except (IndexError, TypeError, ZeroDivisionError):
        return ""
    return ""


def refresh_readings() -> int:
    from .sources import fred

    n = 0
    for ss in PREV_SERIES.values():
        for s in ss:
            try:
                df = fred.fetch_series(s, (date.today() - timedelta(days=800)).isoformat())
                fred._store(s, df)
                n += 1
            except Exception as e:
                db.log("calendar", s, False, str(e)[:200])
    return n


# ---------------------------------------------------------------- yours: earnings and ex-dividend dates
def my_names(min_share: float = 0.01) -> dict[str, dict]:
    """Tickers that matter to you: everything you own directly, plus companies that are at least `min_share` of your
    money through your funds. ticker -> {value, via, shares, direct}."""
    from . import etfscore, holdings

    hs, _ = holdings.holdings()
    tot = sum(h.value or 0 for h in hs) or 1.0
    out = {h.ticker: {"value": h.value or 0, "via": [], "shares": h.shares, "direct": True, "fund": h.cik is None}
           for h in hs if h.value}
    try:
        xr = etfscore.xray([(h.ticker, h.value) for h in hs if h.value])
        for e in xr["rows"]:
            t = e.get("ticker")
            if t and e.get("cik") and e["via"] / tot >= min_share:
                o = out.setdefault(t, {"value": 0.0, "via": [], "shares": 0, "direct": False, "fund": False})
                o["value"] = e["total"]
                o["via"] = sorted(e["funds"], key=lambda k: -e["funds"][k])
    except Exception:
        pass
    return out


def yahoo_dates(ticker: str) -> dict:
    """Next earnings date (with the EPS estimate) and ex-dividend date from Yahoo's calendar."""
    import yfinance as yf

    c = yf.Ticker(ticker).calendar or {}
    out = {}
    ed = c.get("Earnings Date") or []
    if ed:
        ds = sorted(str(x)[:10] for x in ed)
        out["earnings"] = ds[0]
        out["earnings_range"] = len(set(ds)) > 1  # a range = not confirmed yet
        out["eps"] = c.get("Earnings Average")
    if c.get("Ex-Dividend Date"):
        out["ex_div"] = str(c["Ex-Dividend Date"])[:10]
    if c.get("Dividend Date"):
        out["pay_div"] = str(c["Dividend Date"])[:10]
    return out


def my_events(start: date, end: date, names: dict[str, dict], fetch=yahoo_dates) -> list[dict]:
    from .holdings import Px

    px = Px(list(names))
    out = []
    for t, info in names.items():
        cache = db.cfg(f"cal_y_{t}")
        if not cache or cache.get("at", "") < (date.today() - timedelta(days=3)).isoformat():
            try:
                cache = {"at": date.today().isoformat(), **fetch(t)}
                db.set_cfg(f"cal_y_{t}", cache)
            except Exception as e:
                db.log("calendar", t, False, str(e)[:200])
                cache = cache or {}
        who = ("you own it" if info["direct"] else "") + (
            (" · " if info["direct"] else "") + "through " + ", ".join(info["via"][:3]) if info["via"] else "")
        e = cache.get("earnings")
        if e and start.isoformat() <= e <= end.isoformat() and not info.get("fund"):
            eps = cache.get("eps")
            out.append({"date": e, "time": "", "cat": "Earnings", "imp": 2, "key": "earnings", "ticker": t, "mine": True,
                        "title": f"{t} reports earnings" + (" (date not confirmed)" if cache.get("earnings_range")
                                                             else ""),
                        "detail": f"{who} · ${info['value']:,.0f}" + (f" · EPS estimate ${eps:.2f}" if eps else ""),
                        "why": ""})
        x = cache.get("ex_div")
        est = False
        if not x:  # no announced date: a year after last year's (an estimate)
            divs = px.dividends(t)
            for d, _v in divs[::-1]:
                nxt = (date.fromisoformat(d) + timedelta(days=364)).isoformat()
                if start.isoformat() <= nxt <= end.isoformat():
                    x, est = nxt, True
                    break
        if x and start.isoformat() <= x <= end.isoformat() and info["direct"]:
            divs = px.dividends(t)
            ps = divs[-1][1] if divs else None
            amt = f" · about ${ps:.2f}/share, ${ps * info['shares']:,.0f} for your {info['shares']:g}" if ps else ""
            out.append({"date": x, "time": "", "cat": "Dividend", "imp": 1, "key": "exdiv", "ticker": t, "mine": True,
                        "title": f"{t} goes ex-dividend" + (" (estimated)" if est else ""),
                        "detail": f"own it before this day to get the payout{amt}", "why": ""})
    return out


# ---------------------------------------------------------------- refresh and read
def refresh(progress=None) -> dict:
    """Rebuild the next 31 days of events. Each source on its own; a failure keeps the rest."""
    start = date.today()
    end = start + timedelta(days=WINDOW_DAYS)
    ev, notes = [], []
    ev += fomc_events(start, end)
    ev += market_events(start, end)
    key = fred_key()
    if key:
        try:
            ev += release_events(fred_release_dates(start, end, key), start, end)
        except Exception as e:
            notes.append(str(e)[:200])
            db.log("calendar", "fred releases", False, str(e)[:200])
    else:
        notes.append("Add a free FRED API key (Settings → Data sources) for the economic releases.")
    try:
        refresh_readings()
    except Exception as e:
        notes.append(f"last readings: {e}"[:200])
    try:
        ev += my_events(start, end, my_names())
    except Exception as e:
        notes.append(f"your earnings and dividends: {e}"[:200])
        db.log("calendar", "yours", False, str(e)[:200])
    for e in ev:
        e.setdefault("prev", last_reading(e["key"]) if e["key"] in PREV_SERIES else "")
    ev.sort(key=lambda e: (e["date"], _tsort(e.get("time")), -e["imp"]))
    db.set_cfg("econ_calendar", {"built": db.now(), "start": start.isoformat(), "events": ev, "notes": notes})
    return {"events": len(ev), "notes": notes}


def _tsort(t: str | None) -> int:
    if not t:
        return -1  # all-day items (closures) first
    try:
        x = datetime.strptime(t.replace(" ", "").upper(), "%I:%M%p")
        return x.hour * 60 + x.minute
    except ValueError:
        return 9999


def events() -> dict:
    return db.cfg("econ_calendar") or {"events": [], "notes": [], "start": None}


# ===================================================================== the email / page section
DOTS = {3: "●●●", 2: "●●", 1: "●"}


def _chip(cat: str) -> str:
    c = CAT_COL.get(cat, "#5b6b7a")
    return (f"<span style='display:inline-block;border:1px solid {c};color:{c};border-radius:999px;padding:0 7px;"
            f"font-size:11px;font-weight:600;white-space:nowrap'>{html.escape(cat)}</span>")


def _day(d: str) -> str:
    x = date.fromisoformat(d)
    return x.strftime("%a %b ") + str(x.day)


def _row(e: dict, compact: bool = False) -> str:
    mine = e.get("mine")
    title = html.escape(e["title"])
    sub = " · ".join(x for x in (e.get("prev"), e.get("detail")) if x)
    imp_col = "#a33a2b" if e["imp"] == 3 else "#9a5b00" if e["imp"] == 2 else "#9aa19d"
    return (f"<tr style='{'background:#fffaf0;' if mine else ''}'>"
            f"<td style='padding:5px 6px;color:#5d6763;font-size:12px;white-space:nowrap;vertical-align:top;width:92px'>"
            f"{html.escape(e.get('time') or '') or '&nbsp;'}</td>"
            f"<td style='padding:5px 4px;color:{imp_col};font-size:11px;letter-spacing:-1px;vertical-align:top;"
            f"white-space:nowrap;width:28px' title='impact'>{DOTS[e['imp']]}</td>"
            f"<td style='padding:5px 6px;vertical-align:top;width:82px'>{_chip(e['cat'])}</td>"
            f"<td style='padding:5px 6px;vertical-align:top'>{'★ ' if mine else ''}<b style='font-size:13.5px'>{title}</b>"
            + (f"<div style='font-size:12px;color:#5d6763'>{html.escape(sub)}</div>" if sub else "")
            + (f"<div style='font-size:11.5px;color:#8a918d;font-style:italic'>{html.escape(e['why'])}</div>"
               if e.get("why") and not compact else "")
            + "</td></tr>")


def calendar_html(cal: dict | None = None, today: date | None = None) -> str:
    """The month ahead, organized the way people plan:
    1. The big days: the three days most likely to move markets, at a glance.
    2. This week, day by day: every event with its time (Eastern), impact, last reading and why it matters.
    3. The following weeks, week by week: only what's notable (●● and up) and everything of yours.
    Yours (★, highlighted) sit in the same timeline, so you see your earnings next to the Fed and CPI."""
    cal = cal or events()
    today = today or date.today()
    ev = [e for e in cal.get("events", []) if e["date"] >= today.isoformat()]
    if not ev:
        return ""
    # this week: through Sunday (from Thursday on, through next Sunday, so there's always a working week ahead)
    end_week = today + timedelta(days=(6 - today.weekday()) + (7 if today.weekday() >= 3 else 0))
    week = [e for e in ev if e["date"] <= end_week.isoformat()]
    later = [e for e in ev if e["date"] > end_week.isoformat() and (e["imp"] >= 2 or e.get("mine"))]
    # the big days
    score: dict[str, int] = {}
    for e in ev:
        if e["imp"] >= 2 and e["key"] not in ("closed", "early", "opex", "exdiv"):
            score[e["date"]] = score.get(e["date"], 0) + (e["imp"] ** 2)
    big = sorted(sorted(score, key=lambda d: -score[d])[:3])
    chips = "".join(
        f"<td style='padding:4px'><div style='border:1px solid #e2e0d8;border-radius:8px;padding:8px 10px;"
        f"background:#fbfaf6'><div style='font-size:12px;color:#5d6763'>{_day(d)}</div><div style='font-size:13px;"
        f"font-weight:700'>{html.escape(' + '.join(dict.fromkeys(_short(e) for e in ev if e['date'] == d and e['imp'] >= 2 and e['key'] not in ('closed', 'early', 'opex', 'exdiv'))))}"
        f"</div></div></td>" for d in big)
    mine = [e for e in ev if e.get("mine")]
    n_e = sum(1 for e in mine if e["key"] == "earnings")
    n_d = sum(1 for e in mine if e["key"] == "exdiv")
    yours = (f"<div style='font-size:12.5px;margin:6px 0'>★ Yours in the next month: "
             f"<b>{n_e}</b> earnings report{'s' if n_e != 1 else ''}, <b>{n_d}</b> ex-dividend date{'s' if n_d != 1 else ''}"
             "</div>") if mine else ""
    out = ["<h3 style='margin:22px 0 2px;font-size:16px'>The month ahead</h3>",
           "<div style='font-size:12px;color:#666'>Times are Eastern. Impact: <span style='color:#a33a2b'>●●●</span> "
           "market-moving · <span style='color:#9a5b00'>●●</span> notable · <span style='color:#9aa19d'>●</span> minor. "
           "★ = yours.</div>"]
    if big:
        out.append("<div style='font-size:12px;font-weight:700;margin:10px 0 2px;color:#1d2321'>The big days</div>"
                   f"<table cellspacing='0' cellpadding='0' style='border-collapse:separate'><tr>{chips}</tr></table>")
    out.append(yours)
    out.append("<div style='font-size:13px;font-weight:700;margin:12px 0 2px;border-bottom:2px solid #1f6f4a;"
               "padding-bottom:3px'>This week</div>")
    for d in sorted({e["date"] for e in week}):
        de = [e for e in week if e["date"] == d]
        closed = next((e for e in de if e["key"] == "closed"), None)
        head = _day(d) + (" · Today" if d == today.isoformat() else "") + (
            f" · <span style='color:#a33a2b'>{html.escape(closed['title'])}</span>" if closed else "")
        rows = "".join(_row(e) for e in de if e is not closed)
        out.append(f"<div style='font-size:12.5px;font-weight:700;margin:8px 0 0;color:#1d2321'>{head}</div>"
                   + (f"<table cellspacing='0' style='width:100%;border-collapse:collapse;table-layout:fixed'>{rows}</table>" if rows else ""))
    if later:
        out.append("<div style='font-size:13px;font-weight:700;margin:16px 0 2px;border-bottom:2px solid #1f6f4a;"
                   "padding-bottom:3px'>Later this month</div>")
        weeks: dict[str, list] = {}
        for e in later:
            dd = date.fromisoformat(e["date"])
            weeks.setdefault((dd - timedelta(days=dd.weekday())).isoformat(), []).append(e)
        for wk, es in sorted(weeks.items()):
            rows = "".join(_row({**e, "time": _day(e["date"])[:3] + " " + (e.get("time") or "")}, compact=True)
                           for e in es)
            out.append(f"<div style='font-size:12.5px;font-weight:700;margin:8px 0 0'>Week of {_day(wk)[4:]}</div>"
                       f"<table cellspacing='0' style='width:100%;border-collapse:collapse;table-layout:fixed'>{rows}</table>")
    if cal.get("notes"):
        out.append("<div style='font-size:11.5px;color:#8a918d;margin-top:6px'>"
                   + " · ".join(html.escape(n) for n in cal["notes"]) + "</div>")
    return "".join(out)


SHORT = {"fed": "Fed decision", "cpi": "CPI", "jobs": "Jobs report", "pce": "PCE", "gdp": "GDP", "ppi": "PPI",
         "retail": "Retail sales", "jolts": "JOLTS", "eci": "Wages (ECI)", "minutes": "Fed minutes"}


def _short(e: dict) -> str:
    if e["key"] == "earnings":
        return f"{e.get('ticker')} earnings"
    return SHORT.get(e["key"], e["title"])


def next_big(cal: dict | None = None) -> str:
    """For an email subject: the next market-moving event ("CPI Thu Dec 10")."""
    cal = cal or events()
    t = date.today().isoformat()
    e = next((e for e in cal.get("events", []) if e["date"] >= t and e["imp"] == 3), None)
    return f"{_short(e)} {_day(e['date'])}" if e else ""


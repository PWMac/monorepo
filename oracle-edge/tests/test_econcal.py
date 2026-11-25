"""The month ahead (0.24): Fed, releases, market days, yours, the email section."""

from datetime import date, timedelta

import pytest

from oracle_edge import db, econcal, emailer, holdings


def test_fed_and_market_days():
    s, e = date(2026, 11, 25), date(2026, 12, 31)
    fed = econcal.fomc_events(s, e)
    dec = next(x for x in fed if x["key"] == "fed")
    assert dec["date"] == "2026-12-09" and "projections" in dec["title"] and dec["imp"] == 3
    mins = [x for x in fed if x["key"] == "minutes"]
    assert [x["date"] for x in mins] == ["2026-12-30"]  # three weeks after Dec 9 (Oct 28's came Nov 18)
    mk = econcal.market_events(s, e)
    by = {(x["date"], x["key"]): x for x in mk}
    assert ("2026-11-26", "closed") in by and "Thanksgiving" in by[("2026-11-26", "closed")]["title"]
    assert ("2026-11-27", "early") in by and ("2026-12-25", "closed") in by
    assert by[("2026-12-18", "opex")]["imp"] == 2 and "triple" in by[("2026-12-18", "opex")]["title"]
    # the 2027 October meeting is a strategy meeting: no decision
    oct27 = econcal.fomc_events(date(2027, 10, 1), date(2027, 10, 31))
    assert "fed_strategy" in [x["key"] for x in oct27] and "fed" not in [x["key"] for x in oct27]


def test_release_matching():
    rows = [{"release_name": "Employment Situation", "date": "2026-12-04"},
            {"release_name": "Consumer Price Index", "date": "2026-12-10"},
            {"release_name": "Consumer Price Index", "date": "2026-12-10"},
            {"release_name": "H.15 Selected Interest Rates", "date": "2026-12-01"},
            {"release_name": "Personal Income and Outlays", "date": "2026-12-23"},
            {"release_name": "Unemployment Insurance Weekly Claims Report", "date": "2026-12-03"}]
    ev = econcal.release_events(rows, date(2026, 11, 25), date(2026, 12, 26))
    assert [(x["key"], x["imp"]) for x in ev] == [("jobs", 3), ("cpi", 3), ("pce", 3), ("claims", 1)]


def test_last_readings():
    con = db.get()
    con.execute("DELETE FROM macro WHERE series IN ('PAYEMS','UNRATE','CPIAUCSL','CPILFESL')")
    for i in range(14):
        d = date(2025 + (8 + i) // 12, (8 + i) % 12 + 1, 1)
        con.execute("INSERT INTO macro(series,date,value) VALUES('CPIAUCSL',?,?)", (d.isoformat(), 300 * 1.0025 ** i))
        con.execute("INSERT INTO macro(series,date,value) VALUES('CPILFESL',?,?)", (d.isoformat(), 310 * 1.0026 ** i))
        con.execute("INSERT INTO macro(series,date,value) VALUES('PAYEMS',?,?)", (d.isoformat(), 159000 + 100 * i))
        con.execute("INSERT INTO macro(series,date,value) VALUES('UNRATE',?,?)", (d.isoformat(), 4.4))
    assert econcal.last_reading("jobs").endswith("+100k jobs, unemployment 4.4%")
    assert "a year, core" in econcal.last_reading("cpi")
    assert econcal.last_reading("nothing") == ""


@pytest.fixture
def mine():
    for t in ("accounts", "lots", "closed_lots", "txns"):
        db.get().execute(f"DELETE FROM {t}")
    a = holdings.add_account("Cal")
    holdings.add_lot(a, "ZCAL", "2024-01-02", 100, 10, record_txn=False)
    yield
    for t in ("accounts", "lots"):
        db.get().execute(f"DELETE FROM {t}")
    db.get().execute("DELETE FROM config WHERE key LIKE 'cal_y_%'")


def test_your_earnings_and_dividends(mine):
    s = date.today()
    fake = {"earnings": (s + timedelta(days=5)).isoformat(), "earnings_range": False, "eps": 1.23,
            "ex_div": (s + timedelta(days=9)).isoformat()}
    names = {"ZCAL": {"value": 1500.0, "via": [], "shares": 100, "direct": True, "fund": False}}
    ev = econcal.my_events(s, s + timedelta(days=31), names, fetch=lambda t: fake)
    keys = {e["key"]: e for e in ev}
    assert keys["earnings"]["mine"] and "EPS estimate $1.23" in keys["earnings"]["detail"]
    assert keys["exdiv"]["date"] == fake["ex_div"] and "own it before" in keys["exdiv"]["detail"]


def test_calendar_html_and_daily_brief(monkeypatch):
    t = date(2026, 11, 25)  # a Wednesday
    cal = {"events": econcal.fomc_events(t, t + timedelta(days=31)) + econcal.market_events(t, t + timedelta(days=31))
           + econcal.release_events([{"release_name": "Employment Situation", "date": "2026-12-04"},
                                     {"release_name": "Consumer Price Index", "date": "2026-12-10"},
                                     {"release_name": "Unemployment Insurance Weekly Claims Report",
                                      "date": "2026-12-17"}], t, t + timedelta(days=31))
           + [{"date": "2026-12-02", "time": "", "cat": "Earnings", "imp": 2, "key": "earnings", "ticker": "ZCAL",
               "mine": True, "title": "ZCAL reports earnings", "detail": "you own it", "why": ""}],
           "notes": []}
    for e in cal["events"]:
        e.setdefault("prev", "")
    cal["events"].sort(key=lambda e: (e["date"], econcal._tsort(e.get("time"))))
    h = econcal.calendar_html(cal, t)
    assert "The big days" in h and "Fed decision" in h and "This week" in h and "Later this month" in h
    assert h.index("Thanksgiving") < h.index("This week") or "Market closed: Thanksgiving" in h
    # this week runs through next Sunday from a Wednesday? No: Wednesday -> through this Sunday
    assert "Week of Dec 7" in h and "Jobless claims" not in h  # minor events only in this week
    assert "★" in h and "ZCAL reports earnings" in h

    sent = []
    monkeypatch.setattr(emailer, "send", lambda subj, body, text="": sent.append((subj, body)))
    monkeypatch.setattr(emailer, "settings", lambda: {**emailer.DEFAULTS, "enabled": True, "send_actions": False})
    monkeypatch.setattr(emailer, "_pending_alerts", lambda: [])
    monkeypatch.setattr(emailer, "_pending_market", lambda: [])
    db.set_cfg("econ_calendar", {**cal, "events": [{**e, "date": (date.today() + timedelta(days=i % 20)).isoformat()}
                                                   for i, e in enumerate(cal["events"])]})
    db.set_cfg("calendar_sent", None)
    assert emailer.send_digest() == 1 and "Daily brief" in sent[0][0] and "The month ahead" in sent[0][1]
    assert emailer.send_digest() == 0  # once a day

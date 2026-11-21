"""Analyst estimates and the earnings-surprise record, from Yahoo Finance (via yfinance; free, unofficial, so every
field may be missing).

What is kept, one copy per ticker per day (so the app builds its own history of revisions and, later, of how
accurate the estimates were):
  periods    this quarter (0q), next quarter (+1q), this fiscal year (0y), next fiscal year (+1y): EPS and sales
             estimates (average, low, high), last year's figure, growth, number of analysts
  trend      the average EPS estimate now and 7, 30, 60, 90 days ago (revisions)
  revisions  how many analysts raised or cut their estimate in the last 7 and 30 days
  targets    analysts' 12-month price targets (low, mean, median, high) and the average recommendation
The surprise record (reported EPS vs the estimate, quarter by quarter) goes in its own table and accumulates.

Use with care: analysts' long-term growth forecasts are too optimistic and add little predictive power (Chan,
Karceski & Lakonishok 2003), so the valuation uses only the next one to two years, half-weighted.
Yahoo's EPS is usually the company's "adjusted" figure, which can sit above the EPS reported to the SEC.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

from . import db

PERIODS = ("0q", "+1q", "0y", "+1y")
MAX_AGE_HOURS = 20
STALE_DAYS = 45  # estimates older than this are not used by the valuation


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _table(df) -> dict[str, dict[str, float | None]]:
    """A yfinance DataFrame as {index: {column: float}}, column names lower-cased without spaces."""
    if df is None or getattr(df, "empty", True):
        return {}
    out = {}
    for idx, row in df.iterrows():
        out[str(idx)] = {str(c).lower().replace(" ", "").replace("_", ""): _f(row[c]) for c in df.columns}
    return out


def _pick(d: dict, *names):
    for n in names:
        if n.lower() in d and d[n.lower()] is not None:
            return d[n.lower()]
    return None


def parse(eps_est=None, rev_est=None, trend=None, revisions=None, targets=None, info=None) -> dict:
    """Normalize what yfinance returns (DataFrames or dicts) into the stored form."""
    e, r, tr, rv = _table(eps_est), _table(rev_est), _table(trend), _table(revisions)
    info = info or {}
    periods = {}
    for p in PERIODS:
        a, b = e.get(p, {}), r.get(p, {})
        if not a and not b:
            continue
        periods[p] = {"eps": _pick(a, "avg"), "eps_low": _pick(a, "low"), "eps_high": _pick(a, "high"),
                      "eps_year_ago": _pick(a, "yearagoeps"), "eps_growth": _pick(a, "growth"),
                      "analysts": _pick(a, "numberofanalysts"),
                      "sales": _pick(b, "avg"), "sales_low": _pick(b, "low"), "sales_high": _pick(b, "high"),
                      "sales_year_ago": _pick(b, "yearagorevenue"), "sales_growth": _pick(b, "growth"),
                      "sales_analysts": _pick(b, "numberofanalysts")}
    trend_out = {p: {"now": _pick(x, "current"), "7d": _pick(x, "7daysago"), "30d": _pick(x, "30daysago"),
                     "60d": _pick(x, "60daysago"), "90d": _pick(x, "90daysago")} for p, x in tr.items()}
    rev_out = {p: {"up7": _pick(x, "uplast7days"), "up30": _pick(x, "uplast30days"),
                   "down7": _pick(x, "downlast7days"), "down30": _pick(x, "downlast30days")} for p, x in rv.items()}
    t = targets if isinstance(targets, dict) else {}
    tg = {"low": _f(t.get("low")), "mean": _f(t.get("mean") or info.get("targetMeanPrice")),
          "median": _f(t.get("median") or info.get("targetMedianPrice")), "high": _f(t.get("high")),
          "analysts": _f(info.get("numberOfAnalystOpinions")), "rec_mean": _f(info.get("recommendationMean")),
          "rec_key": info.get("recommendationKey")}
    fy_end = None
    for k in ("lastFiscalYearEnd",):
        if info.get(k):
            try:
                fy_end = datetime.fromtimestamp(int(info[k]), timezone.utc).date().isoformat()
            except (TypeError, ValueError, OSError, OverflowError):
                pass
    return {"periods": periods, "trend": trend_out, "revisions": rev_out, "targets": tg, "last_fy_end": fy_end}


def parse_surprises(hist=None, dates=None) -> list[dict]:
    """Quarterly EPS: estimate, actual and surprise, from earnings_history and earnings dates (newest first)."""
    out: dict[str, dict] = {}
    for idx, x in _table(hist).items():
        est, act = _pick(x, "epsestimate"), _pick(x, "epsactual")
        if est is None or act is None:
            continue
        d = str(idx)[:10]
        sp = _pick(x, "surprisepercent")
        out[d] = {"date": d, "est": est, "actual": act,
                  "surprise": sp if sp is not None else ((act - est) / abs(est) if est else None)}
    for idx, x in _table(dates).items():
        est, act = _pick(x, "epsestimate"), _pick(x, "reportedeps")
        if est is None or act is None:
            continue
        d = str(idx)[:10]
        if any(abs((date.fromisoformat(d) - date.fromisoformat(k)).days) <= 45 for k in out):
            continue  # the same quarter, already known from earnings_history
        sp = _pick(x, "surprise(%)", "surprisepercent")
        out[d] = {"date": d, "est": est, "actual": act,
                  "surprise": (sp / 100.0) if sp is not None else ((act - est) / abs(est) if est else None)}
    for v in out.values():  # earnings_history gives a fraction, earnings dates a percent: keep fractions
        if v["surprise"] is not None and abs(v["surprise"]) > 5 and v["est"]:
            v["surprise"] = (v["actual"] - v["est"]) / abs(v["est"])
    return sorted(out.values(), key=lambda v: v["date"], reverse=True)


def fetch(ticker: str) -> tuple[dict, list[dict]]:  # pragma: no cover - network
    import yfinance as yf

    t = yf.Ticker(ticker)

    def safe(fn):
        try:
            return fn()
        except Exception:
            return None
    x = parse(safe(lambda: t.earnings_estimate), safe(lambda: t.revenue_estimate), safe(lambda: t.eps_trend),
              safe(lambda: t.eps_revisions), safe(lambda: t.analyst_price_targets), safe(lambda: t.info))
    sp = parse_surprises(safe(lambda: t.earnings_history), safe(lambda: t.get_earnings_dates(limit=28)))
    return x, sp


def store(ticker: str, x: dict, surprises: list[dict], day: str | None = None) -> None:
    day = day or date.today().isoformat()
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO estimates(ticker, day, fetched_at, json) VALUES(?,?,?,?)",
                    (ticker, day, datetime.now().isoformat(timespec="seconds"), json.dumps(x)))
        con.executemany("INSERT OR REPLACE INTO surprises(ticker, date, eps_est, eps_actual, surprise) "
                        "VALUES(?,?,?,?,?)", [(ticker, v["date"], v["est"], v["actual"], v["surprise"])
                                              for v in surprises])


def get(ticker: str, refresh: bool = False) -> dict | None:
    """Today's estimates (fetched at most once every 20 hours), or the latest stored copy if Yahoo fails."""
    r = db.one("SELECT day, fetched_at, json FROM estimates WHERE ticker=? ORDER BY day DESC LIMIT 1", (ticker,))
    fresh = r and datetime.fromisoformat(r["fetched_at"]) > datetime.now() - timedelta(hours=MAX_AGE_HOURS)
    if r and fresh and not refresh:
        return {**json.loads(r["json"]), "day": r["day"]}
    try:
        x, sp = fetch(ticker)
    except Exception as e:
        db.log("yahoo", f"estimates {ticker}", False, str(e)[:200])
        return {**json.loads(r["json"]), "day": r["day"]} if r else None
    if not x["periods"] and not sp:
        return {**json.loads(r["json"]), "day": r["day"]} if r else None
    store(ticker, x, sp)
    return {**x, "day": date.today().isoformat()}


def surprises(ticker: str, n: int = 12) -> list[dict]:
    return [dict(r) for r in db.rows("SELECT date, eps_est AS est, eps_actual AS actual, surprise FROM surprises "
                                     "WHERE ticker=? ORDER BY date DESC LIMIT ?", (ticker, n))]


def beat_record(sp: list[dict]) -> dict:
    """How often the company beat the EPS estimate, by how much, and its recent streak."""
    xs = [v for v in sp if v.get("surprise") is not None]
    if not xs:
        return {}
    beats = [v for v in xs if v["actual"] > v["est"]]
    streak = 0
    for v in xs:  # newest first
        if v["actual"] > v["est"]:
            streak += 1
        else:
            break
    s_sorted = sorted(v["surprise"] for v in xs)
    return {"n": len(xs), "beats": len(beats), "misses": sum(1 for v in xs if v["actual"] < v["est"]),
            "median": s_sorted[len(s_sorted) // 2], "streak": streak}


def near_term_growth(x: dict | None) -> dict | None:
    """Analysts' EPS growth per year over this and next fiscal year, from last year's EPS (their basis)."""
    if not x:
        return None
    p0, p1 = (x.get("periods") or {}).get("0y") or {}, (x.get("periods") or {}).get("+1y") or {}
    base, e0, e1 = p0.get("eps_year_ago"), p0.get("eps"), p1.get("eps")
    n = p0.get("analysts") or 0
    if base and base > 0 and e1 and e1 > 0:
        return {"growth": (e1 / base) ** 0.5 - 1, "analysts": n, "years": 2}
    if base and base > 0 and e0 and e0 > 0:
        return {"growth": e0 / base - 1, "analysts": n, "years": 1}
    return None


def asof(ticker: str, d: str) -> dict | None:
    """Near-term growth from the estimates stored on or before d (and at most 45 days old). Past dates in a
    backtest find nothing, so history is never valued with estimates that didn't exist yet."""
    # a copy fetched up to 4 days after d still counts (the daily update fetches before valuing the last session)
    lim = (date.fromisoformat(d) + timedelta(days=4)).isoformat()
    r = db.one("SELECT day, json FROM estimates WHERE ticker=? AND day<=? ORDER BY day DESC LIMIT 1", (ticker, lim))
    if not r or abs((date.fromisoformat(d) - date.fromisoformat(r["day"])).days) > STALE_DAYS:
        return None
    return near_term_growth(json.loads(r["json"]))


def history(ticker: str, period: str = "0y", days: int = 400) -> list[tuple[str, float]]:
    """The average EPS estimate for `period` as stored each day (the app's own revision history)."""
    since = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for r in db.rows("SELECT day, json FROM estimates WHERE ticker=? AND day>=? ORDER BY day", (ticker, since)):
        v = ((json.loads(r["json"]).get("periods") or {}).get(period) or {}).get("eps")
        if v is not None:
            out.append((r["day"], v))
    return out


def watchlist(limit: int = 150) -> list[str]:
    """Tickers worth refreshing every day: holdings, the Buy Zone and the best-ranked of the rest of the
    shortlist on the latest live run."""
    t = [r["ticker"] for r in db.rows("SELECT DISTINCT s.ticker FROM real_holdings h JOIN securities s ON s.cik=h.cik")]
    rid = db.scalar("SELECT id FROM runs WHERE kind='live' ORDER BY id DESC LIMIT 1")
    if rid:
        d = db.scalar("SELECT MAX(date) FROM evaluations WHERE run_id=?", (rid,))
        t += [r["ticker"] for r in db.rows(
            "SELECT s.ticker FROM evaluations e JOIN securities s ON s.cik=e.cik WHERE e.run_id=? AND e.date=? AND "
            "e.list IN ('buy','pricey') AND s.is_foreign=0 ORDER BY (e.list='buy') DESC, e.rank_score DESC LIMIT ?",
            (rid, d, limit))]
    return [x for x in dict.fromkeys(t) if x]


def refresh_watchlist(progress=None) -> int:
    ts = watchlist()
    n = 0
    for i, t in enumerate(ts):
        if get(t) is not None:
            n += 1
        if progress and i % 5 == 0:
            progress(i / max(len(ts), 1))
    return n

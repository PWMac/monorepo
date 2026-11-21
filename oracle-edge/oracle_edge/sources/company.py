"""Company profile and management, from Yahoo Finance (via yfinance): business summary, leaders and their pay,
insider and institutional ownership, insider buying and selling, and ISS governance risk scores (1 = low risk,
10 = high). Cached in the database for a week; free, unofficial data, so every field may be missing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .. import db

MAX_AGE_DAYS = 7
GOV = [("auditRisk", "Audit"), ("boardRisk", "Board"), ("compensationRisk", "Pay"),
       ("shareHolderRightsRisk", "Shareholder rights"), ("overallRisk", "Overall")]


def parse(info: dict, insider: list[dict] | None = None) -> dict:
    """Keep only what the app shows, under stable names."""
    info = info or {}
    officers = []
    for o in info.get("companyOfficers") or []:
        if not isinstance(o, dict) or not o.get("name"):
            continue
        officers.append({"name": o.get("name"), "title": o.get("title"), "age": o.get("age"),
                         "born": o.get("yearBorn"), "pay": o.get("totalPay"), "pay_year": o.get("fiscalYear")})
    gov = {label: info.get(k) for k, label in GOV if isinstance(info.get(k), (int, float))}
    return {
        "summary": info.get("longBusinessSummary"), "sector": info.get("sector"), "industry": info.get("industry"),
        "employees": info.get("fullTimeEmployees"), "website": info.get("website"), "country": info.get("country"),
        "insiders": info.get("heldPercentInsiders"), "institutions": info.get("heldPercentInstitutions"),
        "officers": officers, "governance": gov, "insider_activity": insider or [],
    }


def _insider(t) -> list[dict]:
    """Last six months of insider purchases and sales (Yahoo's summary table), as {label, shares, trades}."""
    try:
        df = t.insider_purchases
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    cols = list(df.columns)
    for _, r in df.iterrows():
        label = str(r[cols[0]])
        shares = r.get("Shares") if "Shares" in cols else None
        trades = r.get("Trans") if "Trans" in cols else None
        try:
            shares = float(shares) if shares is not None else None
        except (TypeError, ValueError):
            shares = None
        try:
            trades = int(trades) if trades is not None and trades == trades else None
        except (TypeError, ValueError):
            trades = None
        out.append({"label": label, "shares": shares, "trades": trades})
    return out


def fetch(ticker: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(ticker)
    info = t.info or {}
    return parse(info, _insider(t))


def get(ticker: str, refresh: bool = False) -> dict | None:
    """Cached profile; refetched when older than a week. None if Yahoo has nothing and nothing is cached."""
    r = db.one("SELECT fetched_at, info_json FROM company_info WHERE ticker=?", (ticker,))
    fresh = r and datetime.fromisoformat(r["fetched_at"]) > datetime.now() - timedelta(days=MAX_AGE_DAYS)
    if r and fresh and not refresh:
        return json.loads(r["info_json"])
    try:
        x = fetch(ticker)
    except Exception as e:  # offline or Yahoo changed: keep the old copy if there is one
        db.log("yahoo", f"profile {ticker}", False, str(e)[:200])
        return json.loads(r["info_json"]) if r else None
    if not (x.get("summary") or x.get("officers")):
        return json.loads(r["info_json"]) if r else None
    db.get().execute("INSERT OR REPLACE INTO company_info(ticker, fetched_at, info_json) VALUES(?,?,?)",
                     (ticker, datetime.now().isoformat(timespec="seconds"), json.dumps(x)))
    return x

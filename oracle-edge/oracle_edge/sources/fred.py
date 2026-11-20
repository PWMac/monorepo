"""FRED macro series via the public CSV endpoint (no API key needed).

Series used:
  BOGZ1LM893064105Q  All sectors; corporate equities; liability, market value (Fed Z.1), $ millions, quarterly
  NCBEILQ027S        fallback: nonfinancial corporate equities, $ millions
  GDP                nominal GDP, $ billions SAAR, quarterly
  DGS30 / DGS10      30-yr and 10-yr Treasury yields, %, daily
  VIXCLS             CBOE volatility index (VIX), daily close (market overlay)
"""

from __future__ import annotations

import io

import pandas as pd

from .. import db
from .http import Http

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES = ["BOGZ1LM893064105Q", "NCBEILQ027S", "GDP", "DGS30", "DGS10", "VIXCLS"]

# Daily FX (H.10). invert=True means the series is "foreign units per 1 USD".
FX_SERIES = {
    "EUR": ("DEXUSEU", False), "GBP": ("DEXUSUK", False), "AUD": ("DEXUSAL", False), "NZD": ("DEXUSNZ", False),
    "JPY": ("DEXJPUS", True), "CAD": ("DEXCAUS", True), "CHF": ("DEXSZUS", True), "CNY": ("DEXCHUS", True),
    "HKD": ("DEXHKUS", True), "KRW": ("DEXKOUS", True), "INR": ("DEXINUS", True), "BRL": ("DEXBZUS", True),
    "MXN": ("DEXMXUS", True), "TWD": ("DEXTAUS", True), "SEK": ("DEXSDUS", True), "DKK": ("DEXDNUS", True),
    "NOK": ("DEXNOUS", True), "SGD": ("DEXSIUS", True), "ZAR": ("DEXSFUS", True), "THB": ("DEXTHUS", True),
    "MYR": ("DEXMAUS", True),
}

# FRED normally answers in under a second. When it is having trouble it can hang for minutes per request, so the
# timeout is short and the whole refresh gives up after a few failures in a row (existing data is kept).
_http = Http("Mozilla/5.0 OracleEdge", per_second=2, timeout=20)
GIVE_UP_AFTER = 3


def fetch_series(series_id: str, start: str = "1990-01-01") -> pd.DataFrame:
    r = _http.get(URL, params={"id": series_id, "cosd": start}, retries=2)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna()


def needed_fx() -> list[str]:
    cur = {r["currency"] for r in db.rows("SELECT DISTINCT currency FROM securities WHERE is_foreign=1")}
    return [FX_SERIES[c][0] for c in sorted(cur) if c in FX_SERIES]


def vix_from_yahoo(start: str = "1990-01-01") -> pd.DataFrame:
    """VIX daily closes from Yahoo (^VIX), used when FRED is unavailable."""
    import yfinance as yf

    d = yf.download("^VIX", start=start, auto_adjust=False, progress=False, threads=False)
    if d is None or d.empty:
        raise RuntimeError("Yahoo returned no VIX data")
    close = d["Close"]
    if isinstance(close, pd.DataFrame):  # newer yfinance keeps a ticker level on the columns
        close = close.iloc[:, 0]
    close = close.dropna()
    return pd.DataFrame({"date": close.index.strftime("%Y-%m-%d"), "value": close.values.astype(float)})


def fetch_vix(start: str = "1990-01-01") -> tuple[pd.DataFrame, str]:
    try:
        return fetch_series("VIXCLS", start), "fred"
    except Exception as e:
        db.log("fred", "VIXCLS", False, f"{e} (trying Yahoo ^VIX)")
    return vix_from_yahoo(start), "yahoo"


def _store(series: str, df: pd.DataFrame) -> None:
    with db.tx() as con:
        con.executemany("INSERT OR REPLACE INTO macro(series,date,value) VALUES(?,?,?)",
                        [(series, d, float(v)) for d, v in zip(df["date"], df["value"])])


def refresh_all(progress=None) -> dict:
    """Refresh every series. Gives up after GIVE_UP_AFTER failures in a row (FRED down or blocking): the data
    already stored stays, and the next daily run tries again. VIX falls back to Yahoo."""
    out = {}
    todo = SERIES + needed_fx()
    fails = 0
    for k, s in enumerate(todo):
        if progress:
            progress(k / max(len(todo), 1))
        if fails >= GIVE_UP_AFTER:
            out[s] = 0
            continue
        try:
            df = fetch_series(s)
            _store(s, df)
            out[s] = len(df)
            fails = 0
            db.log("fred", s, True, f"{len(df)} rows")
        except Exception as e:
            out[s] = 0
            fails += 1
            db.log("fred", s, False, str(e))
    if fails >= GIVE_UP_AFTER:
        skipped = [s for s in todo if out.get(s) == 0]
        db.log("fred", "refresh", False, f"FRED is not responding; skipped {len(skipped)} series and kept the "
                                         "data already stored. The next daily update tries again.")
    if not out.get("VIXCLS"):
        try:
            df = vix_from_yahoo()
            _store("VIXCLS", df)
            out["VIXCLS"] = len(df)
            db.log("yahoo", "^VIX", True, f"{len(df)} rows (FRED unavailable)")
        except Exception as e:
            db.log("yahoo", "^VIX", False, str(e))
    if progress:
        progress(1.0)
    return out

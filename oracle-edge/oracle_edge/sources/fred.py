"""FRED macro series via the public CSV endpoint (no API key needed).

Series used:
  BOGZ1LM893064105Q  All sectors; corporate equities; liability, market value (Fed Z.1), $ millions, quarterly
  NCBEILQ027S        fallback: nonfinancial corporate equities, $ millions
  GDP                nominal GDP, $ billions SAAR, quarterly
  DGS30 / DGS10      30-yr and 10-yr Treasury yields, %, daily
"""

from __future__ import annotations

import io

import pandas as pd

from .. import db
from .http import Http

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES = ["BOGZ1LM893064105Q", "NCBEILQ027S", "GDP", "DGS30", "DGS10"]

# Daily FX (H.10). invert=True means the series is "foreign units per 1 USD".
FX_SERIES = {
    "EUR": ("DEXUSEU", False), "GBP": ("DEXUSUK", False), "AUD": ("DEXUSAL", False), "NZD": ("DEXUSNZ", False),
    "JPY": ("DEXJPUS", True), "CAD": ("DEXCAUS", True), "CHF": ("DEXSZUS", True), "CNY": ("DEXCHUS", True),
    "HKD": ("DEXHKUS", True), "KRW": ("DEXKOUS", True), "INR": ("DEXINUS", True), "BRL": ("DEXBZUS", True),
    "MXN": ("DEXMXUS", True), "TWD": ("DEXTAUS", True), "SEK": ("DEXSDUS", True), "DKK": ("DEXDNUS", True),
    "NOK": ("DEXNOUS", True), "SGD": ("DEXSIUS", True), "ZAR": ("DEXSFUS", True), "THB": ("DEXTHUS", True),
    "MYR": ("DEXMAUS", True),
}

_http = Http("Mozilla/5.0 OracleEdge", per_second=2)


def fetch_series(series_id: str, start: str = "1990-01-01") -> pd.DataFrame:
    r = _http.get(URL, params={"id": series_id, "cosd": start})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna()


def needed_fx() -> list[str]:
    cur = {r["currency"] for r in db.rows("SELECT DISTINCT currency FROM securities WHERE is_foreign=1")}
    return [FX_SERIES[c][0] for c in sorted(cur) if c in FX_SERIES]


def refresh_all() -> dict:
    out = {}
    for s in SERIES + needed_fx():
        try:
            df = fetch_series(s)
            with db.tx() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO macro(series,date,value) VALUES(?,?,?)",
                    [(s, d, float(v)) for d, v in zip(df["date"], df["value"])],
                )
            out[s] = len(df)
            db.log("fred", s, True, f"{len(df)} rows")
        except Exception as e:
            out[s] = 0
            db.log("fred", s, False, str(e))
    return out

"""World Bank open data (no key): world stock-market value and world GDP for the global Buffett Indicator.

Series (country WLD = the world aggregate, annual, end-of-year values):
  CM.MKT.LCAP.CD      market capitalization of listed domestic companies, current US$ (World Federation of Exchanges)
  NY.GDP.MKTP.CD      GDP, current US$
  CM.MKT.LCAP.GD.ZS   the two divided, % of GDP, as the World Bank publishes it
Stored in the macro table as WB_WLD_MCAP, WB_WLD_GDP, WB_WLD_MCAP_GDP, dated December 31 of each year.
"""

from __future__ import annotations

from .. import db
from .http import Http

URL = "https://api.worldbank.org/v2/country/WLD/indicator/{ind}"
SERIES = {"CM.MKT.LCAP.CD": "WB_WLD_MCAP", "NY.GDP.MKTP.CD": "WB_WLD_GDP", "CM.MKT.LCAP.GD.ZS": "WB_WLD_MCAP_GDP"}
_http = Http("Mozilla/5.0 OracleEdge", per_second=2, timeout=15)


def parse(js) -> list[tuple[str, float]]:
    """[(YYYY-12-31, value)] from a World Bank JSON response ([paging, [observations]])."""
    if not isinstance(js, list) or len(js) < 2 or not isinstance(js[1], list):
        return []
    out = []
    for o in js[1]:
        y, v = str(o.get("date") or ""), o.get("value")
        if len(y) == 4 and y.isdigit() and v is not None:
            out.append((f"{y}-12-31", float(v)))
    return sorted(out)


def refresh() -> dict:
    out = {}
    for ind, name in SERIES.items():
        try:
            r = _http.get(URL.format(ind=ind), params={"format": "json", "per_page": 200, "date": "1990:2100"},
                          retries=1)
            r.raise_for_status()
            rows = parse(r.json())
            with db.tx() as con:
                con.executemany("INSERT OR REPLACE INTO macro(series,date,value) VALUES(?,?,?)",
                                [(name, d, v) for d, v in rows])
            out[name] = len(rows)
            db.log("worldbank", ind, True, f"{len(rows)} years")
        except Exception as e:  # annual data: if the site is down, keep what's stored and try tomorrow
            out[name] = 0
            db.log("worldbank", ind, False, str(e))
            break
    return out

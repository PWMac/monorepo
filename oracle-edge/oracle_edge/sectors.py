"""Sectors from SEC SIC codes (an approximation of the 11 GICS sectors) and price correlation helpers.

Used by the portfolio limits: maximum sector exposure and maximum correlated exposure.
"""

from __future__ import annotations

import math

from . import db

# (low SIC, high SIC, sector): first match wins, so specific ranges come before broad ones
_RANGES = [
    (1311, 1389, "Energy"), (1200, 1299, "Energy"), (2900, 2999, "Energy"),
    (2830, 2836, "Health Care"), (3840, 3851, "Health Care"), (5120, 5122, "Health Care"), (8000, 8099, "Health Care"),
    (2840, 2844, "Consumer Staples"), (5140, 5149, "Consumer Staples"), (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"), (2000, 2199, "Consumer Staples"), (100, 999, "Consumer Staples"),
    (3570, 3579, "Information Technology"), (3660, 3699, "Information Technology"),
    (3600, 3629, "Information Technology"), (3640, 3659, "Information Technology"),
    (7370, 7379, "Information Technology"), (3800, 3839, "Information Technology"),
    (3852, 3899, "Information Technology"),
    (3630, 3639, "Consumer Discretionary"), (3710, 3716, "Consumer Discretionary"),
    (2200, 2399, "Consumer Discretionary"), (2500, 2599, "Consumer Discretionary"),
    (3100, 3199, "Consumer Discretionary"), (3900, 3999, "Consumer Discretionary"),
    (5200, 5999, "Consumer Discretionary"), (7000, 7299, "Consumer Discretionary"),
    (7500, 7599, "Consumer Discretionary"), (7900, 7999, "Consumer Discretionary"),
    (4800, 4899, "Communication Services"), (2700, 2799, "Communication Services"),
    (7800, 7899, "Communication Services"),
    (4900, 4999, "Utilities"),
    (6500, 6599, "Real Estate"), (6798, 6798, "Real Estate"),
    (6000, 6799, "Financials"),
    (1000, 1199, "Materials"), (1400, 1499, "Materials"), (2400, 2499, "Materials"), (2600, 2699, "Materials"),
    (2800, 2899, "Materials"), (3000, 3099, "Materials"), (3200, 3399, "Materials"),
    (1500, 1799, "Industrials"), (3400, 3569, "Industrials"), (3580, 3599, "Industrials"),
    (3700, 3799, "Industrials"), (4000, 4799, "Industrials"), (5000, 5199, "Industrials"),
    (7300, 7399, "Industrials"), (8100, 8999, "Industrials"),
]


def sector_of_sic(sic) -> str:
    try:
        x = int(sic)
    except (TypeError, ValueError):
        return "Unknown"
    for lo, hi, name in _RANGES:
        if lo <= x <= hi:
            return name
    return "Other"


def sector_map() -> dict[int, str]:
    """cik -> sector. Companies without a SIC code get their own bucket ('Unknown: <cik>') so they never
    get lumped together."""
    out = {}
    for r in db.rows("SELECT cik, sic FROM securities"):
        sec = sector_of_sic(r["sic"])
        out[r["cik"]] = sec if sec not in ("Unknown", "Other") else f"{sec}: {r['cik']}"
    return out


def correlation(ra: dict[str, float], rb: dict[str, float], min_days: int = 120) -> float | None:
    """Pearson correlation of two daily-return series on their common dates."""
    common = [d for d in ra if d in rb]
    if len(common) < min_days:
        return None
    xs = [ra[d] for d in common]
    ys = [rb[d] for d in common]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)

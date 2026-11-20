"""Daily price sources behind one small interface, with automatic fallback.

Free by default (Stooq, then Yahoo via yfinance). Tiingo / EODHD are one-key upgrades.
Order and keys are set in Settings -> Data sources.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .. import db, secrets
from .http import Http

COLUMNS = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume"]


@dataclass
class Health:
    ok: bool
    detail: str


class PriceSource:
    name = "base"
    label = "Base"
    needs_key = False
    supports_bulk = False
    supports_delisted = False
    free = True

    def daily_bars(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def health_check(self) -> Health:
        try:
            df = self.daily_bars(["KO"], date.today().replace(day=1) - pd.Timedelta(days=20), date.today())
            if df.empty:
                return Health(False, "No rows returned for KO")
            last = df.sort_values("date").iloc[-1]
            return Health(True, f"KO last close {last['close']:.2f} on {last['date']}")
        except Exception as e:
            return Health(False, str(e)[:300])


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


class Stooq(PriceSource):
    name, label = "stooq", "Stooq (free, no key)"

    def __init__(self):
        self.http = Http("Mozilla/5.0 OracleEdge", per_second=3)

    @staticmethod
    def symbol(t: str) -> str:
        return t.lower().replace(".", "-") + ".us"

    def daily_bars(self, tickers, start, end):
        frames = []
        for t in tickers:
            url = "https://stooq.com/q/d/l/"
            params = {"s": self.symbol(t), "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d"), "i": "d"}
            r = self.http.get(url, params=params)
            text = r.text.strip()
            if r.status_code != 200 or not text.lower().startswith("date"):
                raise RuntimeError(f"Stooq returned no CSV for {t} ({r.status_code}): {text[:80]}")
            df = pd.read_csv(io.StringIO(text))
            if df.empty:
                continue
            df.columns = [c.lower() for c in df.columns]
            df["ticker"] = t
            df["adj_close"] = df["close"]  # Stooq series are split/dividend adjusted
            df["volume"] = df.get("volume", 0)
            frames.append(df[COLUMNS])
        return pd.concat(frames) if frames else _empty()


class Yahoo(PriceSource):
    name, label = "yahoo", "Yahoo Finance via yfinance (free, can break)"
    supports_bulk = True

    def daily_bars(self, tickers, start, end):
        import yfinance as yf

        frames = []
        for i in range(0, len(tickers), 80):
            chunk = tickers[i : i + 80]
            data = yf.download(
                chunk, start=str(start), end=str(end + pd.Timedelta(days=1)), auto_adjust=False,
                group_by="ticker", threads=True, progress=False,
            )
            if data is None or data.empty:
                continue
            for t in chunk:
                try:
                    d = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                except KeyError:
                    continue
                d = d.dropna(subset=["Close"])
                if d.empty:
                    continue
                df = pd.DataFrame({
                    "ticker": t, "date": d.index.strftime("%Y-%m-%d"), "open": d["Open"].values,
                    "high": d["High"].values, "low": d["Low"].values, "close": d["Close"].values,
                    "adj_close": d.get("Adj Close", d["Close"]).values, "volume": d["Volume"].values,
                })
                frames.append(df)
        return pd.concat(frames) if frames else _empty()


class Tiingo(PriceSource):
    name, label = "tiingo", "Tiingo (paid ~$30/mo, delisted included)"
    needs_key, supports_delisted, free = True, True, False

    def __init__(self):
        self.http = Http("OracleEdge", per_second=5)

    def daily_bars(self, tickers, start, end):
        key = secrets.get("tiingo_key")
        if not key:
            raise RuntimeError("Tiingo key not set")
        frames = []
        for t in tickers:
            r = self.http.get(
                f"https://api.tiingo.com/tiingo/daily/{t}/prices",
                params={"startDate": str(start), "endDate": str(end), "token": key},
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            js = r.json()
            if not js:
                continue
            df = pd.DataFrame(js)
            frames.append(pd.DataFrame({
                "ticker": t, "date": df["date"].str[:10], "open": df["open"], "high": df["high"],
                "low": df["low"], "close": df["close"], "adj_close": df["adjClose"], "volume": df["volume"],
            }))
        return pd.concat(frames) if frames else _empty()


class EODHD(PriceSource):
    name, label = "eodhd", "EODHD (paid ~$20/mo, bulk + delisted)"
    needs_key, supports_delisted, supports_bulk, free = True, True, True, False

    def __init__(self):
        self.http = Http("OracleEdge", per_second=5)

    def daily_bars(self, tickers, start, end):
        key = secrets.get("eodhd_key")
        if not key:
            raise RuntimeError("EODHD key not set")
        frames = []
        for t in tickers:
            r = self.http.get(
                f"https://eodhd.com/api/eod/{t.replace('.', '-')}.US",
                params={"from": str(start), "to": str(end), "api_token": key, "fmt": "json"},
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            js = r.json()
            if not js:
                continue
            df = pd.DataFrame(js)
            frames.append(pd.DataFrame({
                "ticker": t, "date": df["date"], "open": df["open"], "high": df["high"], "low": df["low"],
                "close": df["close"], "adj_close": df["adjusted_close"], "volume": df["volume"],
            }))
        return pd.concat(frames) if frames else _empty()


class SchwabMarketData(PriceSource):
    name, label = "schwab", "Schwab Market Data (free with account, phase 5)"
    needs_key = True

    def daily_bars(self, tickers, start, end):
        raise RuntimeError("Schwab connection arrives in phase 5")


REGISTRY: dict[str, type[PriceSource]] = {c.name: c for c in (Stooq, Yahoo, Tiingo, EODHD, SchwabMarketData)}
DEFAULT_ORDER = ["stooq", "yahoo"]


def ordered_sources() -> list[PriceSource]:
    order = db.cfg("price_sources", DEFAULT_ORDER)
    return [REGISTRY[n]() for n in order if n in REGISTRY]


def store(df: pd.DataFrame, source: str) -> int:
    if df is None or df.empty:
        return 0
    df = df.dropna(subset=["close"])
    recs = [
        (r.ticker, str(r.date)[:10], r.open, r.high, r.low, r.close, r.adj_close, r.volume, source)
        for r in df.itertuples(index=False)
    ]
    with db.tx() as con:
        con.executemany(
            "INSERT OR REPLACE INTO prices(ticker,date,open,high,low,close,adj_close,volume,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            recs,
        )
    return len(recs)


def fetch(tickers: list[str], start: date, end: date, progress=None) -> dict:
    """Fetch prices with fallback. Returns {"rows": n, "by_source": {...}, "missing": [...]}."""
    remaining = list(dict.fromkeys(tickers))
    by_source: dict[str, int] = {}
    total = len(remaining) or 1
    for src in ordered_sources():
        if not remaining:
            break
        failures, successes, still = 0, 0, []
        chunk = 40 if src.supports_bulk else 1
        for i in range(0, len(remaining), chunk):
            part = remaining[i : i + chunk]
            if failures >= 10 and successes == 0:
                still.extend(part)  # source looks down for this run; hand the rest to the next one
                continue
            try:
                df = src.daily_bars(part, start, end)
                got = set(df["ticker"].unique()) if not df.empty else set()
                n = store(df, src.name)
                by_source[src.name] = by_source.get(src.name, 0) + n
                successes += len(got)
                failures += len(set(part) - got)
                still.extend([t for t in part if t not in got])
            except Exception as e:
                failures += len(part)
                still.extend(part)
                db.log(src.name, "prices", False, f"{part[:3]}: {e}")
            if progress:
                progress(1 - (len(remaining) - i) / total)
        db.log(src.name, "prices", successes > 0, f"{successes} tickers ok, {failures} failed")
        remaining = still
    return {"rows": sum(by_source.values()), "by_source": by_source, "missing": remaining}


def compare_sources(old: str, new: str, tickers: list[str], days: int = 60) -> list[dict]:
    """Switch-over check: tickers whose close differs by >0.5% between two sources."""
    a, b = REGISTRY[old](), REGISTRY[new]()
    end = date.today()
    start = end - pd.Timedelta(days=days)
    da, dbb = a.daily_bars(tickers, start, end), b.daily_bars(tickers, start, end)
    if da.empty or dbb.empty:
        return []
    m = da.merge(dbb, on=["ticker", "date"], suffixes=("_a", "_b"))
    m["diff"] = (m["close_a"] / m["close_b"] - 1).abs()
    bad = m[m["diff"] > 0.005].groupby("ticker")["diff"].max().reset_index()
    return bad.to_dict("records")

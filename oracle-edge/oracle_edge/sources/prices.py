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

    SPECIAL = {"BTC-USD": "btcusd"}

    @staticmethod
    def symbol(t: str) -> str:
        return Stooq.SPECIAL.get(t) or t.lower().replace(".", "-") + ".us"

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
    name, label = "yahoo", "Yahoo Finance via yfinance (free; split-adjusted close and dividend-adjusted close)"
    supports_bulk = True

    def __init__(self):
        self.splits_found: list[tuple[str, str, float]] = []  # (ticker, date, ratio) seen in the last download

    def daily_bars(self, tickers, start, end):
        import yfinance as yf

        frames = []
        for i in range(0, len(tickers), 80):
            chunk = tickers[i : i + 80]
            data = yf.download(
                chunk, start=str(start), end=str(end + pd.Timedelta(days=1)), auto_adjust=False,
                group_by="ticker", threads=True, progress=False, actions=True,
            )
            if data is None or data.empty:
                continue
            for t in chunk:
                try:
                    d = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                except KeyError:
                    continue
                if "Stock Splits" in d.columns:
                    sp = d["Stock Splits"].fillna(0)
                    for when, ratio in sp[sp > 0].items():
                        if float(ratio) != 1.0:
                            self.splits_found.append((t, when.strftime("%Y-%m-%d"), float(ratio)))
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
DEFAULT_ORDER = ["yahoo", "stooq"]
"""Yahoo first: its 'Close' is the price that actually traded (adjusted only for splits) and 'Adj Close' adds
dividends, and it reports split dates. Stooq has only a dividend-adjusted close, so it is the fallback."""
OLD_DEFAULT_ORDER = ["stooq", "yahoo"]


def ordered_sources() -> list[PriceSource]:
    order = db.cfg("price_sources", DEFAULT_ORDER)
    return [REGISTRY[n]() for n in order if n in REGISTRY]


REBASE_TOL = 0.0005  # a stored price differing from the source's by more than 0.05% = the basis changed


def store(df: pd.DataFrame, source: str) -> int:
    """Upsert bars. Where they overlap stored bars on a different basis (the source re-adjusted its history for a
    split or dividend), the older stored history is rescaled first, so the series stays continuous."""
    if df is None or df.empty:
        return 0
    df = df.dropna(subset=["close"])
    n = 0
    for t, g in df.groupby("ticker", sort=False):
        rebase_overlap(t, g)
        recs = [(r.ticker, str(r.date)[:10], r.open, r.high, r.low, r.close, r.adj_close, r.volume, source)
                for r in g.itertuples(index=False)]
        with db.tx() as con:
            con.executemany(
                "INSERT OR REPLACE INTO prices(ticker,date,open,high,low,close,adj_close,volume,source) "
                "VALUES(?,?,?,?,?,?,?,?,?)", recs)
        n += len(recs)
    return n


def _ratios(t: str, g: pd.DataFrame) -> list[tuple[str, float | None, float | None]]:
    """(date, new close / stored close, new adj / stored adj) on the dates both have, oldest first."""
    dates = [str(d)[:10] for d in g["date"]]
    if not dates:
        return []
    old = {r["date"]: r for r in db.rows("SELECT date, close, adj_close FROM prices WHERE ticker=? AND date BETWEEN ? "
                                         "AND ?", (t, min(dates), max(dates)))}
    out = []
    for r in g.itertuples(index=False):
        d = str(r.date)[:10]
        o = old.get(d)
        if not o:
            continue
        rc = (float(r.close) / o["close"]) if (o["close"] and r.close) else None
        ra = (float(r.adj_close) / o["adj_close"]) if (o["adj_close"] and r.adj_close) else None
        out.append((d, rc, ra))
    return sorted(out)


def rebase_overlap(t: str, g: pd.DataFrame) -> dict | None:
    """If the new bars' basis differs from the stored bars', rescale every stored bar up to the last date where they
    differ, so old and new join without a jump. A change in the close by a usual split ratio is recorded as a split.
    Holdings in every portfolio are restated so their value doesn't change (more shares after a split; after a
    dividend adjustment, the extra fraction of a share is the reinvested dividend)."""
    rs = _ratios(t, g)
    off = [x for x in rs if (x[1] is not None and abs(x[1] - 1) > REBASE_TOL)
           or (x[2] is not None and abs(x[2] - 1) > REBASE_TOL)]
    if not off:
        return None
    boundary, rc, ra = off[-1]
    # A basis change moves every earlier day by the same ratio. A source correcting one day's close doesn't:
    # then the new bar simply overwrites the old one.
    before = [x for x in rs if x[0] <= boundary]
    same = all((x[1] is None or rc is None or abs(x[1] / rc - 1) <= 0.0002)
               and (x[2] is None or ra is None or abs(x[2] / ra - 1) <= 0.0002) for x in before)
    if len(off) != len(before) or not same:
        return None
    rc = rc or 1.0
    ra = ra or rc
    with db.tx() as con:
        con.execute("UPDATE prices SET open=open*?, high=high*?, low=low*?, close=close*?, adj_close=adj_close*? "
                    "WHERE ticker=? AND date<=?", (rc, rc, rc, rc, ra, t, boundary))
    split = None
    if abs(rc - 1) > 0.15:
        from .. import splits

        sr = splits.nice_ratio(1.0 / rc)
        if sr:
            nxt = [d for d, _, _ in rs if d > boundary]
            split = (nxt[0] if nxt else (date.fromisoformat(boundary) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
            splits.store(t, [(split, sr)], "inferred")
    rebase_holdings(t, ra)
    db.log("prices", "rebase", True, f"{t}: stored history up to {boundary} rescaled (close x{rc:.6g}, adjusted "
                                     f"x{ra:.6g})" + (f"; split on {split}" if split else ""))
    return {"ticker": t, "boundary": boundary, "close": rc, "adj": ra, "split": split}


def rebase_holdings(t: str, ra: float) -> None:
    """Restate stored per-share prices of holdings and open entries after the price history was rescaled by `ra`,
    so their value and returns carry on unchanged."""
    if not ra or abs(ra - 1) <= 1e-12:
        return
    cik = db.scalar("SELECT cik FROM securities WHERE ticker=?", (t,))
    if not cik:
        return
    with db.tx() as con:
        con.execute("UPDATE positions SET units=units/?, avg_cost=avg_cost*? WHERE cik=?", (ra, ra, cik))
        con.execute("UPDATE episodes SET entry_px=entry_px*? WHERE cik=? AND status='open'", (ra, cik))
        con.execute("UPDATE stints SET entry_px=entry_px*? WHERE cik=? AND (status='open' OR fwd_1y IS NULL "
                    "OR bench_fwd_1y IS NULL)", (ra, cik))


def replace_history(t: str, g: pd.DataFrame, source: str) -> int:
    """Swap a ticker's whole stored history for a fresh download (consistent basis end to end), restating
    holdings by the change in basis on the latest date both histories have."""
    g = g.dropna(subset=["close"])
    if g.empty:
        return 0
    rs = _ratios(t, g)
    if rs:
        _, rc, ra = rs[-1]
        rebase_holdings(t, ra or rc or 1.0)
    recs = [(r.ticker, str(r.date)[:10], r.open, r.high, r.low, r.close, r.adj_close, r.volume, source)
            for r in g.itertuples(index=False)]
    with db.tx() as con:
        con.execute("DELETE FROM prices WHERE ticker=?", (t,))
        con.executemany("INSERT INTO prices(ticker,date,open,high,low,close,adj_close,volume,source) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", recs)
    return len(recs)


def refresh_history(tickers: list[str], start: date, progress=None) -> dict:
    """Re-download whole price histories (Yahoo first, for the actually-traded close and the split dates; Stooq for
    any Yahoo misses) and record every split. Used once to apply the share-basis fixes and on demand."""
    from .. import splits as _splits

    todo = list(dict.fromkeys(tickers))
    done, missing, n_splits = 0, [], 0
    total = len(todo) or 1
    y = Yahoo()
    for i in range(0, len(todo), 40):
        part = todo[i : i + 40]
        y.splits_found = []
        try:
            df = y.daily_bars(part, start, date.today())
        except Exception as e:
            db.log("yahoo", "history", False, f"{part[:3]}: {e}")
            df = _empty()
        got = set(df["ticker"].unique()) if not df.empty else set()
        for t in got:
            replace_history(t, df[df["ticker"] == t], "yahoo")
            done += 1
        by_t: dict[str, list] = {}
        for t, d, r in y.splits_found:
            by_t.setdefault(t, []).append((d, r))
        for t, rows in by_t.items():
            n_splits += _splits.store(t, rows, "yahoo")
        missing += [t for t in part if t not in got]
        if progress:
            progress(min(1.0, (i + len(part)) / total))
    still = []
    if missing:
        st = Stooq()
        for t in missing:
            try:
                df = st.daily_bars([t], start, date.today())
            except Exception:
                df = _empty()
            if df.empty:
                still.append(t)
                continue
            replace_history(t, df, "stooq")
            done += 1
    db.log("prices", "history", done > 0, f"{done} histories replaced, {n_splits} splits recorded, "
                                         f"{len(still)} not found")
    return {"done": done, "splits": n_splits, "missing": still}


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
                if isinstance(src, Yahoo):
                    src.splits_found = []
                df = src.daily_bars(part, start, end)
                got = set(df["ticker"].unique()) if not df.empty else set()
                n = store(df, src.name)
                if isinstance(src, Yahoo) and src.splits_found:
                    from .. import splits as _splits

                    for t, d, r in src.splits_found:
                        _splits.store(t, [(d, r)], "yahoo")
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

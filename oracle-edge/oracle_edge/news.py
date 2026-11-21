"""Headlines for the Today page: general market news, and news on the stocks you hold, the model holds or that
have price alerts. Headlines and links only (the articles stay on their publishers' sites).

Sources (free, no key): Yahoo Finance's market news RSS (Google News' business search as the fallback) for the
market, and Yahoo Finance's per-ticker news (via yfinance) for stocks. Results are cached in the database
(market 30 minutes, each stock 60 minutes) so the page never waits on a slow site twice.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import db

MARKET_FEEDS = [
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Google News", "https://news.google.com/rss/search?q=stock+market+when:2d&hl=en-US&gl=US&ceid=US:en"),
]
MARKET_TTL, STOCK_TTL = 30, 60  # minutes


def _when(s) -> str | None:
    """Any of RFC 822 ('Thu, 20 Nov 2026 21:08:53 GMT'), ISO 8601 or epoch seconds -> ISO UTC."""
    if s is None or s == "":
        return None
    try:
        if isinstance(s, (int, float)):
            d = datetime.fromtimestamp(float(s), tz=timezone.utc)
        elif str(s)[:4].isdigit():
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        else:
            d = parsedate_to_datetime(str(s))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def parse_rss(text: str, default_source: str = "") -> list[dict]:
    """RSS 2.0 items -> [{title, url, source, when}]. Google News titles end with ' - Publisher': split it off."""
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        url = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None and src_el.text else default_source
        if src and title.endswith(" - " + src):
            title = title[: -(len(src) + 3)]
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "source": src, "when": _when(it.findtext("pubDate"))})
    return out


def parse_yahoo(items) -> list[dict]:
    """yfinance Ticker.news, in either the newer layout (fields under 'content') or the older flat one."""
    out = []
    for n in items or []:
        c = n.get("content") if isinstance(n, dict) else None
        if isinstance(c, dict):
            url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or "")
            out.append({"title": c.get("title") or "", "url": url,
                        "source": (c.get("provider") or {}).get("displayName") or "Yahoo Finance",
                        "when": _when(c.get("pubDate") or c.get("displayTime"))})
        elif isinstance(n, dict):
            out.append({"title": n.get("title") or "", "url": n.get("link") or "", "source": n.get("publisher") or "",
                        "when": _when(n.get("providerPublishTime"))})
    return [x for x in out if x["title"] and x["url"].startswith("http")]


def _cache_get(key: str, ttl_min: int):
    r = db.one("SELECT fetched_at, items_json FROM news_cache WHERE key=?", (key,))
    if not r:
        return None, False
    fresh = datetime.fromisoformat(r["fetched_at"]) > datetime.now() - timedelta(minutes=ttl_min)
    return json.loads(r["items_json"] or "[]"), fresh


def _cache_put(key: str, items: list[dict]) -> None:
    db.get().execute("INSERT OR REPLACE INTO news_cache(key,fetched_at,items_json) VALUES(?,?,?)",
                     (key, datetime.now().isoformat(timespec="seconds"), json.dumps(items)))


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for x in sorted(items, key=lambda x: x.get("when") or "", reverse=True):
        k = x["title"].lower()[:80]
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def market(limit: int = 12, refresh: bool = True) -> list[dict]:
    cached, fresh = _cache_get("market", MARKET_TTL)
    if cached is not None and (fresh or not refresh):
        return cached[:limit]
    from .sources.http import Http

    http = Http("Mozilla/5.0 OracleEdge", per_second=2, timeout=12)
    items: list[dict] = []
    for name, url in MARKET_FEEDS:
        try:
            r = http.get(url, retries=1)
            if r.status_code == 200:
                items += parse_rss(r.text, name)
        except Exception as e:
            db.log("news", name, False, str(e)[:200])
        if len(items) >= limit:
            break
    if items:
        items = _dedupe(items)
        _cache_put("market", items[:40])
        return items[:limit]
    return (cached or [])[:limit]  # the sites are down: show the last headlines we had


def stock(ticker: str, limit: int = 4, refresh: bool = True) -> list[dict]:
    key = f"t:{ticker}"
    cached, fresh = _cache_get(key, STOCK_TTL)
    if cached is not None and (fresh or not refresh):
        return cached[:limit]
    try:
        import yfinance as yf

        items = _dedupe(parse_yahoo(yf.Ticker(ticker).news))
        _cache_put(key, items[:15])
        return items[:limit]
    except Exception as e:
        db.log("news", ticker, False, str(e)[:200])
        return (cached or [])[:limit]


def watched(run_id: int | None) -> list[tuple[str, str]]:
    """(ticker, why) for your real holdings, the model's holdings and stocks with price alerts this week."""
    out: dict[str, str] = {}
    for r in db.rows("SELECT s.ticker FROM real_holdings h JOIN securities s ON s.cik=h.cik"):
        out.setdefault(r["ticker"], "you hold it")
    if run_id:
        for r in db.rows("""SELECT s.ticker FROM positions p JOIN portfolios f ON f.id=p.portfolio_id
                            JOIN securities s ON s.cik=p.cik WHERE f.run_id=? AND f.kind='model'""", (run_id,)):
            out.setdefault(r["ticker"], "model holding")
    for r in db.rows("""SELECT DISTINCT s.ticker FROM price_alerts a JOIN securities s ON s.cik=a.cik
                        WHERE a.date >= date((SELECT MAX(date) FROM price_alerts), '-7 days')"""):
        out.setdefault(r["ticker"], "price alert")
    return [(t, w) for t, w in out.items() if t]


def for_watched(run_id: int | None, per_stock: int = 3, max_stocks: int = 30, deadline_s: float = 20.0) -> list[dict]:
    """[{ticker, why, items}] newest first. Fetched in parallel; whatever hasn't arrived by the deadline uses the
    cache (or is skipped), so a slow news site never holds the page up."""
    from concurrent.futures import ThreadPoolExecutor, wait

    w = watched(run_id)[:max_stocks]
    out = []
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {pool.submit(stock, t, per_stock): (t, why) for t, why in w}
        done, _ = wait(futs, timeout=deadline_s)
        for f, (t, why) in futs.items():
            if f in done and not f.exception():
                items = f.result()
            else:
                items = stock(t, per_stock, refresh=False) or []
            if items:
                out.append({"ticker": t, "why": why, "items": items})
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    out.sort(key=lambda g: g["items"][0].get("when") or "", reverse=True)
    return out


def ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    except Exception:
        return ""
    m = int(d.total_seconds() // 60)
    if m < 60:
        return f"{max(m, 1)} min ago"
    if m < 48 * 60:
        return f"{m // 60} h ago"
    return f"{m // 1440} days ago"

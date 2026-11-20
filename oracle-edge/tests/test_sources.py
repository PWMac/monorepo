"""Adapters against fake HTTP responses in each provider's real format (no network needed)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from oracle_edge import db
from oracle_edge.sources import fred, prices, sec
from oracle_edge.sources.http import Http


class Resp:
    def __init__(self, status=200, text="", js=None):
        self.status_code, self.text, self._js = status, text, js
        self.request = httpx.Request("GET", "http://x")

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=self.request, response=self)  # type: ignore[arg-type]


def test_stooq_csv(monkeypatch):
    csv = "Date,Open,High,Low,Close,Volume\n2026-11-13,70.1,71.0,69.8,70.5,1234567\n2026-11-14,70.5,71.2,70.0,71.0,1500000\n"
    monkeypatch.setattr(Http, "get", lambda self, url, params=None, retries=3: Resp(text=csv))
    df = prices.Stooq().daily_bars(["KO"], date(2026, 11, 1), date(2026, 11, 15))
    assert list(df["close"]) == [70.5, 71.0] and set(df["ticker"]) == {"KO"}


def test_stooq_blocked_raises(monkeypatch):
    monkeypatch.setattr(Http, "get", lambda self, url, params=None, retries=3: Resp(text="<html>captcha</html>"))
    with pytest.raises(RuntimeError):
        prices.Stooq().daily_bars(["KO"], date(2026, 11, 1), date(2026, 11, 15))


def test_tiingo_json(monkeypatch):
    from oracle_edge import secrets

    secrets.set("tiingo_key", "abc")
    js = [{"date": "2026-11-14T00:00:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
           "adjClose": 1.4}]
    monkeypatch.setattr(Http, "get", lambda self, url, params=None, retries=3: Resp(js=js))
    df = prices.Tiingo().daily_bars(["KO"], date(2026, 11, 1), date(2026, 11, 15))
    assert df.iloc[0]["date"] == "2026-11-14" and df.iloc[0]["adj_close"] == 1.4


def test_fallback_to_next_source(monkeypatch):
    db.get()
    db.set_cfg("price_sources", ["stooq", "tiingo"])

    def fake_get(self, url, params=None, retries=3):
        if "stooq" in url:
            return Resp(text="No data")
        return Resp(js=[{"date": "2026-11-14", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
                         "adjClose": 1.5}])

    monkeypatch.setattr(Http, "get", fake_get)
    res = prices.fetch(["AAA", "BBB"], date(2026, 11, 1), date(2026, 11, 15))
    assert res["missing"] == [] and res["by_source"].get("tiingo") == 2
    assert db.scalar("SELECT source FROM prices WHERE ticker='AAA'") == "tiingo"
    db.set_cfg("price_sources", prices.DEFAULT_ORDER)


def test_fred_csv(monkeypatch):
    csv = "observation_date,GDP\n2025-01-01,29000.5\n2025-04-01,.\n2025-07-01,30000\n"
    monkeypatch.setattr(Http, "get", lambda self, url, params=None, retries=3: Resp(text=csv))
    df = fred.fetch_series("GDP")
    assert list(df["value"]) == [29000.5, 30000.0]


def test_sec_sic_routing(monkeypatch):
    db.get()
    db.set_cfg("sec_user_agent", "Test User test@example.com")
    sec.reset_http()
    db.get().execute("INSERT OR REPLACE INTO securities(cik,ticker,name,exchange) VALUES(777,'BNK','A Bank','NYSE')")
    monkeypatch.setattr(Http, "get", lambda self, url, params=None, retries=3: Resp(
        js={"sic": "6022", "sicDescription": "STATE COMMERCIAL BANKS"}))
    sic, desc = sec.fetch_sic(777)
    sec.set_sic(777, sic, desc)
    assert db.scalar("SELECT category FROM securities WHERE cik=777") == "bank"


def test_sec_requires_identity():
    db.get()
    db.set_cfg("sec_user_agent", "")
    sec.reset_http()
    with pytest.raises(RuntimeError, match="SEC identity"):
        sec.http()

"""ETFs (0.23): N-PORT parsing, matching holdings to companies, look-through scores and the Fund Score."""

import json
from datetime import date, timedelta

import pytest

from oracle_edge import db, etfs, etfscore, rules

NPORT = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport" xmlns:com="http://www.sec.gov/edgar/common">
  <headerData><submissionType>NPORT-P</submissionType></headerData>
  <formData>
    <genInfo><regName>Test Trust</regName><seriesName>Test Quality ETF</seriesName><seriesId>S000000001</seriesId>
      <repPdEnd>2026-12-31</repPdEnd><repPdDate>{asof}</repPdDate></genInfo>
    <fundInfo><totAssets>1000000</totAssets><netAssets>990000</netAssets></fundInfo>
    <invstOrSecs>
      <invstOrSec><name>Alpha Corp</name><lei>X</lei><title>Alpha Corp</title><cusip>000000AA1</cusip>
        <identifiers><isin value="US000000AA11"/><ticker value="ZTA"/></identifiers>
        <balance>100</balance><units>NS</units><curCd>USD</curCd><valUSD>500000</valUSD><pctVal>50.0</pctVal>
        <payoffProfile>Long</payoffProfile><assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>US</invCountry>
      </invstOrSec>
      <invstOrSec><name>Beta Holdings Inc</name><lei>Y</lei><title>BETA HOLDINGS INC COM</title><cusip>000000BB2</cusip>
        <identifiers><isin value="US000000BB22"/></identifiers>
        <balance>10</balance><units>NS</units><curCd>USD</curCd><valUSD>300000</valUSD><pctVal>30.0</pctVal>
        <payoffProfile>Long</payoffProfile><assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>US</invCountry>
      </invstOrSec>
      <invstOrSec><name>Foreign Thing PLC</name><lei>Z</lei><title>Foreign Thing</title><cusip>N/A</cusip>
        <identifiers><isin value="GB0000000001"/></identifiers>
        <balance>10</balance><units>NS</units><curCd>GBP</curCd><valUSD>200000</valUSD><pctVal>20.0</pctVal>
        <payoffProfile>Long</payoffProfile><assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>GB</invCountry>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>"""


def _prices(t, start, days, p0, step=0.0):
    d = date.fromisoformat(start)
    p = p0
    con = db.get()
    for _ in range(days):
        if d.weekday() < 5:
            con.execute("INSERT OR REPLACE INTO prices(ticker,date,close,adj_close,volume,source) VALUES(?,?,?,?,?,?)",
                        (t, d.isoformat(), p, p, 1e6, "test"))
            p += step
        d += timedelta(days=1)


@pytest.fixture
def world():
    rules.seed_profiles()
    p = rules.default_profile()
    con = db.get()
    for t in ("etf_holdings", "etfs", "etf_scores", "cusip_map", "etf_aum"):
        con.execute(f"DELETE FROM {t}")
    for cik, t, nm in ((991001, "ZTA", "Alpha Corp"), (991002, "ZTB", "Beta Holdings, Inc.")):
        con.execute("INSERT OR REPLACE INTO securities(cik,ticker,name,sic) VALUES(?,?,?,?)", (cik, t, nm, 3571))
    q = {991001: (85, 1, 82), 991002: (60, 0, 40)}
    for cik, (qq, g, w) in q.items():
        con.execute("DELETE FROM snapshots WHERE cik=?", (cik,))
        con.execute("INSERT INTO snapshots(cik,profile_id,profile_version,filed,quality,gates_passed) VALUES(?,?,?,?,?,?)",
                    (cik, *p.snap, "2026-03-01", qq, g))
        con.execute("DELETE FROM wonder WHERE cik=?", (cik,))
        con.execute("INSERT INTO wonder(cik,profile_id,cohort,score) VALUES(?,?,?,?)", (cik, p.snap[0], "now", w))
    con.execute("INSERT OR REPLACE INTO vd_values(cik,date,price,iv,er,buy_below) VALUES(991001,'2026-11-20',100,150,0.09,"
                "110)")
    con.execute("INSERT OR REPLACE INTO vd_values(cik,date,price,iv,er,buy_below) VALUES(991002,'2026-11-20',50,40,0.03,30)")
    start = (date.today() - timedelta(days=400)).isoformat()
    _prices("SPY", start, 400, 400, 0.1)
    _prices("ZTA", start, 400, 80, 0.05)  # rises: its weight drifts up
    _prices("ZTB", start, 400, 50, 0.0)
    _prices("ZQE", start, 400, 20, 0.01)
    yield (date.today() - timedelta(days=100)).isoformat()
    for t in ("etf_holdings", "etfs", "etf_scores", "cusip_map", "etf_aum"):
        con.execute(f"DELETE FROM {t}")
    for cik in (991001, 991002):
        for t in ("snapshots", "wonder", "vd_values", "securities"):
            con.execute(f"DELETE FROM {t} WHERE cik=?", (cik,))
    con.execute("DELETE FROM prices WHERE source='test' AND ticker IN ('SPY','ZTA','ZTB','ZQE')")


def test_parse_nport_series_map_and_atom():
    doc = etfs.parse_nport(NPORT.format(asof="2026-09-30"))
    assert doc["series_id"] == "S000000001" and doc["net_assets"] == 990000 and doc["rep_date"] == "2026-09-30"
    assert [(h["ticker"], h["cusip"], h["weight"]) for h in doc["holdings"]] == [
        ("ZTA", "000000AA1", 50.0), (None, "000000BB2", 30.0), (None, None, 20.0)]
    sm = etfs.parse_series_map({"fields": ["cik", "seriesId", "classId", "symbol"],
                                "data": [[884394, "S000004310", "C000012345", "spy"]]})
    assert sm["SPY"] == {"cik": 884394, "series": "S000004310", "class": "C000012345"}
    atom = ("<feed><entry><content><accession-number>0001752724-26-123456</accession-number>"
            "<filing-date>2026-11-25</filing-date><filing-type>NPORT-P</filing-type></content></entry>"
            "<entry><content><accession-number>0001752724-26-000001</accession-number><filing-date>2026-08-25"
            "</filing-date><filing-type>NPORT-P/A</filing-type></content></entry></feed>")
    assert etfs.parse_browse_atom(atom) == [{"acc": "0001752724-26-123456", "date": "2026-11-25"}]


def test_facts_units():
    f = etfs.parse_facts({"longName": "Test", "netExpenseRatio": 0.03, "yield": 0.0125, "totalAssets": 5e9,
                          "category": "Large Blend", "fundInceptionDate": 1262304000})
    assert f["expense"] == pytest.approx(0.0003) and f["yield"] == 0.0125 and f["inception"] == "2010-01-01"
    assert etfs.benchmark_for("Foreign Large Blend") == "VXUS" and etfs.benchmark_for("Intermediate Core Bond") == "AGG"
    assert etfs.benchmark_for("Large Blend", "SPY") == "VOO"


def test_matching_look_through_and_score(world):
    asof = world
    doc = etfs.parse_nport(NPORT.format(asof=asof))
    etfs.store_holdings("ZQE", doc, "acc-1", "SEC N-PORT")
    by = {r["name"]: (r["cik"], r["matched_by"]) for r in db.rows("SELECT * FROM etf_holdings WHERE etf='ZQE'")}
    assert by["Alpha Corp"] == (991001, "ticker") and by["Beta Holdings Inc"] == (991002, "name")
    assert by["Foreign Thing PLC"] == (None, None)
    assert db.scalar("SELECT cik FROM cusip_map WHERE cusip='000000AA1'") == 991001  # learned for other funds

    lt = etfscore.look_through("ZQE")
    m = lt["m"]
    a = next(r for r in lt["rows"] if r["name"] == "Alpha Corp")
    assert a["w"] > 0.5  # Alpha rose since the filing, so its weight did too
    assert m["coverage"] == pytest.approx(a["w"] + next(r["w"] for r in lt["rows"] if r["name"].startswith("Beta")))
    assert m["wonderful"] == pytest.approx(a["w"]) and m["quality"] == pytest.approx(a["w"])
    assert m["oracle"] == pytest.approx(a["w"])  # Alpha: wonderful, quality, below its value
    assert m["fails"] > 0 and m["price_vs_value"] is not None

    db.get().execute("INSERT OR REPLACE INTO etfs(ticker, expense, aum, inception, nav, benchmark) VALUES('ZQE', 0.001,"
                     " 2e9, '2015-01-01', NULL, 'SPY')")
    r = etfscore.score_one("ZQE", etfscore.Book())
    assert 0 <= r["score"] <= 100 and set(r["pillars"]) == {"owns", "price", "track", "cost"}
    s = etfscore.scores()["ZQE"]
    assert s["m"]["verdict"] in ("BUY", "HOLD", "WATCH", "TRIM") and s["m"]["fair_value"] > 0
    assert json.loads(db.scalar("SELECT pillars_json FROM etf_scores WHERE etf='ZQE'"))["parts"]["cost"]


def test_ramp_and_shrink():
    assert etfscore.ramp(0.0005, 0.0075, 0.0005) == 100 and etfscore.ramp(0.01, 0.0075, 0.0005) == 0
    assert etfscore._shrink(90, 0.5) == 70
    f = etfscore.fund_score({"expense": 0.0003, "aum": 1e11, "inception": "2001-01-01"}, {}, {}, {})
    assert f["pillars"]["owns"] is None and f["score"] == pytest.approx(f["pillars"]["cost"])


def test_xray_and_etf_buy_candidates(world):
    from oracle_edge import orderplan

    etfs.store_holdings("ZQE", etfs.parse_nport(NPORT.format(asof=world)), "acc-1", "SEC N-PORT")
    xr = etfscore.xray([("ZQE", 1000.0), ("ZTA", 500.0)])
    alpha = next(e for e in xr["rows"] if e.get("cik") == 991001)
    assert alpha["direct"] == 500 and alpha["via"] > 500 and alpha["funds"] == {"ZQE": alpha["via"]}
    assert xr["total"] == 1500 and 0 < xr["m"]["oracle"] <= 1
    assert sum(e["w"] for e in xr["rows"]) == pytest.approx(1.0)
    db.get().execute("INSERT OR REPLACE INTO etfs(ticker, expense, aum, inception, benchmark) VALUES('ZQE', 0.0005, "
                     "5e10, '2005-01-01', 'SPY')")
    etfscore.score_one("ZQE", etfscore.Book())
    db.get().execute("UPDATE etf_scores SET score=75, metrics_json=json_set(metrics_json, '$.verdict', 'BUY') "
                     "WHERE etf='ZQE'")
    c = orderplan.etf_candidates()
    assert [x["ticker"] for x in c] == ["ZQE"] and c[0]["etf"] and "Fund Score 75" in c[0]["why"]

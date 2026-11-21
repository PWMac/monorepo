"""Cost of sales reported in pieces (no total): filled from Yahoo when it matches the SEC's sales."""

from __future__ import annotations

import pandas as pd
import pytest

from oracle_edge import db, grossfill, rules
from oracle_edge.rules import confidence


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _facts(cik, rows):
    with db.tx() as con:
        con.execute("DELETE FROM facts WHERE cik=?", (cik,))
        con.executemany("INSERT INTO facts(cik,item,fy_end,value,filed,form,unit) VALUES(?,?,?,?,?,?,?)",
                        [(cik, it, fy, v, f, "10-K", "USD") for it, fy, v, f in rows])


def test_kinds_tell_a_gap_from_a_business_without_cost_of_sales():
    _facts(990030, [("revenue", "2014-12-31", 100, "2015-02-20"), ("cogs", "2014-12-31", 20, "2015-02-20"),
                    ("revenue", "2024-12-31", 300, "2025-02-20")])
    _facts(990031, [("revenue", "2024-12-31", 300, "2025-02-20")])
    _facts(990032, [("revenue", "2024-12-31", 300, "2025-02-20"), ("gross_profit", "2024-12-31", 200, "2025-02-20")])
    k = grossfill.kinds()
    assert (k[990030], k[990031], k[990032]) == ("gap", "none", "ok")
    assert grossfill.kind(990030) == "gap"


def test_yahoo_cost_of_revenue_is_parsed_matched_and_dated_with_the_filing():
    df = pd.DataFrame({pd.Timestamp("2024-12-31"): [303.0, 60.0, 243.0], pd.Timestamp("2023-12-31"): [250.0, 55.0, 195.0],
                       pd.Timestamp("2022-12-31"): [210.0, None, 160.0]},
                      index=["Total Revenue", "Cost Of Revenue", "Gross Profit"])
    y = grossfill.parse_income(df)
    assert y["2024-12-31"] == {"revenue": 303.0, "cogs": 60.0} and y["2022-12-31"]["cogs"] == 50.0  # from gross profit
    _facts(990033, [("revenue", "2014-12-31", 100, "2015-02-20"), ("cogs", "2014-12-31", 20, "2015-02-20"),
                    ("revenue", "2024-12-31", 300, "2025-02-20"), ("revenue", "2025-01-01", 0, "2025-02-20"),
                    ("revenue", "2023-12-30", 200, "2024-02-20")])
    added = grossfill.apply(990033, y)
    assert added == 1  # 2024 matches (within 3%); 2023's sales disagree (250 vs 200) so its cost isn't trusted
    r = db.one("SELECT value, filed, form FROM facts WHERE cik=990033 AND item='cogs' AND fy_end='2024-12-31'")
    assert r["value"] == 60.0 and r["filed"] == "2025-02-20" and r["form"] == "YF"
    assert grossfill.apply(990033, y) == 0  # already filled
    with db.tx() as con:
        for c in (990030, 990031, 990032, 990033):
            con.execute("DELETE FROM facts WHERE cik=?", (c,))


def test_no_confidence_penalty_for_a_business_without_cost_of_sales():
    base = {"years": ["2023", "2024"], "revenue": [1, 1], "net_income": [1, 1], "equity": [1, 1], "ocf": [1, 1],
            "capex": [1, 1], "shares_diluted": [1, 1]}
    none = {**base, "gross_profit": [None, None], "cogs": [None, None]}
    gap = {**base, "gross_profit": [0.5, None], "cogs": [0.5, None]}
    assert confidence(none, [], 10, 10) == 1.0
    assert confidence(gap, [], 10, 10) == pytest.approx(0.97)


def test_operating_net_and_cash_flow_margins_are_measured_for_every_company():
    from oracle_edge import wonderful

    rj = [{"id": "r23", "value": 0.02, "series": [0.50, 0.55, None, 0.60]},
          {"id": "r7", "value": 0.4, "series": [0.40, 0.42, 0.44]}]
    rev = {"2022-12-31": 100.0, "2023-12-31": 120.0, "2024-12-31": 150.0}
    ocf = {"2022-12-31": 50.0, "2023-12-31": 54.0, "2024-12-31": 60.0}
    f = wonderful.features(rj, {}, "2024-12-31", rev, ocf)
    assert f["om_last"] == 0.60 and f["om_med"] == pytest.approx(0.55)
    assert f["nm_last"] == 0.44 and f["nm_med"] == pytest.approx(0.42)
    assert f["ocfm_last"] == pytest.approx(0.40) and f["ocfm_med"] == pytest.approx(0.45)
    # a margin is never scored: Wonderfulness weights only the pillar measures
    assert not {"om_med", "nm_med", "ocfm_med"} & {k for items in wonderful.PILLARS.values() for k, _ in items}
    g = wonderful.features(rj, {}, "2025-12-31", rev, ocf)
    assert "ocfm_last" not in g  # no cash flow for that year: no latest figure (the median still stands)
    assert g["ocfm_med"] == pytest.approx(0.45)


def test_margin_filters():
    from oracle_edge.ui import wonder_page as wp

    r = {"om_med": 0.30, "nm_med": 0.20, "ocfm_med": 0.25, "gm_med": None}
    assert wp.passes(r, {"om_med": {"min": 25}, "ocfm_med": {"min": 20, "max": 30}})
    assert not wp.passes(r, {"nm_med": {"min": 25}})
    assert not wp.passes(r, {"gm_med": {"min": 10}})  # no gross margin: a gross-margin filter excludes it

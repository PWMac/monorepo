"""Filling data gaps: derived line items, the $1 test for loss makers, Yahoo FX fallback, known ADR ratios."""

from __future__ import annotations

from oracle_edge import db
from oracle_edge.metrics import build_statement, compute

YEARS = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31"]


def _state(items: dict[str, list]) -> dict:
    return {(k, y): v for k, vals in items.items() for y, v in zip(YEARS, vals) if v is not None}


def test_sga_from_separate_selling_and_admin_lines():
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [10] * 4, "selling_marketing": [8] * 4,
                                "gna": [5] * 4}), 10)
    assert s["sga"] == [13.0] * 4


def test_da_from_separate_depreciation_and_amortization():
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [10] * 4, "depreciation": [-6] * 4,
                                "amortization": [2] * 4}), 10)
    assert s["da"] == [8.0] * 4


def test_operating_income_derived_when_not_reported():
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [10] * 4, "costs_and_expenses": [80] * 4}), 10)
    assert s["operating_income"] == [20] * 4
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [10] * 4, "pretax_income": [14] * 4,
                                "interest": [-3] * 4}), 10)
    assert s["operating_income"] == [17] * 4  # pre-tax income + interest = EBIT
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [10] * 4, "operating_income": [25] * 4,
                                "pretax_income": [14] * 4}), 10)
    assert s["operating_income"] == [25] * 4  # a reported line always wins


def test_dollar_test_for_a_company_that_lost_money():
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [-5] * 4, "shares_diluted": [10] * 4,
                                "equity": [50] * 4}), 10)
    m = compute(s, fy_prices=dict(zip(YEARS, [20.0, 18.0, 15.0, 12.0])))
    assert m["dollar_test"].value == 0.0  # fails instead of showing as missing
    s = build_statement(_state({"revenue": [100] * 4, "net_income": [10] * 4, "dividends_paid": [12] * 4,
                                "shares_diluted": [10] * 4, "equity": [50] * 4}), 10)
    m = compute(s, fy_prices=dict(zip(YEARS, [20.0, 21.0, 23.0, 25.0])))
    assert m["dollar_test"].value is None and "paid out" in m["dollar_test"].detail


def test_fx_falls_back_to_yahoo_series():
    from oracle_edge.market import Macro

    with db.tx() as con:
        con.execute("DELETE FROM macro WHERE series='YFX_CLP'")
        con.execute("INSERT INTO macro(series,date,value) VALUES('YFX_CLP','2026-11-19',950.0)")
    assert abs(Macro().fx("CLP", "2026-11-20") - 1 / 950.0) < 1e-12


def test_known_adr_ratios_fill_unknown_but_never_replace_manual():
    from oracle_edge import pipeline

    con = db.get()
    con.execute("DELETE FROM securities WHERE ticker IN ('OMAB','AMX')")
    con.execute("INSERT INTO securities(cik,ticker,name,is_foreign,currency) VALUES(9001,'OMAB','OMA',1,'MXN')")
    con.execute("INSERT INTO securities(cik,ticker,name,is_foreign,currency,adr_ratio,adr_source) "
                "VALUES(9002,'AMX','AMX',1,'MXN',15,'manual: set by me')")
    pipeline.apply_known_adr()
    assert db.scalar("SELECT adr_ratio FROM securities WHERE cik=9001") == 8.0
    assert db.scalar("SELECT adr_ratio FROM securities WHERE cik=9002") == 15  # manual stays


def test_gap_list_skips_not_applicable_checks():
    from oracle_edge.ui.data import _real_gaps

    miss = ["Gross margin", "SG&A margin", "R&D margin", "Depreciation margin", 'The "$1 test"', "Tax margin"]
    assert _real_gaps(miss, priced=False) == ["Tax margin"]
    assert _real_gaps(["SG&A margin", 'The "$1 test"'], priced=True) == ["SG&A margin", 'The "$1 test"']

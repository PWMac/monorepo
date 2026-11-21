"""Wonderfulness: measures, percentile scoring, strengths and red flags, company profile parsing."""

import pytest

from oracle_edge import db, rules, wonderful
from oracle_edge.sources import company


@pytest.fixture(scope="module", autouse=True)
def _db():
    db.get()
    rules.seed_profiles()


def _rj(roic=0.25, gm=0.5, debt=1.0):
    return [{"id": "r20", "value": roic, "consistency": 0.9, "series": [roic - 0.02, roic, roic + 0.01, roic]},
            {"id": "r19", "value": roic, "series": [roic]}, {"id": "r1", "value": gm, "consistency": 1.0},
            {"id": "r10", "value": debt}, {"id": "r5", "value": 0.05, "consistency": 1.0},
            {"id": "r22", "value": 0.9}, {"id": "r21", "value": 1.5}, {"id": "r14", "value": -0.01},
            {"id": "r9", "value": 1.0}]


def test_features_growth_and_rule_values():
    rev = {f"{2015 + i}-12-31": 100.0 * 1.1 ** i for i in range(11)}
    rev["2019-12-31"] = rev["2018-12-31"] * 0.95  # one down year
    f = wonderful.features(_rj(), {"incr_roic": 0.3}, "2025-12-31", rev)
    assert f["roic_med"] == 0.25 and f["roic_last"] == 0.25 and f["roic_min"] == pytest.approx(0.23)
    assert f["roic_incr"] == 0.3 and f["gm_med"] == 0.5
    assert f["rev_g1"] == pytest.approx(0.10)
    assert f["rev_g10"] == pytest.approx(0.10)
    assert f["rev_up"] == 9 and f["rev_years"] == 10


def test_scores_are_percentiles_and_rank_in_order():
    feats = {}
    for i in range(40):
        feats[i] = {"roic_last": i / 100, "roic_med": i / 100, "roic_years": i / 40, "roic_incr": i / 100,
                    "gm_med": 0.2 + i / 100, "rev_g5": i / 200,
                    "rev_up_share": 0.5 + i / 100, "debt_payback": 5 - i / 10, "interest_burden": 0.2 - i / 400,
                    "profitable_years": 1.0, "dollar_test": i / 10}
    res = wonderful.score_cross_section(feats)
    assert res[39]["rank"] == 1 and res[0]["rank"] == 40
    # growth and allocation have too few measures here, so they count as 50 for everyone
    assert res[39]["score"] > 70 and res[0]["score"] < 35 and res[39]["feats"]["missing"] == ["growth", "allocation"]
    # lower debt payback is better: the company with the least debt has the highest strength percentile
    assert res[39]["pct"]["debt_payback"] > res[0]["pct"]["debt_payback"]


def test_a_missing_pillar_counts_as_average_and_never_lifts_a_company():
    base = {i: {"roic_last": i / 100, "roic_med": i / 100, "roic_years": i / 30, "roic_incr": i / 100,
                "rev_g5": i / 100, "rev_up_share": i / 100, "predictability": i / 30,
                "debt_payback": 5 - i / 10, "interest_burden": 0.2 - i / 300, "profitable_years": i / 30,
                "dollar_test": i / 10} for i in range(30)}
    no_growth = dict(base)
    no_growth[29] = {k: v for k, v in base[29].items() if k not in ("rev_g5", "rev_up_share")}
    a, b = wonderful.score_cross_section(base)[29], wonderful.score_cross_section(no_growth)[29]
    assert b["score"] < a["score"] and "growth" in b["feats"]["missing"]


def test_strengths_and_red_flags_say_what_would_need_to_change():
    w = {"f": {"roic_med": 0.06, "debt_payback": 7.0, "gm_med": 0.62, "dollar_test": 2.5, "share_change": 0.03},
         "pct": {"roic_med": 0.2, "debt_payback": 0.1, "gm_med": 0.95, "dollar_test": 0.9, "share_change": 0.1}}
    strengths, flags = wonderful.explain(w)
    assert {s["key"] for s in strengths} == {"gm_med", "dollar_test"}
    keys = [f["key"] for f in flags]
    assert set(keys) == {"roic_med", "debt_payback", "share_change"}
    assert all(f["change"] for f in flags) and "15%" in next(f for f in flags if f["key"] == "roic_med")["change"]


def test_tiers():
    assert wonderful.tier(85) == "Exceptional" and wonderful.tier(72) == "Wonderful" and wonderful.tier(30) == "Weak"


def test_company_profile_parse_keeps_leaders_and_governance():
    info = {"longBusinessSummary": "Makes things.", "companyOfficers": [
        {"name": "Ann Lee", "title": "CEO", "age": 55, "totalPay": 3_000_000}, {"title": "no name"}],
        "heldPercentInsiders": 0.12, "overallRisk": 2, "boardRisk": 4, "auditRisk": "n/a"}
    x = company.parse(info, [{"label": "Net Shares Purchased (Sold)", "shares": 1000.0, "trades": 3}])
    assert [o["name"] for o in x["officers"]] == ["Ann Lee"] and x["officers"][0]["pay"] == 3_000_000
    assert x["governance"] == {"Board": 4, "Overall": 2} and x["insiders"] == 0.12
    assert x["insider_activity"][0]["trades"] == 3

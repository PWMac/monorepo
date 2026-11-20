"""Synthetic market in the exact SEC companyfacts / ticker-map formats, for offline tests and demos.

Archetypes: wonderful (cheap or pricey), average, cyclical, levered, bank, foreign (IFRS/EUR), plus noise.
Each company files a 10-K ~50 days after a Dec-31 year end, with 3 years of income/cash-flow
and 2 years of balance-sheet comparatives (like real filings).
"""

from __future__ import annotations

import json
import math
import random
import zipfile
from datetime import date, timedelta

YEARS = list(range(2008, 2026))


def _arche(kind, rng):
    if kind == "wonderful":
        return dict(gm=rng.uniform(.5, .7), sga=rng.uniform(.15, .28), rnd=rng.uniform(0, .12), da=rng.uniform(.02, .06),
                    nm=rng.uniform(.21, .30), g=rng.uniform(.06, .11), roe=rng.uniform(.22, .35), debt_years=rng.uniform(.5, 2.5),
                    capex=rng.uniform(.1, .3), vol=.02, losses=0)
    if kind == "average":
        return dict(gm=rng.uniform(.25, .38), sga=rng.uniform(.45, .65), rnd=rng.uniform(0, .05), da=rng.uniform(.08, .15),
                    nm=rng.uniform(.05, .10), g=rng.uniform(.01, .05), roe=rng.uniform(.08, .13), debt_years=rng.uniform(2, 5),
                    capex=rng.uniform(.5, .9), vol=.08, losses=0)
    if kind == "cyclical":
        return dict(gm=rng.uniform(.2, .35), sga=rng.uniform(.3, .5), rnd=0, da=rng.uniform(.1, .2), nm=rng.uniform(.04, .12),
                    g=rng.uniform(0, .04), roe=rng.uniform(.06, .15), debt_years=rng.uniform(2, 6), capex=rng.uniform(.8, 1.4),
                    vol=.35, losses=2)
    if kind == "levered":
        return dict(gm=rng.uniform(.4, .6), sga=rng.uniform(.25, .35), rnd=0, da=rng.uniform(.05, .1), nm=rng.uniform(.12, .2),
                    g=rng.uniform(.03, .07), roe=rng.uniform(.25, .4), debt_years=rng.uniform(7, 12), capex=rng.uniform(.3, .6),
                    vol=.05, losses=0)
    raise ValueError(kind)


def make_company(cik, ticker, kind, rng, pe_band):
    a = _arche(kind if kind in ("wonderful", "average", "cyclical", "levered") else "wonderful", rng)
    if kind in ("decliner", "bubble", "bank", "insurer", "foreign", "qwarn"):
        a.update(nm=0.25, roe=0.28, debt_years=1.0, vol=0.01, g=0.07)
    rev0 = rng.uniform(5e8, 2e10)
    shares0 = rng.uniform(5e7, 1e9)
    buyback = rng.uniform(.005, .025) if kind == "wonderful" else rng.uniform(-.02, .005)
    rows = {}
    loss_years = set(rng.sample(YEARS[2:], a["losses"])) if a["losses"] else set()
    eq = rev0 * a["nm"] / a["roe"]
    re_ = eq * 0.8
    treasury = 0.0
    for i, y in enumerate(YEARS):
        rev = rev0 * (1 + a["g"]) ** i * (1 + rng.gauss(0, a["vol"] * 0.3))
        gp = rev * a["gm"]
        sga = gp * a["sga"]
        rnd = gp * a["rnd"]
        da = gp * a["da"]
        ni = rev * a["nm"] * (1 + rng.gauss(0, a["vol"]))
        if kind == "decliner" and y >= 2021:
            ni = rev * max(a["nm"] - 0.12 * (y - 2020), -0.05)
            a["debt_years"] = 3 + 3 * (y - 2020)
        if y in loss_years:
            ni = -abs(ni) * 0.5
        opinc = ni / 0.79 + rev * 0.01
        interest = max(opinc, 1) * (0.04 if a["debt_years"] < 3 else 0.2)
        pretax = opinc - interest
        tax = pretax * 0.21 if pretax > 0 else 0
        shares = shares0 * (1 - buyback) ** i
        capex = abs(ni) * a["capex"]
        ocf = ni + da * 0.9 + rng.gauss(0, abs(ni) * 0.03)
        div = max(ni, 0) * 0.35
        bb = max(ni, 0) * 0.3 if buyback > 0 else 0
        treasury += bb
        re_ += ni - div
        eq = max(eq * (1 + a["g"] * 0.5) + ni - div - bb, rev * 0.05)
        ltd = max(ni, rev * 0.02) * a["debt_years"]
        cash = rev * (0.15 if kind == "wonderful" else 0.05)
        assets = eq + ltd + rev * 0.25
        rows[y] = dict(Revenues=rev, CostOfRevenue=rev - gp, GrossProfit=gp, SellingGeneralAndAdministrativeExpense=sga,
                       ResearchAndDevelopmentExpense=rnd, DepreciationDepletionAndAmortization=da, InterestExpense=interest,
                       OperatingIncomeLoss=opinc,
                       IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest=pretax,
                       IncomeTaxExpenseBenefit=tax, NetIncomeLoss=ni, EarningsPerShareDiluted=ni / shares,
                       WeightedAverageNumberOfDilutedSharesOutstanding=shares,
                       NetCashProvidedByUsedInOperatingActivities=ocf, PaymentsToAcquirePropertyPlantAndEquipment=capex,
                       PaymentsOfDividends=div, PaymentsForRepurchaseOfCommonStock=bb,
                       CashAndCashEquivalentsAtCarryingValue=cash, LongTermDebtNoncurrent=ltd, Assets=assets,
                       Liabilities=assets - eq, StockholdersEquity=eq, TreasuryStockValue=treasury,
                       RetainedEarningsAccumulatedDeficit=re_)
        if kind == "bank":
            assets = rev * 12
            ni = assets * 0.012 * (1 + rng.gauss(0, 0.03))
            eq = assets * 0.10
            loans = assets * 0.6
            rows[y].update(Revenues=rev, NetIncomeLoss=ni, EarningsPerShareDiluted=ni / shares, Assets=assets,
                           StockholdersEquity=eq, Liabilities=assets - eq, Deposits=assets * 0.75,
                           LoansAndLeasesReceivableNetReportedAmount=loans, InterestIncomeExpenseNet=rev * 0.7,
                           NoninterestIncome=rev * 0.3, NoninterestExpense=rev * 0.55,
                           FinancingReceivableAllowanceForCreditLossesWriteOffsNet=loans * (0.008 if y == 2020 else 0.003),
                           LongTermDebtNoncurrent=assets * 0.05, Goodwill=eq * 0.1)
            for k in ("GrossProfit", "CostOfRevenue"):
                rows[y].pop(k, None)
        if kind == "insurer":
            prem = rev * 0.85
            ni = rev * 0.11 * (1 + rng.gauss(0, 0.05))
            eq = prem * 0.7 * (1.07 ** i) / (1.07 ** 6)
            rows[y].update(PremiumsEarnedNet=prem, PolicyholderBenefitsAndClaimsIncurredNet=prem * 0.62,
                           DeferredPolicyAcquisitionCostAmortizationExpense=prem * 0.17,
                           OtherUnderwritingExpense=prem * 0.12 if y != 2017 else prem * 0.25,
                           NetInvestmentIncome=rev * 0.12, NetIncomeLoss=ni, EarningsPerShareDiluted=ni / shares,
                           StockholdersEquity=eq, LiabilityForClaimsAndClaimsAdjustmentExpense=prem * 1.4 * 1.06 ** i,
                           UnearnedPremiums=prem * 0.45,
                           LiabilityForUnpaidClaimsAndClaimsAdjustmentExpenseIncurredClaimsPriorYears=-eq * 0.01)
            for k in ("GrossProfit", "CostOfRevenue"):
                rows[y].pop(k, None)
    out = {"rows": rows, "pe_band": pe_band, "shares0": shares0, "buyback": buyback}
    if kind == "qwarn":
        r25 = rows[2025]
        out["q_override"] = {(2025, 6): {"NetIncomeLoss": -abs(r25["NetIncomeLoss"]) * 1.2},
                             (2025, 9): {"NetIncomeLoss": -abs(r25["NetIncomeLoss"]) * 1.4,
                                         "shares": r25["WeightedAverageNumberOfDilutedSharesOutstanding"] * 1.12}}
    return out


DURATION = {"InterestIncomeExpenseNet", "NoninterestIncome", "NoninterestExpense",
            "FinancingReceivableAllowanceForCreditLossesWriteOffsNet", "PremiumsEarnedNet",
            "PolicyholderBenefitsAndClaimsIncurredNet", "DeferredPolicyAcquisitionCostAmortizationExpense",
            "OtherUnderwritingExpense", "NetInvestmentIncome",
            "LiabilityForUnpaidClaimsAndClaimsAdjustmentExpenseIncurredClaimsPriorYears", "Revenues", "CostOfRevenue", "GrossProfit", "SellingGeneralAndAdministrativeExpense",
            "ResearchAndDevelopmentExpense", "DepreciationDepletionAndAmortization", "InterestExpense",
            "OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeTaxExpenseBenefit", "NetIncomeLoss", "EarningsPerShareDiluted",
            "WeightedAverageNumberOfDilutedSharesOutstanding", "NetCashProvidedByUsedInOperatingActivities",
            "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsOfDividends", "PaymentsForRepurchaseOfCommonStock"}
IFRS = {"Revenues": "Revenue", "CostOfRevenue": "CostOfSales", "GrossProfit": "GrossProfit", "NetIncomeLoss": "ProfitLoss",
        "EarningsPerShareDiluted": "DilutedEarningsLossPerShare", "StockholdersEquity": "Equity", "Assets": "Assets",
        "Liabilities": "Liabilities", "NetCashProvidedByUsedInOperatingActivities": "CashFlowsFromUsedInOperatingActivities",
        "OperatingIncomeLoss": "ProfitLossFromOperatingActivities", "CashAndCashEquivalentsAtCarryingValue":
        "CashAndCashEquivalents", "PaymentsToAcquirePropertyPlantAndEquipment":
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "RetainedEarningsAccumulatedDeficit":
        "RetainedEarnings", "DepreciationDepletionAndAmortization": "AdjustmentsForDepreciationAndAmortisationExpense",
        "PaymentsOfDividends": "DividendsPaidClassifiedAsFinancingActivities", "LongTermDebtNoncurrent":
        "NoncurrentPortionOfNoncurrentBorrowings",
        "WeightedAverageNumberOfDilutedSharesOutstanding": "WeightedAverageShares"}


QUARTER_ITEMS = ["Revenues", "NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities",
                 "PaymentsToAcquirePropertyPlantAndEquipment", "OperatingIncomeLoss"]
QUARTER_INSTANTS = ["StockholdersEquity", "LongTermDebtNoncurrent", "Assets"]


def _add_quarters(facts, cik, y, comp):
    """10-Qs for Q1-Q3 of fiscal year y+1 (filed ~40 days after quarter end), with prior-year comparatives."""
    rows = comp["rows"]
    if y + 1 not in rows:
        return
    qends = [("03-31", 3, 90), ("06-30", 6, 181), ("09-30", 9, 273)]
    for mmdd, months, ytd_days in qends:
        cur_y = y + 1
        filed = (date(cur_y, int(mmdd[:2]), int(mmdd[3:])) + timedelta(days=40 + cik % 5)).isoformat()
        q_override = comp.get("q_override", {}).get((cur_y, months))
        for yy in (cur_y, cur_y - 1):
            if yy not in rows:
                continue
            frac = months / 12
            for c in QUARTER_ITEMS + ["EarningsPerShareDiluted"]:
                if c not in rows[yy]:
                    continue
                v = rows[yy][c] * frac
                if q_override and yy == cur_y and c in q_override:
                    v = q_override[c]
                e = {"start": f"{yy}-01-01", "end": f"{yy}-{mmdd}", "val": round(v, 4), "fy": cur_y,
                     "fp": f"Q{months // 3}", "form": "10-Q", "filed": filed}
                unit = "USD/shares" if "PerShare" in c else "USD"
                facts["us-gaap"].setdefault(c, {"units": {}})["units"].setdefault(unit, []).append(e)
            sh = rows[yy]["WeightedAverageNumberOfDilutedSharesOutstanding"]
            if q_override and yy == cur_y and "shares" in q_override:
                sh = q_override["shares"]
            qs = f"{yy}-{int(mmdd[:2]) - 2:02d}-01"
            facts["us-gaap"].setdefault("WeightedAverageNumberOfDilutedSharesOutstanding", {"units": {}})["units"] \
                .setdefault("shares", []).append({"start": qs, "end": f"{yy}-{mmdd}", "val": round(sh), "fy": cur_y,
                                                  "fp": "Q", "form": "10-Q", "filed": filed})
        for c in QUARTER_INSTANTS:
            if c in rows[cur_y]:
                facts["us-gaap"].setdefault(c, {"units": {}})["units"].setdefault("USD", []).append(
                    {"end": f"{cur_y}-{mmdd}", "val": round(rows[cur_y][c]), "fy": cur_y, "fp": "Q", "form": "10-Q",
                     "filed": filed})


def companyfacts_doc(cik, name, comp, foreign=False):
    facts: dict = {"us-gaap": {}, "dei": {}}
    if foreign:
        facts = {"ifrs-full": {}, "dei": {}}
    for y in YEARS:
        filed = (date(y + 1, 2, 20) + timedelta(days=cik % 9)).isoformat()
        form = "20-F" if foreign else "10-K"
        for back in range(3):
            yy = y - back
            if yy not in comp["rows"]:
                continue
            for concept, val in comp["rows"][yy].items():
                is_dur = concept in DURATION
                if not is_dur and back > 1:
                    continue
                tax, cname = ("ifrs-full", IFRS.get(concept)) if foreign else ("us-gaap", concept)
                if not cname:
                    continue
                unit = "USD/shares" if "PerShare" in concept else ("shares" if "Shares" in concept else "USD")
                if foreign:
                    unit = "EUR/shares" if "PerShare" in concept else ("shares" if "Shares" in concept else "EUR")
                e = {"end": f"{yy}-12-31", "val": round(val, 4 if "PerShare" in concept else 0), "accn": f"0000{cik}-{y}",
                     "fy": y, "fp": "FY", "form": form, "filed": filed}
                if is_dur:
                    e["start"] = f"{yy}-01-01"
                node = facts[tax].setdefault(cname, {"label": cname, "units": {}})
                node["units"].setdefault(unit, []).append(e)
        if not foreign and y >= 2016:
            _add_quarters(facts, cik, y, comp)
        sh = comp["shares0"] * (1 - comp["buyback"]) ** (y - YEARS[0])
        facts["dei"].setdefault("EntityCommonStockSharesOutstanding", {"units": {"shares": []}})["units"]["shares"].append(
            {"end": f"{y + 1}-02-10", "val": round(sh), "fy": y, "fp": "FY", "form": "10-K", "filed": filed})
    return {"cik": cik, "entityName": name, "facts": facts}


def build(seed=7, n_noise=40):
    rng = random.Random(seed)
    specs = []
    names = iter(["Alpha Brands", "Beacon Payments", "Cobalt Software", "Delta Foods", "Evergreen Rail", "Fjord Media",
                  "Granite Tools", "Harbor Insure", "Iris Health", "Juniper Retail", "Keystone Bank", "Lumen Devices",
                  "Meridian Chem", "Nimbus Cloud", "Orchid Apparel", "Pioneer Energy", "Quartz Labs", "Redwood Homes",
                  "Summit Beverages", "Tidewater Logistics", "Umbra Optics", "Vantage Data", "Willow Pharma",
                  "Xenon Motors", "Yarrow Airlines", "Zephyr Gaming"])
    plan = ([("decliner", (10, 13)), ("bubble", (11, 14))] + [("wonderful", (10, 15))] * 6 + [("wonderful", (28, 40))] * 5 + [("wonderful", (14, 30))] * 6 +
            [("average", (9, 16))] * 3 + [("cyclical", (6, 14))] * 2 + [("levered", (12, 18))] * 2 +
            [("bank", (7, 10))] + [("foreign", (6, 9))] + [("insurer", (8, 11))] + [("qwarn", (7, 10))])
    for i, (kind, band) in enumerate(plan):
        cik = 100000 + i
        nm = next(names, f"Company {i}")
        tk = "".join(w[0] for w in nm.split())[:2].upper() + chr(65 + i % 26)
        specs.append((cik, tk, nm, kind, band))
    for j in range(n_noise):
        kind = rng.choice(["average", "cyclical", "levered"])
        cik = 200000 + j
        specs.append((cik, f"N{j:02d}X", f"Noise Co {j}", kind, (8, 25)))
    comps, tickers, docs = {}, [], {}
    for cik, tk, nm, kind, band in specs:
        c = make_company(cik, tk, kind, rng, band)
        comps[tk] = (cik, kind, c)
        tickers.append([cik, nm, tk, "NYSE" if cik % 2 else "Nasdaq"])
        docs[cik] = companyfacts_doc(cik, nm, c, foreign=(kind == "foreign"))
    return comps, {"fields": ["cik", "name", "ticker", "exchange"], "data": tickers}, docs


def write_zip(docs, path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for cik, d in docs.items():
            z.writestr(f"CIK{cik:010d}.json", json.dumps(d))


def price_frames(comps, start="2009-01-02", end="2026-11-17", seed=11):
    """Price = trailing EPS x a P/E that wanders inside the company's band; SPY/VTI as a drifting index."""
    import pandas as pd

    rng = random.Random(seed)
    days = pd.bdate_range(start, end)
    frames = []
    for tk, (cik, kind, c) in comps.items():
        lo, hi = c["pe_band"]
        phase = rng.uniform(0, 6.28)
        eps = {y: r["EarningsPerShareDiluted"] for y, r in c["rows"].items()}
        closes = []
        noise = 0.0
        for k, d in enumerate(days):
            y = d.year - 1 if d.month > 2 else d.year - 2
            e = max(eps.get(min(max(y, YEARS[0]), YEARS[-1]), 0.5), 0.3)
            if kind == "foreign":
                e *= 1.10 * 2  # EUR per ordinary share -> USD per ADS (2 shares per ADS)
            pe = lo + (hi - lo) * (0.5 + 0.5 * math.sin(phase + k / 260.0))
            if kind == "bubble" and d.year >= 2023:
                pe *= min(1 + (d.year - 2023 + d.dayofyear / 365) * 1.6, 5.5)
            noise = 0.97 * noise + rng.gauss(0, 0.01)
            closes.append(max(e * pe * math.exp(noise), 1.0))
        df = pd.DataFrame({"ticker": tk, "date": days.strftime("%Y-%m-%d"), "close": closes})
        df["open"] = df["close"].shift(1).fillna(df["close"]) * (1 + rng.gauss(0, 0.002))
        df["high"] = df[["open", "close"]].max(axis=1) * 1.01
        df["low"] = df[["open", "close"]].min(axis=1) * 0.99
        df["adj_close"] = df["close"]
        df["volume"] = 2_000_000
        frames.append(df)
    for tk, drift in (("SPY", 0.09), ("VTI", 0.09)):
        v, vals = 100.0, []
        for _ in days:
            v *= math.exp(drift / 252 + rng.gauss(0, 0.011))
            vals.append(v)
        df = pd.DataFrame({"ticker": tk, "date": days.strftime("%Y-%m-%d"), "close": vals})
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"], df["low"], df["adj_close"], df["volume"] = df["close"], df["close"], df["close"], 5e7
        frames.append(df)
    return pd.concat(frames)


def macro_rows():
    rows = []
    for y in range(2005, 2027):
        for q, m in enumerate((1, 4, 7, 10)):
            d = f"{y}-{m:02d}-01"
            gdp = 13000 * 1.04 ** (y - 2005)
            rows.append(("GDP", d, gdp))
            rows.append(("BOGZ1LM893064105Q", d, gdp * 1000 * (1.0 + 0.07 * (y - 2005))))
    for y in range(2005, 2027):
        for m in range(1, 13):
            rows.append(("DGS30", f"{y}-{m:02d}-01", 3.0 + 1.5 * math.sin(y / 3)))
            rows.append(("DGS10", f"{y}-{m:02d}-01", 2.5 + 1.5 * math.sin(y / 3)))
            rows.append(("DEXUSEU", f"{y}-{m:02d}-01", 1.10))
    return rows

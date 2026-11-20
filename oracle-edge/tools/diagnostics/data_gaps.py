"""Oracle Edge data-gap diagnostics (read-only). Writes data_gaps.json to C:\\OracleEdge\\logs.

Looks at every kind of missing data: line items the scoring rules can't compute, XBRL tags companies use that the
concept map doesn't know yet, foreign filers without an ADR ratio, tickers with no price, and stale macro series.
"""
import json
import re
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import yaml

APP = Path(r"C:\OracleEdge")
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "data" / "oracle_edge.db"
ZIP = APP / "data" / "cache" / "companyfacts.zip"
CMAP = APP / "oracle_edge" / "concept_map.yaml"
OUT = APP / "logs" / "data_gaps.json"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
con.row_factory = sqlite3.Row
q = lambda sql, a=(): [dict(r) for r in con.execute(sql, a).fetchall()]  # noqa: E731
out = {}

# ---- 1. which scoring rules come out empty, for which companies -----------------------------------------------
sec = {r["cik"]: r for r in q("SELECT cik, ticker, name, category, sic, sic_desc, is_foreign, currency, adr_ratio, "
                               "adr_source, active FROM securities")}
priced = {r["ticker"] for r in q("SELECT DISTINCT ticker FROM prices")}
shortlist = {r["cik"] for r in q("SELECT DISTINCT cik FROM snapshots WHERE gates_passed=1")}
latest = q("""SELECT sn.cik, sn.profile_id, sn.filed, sn.fy_end, sn.rules_json, sn.gates_passed FROM snapshots sn
              JOIN (SELECT cik, MAX(filed) m FROM snapshots GROUP BY cik) x ON x.cik=sn.cik AND x.m=sn.filed
              WHERE sn.quality IS NOT NULL""")
seen = set()
rule_missing = Counter()
rule_missing_short = Counter()
gap_cos = []
for r in latest:
    if r["cik"] in seen:  # one row per company (snapshots repeat per profile)
        continue
    seen.add(r["cik"])
    info = sec.get(r["cik"]) or {}
    rs = json.loads(r["rules_json"] or "[]")
    miss = [x.get("id") or x["name"] for x in rs if x.get("value") is None and x.get("mode") in ("gate", "scored")]
    names = {x.get("id") or x["name"]: x["name"] for x in rs}
    for m in miss:
        rule_missing[names[m]] += 1
        if r["cik"] in shortlist:
            rule_missing_short[names[m]] += 1
    if miss:
        gap_cos.append({"cik": r["cik"], "ticker": info.get("ticker"), "name": info.get("name"),
                        "category": info.get("category"), "sic": info.get("sic"), "sic_desc": info.get("sic_desc"),
                        "foreign": info.get("is_foreign"), "priced": info.get("ticker") in priced,
                        "shortlist": r["cik"] in shortlist, "fy_end": r["fy_end"],
                        "missing": [names[m] for m in miss]})
out["companies_scored"] = len(seen)
out["companies_with_gaps"] = len(gap_cos)
out["rule_missing_all"] = rule_missing.most_common()
out["rule_missing_shortlist"] = rule_missing_short.most_common()
out["gap_by_category"] = Counter(g["category"] for g in gap_cos).most_common()
out["gap_by_sic"] = Counter(f"{g['sic']} {g['sic_desc']}" for g in gap_cos if len(g["missing"]) >= 3).most_common(40)

# ---- 2. which normalized line items are absent in recent years, and which unmapped tags those companies use -----
cmap = yaml.safe_load(open(CMAP, encoding="utf-8"))
mapped = {t for v in cmap.values() for t in v.get("tags", [])}
items = list(cmap)
have = defaultdict(set)
for r in q("SELECT cik, item FROM facts WHERE fy_end >= date('now','-3 years') GROUP BY cik, item"):
    have[r["cik"]].add(r["item"])
standard = [c for c, s in sec.items() if s["active"] and s["category"] == "standard" and c in have]
item_absent = Counter()
item_absent_short = Counter()
absent_by_cik = {}
for c in standard:
    miss = [i for i in items if i not in have[c]]
    absent_by_cik[c] = miss
    for i in miss:
        item_absent[i] += 1
        if c in shortlist:
            item_absent_short[i] += 1
out["standard_companies_with_recent_facts"] = len(standard)
out["item_absent_all"] = item_absent.most_common()
out["item_absent_shortlist"] = item_absent_short.most_common()

KEYS = {
    "revenue": r"Revenue|Sales", "cogs": r"CostOf|CostsOf|CostAndExpense", "gross_profit": r"GrossProfit",
    "sga": r"GeneralAndAdministrative|SellingGeneral|Administrative", "selling_marketing": r"Selling|Marketing|Advertising",
    "rnd": r"Research", "da": r"Depreciation|Amortization|Amortisation", "interest": r"Interest",
    "operating_income": r"OperatingIncome|OperatingProfit|ProfitLossFromOperating", "pretax_income": r"BeforeIncomeTax|BeforeTax",
    "income_tax": r"IncomeTax", "net_income": r"NetIncome|ProfitLoss", "eps_diluted": r"EarningsPerShare|EarningsLossPerShare",
    "shares_diluted": r"WeightedAverage", "ocf": r"OperatingActivities", "capex": r"PaymentsToAcquire|PurchaseOf|Capital",
    "dividends_paid": r"Dividend", "buybacks": r"Repurchase|Redeem|TreasuryStockValueAcquired",
}
tag_counts = defaultdict(Counter)
tag_examples = defaultdict(lambda: defaultdict(list))
scanned = 0
if ZIP.exists():
    want = {c for c in standard if absent_by_cik.get(c)}
    with zipfile.ZipFile(ZIP) as zf:
        for nm in zf.namelist():
            m = re.match(r"CIK0*(\d+)\.json$", nm)
            if not m or int(m.group(1)) not in want:
                continue
            c = int(m.group(1))
            try:
                doc = json.loads(zf.read(nm))
            except Exception:
                continue
            scanned += 1
            present = set()
            for tax, concepts in (doc.get("facts") or {}).items():
                if tax not in ("us-gaap", "ifrs-full"):
                    continue
                for cname, body in concepts.items():
                    recent = any(str(f.get("end", ""))[:4] >= "2022" and f.get("form", "").startswith(("10-K", "20-F", "40-F"))
                                 for u in (body.get("units") or {}).values() for f in u)
                    if recent:
                        present.add(f"{tax}:{cname}")
            for i in absent_by_cik[c]:
                pat = KEYS.get(i)
                if not pat:
                    continue
                for t in present:
                    if t not in mapped and re.search(pat, t.split(":")[1]):
                        tag_counts[i][t] += 1
                        if len(tag_examples[i][t]) < 4:
                            tag_examples[i][t].append(sec[c]["ticker"])
out["zip_scanned"] = scanned
out["unmapped_tags"] = {i: [{"tag": t, "companies": n, "eg": tag_examples[i][t]} for t, n in tag_counts[i].most_common(30)]
                        for i in tag_counts}

# ---- 3. shortlist companies with gaps (the ones that matter most) ---------------------------------------------
out["shortlist_gaps"] = sorted([g for g in gap_cos if g["shortlist"]], key=lambda g: -len(g["missing"]))[:150]
out["worst_gaps_priced"] = sorted([g for g in gap_cos if g["priced"] and not g["shortlist"]],
                                  key=lambda g: -len(g["missing"]))[:80]

# ---- 4. foreign filers, prices, macro -------------------------------------------------------------------------
out["adr_unknown"] = [dict(s) for c, s in sec.items() if s["is_foreign"] and not s["adr_ratio"] and c in shortlist]
cfg = {r["key"]: r["value_json"] for r in q("SELECT key, value_json FROM config")}
lp = json.loads(cfg.get("last_price_fetch") or "{}")
out["price_missing"] = lp.get("missing", [])
out["price_missing_detail"] = [{"ticker": t, **{k: v for k, v in (next((dict(s) for s in sec.values() if s["ticker"] == t), {})).items()
                                               if k in ("name", "category", "is_foreign", "currency")}}
                               for t in lp.get("missing", [])[:200]]
out["macro"] = q("SELECT series, COUNT(*) n, MIN(date) first, MAX(date) last FROM macro GROUP BY series ORDER BY series")
out["prices_by_source"] = q("SELECT source, COUNT(*) n, MAX(date) last FROM prices GROUP BY source")
out["stale_prices"] = q("""SELECT ticker, MAX(date) last FROM prices GROUP BY ticker
                           HAVING last < date('now','-7 days') ORDER BY last DESC LIMIT 200""")
out["recent_problems"] = q("SELECT ts, source, kind, detail FROM data_log WHERE ok=0 ORDER BY ts DESC LIMIT 80")

OUT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
print(f"Wrote {OUT}")
print(f"{len(gap_cos)} of {len(seen)} scored companies have a missing rule; zip scanned {scanned} files.")

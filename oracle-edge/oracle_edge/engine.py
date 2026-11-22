"""Scoring engine: point-in-time fundamental snapshots + daily valuation and ranking."""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass

from . import db
from .market import Macro, Regime
from .metrics import build_statement, compute, data_years, valuation_inputs
from .metrics_fin import compute_bank, compute_insurer, financial_valuation_inputs
from .rules import Profile, confidence, gate_margin, level_score, quality_from, score_rule
from .valuation import (predictability_mos, rank_score, value, value_driver, value_enhanced, value_financial,
                        value_score)


# ================================================================== price book
class PriceBook:
    """In-memory daily prices for a set of tickers (fast as-of lookups for backtests)."""

    def __init__(self, tickers: list[str] | None = None):
        self.d: dict[str, tuple[list[str], list[float], list[float], list[float], list[float]]] = {}
        if tickers is not None:
            self.load(tickers)

    def load(self, tickers: list[str]) -> None:
        tickers = [t for t in dict.fromkeys(tickers) if t and t not in self.d]
        for i in range(0, len(tickers), 400):
            chunk = tickers[i : i + 400]
            q = ",".join("?" * len(chunk))
            rows = db.rows(
                f"SELECT ticker,date,open,close,adj_close,volume FROM prices WHERE ticker IN ({q}) ORDER BY ticker,date",
                chunk,
            )
            cur = None
            for r in rows:
                if r["ticker"] != cur:
                    cur = r["ticker"]
                    self.d[cur] = ([], [], [], [], [])
                a = self.d[cur]
                a[0].append(r["date"])
                a[1].append(r["open"] if r["open"] else r["close"])
                a[2].append(r["close"])
                a[3].append(r["adj_close"] if r["adj_close"] else r["close"])
                a[4].append(r["volume"] or 0.0)

    def _i(self, t, d):
        a = self.d.get(t)
        if not a:
            return None, None
        i = bisect_right(a[0], d) - 1
        return (a, i) if i >= 0 else (None, None)

    def close(self, t, d):
        a, i = self._i(t, d)
        return a[2][i] if a else None

    def adj(self, t, d):
        a, i = self._i(t, d)
        return a[3][i] if a else None

    def last_date(self, t, d):
        a, i = self._i(t, d)
        return a[0][i] if a else None

    def open_adj_on(self, t, d):
        """Adjusted open on exactly date d (for next-open fills)."""
        a, i = self._i(t, d)
        if not a or a[0][i] != d:
            return None
        c = a[2][i] or 1.0
        return a[1][i] * (a[3][i] / c)

    def downtrend(self, t, d) -> bool:
        """Price below a falling 50-day average (the "falling knife" test used by the timing filter)."""
        a, i = self._i(t, d)
        if not a or i < 60:
            return False
        c = a[3]
        sma = sum(c[i - 49 : i + 1]) / 50
        prev = sum(c[i - 59 : i - 9]) / 50
        return c[i] < sma < prev

    def sma(self, t, d, n=200, back=0):
        """n-day average of adjusted closes ending `back` trading days before d."""
        a, i = self._i(t, d)
        if not a or i - back < n - 1:
            return None
        c = a[3]
        j = i - back
        return sum(c[j - n + 1 : j + 1]) / n

    def returns(self, t, d, n=252) -> dict[str, float]:
        """Daily returns over the last n trading days ending at d, keyed by date."""
        a, i = self._i(t, d)
        if not a or i < 2:
            return {}
        lo = max(1, i - n + 1)
        c = a[3]
        return {a[0][k]: c[k] / c[k - 1] - 1 for k in range(lo, i + 1) if c[k - 1]}

    def dollar_volume(self, t, d, n=20):
        a, i = self._i(t, d)
        if not a:
            return None
        lo = max(0, i - n + 1)
        vals = [a[2][k] * a[4][k] for k in range(lo, i + 1)]
        return sum(vals) / len(vals) if vals else None

    def fy_prices(self, t, fy_ends) -> dict[str, float]:
        out = {}
        for y in fy_ends:
            a, i = self._i(t, y)
            if a and (len(a[0]) and a[0][i] >= y[:8] + "01"):  # within the same month at least
                out[y] = a[2][i]
        return out

    def window_adj(self, t, d0, d1) -> list[float]:
        a = self.d.get(t)
        if not a:
            return []
        i0 = bisect_right(a[0], d0) - 1
        i1 = bisect_right(a[0], d1) - 1
        return a[3][max(i0, 0) : i1 + 1]

    def trading_days(self, t, d0, d1) -> list[str]:
        a = self.d.get(t)
        if not a:
            return []
        i0 = bisect_right(a[0], d0 if d0 else "")
        i1 = bisect_right(a[0], d1)
        return a[0][i0:i1]


# ================================================================== snapshots
def _facts_for(cik: int):
    return db.rows("SELECT item, fy_end, value, filed FROM facts WHERE cik=? ORDER BY filed", (cik,))


def build_snapshots(profile: Profile, ciks: list[int], since: str | None = None, prices: PriceBook | None = None,
                    progress=None, latest_only: bool = False) -> int:
    """Compute fundamental snapshots (one per filing date) for each company.

    since: only store snapshots whose filing date >= since (plus the last one before it, so the
    company has a valid snapshot on `since`). latest_only: just the most recent filing (daily runs).
    """
    from .splits import Splits

    st = profile.settings
    lookback, min_years = st["lookback_years"], st["min_years"]
    rules_by_id = {r["id"]: r for r in profile.all_rules()}
    weights = profile.body["category_weights"]
    sec = {r["cik"]: r for r in db.rows("SELECT cik,ticker,category,is_foreign,currency FROM securities")}
    sp = Splits()
    con = db.get()
    n_done = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for k, cik in enumerate(ciks):
            info = sec.get(cik)
            if not info or info["category"] == "excluded":
                continue
            facts = _facts_for(cik)
            if not facts:
                continue
            filed_dates = sorted({f["filed"] for f in facts})
            csp = sp.for_company(info["ticker"], facts)
            if latest_only:
                targets = filed_dates[-1:]
            elif since:
                before = [f for f in filed_dates if f < since]
                targets = ([before[-1]] if before else []) + [f for f in filed_dates if f >= since]
            else:
                targets = filed_dates
            targets_set = set(targets)
            state: dict[tuple[str, str], tuple[float, str]] = {}
            idx = 0
            for F in filed_dates:
                while idx < len(facts) and facts[idx]["filed"] <= F:
                    f = facts[idx]
                    state[(f["item"], f["fy_end"])] = (f["value"], f["filed"])
                    idx += 1
                if F not in targets_set:
                    continue
                row = _snapshot_row(cik, info, profile, rules_by_id, weights, on_basis(state, F, csp, info["ticker"]),
                                    lookback, min_years, F, prices, csp)
                if row:
                    con.execute(
                        "INSERT OR REPLACE INTO snapshots(cik,profile_id,profile_version,filed,fy_end,quality,"
                        "gates_passed,failed_gates,near_miss,category_scores,rules_json,val_inputs,confidence,routed,"
                        "gate_margin,gate_margin_rule) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        row,
                    )
            n_done += 1
            if n_done % 200 == 0:
                con.execute("COMMIT")
                con.execute("BEGIN IMMEDIATE")
            if progress and k % 25 == 0:
                progress(k / max(len(ciks), 1))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n_done


DATA_RULE_IDS = {"u1", "bu1", "iu1"}
SHARE_ITEMS = ("shares_diluted",)   # share counts: x split factor
PER_SHARE_ITEMS = ("eps_diluted",)  # per-share figures: / split factor


def on_basis(state: dict, F: str, sp, ticker: str | None) -> dict:
    """Point-in-time facts (value, filed) restated on the share basis of date F. Each filing states per-share
    figures on the basis of the day it was filed; a later split (before F) is applied to the older filings so
    every year of the record is comparable. Everything else passes through unchanged."""
    out = {}
    for (item, fy), (v, filed) in state.items():
        if v is not None and (item in SHARE_ITEMS or item in PER_SHARE_ITEMS):
            f = sp.factor(ticker, filed, F)
            if f != 1.0:
                v = v * f if item in SHARE_ITEMS else v / f
        out[(item, fy)] = v
    return out


def _snapshot_row(cik, info, profile, rules_by_id, weights, state, lookback, min_years, F, prices, sp=None):
    s = build_statement(state, lookback)
    if not s["years"]:
        return None
    cat = info["category"] if info["category"] in ("bank", "insurer") else "standard"
    fyp = prices.fy_prices(info["ticker"], s["years"]) if prices else None
    if fyp and sp is not None:
        # stored prices are on today's share basis; this snapshot's per-share figures on the basis of date F
        f = sp.factor(info["ticker"], F)
        if f != 1.0:
            fyp = {y: v * f for y, v in fyp.items()}
    m = compute(s, fyp)
    if cat == "bank":
        m.update(compute_bank(s))
    elif cat == "insurer":
        m.update(compute_insurer(s))
    results = []
    for r in profile.rulebook(cat):
        if r["mode"] == "off" or r["category"] == "valuation":
            continue
        mr = m.get(r["metric"])
        if mr is None:
            continue
        rr = score_rule(r, mr)
        if r["id"] in DATA_RULE_IDS:
            rr.passed = (mr.value or 0) >= min_years
        results.append(rr)
    q, cats = quality_from(results, weights, rules_by_id)
    failed = [r for r in results if r.mode == "gate" and r.passed is not True]
    near = None
    sm = profile.settings.get("near_miss_margin", 0.15)
    if len(failed) == 1:
        fr = failed[0]
        gm = gate_margin(fr.value, rules_by_id[fr.id])
        if gm is not None and gm >= -sm and fr.id not in DATA_RULE_IDS:
            near = json.dumps({"id": fr.id, "name": fr.name, "value": fr.value, "target": rules_by_id[fr.id]["target"]})
    # how close is the closest passing gate to failing? (early-warning input)
    tight, tight_rule = None, None
    for r in results:
        rd = rules_by_id[r.id]
        if r.mode == "gate" and r.passed and r.id not in DATA_RULE_IDS:
            # a value already at the 'excellent' anchor has nowhere meaningful to slip from (e.g. 10 of 10 years)
            if rd["target"] == rd["excellent"] or level_score(r.value, rd) == 100.0:
                continue
            gm = gate_margin(r.value, rd)
            if gm is not None and (tight is None or gm < tight):
                tight, tight_rule = gm, json.dumps({"id": r.id, "name": r.name, "value": r.value,
                                                     "target": rules_by_id[r.id]["target"],
                                                     "fmt": rules_by_id[r.id].get("fmt", "")})
    conf = confidence(s, results, data_years(s), lookback)
    vi = valuation_inputs(s, m)
    if cat in ("bank", "insurer"):
        vi.update(financial_valuation_inputs(s, m, cat))
    routed = cat if cat != "standard" else ("foreign" if info["is_foreign"] else "")
    return (
        cik, *profile.snap, F, s["years"][-1], q, int(not failed),
        json.dumps([{"id": r.id, "name": r.name, "value": r.value} for r in failed]), near, json.dumps(cats),
        json.dumps([r.to_dict() for r in results]), json.dumps(vi), conf, routed, tight, tight_rule,
    )


class SnapshotBook:
    """All snapshots of one profile in memory: latest snapshot as of any date (point-in-time)."""

    def __init__(self, profile: Profile, light: bool = True):
        cols = ("cik, filed, fy_end, quality, gates_passed, failed_gates, near_miss, val_inputs, confidence, routed, "
                "gate_margin, gate_margin_rule")
        if not light:
            cols += ", rules_json, category_scores"
        rows = db.rows(
            f"SELECT {cols} FROM snapshots WHERE profile_id=? AND profile_version=? ORDER BY cik, filed",
            profile.snap,
        )
        self.by_cik: dict[int, tuple[list[str], list]] = {}
        for r in rows:
            a = self.by_cik.setdefault(r["cik"], ([], []))
            a[0].append(r["filed"])
            a[1].append(r)
        self.q: dict[int, tuple[list[str], list]] = {}
        for r in db.rows("SELECT cik, filed, period_end, flags_json, metrics_json FROM qsnaps ORDER BY cik, filed"):
            a = self.q.setdefault(r["cik"], ([], []))
            a[0].append(r["filed"])
            a[1].append(r)
        self.info = {r["cik"]: dict(r) for r in db.rows(
            "SELECT cik, ticker, category, is_foreign, currency, adr_ratio, adr_source FROM securities")}
        from .splits import Splits

        self.splits = Splits()

    def asof(self, cik: int, d: str):
        a = self.by_cik.get(cik)
        if not a:
            return None
        i = bisect_right(a[0], d) - 1
        return a[1][i] if i >= 0 else None

    def quarterly(self, cik: int, d: str, annual_filed: str | None):
        """Latest 10-Q check filed on/before d and AFTER the latest annual report (else None)."""
        a = self.q.get(cik)
        if not a:
            return None
        i = bisect_right(a[0], d) - 1
        if i < 0:
            return None
        r = a[1][i]
        if annual_filed and r["filed"] <= annual_filed:
            return None
        return r

    def ciks(self):
        return list(self.by_cik.keys())

    # ---- a strategy's universe (0.18): Wonderfulness as known on a date, and industries
    def wonder_asof(self, cik: int, d: str) -> float | None:
        """Wonderfulness from the latest annual report filed on or before d (each fiscal year's score, ranked within
        its year; today's 'now' score once it is the newest), so a backtest only sees what was known then."""
        wb = getattr(self, "_wonder", None)
        if wb is None:
            wb = self._wonder = wonder_book()
        a = wb.get(cik)
        if not a:
            return None
        i = bisect_right(a[0], d) - 1
        return a[1][i] if i >= 0 else None

    def industry(self, cik: int) -> str:
        ind = getattr(self, "_ind", None)
        if ind is None:
            ind = self._ind = {r["cik"]: r["sic_desc"] or "" for r in db.rows("SELECT cik, sic_desc FROM securities")}
        return ind.get(cik, "")


def wonder_book() -> dict[int, tuple[list[str], list[float]]]:
    """cik -> (filing dates, Wonderfulness), oldest first, from the shared scores (the primary strategy's)."""
    from .rules import default_profile

    pid = default_profile().snap[0]
    out: dict[int, tuple[list[str], list[float]]] = {}
    rows = db.rows("SELECT cik, filed, score, cohort FROM wonder WHERE profile_id=? AND filed IS NOT NULL "
                   "ORDER BY cik, filed, cohort", (pid,))
    for r in rows:
        a = out.setdefault(r["cik"], ([], []))
        if a[0] and a[0][-1] == r["filed"]:
            if r["cohort"] == "now":  # same report: today's ranking replaces that year's
                a[1][-1] = r["score"]
            continue
        a[0].append(r["filed"])
        a[1].append(r["score"])
    return out


def universe_ok(st: dict, snaps, cik: int, d: str) -> tuple[bool, str]:
    """Whether a company is in the strategy's universe on day d (beyond Quality, size and the checklist, which the
    evaluation checks itself): Wonderfulness at least universe_wonder_min, and its industry among
    universe_industries (when set) and not among universe_exclude_industries."""
    wmin = st.get("universe_wonder_min")
    if wmin not in (None, "", 0):
        w = snaps.wonder_asof(cik, d)
        if w is None or w < float(wmin):
            return False, f"Wonderfulness {'unknown' if w is None else f'{w:.0f}'} under {float(wmin):.0f}"
    inc, exc = st.get("universe_industries") or [], st.get("universe_exclude_industries") or []
    if inc or exc:
        ind = snaps.industry(cik)
        if inc and ind not in inc:
            return False, "industry not in this strategy's list"
        if exc and ind in exc:
            return False, "industry excluded by this strategy"
    return True, ""


class _OneBook(SnapshotBook):
    """SnapshotBook for a single company (the stock page's on-the-spot evaluation)."""

    def __init__(self, profile: Profile, cik: int):  # noqa: D107 - deliberately skips the full load
        from .splits import Splits

        cols = ("cik, filed, fy_end, quality, gates_passed, failed_gates, near_miss, val_inputs, confidence, routed, "
                "gate_margin, gate_margin_rule")
        rows = db.rows(f"SELECT {cols} FROM snapshots WHERE profile_id=? AND profile_version=? AND cik=? ORDER BY filed",
                       (*profile.snap, cik))
        self.by_cik = {cik: ([r["filed"] for r in rows], list(rows))} if rows else {}
        q = db.rows("SELECT cik, filed, period_end, flags_json, metrics_json FROM qsnaps WHERE cik=? ORDER BY filed",
                    (cik,))
        self.q = {cik: ([r["filed"] for r in q], list(q))} if q else {}
        self.info = {r["cik"]: dict(r) for r in db.rows(
            "SELECT cik, ticker, category, is_foreign, currency, adr_ratio, adr_source FROM securities WHERE cik=?",
            (cik,))}
        self.splits = Splits()


def evaluate_one(cik: int, profile: Profile, d: str | None = None) -> dict | None:
    """Evaluate one company exactly as the daily update would (the profile's rules and valuation model, the latest
    price), even if it's off the shortlist. Returns a row shaped like the evaluations table's, with
    detail.on_demand = True. None without a snapshot or a recent price."""
    from datetime import date as _date

    from .market import Macro, price_lookup

    t = db.scalar("SELECT ticker FROM securities WHERE cik=?", (cik,))
    if not t:
        return None
    book = _OneBook(profile, cik)
    if not book.by_cik:
        return None
    pb = PriceBook([t])
    d = d or pb.last_date(t, _date.today().isoformat())
    if not d:
        return None
    m = Macro()
    st = profile.settings
    regime = m.regime(d, st["mos_gate"], st.get("regime_modifier", False), price_lookup("VTI"))
    evs = evaluate(d, profile, book, pb, regime, {cik: t}, owned={cik}, macro=m)
    if not evs:
        return None
    e = evs[0]
    return {"run_id": None, "date": d, "cik": cik, "list": e.list, "quality": e.quality, "value": e.value,
            "rank_score": e.rank_score, "rank": None, "iv": e.iv, "mos": e.mos, "exp_return": e.er,
            "buy_below": e.buy_below, "strong_buy_below": e.strong_buy_below, "price": e.price,
            "confidence": e.confidence,
            "detail": {"note": e.note, "failed": e.failed, "near": e.near, "filed": e.filed,
                       "expensive_above": e.expensive_above, "pe_now": e.pe_now, "pe_median": e.pe_median,
                       **(e.detail or {}), "on_demand": True}}


# ================================================================== daily evaluation
@dataclass
class Eval:
    cik: int
    ticker: str
    list: str  # buy | pricey | near | pending | out
    quality: float | None
    value: float | None
    rank_score: float | None
    confidence: float | None
    price: float | None
    iv: float | None
    mos: float | None
    er: float | None
    buy_below: float | None
    strong_buy_below: float | None
    expensive_above: float | None
    pe_now: float | None
    pe_median: float | None
    gates_passed: bool
    failed: list
    near: dict | None
    filed: str | None
    note: str = ""
    rank: int | None = None
    detail: dict | None = None
    qflags: list | None = None
    qfiled: str | None = None
    gate_margin: float | None = None
    gate_margin_rule: dict | None = None
    category: str = "standard"


def _pending(base, note, category="standard"):
    return Eval(list="pending", value=None, rank_score=None, iv=None, mos=None, er=None, buy_below=None,
                strong_buy_below=None, expensive_above=None, pe_now=None, pe_median=None, note=note,
                category=category, **base)


PER_SHARE_VI = ("oe_ps", "eps3", "eps_last", "bvps", "book_ps", "oe_norm_ps", "eps_norm")


def fair_pe_test(price: float, v, vi: dict, st: dict) -> tuple[float, float] | None:
    """The "fair price" test (price_test: fair_pe): the price at which the P/E on the latest annual EPS is
    fair_pe_mult times the company's own median P/E over its record, and a value score that rises as the P/E
    falls toward half its usual level. Returns (buy-below price, value score), or None to use the DCF test
    (setting off, or no usable P/E history). On the app's history (2015-2026), how cheap a wonderful company was
    against its own past P/E said a little more about the next one to two years than the DCF margin of safety did."""
    if st.get("price_test", "dcf") != "fair_pe":
        return None
    eps = vi.get("eps_last")
    if not (v.pe_median and v.pe_median > 0 and eps and eps > 0 and price):
        return None
    mult = float(st.get("fair_pe_mult", 1.5))
    rel = (price / eps) / v.pe_median
    score = 100.0 * min(max((mult - rel) / max(mult - 0.5, 1e-9), 0.0), 1.0)
    return mult * v.pe_median * eps, score


def _profile_flags(flags: list[dict], st: dict) -> list[dict]:
    """Business-health flags count only for profiles that switch the check on; there a severe one acts like any
    hard quarterly flag (blocks buying, and a sale once the next filing confirms it)."""
    if not st.get("health_check"):
        return [f for f in flags if not f.get("health")]
    return [({**f, "hard": True} if f.get("health") and f.get("severe") else f) for f in flags]


def split_scale(vi: dict, f: float) -> dict:
    """Valuation inputs restated after splits: per-share figures / f, the share count x f."""
    if f == 1.0:
        return vi
    out = dict(vi)
    for k in PER_SHARE_VI:
        if out.get(k) is not None:
            out[k] = out[k] / f
    out["eps_by_fy"] = {y: e / f for y, e in (vi.get("eps_by_fy") or {}).items()}
    if out.get("shares"):
        out["shares"] = out["shares"] * f
    if out.get("vd_shares"):
        out["vd_shares"] = out["vd_shares"] * f
    return out


def _foreign_scale(vi: dict, info: dict, d: str, macro, pb_fy_dates) -> tuple[dict | None, str]:
    """Convert per-ordinary-share figures in the reporting currency to US$ per traded share (ADS)."""
    ratio = info.get("adr_ratio")
    if not ratio:
        return None, "foreign filer: ADR share ratio unknown (set it on the stock page)"
    fx = macro.fx(info.get("currency") or "USD", d) if macro else (1.0 if (info.get("currency") or "USD") == "USD" else None)
    if fx is None:
        return None, f"foreign filer: no exchange rate for {info.get('currency')}"
    f = fx * ratio
    out = dict(vi)
    for k in ("oe_ps", "eps3", "eps_last", "bvps", "book_ps", "oe_norm_ps", "eps_norm"):
        if out.get(k) is not None:
            out[k] = out[k] * f
    eps = {}
    for y, e in (vi.get("eps_by_fy") or {}).items():
        fxy = macro.fx(info.get("currency") or "USD", y) if macro else fx
        if fxy:
            eps[y] = e * fxy * ratio
    out["eps_by_fy"] = eps
    if out.get("shares"):
        out["shares"] = out["shares"] / ratio  # number of traded shares (ADS) equivalent
    out["vd_ps_mult"] = f  # value-driver: reporting currency per ordinary share -> US$ per traded share
    return out, ""


def sane_shares(sh: float | None, ref: float | None) -> float | None:
    """A share count checked against another source (the annual report's): filings sometimes give it in thousands
    or millions. Rescaled when it's off by about 1,000x or 1,000,000x; None when it's off by anything else over 3x."""
    if not sh or not ref or sh <= 0 or ref <= 0:
        return sh
    k = ref / sh
    if 1 / 3 <= k <= 3:
        return sh
    for f in (1e3, 1e6):
        if 1 / 3 <= k / f <= 3:
            return sh * f
        if 1 / 3 <= k * f <= 3:
            return sh / f
    return None


def ttm_inputs(qs, vi: dict, split_factor: float = 1.0) -> dict | None:
    """The latest 10-Q's trailing-twelve-month sales and operating profit (adjusted like the annual figures: R&D
    as an investment, the interest part of leases) and balance sheet, for the value-driver model."""
    if not qs:
        return None
    m = json.loads(qs["metrics_json"] or "{}")
    if not m.get("ttm_revenue") or m.get("ttm_operating_income") is None:
        return None
    from .metrics import LEASE_RATE

    oi = m["ttm_operating_income"]
    rd = vi.get("vd_rnd") or {}
    if rd.get("capitalized") and m.get("ttm_rnd") is not None and rd.get("amort") is not None:
        oi += abs(m["ttm_rnd"]) - rd["amort"]
    lease = m.get("op_lease_liability")
    if lease is None:
        lease = (vi.get("vd_claims") or {}).get("leases")
    oi += LEASE_RATE * (lease or 0.0)
    cash = None
    if m.get("cash") is not None:
        cash = m["cash"] + (m.get("st_investments") or 0.0)
    return {"sales": m["ttm_revenue"], "oi": oi, "cash": cash, "debt": m.get("debt"), "leases": lease,
            "shares": sane_shares((m["shares"] * split_factor) if m.get("shares") else None,
                                  vi.get("vd_shares") or vi.get("shares")),
            "period_end": m.get("period_end")}


def evaluate(date: str, profile: Profile, snaps: SnapshotBook, pb: PriceBook, regime: Regime,
             tickers: dict[int, str], owned: set[int] | None = None, macro=None) -> list[Eval]:
    st = profile.settings
    owned = owned or set()
    qmin = st["quality_min"]
    mos_gate = regime.mos_gate
    hurdle = st["hurdle"]
    out: list[Eval] = []
    for cik in snaps.ciks():
        s = snaps.asof(cik, date)
        if s is None:
            continue
        t = tickers.get(cik)
        if not t:
            continue
        info = snaps.info.get(cik, {})
        gates = bool(s["gates_passed"])
        q = s["quality"]
        near = json.loads(s["near_miss"]) if s["near_miss"] else None
        routed = s["routed"] or ""
        category = routed if routed in ("bank", "insurer") else "standard"
        interesting = (gates and q is not None and q >= qmin) or near or cik in owned
        if not interesting:
            continue
        if cik not in owned and not universe_ok(st, snaps, cik, date)[0]:
            continue  # outside this strategy's universe (holdings are still followed)
        foreign = bool(info.get("is_foreign"))
        if foreign and not st.get("include_foreign", True) and cik not in owned:
            continue
        failed = json.loads(s["failed_gates"] or "[]")
        qs = snaps.quarterly(cik, date, s["filed"]) if st.get("quarterly_warnings", True) else None
        qflags = _profile_flags(json.loads(qs["flags_json"]) if qs else [], st)
        price = pb.close(t, date)
        last = pb.last_date(t, date)
        stale = last is not None and last < date and (_days_between(last, date) > 7)
        base = dict(cik=cik, ticker=t, quality=q, confidence=s["confidence"], price=price, gates_passed=gates,
                    failed=failed, near=near, filed=s["filed"], qflags=qflags, qfiled=qs["filed"] if qs else None,
                    gate_margin=s["gate_margin"],
                    gate_margin_rule=json.loads(s["gate_margin_rule"]) if s["gate_margin_rule"] else None)
        if price is None or stale:
            if cik in owned or (gates and q and q >= qmin):
                out.append(_pending(base, "no recent price", category))
            continue
        vi = json.loads(s["val_inputs"] or "{}")
        # the snapshot is on the share basis of its filing date; prices on today's (every split applied)
        vi = split_scale(vi, snaps.splits.factor(t, s["filed"]) if hasattr(snaps, "splits") else 1.0)
        if foreign:
            vi2, why = _foreign_scale(vi, info, date, macro, None)
            if vi2 is None:
                out.append(_pending(base, why, category))
                continue
            vi = vi2
            el = vi.get("eps_last")
            if el and el > 0 and not (3 <= price / el <= 150):
                out.append(_pending(base, f"foreign filer: implied P/E {price / el:.0f} looks wrong; check the ADR "
                                          f"ratio ({info.get('adr_ratio')}) on the stock page", category))
                continue
        fyp = pb.fy_prices(t, list(vi.get("eps_by_fy", {}).keys()))
        gate_c = mos_gate
        if category in ("bank", "insurer"):
            v = value_financial(price, vi, st, regime.t30, mos_gate, fyp)
        elif st.get("valuation_model") == "value_driver":
            qv = snaps.quarterly(cik, date, s["filed"]) if not foreign else None
            ttm = ttm_inputs(qv, vi, snaps.splits.factor(t, qv["filed"]) if qv else 1.0)
            from . import estimates

            est = estimates.asof(t, date) if not foreign else None
            v = value_driver(price, vi, st, mos_gate, fyp, ttm, est, signal=True)  # the strict buy rule
        elif st.get("valuation_model") == "enhanced":
            gate_c = predictability_mos(mos_gate, vi, st)
            v = value_enhanced(price, vi, st, regime.t30, gate_c, fyp)
        else:
            v = value(price, vi, st, regime.t30, mos_gate, fyp)
        vs = value_score(v.er, {"target": hurdle})
        if category == "standard" and v.earnings_yield is not None and v.earnings_yield < regime.t30:
            vs = max(0.0, vs - 10)  # rule 26: earnings yield below the 30-yr Treasury
        buy_below = v.buy_below
        fair = fair_pe_test(price, v, vi, st)
        if fair is not None:  # "fair price" test: P/E against the company's own history instead of the DCF
            buy_below, vs = fair
        conf = s["confidence"] or 0.9
        rs = rank_score(q or 0, vs, conf, st["rank_quality_weight"]) if q is not None else None
        shares = vi.get("shares") or 0  # point-in-time diluted shares from the latest annual report
        mcap = price * shares if shares else None
        dvol = pb.dollar_volume(t, date)
        size_ok = (mcap or 0) >= st["min_market_cap"] and (dvol or 0) >= st["min_dollar_volume"]
        # cheap enough = at or below the buy-below price, which already combines the margin of safety and the
        # price that earns the hurdle under every model used (so the Buy Zone and the price shown always agree)
        if fair is not None:
            val_ok = price <= buy_below
        else:
            val_ok = v.mos is not None and v.er is not None and v.buy_below is not None and price <= v.buy_below
        if gates and q is not None and q >= qmin and size_ok and val_ok:
            lst = "buy"
        elif gates and q is not None and q >= qmin and size_ok:
            lst = "pricey"
        elif near and size_ok:
            lst = "near"
        else:
            lst = "out"
        note = "" if size_ok else "below size/liquidity floor"
        if foreign:
            note = (note + " · " if note else "") + f"{info.get('currency')} → US$ at ADR ratio {info.get('adr_ratio'):g}"
        out.append(Eval(list=lst, value=vs, rank_score=rs, iv=v.iv, mos=v.mos, er=v.er, buy_below=buy_below,
                        strong_buy_below=v.strong_buy_below, expensive_above=v.expensive_above, pe_now=v.pe_now,
                        pe_median=v.pe_median, note=note, category=category,
                        detail={"iv_a": v.iv_a, "iv_b": v.iv_b, "er_a": v.er_a, "er_b": v.er_b, "r": v.discount_rate,
                                "g": v.growth, "exit_pe": v.exit_pe, "ey": v.earnings_yield, "pepg": v.pepg,
                                "mcap": mcap, "dvol": dvol,
                                "model_a": "ROE-justified price-to-book" if category != "standard" else (
                                    "Value-driver DCF" if (v.extra or {}).get("model") == "value_driver"
                                    else "Owner-earnings DCF"),
                                "qflags": qflags, "qfiled": base["qfiled"], "category": category,
                                "gate_margin": s["gate_margin"], "gate_margin_rule": base["gate_margin_rule"],
                                **({"valuation": v.extra, "mos_gate": gate_c} if v.extra else {})},
                        **base))
    buys = sorted([e for e in out if e.list == "buy"], key=lambda e: (-(e.rank_score or 0), -(e.mos or 0)))
    for i, e in enumerate(buys, 1):
        e.rank = i
    return out


def _days_between(a: str, b: str) -> int:
    from datetime import date as _d

    return (_d.fromisoformat(b) - _d.fromisoformat(a)).days


class _SharesCache(dict):
    """Latest dei shares outstanding per company (loaded lazily)."""

    loaded = False

    def get(self, cik, default=None):  # type: ignore[override]
        if not self.loaded:
            for r in db.rows(
                "SELECT s.cik, s.value FROM shares_out s JOIN (SELECT cik, MAX(as_of) m FROM shares_out GROUP BY cik) x "
                "ON s.cik=x.cik AND s.as_of=x.m"
            ):
                self[r["cik"]] = r["value"]
            self.loaded = True
        return super().get(cik, default)


db_shares_cache = _SharesCache()


def store_evals(run_id: int, date: str, evals: list[Eval]) -> None:
    keep = [e for e in evals if e.list in ("buy", "pricey", "near", "pending") or e.list == "out_owned"]
    with db.tx() as con:
        con.execute("DELETE FROM evaluations WHERE run_id=? AND date=?", (run_id, date))
        con.executemany(
            "INSERT OR REPLACE INTO evaluations(run_id,date,cik,list,quality,value,rank_score,rank,iv,mos,exp_return,"
            "buy_below,strong_buy_below,price,confidence,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run_id, date, e.cik, e.list, e.quality, e.value, e.rank_score, e.rank, e.iv, e.mos, e.er, e.buy_below,
                 e.strong_buy_below, e.price, e.confidence,
                 json.dumps({"note": e.note, "failed": e.failed, "near": e.near, "filed": e.filed,
                             "expensive_above": e.expensive_above, "pe_now": e.pe_now, "pe_median": e.pe_median,
                             **(e.detail or {})}))
                for e in keep
            ],
        )


__all__ = ["PriceBook", "SnapshotBook", "build_snapshots", "evaluate", "store_evals", "Eval", "Macro"]

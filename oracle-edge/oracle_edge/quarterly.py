"""Quarterly early warnings: trailing-twelve-month (TTM) checks after every 10-Q.

TTM for a flow item at quarter end E = year-to-date(E) + last fiscal year - year-to-date(same period a year
earlier). Everything is point-in-time: only facts filed on or before each 10-Q's filing date are used.

Flags (hard = blocks new buys and can confirm a REVIEW into a SELL; soft = REVIEW only):
  ttm_loss          hard  trailing-12-month net income <= 0
  debt_payback_ttm  hard  long-term debt / TTM net income above the debt gate (standard companies)
  roe_ttm           hard  TTM return on equity below the ROE gate's worst-year floor (standard / insurers)
  roa_ttm           hard  TTM return on assets below the bank ROA floor (banks)
  cash_vs_profit    soft  TTM operating cash flow / TTM net income below the minimum (standard companies)
  dilution          soft  diluted share count up more than the limit vs a year earlier
Deterioration checks (soft; catch businesses that still look fine on 10-year averages):
  incr_roic_decline soft  incremental ROIC (3-year windows) fell three years running, by 10+ points
  gm_decline        soft  gross margin below the same quarter a year earlier for 3+ quarters in a row
  acquired_growth   soft  3-year acquisition spending >= 1.5x the revenue added (growth bought, not built)
  receivables       soft  receivables growing 15+ points faster than sales
  inventory         soft  inventory growing 15+ points faster than sales
  sbc_vs_fcf        soft  stock-based pay >= 25% of free cash flow and up 5+ points in a year
  concentration     soft  a disclosed concentration (e.g. one customer) of 20%+ of revenue
"""

from __future__ import annotations

import json
from datetime import date

from . import db
from .rules import Profile


def _d(a: str) -> date:
    return date.fromisoformat(a)


def _days(a: str, b: str) -> int:
    return (_d(b) - _d(a)).days


class _State:
    def __init__(self):
        self.q: dict[str, dict[str, dict[str, float]]] = {}  # item -> end -> start ('' = instant) -> value
        self.a: dict[str, dict[str, float]] = {}             # item -> fy_end -> value

    def put_q(self, item, start, end, value):
        self.q.setdefault(item, {}).setdefault(end, {})[start] = value

    def put_a(self, item, fy_end, value):
        self.a.setdefault(item, {})[fy_end] = value


def _ttm(st: _State, item: str, E: str) -> float | None:
    durs = [(s0, v) for s0, v in st.q.get(item, {}).get(E, {}).items() if s0]
    if not durs:
        return None
    S, ytd = min(durs, key=lambda x: x[0])  # longest period ending at E = year-to-date
    dur = _days(S, E)
    if dur > 300:
        return ytd
    fy = [(fe, v) for fe, v in st.a.get(item, {}).items() if 0 <= _days(fe, S) <= 12]
    if not fy:
        return None
    fy_val = max(fy)[1]
    for e0, starts in st.q.get(item, {}).items():
        if abs(_days(e0, E) - 365) <= 12:
            for s0, v in starts.items():
                if s0 and abs(_days(s0, e0) - dur) <= 15:
                    return ytd + fy_val - v
    return None


def _instant(st: _State, item: str, E: str, back_days: int = 0) -> float | None:
    for e0, starts in st.q.get(item, {}).items():
        if "" in starts and abs(_days(e0, E) - back_days) <= 12:
            return starts[""]
    for fe, v in st.a.get(item, {}).items():
        if abs(_days(fe, E) - back_days) <= 12:
            return v
    return None


def _quarter_shares(st: _State, E: str, back_days: int = 0) -> float | None:
    for e0, starts in st.q.get("shares_diluted", {}).items():
        if abs(_days(e0, E) - back_days) <= 12:
            for s0, v in starts.items():
                if s0 and 80 <= _days(s0, e0) <= 100:
                    return v
    return None


def _quarter_only(st: _State, item: str, E: str) -> float | None:
    """The 3-month value for the quarter ending E (not year-to-date)."""
    for s0, v in st.q.get(item, {}).get(E, {}).items():
        if s0 and 80 <= _days(s0, E) <= 100:
            return v
    return None


def _quarter_ends(st: _State, E: str, n: int) -> list[str]:
    """The last n quarter ends up to E that have a 3-month revenue figure."""
    ends = sorted(e for e in st.q.get("revenue", {}) if e <= E and _quarter_only(st, "revenue", e) is not None)
    return ends[-n:]


def _year_ago(st: _State, item: str, E: str) -> str | None:
    for e0 in st.q.get(item, {}):
        if abs(_days(e0, E) - 365) <= 12:
            return e0
    return None


def _annual(st: _State, item: str) -> list[tuple[str, float]]:
    return sorted(st.a.get(item, {}).items())


def deterioration(st: _State, E: str, s: dict) -> list[dict]:
    """Early signs of a business getting worse while its 10-year averages still look good. All soft."""
    flags = []
    # ---- incremental ROIC, 3-year windows, from annual reports
    fys = sorted(st.a.get("operating_income", {}))
    if len(fys) >= 6:
        nop, ic = [], []
        for fy in fys:
            oi = st.a["operating_income"].get(fy)
            p, t = st.a.get("pretax_income", {}).get(fy), st.a.get("income_tax", {}).get(fy)
            tr = min(max((t / p) if (p and t is not None and p > 0) else 0.21, 0.0), 0.35)
            nop.append(oi * (1 - tr) if oi is not None else None)
            e = st.a.get("equity", {}).get(fy)
            ts = abs(st.a.get("treasury_stock", {}).get(fy) or 0.0)
            if e is not None and e <= 0 and ts:
                e += ts
            debt = (st.a.get("lt_debt", {}).get(fy) or 0) + (st.a.get("st_debt", {}).get(fy) or 0)
            c = debt + (e or 0) - (st.a.get("cash", {}).get(fy) or 0)
            ic.append(c if (e is not None and c > 0) else None)
        from .metrics import incremental_roic

        inc = [incremental_roic(nop, ic, 3, k) for k in range(len(fys) - 3, len(fys))]
        if all(x is not None for x in inc) and inc[0] > inc[1] > inc[2] and inc[0] - inc[2] >= s.get(
                "warn_incr_roic_drop", 0.10):
            flags.append({"id": "incr_roic_decline", "name": "Incremental ROIC falling", "hard": False,
                          "detail": f"return on new capital {inc[0]:.0%} → {inc[1]:.0%} → {inc[2]:.0%} over 3 years"})
    # ---- gross margin vs the same quarter a year earlier
    n_q = int(s.get("warn_gm_quarters", 3))
    ends = _quarter_ends(st, E, n_q)
    downs = []
    for e in ends:
        r1, gp1 = _quarter_only(st, "revenue", e), _quarter_only(st, "gross_profit", e)
        if gp1 is None and r1 is not None and _quarter_only(st, "cogs", e) is not None:
            gp1 = r1 - _quarter_only(st, "cogs", e)
        e0 = _year_ago(st, "revenue", e)
        if not (e0 and r1 and gp1 is not None):
            break
        r0, gp0 = _quarter_only(st, "revenue", e0), _quarter_only(st, "gross_profit", e0)
        if gp0 is None and r0 is not None and _quarter_only(st, "cogs", e0) is not None:
            gp0 = r0 - _quarter_only(st, "cogs", e0)
        if not (r0 and gp0 is not None):
            break
        downs.append((gp0 / r0, gp1 / r1))
    if len(ends) >= n_q and len(downs) == n_q and all(b < a - s.get("warn_gm_drop", 0.01) for a, b in downs):
        flags.append({"id": "gm_decline", "name": "Gross margin slipping", "hard": False,
                      "detail": f"below the year-earlier quarter {n_q} quarters running "
                                f"({downs[-1][0]:.1%} → {downs[-1][1]:.1%} latest)"})
    # ---- growth bought through acquisitions
    rev = _annual(st, "revenue")
    acq = dict(_annual(st, "acquisitions"))
    if len(rev) >= 4:
        (f0, r0), (f1, r1) = rev[-4], rev[-1]
        spent = sum(abs(acq.get(f, 0.0) or 0.0) for f, _ in rev[-3:])
        added = r1 - r0
        if added > 0 and spent >= s.get("warn_acq_ratio", 1.5) * added and spent >= 0.10 * r1:
            flags.append({"id": "acquired_growth", "name": "Growth mostly bought", "hard": False,
                          "detail": f"spent {spent / 1e6:,.0f}M on acquisitions vs {added / 1e6:,.0f}M of revenue "
                                    f"added over 3 years"})
    # ---- receivables / inventory outgrowing sales
    rev_t, rev_t0 = _ttm(st, "revenue", E), None
    e_ago = _year_ago(st, "revenue", E)
    if e_ago:
        rev_t0 = _ttm(st, "revenue", e_ago)
    if rev_t and rev_t0 and rev_t0 > 0:
        sales_g = rev_t / rev_t0 - 1
        for item, fid, name in (("receivables", "receivables", "Receivables outgrowing sales"),
                                ("inventory", "inventory", "Inventory outgrowing sales")):
            a1, a0 = _instant(st, item, E), _instant(st, item, E, 365)
            if a1 and a0 and a0 > 0 and a1 >= 0.05 * rev_t:
                g = a1 / a0 - 1
                if g - sales_g >= s.get("warn_wc_gap", 0.15):
                    flags.append({"id": fid, "name": name, "hard": False,
                                  "detail": f"{item} up {g:.0%} vs sales up {sales_g:.0%} over the year"})
    # ---- stock-based pay eating free cash flow
    def _sbc_ratio(at):
        sbc, ocf, capex = _ttm(st, "sbc", at), _ttm(st, "ocf", at), _ttm(st, "capex", at)
        if sbc is None or ocf is None:
            return None
        fcf = ocf - abs(capex or 0.0)
        return (sbc / fcf) if fcf > 0 else None

    r1 = _sbc_ratio(E)
    r0 = _sbc_ratio(e_ago) if e_ago else None
    if r1 is not None and r1 >= s.get("warn_sbc_fcf", 0.25) and (r0 is None or r1 - r0 >= 0.05):
        flags.append({"id": "sbc_vs_fcf", "name": "Stock pay rising vs free cash flow", "hard": False,
                      "detail": f"stock-based pay is {r1:.0%} of free cash flow" + (f" (was {r0:.0%})" if r0 is not None
                                                                                   else "")})
    # ---- concentration, only where it's disclosed as a plain figure
    conc = _annual(st, "customer_concentration")
    if conc and conc[-1][1] is not None and conc[-1][1] >= s.get("warn_concentration", 0.20):
        flags.append({"id": "concentration", "name": "Concentration risk", "hard": False,
                      "detail": f"filing discloses a concentration of {conc[-1][1]:.0%} (often one customer)"})
    return flags


def check(st: _State, E: str, category: str, prof: Profile) -> tuple[list[dict], dict]:
    s = prof.settings
    rules = {r["id"]: r for r in prof.all_rules()}
    ni = _ttm(st, "net_income", E)
    ocf = _ttm(st, "ocf", E)
    eq, eq0 = _instant(st, "equity", E), _instant(st, "equity", E, 365)
    ta, ta0 = _instant(st, "total_assets", E), _instant(st, "total_assets", E, 365)
    ltd = _instant(st, "lt_debt", E)
    sh, sh0 = _quarter_shares(st, E), _quarter_shares(st, E, 365)
    m = {"period_end": E, "ttm_net_income": ni, "ttm_ocf": ocf, "equity": eq, "lt_debt": ltd, "shares": sh,
         "shares_year_ago": sh0}
    flags: list[dict] = []
    if ni is not None and ni <= 0:
        flags.append({"id": "ttm_loss", "name": "Trailing-12-month loss", "hard": True,
                      "detail": f"TTM net income {ni / 1e6:,.0f}M"})
    if category == "standard":
        tgt = rules.get("r10", {}).get("target", 4.0)
        if ni and ni > 0 and ltd is not None and ltd / ni > tgt:
            flags.append({"id": "debt_payback_ttm", "name": "Debt payback (quarterly)", "hard": True,
                          "detail": f"LT debt = {ltd / ni:.1f} yrs of TTM earnings (limit {tgt:g})"})
        floor = rules.get("r19", {}).get("min_year", 0.10)
        if ni is not None and eq and eq0 and (eq + eq0) > 0:
            roe = ni / ((eq + eq0) / 2)
            m["ttm_roe"] = roe
            if roe < floor:
                flags.append({"id": "roe_ttm", "name": "Return on equity (quarterly)", "hard": True,
                              "detail": f"TTM ROE {roe:.1%} < {floor:.0%}"})
        if ni and ni > 0 and ocf is not None and ocf / ni < s.get("cash_vs_profit_min", 0.5):
            flags.append({"id": "cash_vs_profit", "name": "Profits far above cash flow", "hard": False,
                          "detail": f"TTM operating cash flow is {ocf / ni:.0%} of net income"})
    elif category == "bank":
        floor = rules.get("b1", {}).get("floor", 0.005)
        if ni is not None and ta and ta0:
            roa = ni / ((ta + ta0) / 2)
            m["ttm_roa"] = roa
            if roa < floor:
                flags.append({"id": "roa_ttm", "name": "Return on assets (quarterly)", "hard": True,
                              "detail": f"TTM ROA {roa:.2%} < {floor:.2%}"})
    elif category == "insurer":
        floor = rules.get("i3", {}).get("floor", 0.06)
        if ni is not None and eq and eq0 and (eq + eq0) > 0:
            roe = ni / ((eq + eq0) / 2)
            m["ttm_roe"] = roe
            if roe < floor:
                flags.append({"id": "roe_ttm", "name": "Return on equity (quarterly)", "hard": True,
                              "detail": f"TTM ROE {roe:.1%} < {floor:.0%}"})
    if sh and sh0 and sh / sh0 - 1 > s.get("dilution_max", 0.05):
        flags.append({"id": "dilution", "name": "Sudden dilution", "hard": False,
                      "detail": f"diluted shares up {sh / sh0 - 1:.0%} vs a year earlier"})
    if category == "standard" and s.get("deterioration_warnings", True):
        try:
            flags += deterioration(st, E, s)
        except Exception:  # a data oddity in one company must never stop the run
            pass
    return flags, m


def build(profile: Profile, ciks: list[int], since: str | None = None, latest_only: bool = False,
          progress=None) -> int:
    cats = {r["cik"]: (r["category"] or "standard") for r in db.rows("SELECT cik, category FROM securities")}
    con = db.get()
    n = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for k, cik in enumerate(ciks):
            cat = cats.get(cik, "standard")
            if cat == "excluded":
                continue
            qrows = db.rows("SELECT item, start, end, value, filed FROM qfacts WHERE cik=? ORDER BY filed", (cik,))
            if not qrows:
                continue
            arows = db.rows("SELECT item, fy_end, value, filed FROM facts WHERE cik=? ORDER BY filed", (cik,))
            events = sorted([(r["filed"], 0, r) for r in arows] + [(r["filed"], 1, r) for r in qrows],
                            key=lambda x: (x[0], x[1]))
            qfiled = sorted({r["filed"] for r in qrows})
            if latest_only:
                targets = set(qfiled[-1:])
            elif since:
                targets = {f for f in qfiled if f >= since}
            else:
                targets = set(qfiled)
            st = _State()
            i = 0
            for F in qfiled:
                while i < len(events) and events[i][0] <= F:
                    _, kind, r = events[i]
                    if kind == 0:
                        st.put_a(r["item"], r["fy_end"], r["value"])
                    else:
                        st.put_q(r["item"], r["start"], r["end"], r["value"])
                    i += 1
                if F not in targets:
                    continue
                ends = [r["end"] for r in qrows if r["filed"] == F and r["item"] == "net_income" and r["start"]]
                if not ends:
                    continue
                E = max(ends)
                flags, m = check(st, E, cat, profile)
                con.execute("INSERT OR REPLACE INTO qsnaps(cik, filed, period_end, flags_json, metrics_json) "
                            "VALUES(?,?,?,?,?)", (cik, F, E, json.dumps(flags), json.dumps(m)))
            n += 1
            if n % 200 == 0:
                con.execute("COMMIT")
                con.execute("BEGIN IMMEDIATE")
            if progress and k % 25 == 0:
                progress(k / max(len(ciks), 1))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n

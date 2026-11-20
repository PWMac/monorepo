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
    return flags, m


def build(profile: Profile, ciks: list[int], since: str | None = None, latest_only: bool = False,
          progress=None) -> int:
    cats = {r["cik"]: (r["category"] or "standard") for r in db.rows("SELECT cik, category FROM securities")}
    con = db.get()
    n = 0
    con.execute("BEGIN")
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
                con.execute("BEGIN")
            if progress and k % 25 == 0:
                progress(k / max(len(ciks), 1))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n

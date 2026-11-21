"""Profiles (rule sets) and quality scoring."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

import yaml

from . import db, paths
from .metrics import MetricResult

QUALITY_CATEGORIES = ["profitability", "returns", "balance", "cash", "shareholder"]


# ------------------------------------------------------------------ profiles
def load_yaml_profile(name: str = "oracle_classic") -> dict:
    with open(paths.RULES_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def seed_profiles() -> None:
    if db.scalar("SELECT COUNT(*) FROM profiles", default=0):
        _seed_timing()
        _seed_risk_optimized()
        _seed_quality_core()
        refresh_notes()
        return
    classic = load_yaml_profile()
    _insert("Oracle Classic", classic, locked=True, active=True, notes=classic.get("description"))
    b15 = copy.deepcopy(classic)
    b15["settings"]["hurdle"] = 0.15
    b15["description"] = "Stricter: the Buffettology 15% expected-return bar."
    _insert("Buffettology 15%", b15, locked=True, active=False, notes=b15["description"])
    relaxed = copy.deepcopy(classic)
    relaxed["settings"]["lookback_years"] = 5
    relaxed["settings"]["min_years"] = 5
    relaxed["description"] = "5-year lookbacks so backtests can start earlier (less Oracle-faithful)."
    _insert("Relaxed backfill (5y)", relaxed, locked=True, active=False, notes=relaxed["description"])
    _seed_timing()
    _seed_risk_optimized()
    _seed_quality_core()
    refresh_notes()


TIMING_NAME = "Oracle Classic + timing"


def _seed_timing() -> None:
    """Same rules as Oracle Classic, but new buys wait while the price is sliding. Backtest it against Classic."""
    if db.scalar("SELECT COUNT(*) FROM profiles WHERE name=?", (TIMING_NAME,), 0):
        return
    t = load_yaml_profile()
    t["settings"]["timing_filter"] = True
    t["description"] = ("Oracle Classic, but a BUY waits while the price is below a falling 50-day average "
                        "(up to 60 signal days). Run a backtest against Classic to see if it helps.")
    _insert(TIMING_NAME, t, locked=True, active=False, notes=t["description"])


RISK_NAME = "Oracle Edge 15% · Risk Optimized"
RISK_SETTINGS = {
    "hurdle": 0.15, "portfolio_mode": "focused", "max_holdings": 7, "new_money": "reserve", "fund_strong": False,
    "sizing": "value_quality", "max_position": 0.35, "max_sector": 0.50, "max_correlated": 0.60,
    "trade_cost_bps": 10,
}


def _seed_risk_optimized() -> None:
    """Buffettology 15% with 7 holdings and the sliding reserve, plus value-and-quality sizing and the exposure
    limits. The market overlay and the enhanced valuation stay off until the experiments show they help."""
    if db.scalar("SELECT COUNT(*) FROM profiles WHERE name=?", (RISK_NAME,), 0):
        return
    src = db.one("SELECT body_json FROM profiles WHERE name=?", ("Buffettology 15% · best 7",))
    body = _with_defaults(json.loads(src["body_json"])) if src else load_yaml_profile()
    body["settings"].update(RISK_SETTINGS)
    body["description"] = ("Buffettology 15%, 7 holdings, sliding reserve; better businesses at better prices get "
                           "more money; at most 35% in one stock, 50% in one sector, 60% in closely correlated stocks.")
    _insert(RISK_NAME, body, locked=False, active=False, notes=body["description"])


CORE_NAME = "Buffettology Quality Core"
CORE_SETTINGS = {
    "health_check": True, "price_test": "fair_pe", "fair_pe_mult": 1.5, "max_mos": 0.40, "max_holdings": 25,
    "sizing": "equal", "new_money": "spread", "max_position": 0.10, "fund_strong": False, "trade_cost_bps": 10,
}


def _seed_quality_core() -> None:
    """Buffett's portfolio principles as tested in November 2026 (Guide, "Rebuilding the strategy"): wonderful
    businesses that are still healthy now, bought at a fair price against their own history rather than at the
    deepest discount, 25 holdings, held while the business stays wonderful and healthy."""
    if db.scalar("SELECT COUNT(*) FROM profiles WHERE name=?", (CORE_NAME,), 0):
        return
    src = db.one("SELECT body_json FROM profiles WHERE name=?", ("Buffettology 15% · best 7",))
    body = _with_defaults(json.loads(src["body_json"])) if src else load_yaml_profile()
    body["settings"].update(CORE_SETTINGS)
    body["description"] = PURPOSE[CORE_NAME]
    _insert(CORE_NAME, body, locked=False, active=False, notes=PURPOSE[CORE_NAME])


def _insert(name, body, locked=False, active=False, parent_id=None, notes=None) -> int:
    cur = db.get().execute(
        "INSERT INTO profiles(name,parent_id,version,locked,active,body_json,notes,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (name, parent_id, 1, int(locked), int(active), json.dumps(body), notes, db.now(), db.now()),
    )
    return cur.lastrowid


def save_as(source_id: int, new_name: str, body: dict | None = None) -> int:
    src = get_profile(source_id)
    body = body or src.body
    # no note: the card then says what differs from the profile it was copied from
    return _insert(new_name, body, locked=False, active=False, parent_id=source_id, notes=None)


def set_notes(pid: int, text: str | None) -> None:
    """The profile's purpose line, written by the user. Blank = describe it automatically."""
    db.get().execute("UPDATE profiles SET notes=?, updated_at=? WHERE id=?", ((text or "").strip() or None, db.now(), pid))


# ------------------------------------------------------------------ descriptions
# Purpose lines for the built-in profiles. A user-written note is never overwritten: only these names, and
# only when the note is still empty or one of the app's own older defaults.
PURPOSE = {
    "Oracle Classic":
        "The baseline: the Oracle's rules of thumb applied as written. Locked, so there is always a reference "
        "to compare every other profile against.",
    "Buffettology 15%":
        "Oracle Classic with a higher bar: buys only when the expected 10-year return is at least 15% (the "
        "Buffettology test), so fewer stocks qualify and they have to be cheaper.",
    "Relaxed backfill (5y)":
        "For research: judges companies on 5 years of results instead of 10, so backtests can start in earlier "
        "years. Less strict than the Oracle's rules, so expect more and weaker buys. Not meant to trade on.",
    "Oracle Classic + timing":
        "Oracle Classic with a timing filter: a BUY waits while the price is still sliding (below a falling "
        "50-day average), for up to 60 signal days. Backtest it against Classic to see whether waiting helps.",
    "Buffettology 15% · best 7":
        "The live profile: Buffettology 15% concentrated in the best 7 stocks. Chosen on backtests that the "
        "November 2026 audit showed were inflated; see the Guide, \"Rebuilding the strategy\".",
    "Buffettology Quality Core":
        "Buffett's portfolio principles, rebuilt after the November 2026 tests: wonderful businesses that are "
        "still growing now (no value traps), bought at a fair P/E for that company rather than the deepest "
        "discount, 25 holdings, kept while the business stays wonderful and healthy. In the tests it fell no further "
        "than the S&P 500 but trailed it by 2 to 5 points a year; read the Guide before trading on it.",
    "Oracle Edge 15% · Risk Optimized":
        "Buffettology 15% with risk limits: 7 holdings, a cash reserve for strong buys, bigger slices for better "
        "businesses at better prices, and caps of 35% per stock, 50% per sector and 60% in stocks that move "
        "together.",
}
_OLD_DEFAULTS = ("Copy of ", "Buffett's rules of thumb", "The Oracle's rules of thumb, 10-year",
                 "Stricter: Mary Buffett", "Stricter: the Buffettology 15%", "5-year lookbacks so backtests",
                 "Oracle Classic, but a BUY waits", "Buffettology 15%, 7 holdings, sliding reserve",
                 "The live profile: Buffettology 15% concentrated in the best 7 stocks. Chosen after")


def _is_default_note(text: str | None) -> bool:
    return not text or any(text.startswith(x) for x in _OLD_DEFAULTS)


def refresh_notes() -> None:
    """Replace the app's own old placeholder notes ("Copy of …", the original one-liners) with better ones.
    Notes the user wrote are left alone."""
    for r in db.rows("SELECT id, name, notes FROM profiles"):
        if not _is_default_note(r["notes"]):
            continue
        new = PURPOSE.get(r["name"])
        if (new or None) != r["notes"]:
            db.get().execute("UPDATE profiles SET notes=? WHERE id=?", (new, r["id"]))


def _pct(v) -> str:
    return f"{float(v) * 100:.0f}%"


_SIZING = {"equal": "equal slices", "conviction": "bigger slices for a higher Oracle Rank",
           "value_quality": "bigger slices for better businesses at better prices",
           "size": "bigger slices for bigger companies"}
_NEW_MONEY = {"best_value": "new money to the best value", "best_rank": "new money to the best Oracle Rank",
              "spread": "new money spread evenly", "patient": "new money waits for strong-buy prices",
              "reserve": "part of new money kept in reserve for strong buys"}


def describe(body: dict) -> str:
    """What a profile does, in one plain paragraph, built from its settings so it is always accurate."""
    st = body.get("settings", {})
    if st.get("price_test") == "fair_pe":
        out = [f"Buys when Quality is at least {st.get('quality_min', 70):.0f} on {st.get('lookback_years', 10)} years "
               f"of results and the P/E is at most {float(st.get('fair_pe_mult', 1.5)):g} times the company's own "
               "usual P/E (a fair price, not a bargain hunt)."]
    else:
        out = [f"Buys when Quality is at least {st.get('quality_min', 70):.0f} on {st.get('lookback_years', 10)} years "
               f"of results, the price is {_pct(st.get('mos_gate', 0.25))} or more below value and the expected "
               f"10-year return is at least {_pct(st.get('hurdle', 0.10))}."]
    if st.get("portfolio_mode", "focused") == "focused":
        out.append(f"Holds the best {st.get('max_holdings', 15)} ({_SIZING.get(st.get('sizing'), 'equal slices')}); "
                   f"{_NEW_MONEY.get(st.get('new_money'), 'new money to the best value')}.")
    else:
        out.append("Variable portfolio (the old style): buys every Buy Zone stock.")
    extra = []
    if st.get("timing_filter"):
        extra.append(f"buys wait while the price is sliding (up to {st.get('timing_max_wait', 60)} days)")
    caps = [f"{_pct(st[k])} {w}" for k, w in (("max_position", "per stock"), ("max_sector", "per sector"),
                                               ("max_correlated", "in stocks that move together")) if st.get(k)]
    if caps:
        extra.append("at most " + ", ".join(caps))
    if st.get("fund_strong"):
        extra.append("trims weaker holdings to fund a strong buy")
    if st.get("market_overlay"):
        extra.append("buys less while the S&P 500 is below its 200-day average")
    if st.get("regime_modifier"):
        extra.append("asks a bigger margin of safety when the market is expensive")
    if st.get("valuation_model") == "enhanced":
        extra.append("enhanced valuation")
    if st.get("predictability_mos"):
        extra.append("bigger margin of safety for less predictable earnings")
    if st.get("health_check"):
        extra.append("skips businesses whose sales, profits or returns are slipping right now and sells when the "
                     "slide is confirmed")
    if st.get("max_mos") is not None:
        extra.append(f"won't buy more than {_pct(st['max_mos'])} below the estimated value (too cheap to trust)")
    if st.get("min_rank_score"):
        extra.append(f"Oracle Rank of {st['min_rank_score']:.0f}+ to buy")
    if extra:
        out.append(extra[0][0].upper() + "; ".join(extra)[1:] + ".")
    return " ".join(out)


_DIFF_KEYS = [  # (setting, label, format)
    ("hurdle", "expected-return bar", "pct"), ("mos_gate", "margin of safety", "pct"),
    ("quality_min", "Quality bar", "num"), ("lookback_years", "years of results", "num"),
    ("min_years", "minimum years", "num"), ("portfolio_mode", "portfolio style", "txt"),
    ("max_holdings", "holdings", "num"), ("sizing", "sizing", "sizing"), ("new_money", "new money", "money"),
    ("max_position", "max per stock", "pct"), ("max_sector", "max per sector", "pct"),
    ("max_correlated", "max in correlated stocks", "pct"), ("fund_strong", "fund strong buys", "bool"),
    ("timing_filter", "timing filter", "bool"), ("market_overlay", "market overlay", "bool"),
    ("regime_modifier", "bigger margin when expensive", "bool"), ("valuation_model", "valuation", "txt"),
    ("min_rank_score", "minimum Oracle Rank", "num"), ("include_foreign", "foreign filers", "bool"),
    ("initial_capital", "starting money", "usd"), ("weekly_contribution", "weekly amount", "usd"),
    ("trade_cost_bps", "trading cost (bps)", "num"), ("rank_quality_weight", "Quality weight in rank", "num"),
    ("health_check", "business health check", "bool"), ("price_test", "price test", "txt"),
    ("fair_pe_mult", "fair P/E (x usual)", "num"), ("max_mos", "too-cheap guard", "pct"),
]


def _fmt(v, f) -> str:
    if v is None:
        return "off"
    if f == "pct":
        return _pct(v)
    if f == "bool":
        return "on" if v else "off"
    if f == "usd":
        return f"${float(v):,.0f}"
    if f == "num":
        return f"{float(v):g}"
    if f == "sizing":
        return _SIZING.get(v, str(v))
    if f == "money":
        return {"best_value": "best value", "best_rank": "best Oracle Rank", "spread": "spread evenly",
                "patient": "patient", "reserve": "sliding reserve"}.get(v, str(v))
    return str(v)


def differences(body: dict, base: dict) -> list[str]:
    """Plain-language list of what differs from another profile ("holdings 7 (was 15)")."""
    a, b = body.get("settings", {}), base.get("settings", {})
    out = []
    for k, label, f in _DIFF_KEYS:
        if _tidy(a.get(k)) != _tidy(b.get(k)):
            out.append(f"{label} {_fmt(a.get(k), f)} (was {_fmt(b.get(k), f)})")
    n = sum(1 for book in ("rules", "bank_rules", "insurer_rules")
            for x, y in zip(body.get(book, []), base.get(book, []))
            if _tidy({k: x.get(k) for k in ("mode", "floor", "target", "excellent", "weight")})
            != _tidy({k: y.get(k) for k in ("mode", "floor", "target", "excellent", "weight")}))
    if n:
        out.append(f"{n} rule{'s' if n > 1 else ''} changed")
    return out


def parent_of(pid: int) -> "Profile | None":
    r = db.one("SELECT parent_id FROM profiles WHERE id=?", (pid,))
    try:
        return get_profile(r["parent_id"]) if r and r["parent_id"] else None
    except KeyError:
        return None


def update_profile(pid: int, body: dict) -> None:
    """Save a profile. The version (which keys the company scores) only goes up when a scoring rule changed."""
    p = get_profile(pid)
    if p.locked:
        raise ValueError("This profile is locked. Use Save as to edit a copy.")
    bump = 1 if scoring_hash(body) != scoring_hash(p.body) else 0
    db.get().execute(
        "UPDATE profiles SET body_json=?, version=version+?, updated_at=? WHERE id=?",
        (json.dumps(body), bump, db.now(), pid)
    )


@dataclass
class Profile:
    id: int
    name: str
    version: int
    locked: bool
    active: bool
    body: dict
    notes: str = ""

    @property
    def settings(self) -> dict:
        return self.body["settings"]

    @property
    def rules(self) -> list[dict]:
        return self.body["rules"]

    def rulebook(self, category: str) -> list[dict]:
        """Rules for a company category: standard, bank or insurer."""
        if category == "bank":
            return self.body["bank_rules"]
        if category == "insurer":
            return self.body["insurer_rules"]
        return self.body["rules"]

    def all_rules(self) -> list[dict]:
        return self.body["rules"] + self.body["bank_rules"] + self.body["insurer_rules"]

    @property
    def snap(self) -> tuple[int, int]:
        """(profile_id, version) whose company scores this profile uses. Profiles that differ only in portfolio,
        signal or alert settings share scores, so trying another number of holdings never re-scores
        every company."""
        if getattr(self, "_snap", None) is None:
            self._snap = _snap_key(self)
        return self._snap


# Settings that change company scores (snapshots). Everything else (portfolio size, sizing, signal ladder,
# alerts, timing) only affects decisions made later, so changing it never needs a re-score.
SCORING_SETTINGS = ("lookback_years", "min_years", "near_miss_margin")


def _tidy(x):
    """Round floats so editor round-trips (0.07 -> 7 -> 0.07000000000000001) don't look like rule changes."""
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):  # 3 and 3.0 are the same threshold
        return float(f"{float(x):.9g}")
    if isinstance(x, dict):
        return {k: _tidy(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_tidy(v) for v in x]
    return x


def scoring_hash(body: dict) -> str:
    core = {k: body.get(k) for k in ("rules", "bank_rules", "insurer_rules", "category_weights")}
    core["settings"] = {k: body.get("settings", {}).get(k) for k in SCORING_SETTINGS}
    return hashlib.sha1(json.dumps(_tidy(core), sort_keys=True, default=str).encode()).hexdigest()


def _has_snaps(pid: int, version: int) -> bool:
    return bool(db.one("SELECT 1 FROM snapshots WHERE profile_id=? AND profile_version=? LIMIT 1", (pid, version)))


def _snap_key(p: "Profile") -> tuple[int, int]:
    if _has_snaps(p.id, p.version):
        return p.id, p.version
    h = scoring_hash(p.body)
    for r in db.rows("SELECT id, version, body_json FROM profiles WHERE id != ? ORDER BY id", (p.id,)):
        if scoring_hash(_with_defaults(json.loads(r["body_json"]))) == h and _has_snaps(r["id"], r["version"]):
            return r["id"], r["version"]
    return p.id, p.version


def _with_defaults(body: dict) -> dict:
    """Profiles saved by older versions get any new rulebooks/settings from the YAML defaults."""
    base = load_yaml_profile()
    for k in ("bank_rules", "insurer_rules"):
        if k not in body:
            body[k] = copy.deepcopy(base[k])
    for k, v in base["settings"].items():
        body["settings"].setdefault(k, v)
    for r in body.get("rules", []):  # wording from before the ROE floor looked at the last 5 years only
        if r.get("id") == "r19" and "(10-yr avg; no year < 10%)" in (r.get("formula") or ""):
            r["formula"] = r["formula"].replace("(10-yr avg; no year < 10%)", "(10-yr avg; no year < 10% in the last 5)")
    return body


def get_profile(pid: int) -> Profile:
    r = db.one("SELECT * FROM profiles WHERE id=?", (pid,))
    if not r:
        raise KeyError(pid)
    return Profile(r["id"], r["name"], r["version"], bool(r["locked"]), bool(r["active"]),
                   _with_defaults(json.loads(r["body_json"])), r["notes"] or "")


def list_profiles() -> list[Profile]:
    return [get_profile(r["id"]) for r in db.rows("SELECT id FROM profiles ORDER BY id")]


def active_profiles() -> list[Profile]:
    return [p for p in list_profiles() if p.active]


def default_profile() -> Profile:
    act = active_profiles()
    return act[0] if act else list_profiles()[0]


# ------------------------------------------------------------------ scoring
def level_score(v: float | None, rule: dict) -> float | None:
    if v is None:
        return None
    f, t, e = rule["floor"], rule["target"], rule["excellent"]
    d = rule["direction"]
    if d == "range":
        lo, hi, ceil = t, e, rule.get("ceiling", e * 1.5)
        if lo <= v <= hi:
            return 100.0
        if v < lo:
            return max(0.0, 100.0 * (v - f) / (lo - f)) if lo != f else 0.0
        return max(0.0, 100.0 * (ceil - v) / (ceil - hi)) if ceil != hi else 0.0
    if d == "lower":  # flip to "higher is better"
        v, f, t, e = -v, -f, -t, -e
    if v <= f:
        return 0.0
    if v >= e:
        return 100.0
    if v <= t:
        return 70.0 * (v - f) / (t - f) if t != f else 70.0
    return 70.0 + 30.0 * (v - t) / (e - t) if e != t else 100.0


def meets_target(v: float | None, rule: dict) -> bool | None:
    if v is None:
        return None
    d = rule["direction"]
    if d == "range":
        return rule["target"] <= v <= rule["excellent"]
    return v >= rule["target"] if d == "higher" else v <= rule["target"]


def gate_margin(v: float | None, rule: dict) -> float | None:
    """Relative distance from the gate threshold (positive = passing)."""
    if v is None:
        return None
    t = rule["target"]
    denom = abs(t) if t else 1.0
    return (v - t) / denom if rule["direction"] != "lower" else (t - v) / denom


@dataclass
class RuleResult:
    id: str
    name: str
    category: str
    mode: str
    value: float | None
    score: float | None
    passed: bool | None
    consistency: float | None
    series: list = field(default_factory=list)
    years: list = field(default_factory=list)
    detail: str = ""

    def to_dict(self):
        return self.__dict__


ROE_FLOOR_WINDOW = 5


def roe_floor_ok(series, rule: dict) -> bool:
    """The return-on-equity gate's yearly floor (min_year, e.g. 10%) applies to the most recent min_year_window
    years only (default 5). Tested on the app's history (2012-2022 company-years, ROE averaging 15%+): companies
    whose only weak year was 6-10 years back did as well over the next 3 years as those with no weak year at all
    (22% vs 20% went on to compound, median ROE 24% for both); a weak year in the last 3 was a real warning
    (11% compounded, median ROE 10%); one 4-5 years back was in between (21% compounded, but 30% saw ROIC fall
    under 10%), so 5 years is the cautious choice."""
    n = int(rule.get("min_year_window") or ROE_FLOOR_WINDOW)
    recent = [x for x in list(series)[-n:] if x is not None]
    return not recent or min(recent) >= rule.get("min_year", 0)


def score_rule(rule: dict, m: MetricResult) -> RuleResult:
    v = m.value
    lvl = level_score(v, rule)
    if m.ok_years is not None:
        cons = m.ok_years
    else:
        flags = [meets_target(x, rule) for x in m.series if x is not None]
        flags = [f for f in flags if f is not None]
        cons = (sum(flags) / len(flags)) if flags and rule["metric"] not in ("dollar_test", "has_preferred") else None
    if lvl is None:
        score = None
    elif cons is None:
        score = lvl
    else:
        score = 0.6 * lvl + 0.4 * 100 * cons
    # trend penalty for margins that are falling
    if rule["metric"] == "margin_stability" and getattr(m, "trend", 0) < -0.005 and score is not None:
        score = max(0.0, score - 10)
    passed = meets_target(v, rule)
    if rule["metric"] == "roe" and passed and rule.get("min_year") is not None:
        passed = roe_floor_ok(m.series, rule)
    series = [None if (x is None) else round(float(x), 4) for x in m.series][-10:]
    return RuleResult(rule["id"], rule["name"], rule["category"], rule["mode"], v, score, passed, cons, series,
                      m.years[-10:], m.detail)


def quality_from(results: list[RuleResult], weights: dict, rules_by_id: dict) -> tuple[float | None, dict]:
    cats: dict[str, list[tuple[float, float]]] = {}
    for r in results:
        if r.mode not in ("gate", "scored") or r.category not in QUALITY_CATEGORIES or r.score is None:
            continue
        w = rules_by_id[r.id].get("weight", 1.0)
        cats.setdefault(r.category, []).append((r.score, w))
    cat_scores = {c: sum(s * w for s, w in v) / sum(w for _, w in v) for c, v in cats.items() if sum(w for _, w in v)}
    tw = sum(weights.get(c, 0) for c in cat_scores)
    if not tw:
        return None, cat_scores
    q = sum(cat_scores[c] * weights.get(c, 0) for c in cat_scores) / tw
    return q, cat_scores


def confidence(s: dict, results: list[RuleResult], data_years: int, lookback: int) -> float:
    c = 1.0
    if s["years"]:
        for k in ("revenue", "net_income", "equity", "ocf", "capex", "shares_diluted", "gross_profit"):
            if s[k][-1] is None:
                c -= 0.03
    c -= 0.02 * max(0, lookback - data_years)
    conv = next((r for r in results if r.id == "r18"), None)
    if conv and conv.value is not None and conv.value < 0.7:
        c -= 0.05
    return round(max(0.85, min(1.0, c)), 3)

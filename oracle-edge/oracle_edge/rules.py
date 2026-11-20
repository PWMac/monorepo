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
        return
    classic = load_yaml_profile()
    _insert("Oracle Classic", classic, locked=True, active=True, notes=classic.get("description"))
    b15 = copy.deepcopy(classic)
    b15["settings"]["hurdle"] = 0.15
    b15["description"] = "Stricter: Mary Buffett's 15% expected-return bar."
    _insert("Buffettology 15%", b15, locked=True, active=False, notes=b15["description"])
    relaxed = copy.deepcopy(classic)
    relaxed["settings"]["lookback_years"] = 5
    relaxed["settings"]["min_years"] = 5
    relaxed["description"] = "5-year lookbacks so backtests can start earlier (less Buffett-faithful)."
    _insert("Relaxed backfill (5y)", relaxed, locked=True, active=False, notes=relaxed["description"])
    _seed_timing()


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
    return _insert(new_name, body, locked=False, active=False, parent_id=source_id, notes=f"Copy of {src.name}")


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
    if rule["metric"] == "roe" and passed:
        my = getattr(m, "min_year", None)
        passed = my is None or my >= rule.get("min_year", 0)
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

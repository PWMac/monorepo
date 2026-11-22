"""Score movers: the companies whose Wonderfulness moved most with an annual report filed in the last week (or
month), for the Today page.

A company's scores change when it files a new annual report. The change shown is like for like: this fiscal year's
Wonderfulness against last fiscal year's (each ranked within its own year's cohort), and the Oracle checklist's
Quality from this report against the one before. The pillar that moved most says why."""

from __future__ import annotations

import json
from datetime import date, timedelta

from . import db, rules

PILLARS = {"moat": "moat", "growth": "growth", "strength": "financial strength", "allocation": "capital allocation"}


def movers(profile=None, days: int = 7, n: int = 7, end: str | None = None, min_change: float = 1.0) -> dict:
    p = profile or rules.default_profile()
    pid = p.snap[0]
    end = end or db.scalar("SELECT MAX(filed) FROM wonder WHERE profile_id=? AND cohort='now'", (pid,)) or \
        date.today().isoformat()
    since = (date.fromisoformat(end[:10]) - timedelta(days=days)).isoformat()
    now = db.rows("SELECT w.cik, w.as_of, w.filed, w.score, s.ticker, s.name FROM wonder w JOIN securities s ON s.cik=w.cik "
                  "WHERE w.profile_id=? AND w.cohort='now' AND w.filed>? AND w.filed<=? AND s.ticker IS NOT NULL "
                  "AND COALESCE(s.active, 1)=1", (pid, since, end))
    out = []
    for r in now:
        fy = db.rows("SELECT cohort, as_of, score, pillars_json FROM wonder WHERE cik=? AND profile_id=? AND cohort LIKE 'FY%' "
                     "ORDER BY as_of DESC LIMIT 3", (r["cik"], pid))
        cur = next((x for x in fy if x["as_of"] == r["as_of"]), None)
        prev = next((x for x in fy if x["as_of"] < r["as_of"]), None)
        if not cur or not prev or cur["score"] is None or prev["score"] is None:
            continue
        dw = cur["score"] - prev["score"]
        pc, pp = json.loads(cur["pillars_json"] or "{}"), json.loads(prev["pillars_json"] or "{}")
        # the pillar that moved most in the same direction as the score
        moves = sorted(((k, (pc.get(k) or 0) - (pp.get(k) or 0)) for k in PILLARS if k in pc and k in pp),
                       key=lambda kv: -kv[1] if dw >= 0 else kv[1])
        why = moves[0] if moves and abs(moves[0][1]) >= 1 else None
        q = db.rows("SELECT filed, quality FROM snapshots WHERE cik=? AND profile_id=? AND profile_version=? "
                    "ORDER BY filed DESC LIMIT 2", (r["cik"], *p.snap))
        dq = (q[0]["quality"] - q[1]["quality"]) if len(q) == 2 and q[0]["quality"] is not None and \
            q[1]["quality"] is not None else None
        out.append({"cik": r["cik"], "ticker": r["ticker"], "name": r["name"], "filed": r["filed"],
                    "score": r["score"], "fy_score": cur["score"], "prev": prev["score"], "change": dw,
                    "quality": q[0]["quality"] if q else None, "q_change": dq,
                    "why": (f"{PILLARS[why[0]]} {why[1]:+.0f}" if why else "")})
    up = sorted([x for x in out if x["change"] >= min_change], key=lambda x: -x["change"])[:n]
    down = sorted([x for x in out if x["change"] <= -min_change], key=lambda x: x["change"])[:n]
    return {"up": up, "down": down, "since": since, "end": end, "filed": len(now), "compared": len(out)}

"""Exit rules (0.19): when a strategy trims, sells, switches or warns about a holding.

A strategy keeps a list of rules in settings["exit_rules"]. Each rule is a dict:

    {"on": True, "when": <trigger id>, "x": <threshold>, "y": <second threshold or None>,
     "do": "sell" | "trim" | "trim_to" | "switch" | "warn", "amount": <fraction to trim / weight to trim to>,
     "confirm": "now" | "checks" | "filing", "checks": <signal days in a row>, "min_days": <days held>}

Strategies saved before 0.19 have no list: legacy_rules() writes the one that reproduces exactly what their
old settings did (sell on a broken thesis or low Quality, REVIEW then sell when the next filing confirms,
nose-bleed and oversize trims, swaps; or, for top-ranked buying, sell below rank K or on shrinking sales).

How a holding is checked each signal day (PortfolioSim.apply_exits):
  1. sell and switch rules confirmed "now" or after N "checks", in list order: the first that fires sells it;
  2. "filing" rules and "warn" rules together, as the old REVIEW did: any warning puts the holding under REVIEW;
     a filing rule that still fires once a newer filing is out carries out its action;
  3. trim rules, in list order: the first that fires trims, once.
A holding acted on (or under REVIEW) is not checked further that day.
"""

from __future__ import annotations

import copy

# trigger id -> label, unit of x ("num", "pct" fraction, "x" multiple, "rank", "pts", "days", None), what y means,
# group, default x, plain explanation
TRIGGERS: dict[str, dict] = {
    # ---- scores
    "quality_below": {"group": "Scores", "label": "Quality below", "unit": "num", "x": 55,
                      "help": "Today's Quality score is under X."},
    "quality_drop": {"group": "Scores", "label": "Quality down since bought (pts)", "unit": "pts", "x": 10,
                     "help": "Quality has fallen X points or more since the first purchase."},
    "wonder_below": {"group": "Scores", "label": "Wonderfulness below", "unit": "num", "x": 60,
                     "help": "Wonderfulness, as of the latest annual report, is under X."},
    "wonder_drop": {"group": "Scores", "label": "Wonderfulness down since bought (pts)", "unit": "pts", "x": 10,
                    "help": "Wonderfulness has fallen X points or more since the first purchase."},
    "combined_below": {"group": "Scores", "label": "Combined score below", "unit": "num", "x": 75,
                       "help": "(Wonderfulness + Quality) ÷ 2 is under X."},
    "rank_below": {"group": "Scores", "label": "Ranks below # by combined score", "unit": "rank", "x": 8,
                   "help": "Among every scored company, it ranks below #X by (Wonderfulness + Quality) ÷ 2."},
    "fails_rules": {"group": "Scores", "label": "Fails a must-pass rule", "unit": None,
                    "help": "The latest annual report fails one of the house view's must-pass rules."},
    "near_limit": {"group": "Scores", "label": "A must-pass rule close to its limit", "unit": "num", "x": 10,
                   "help": "A must-pass rule is within X points (its margin score) of failing."},
    # ---- value
    "price_over_value": {"group": "Value", "label": "Price at least × estimated value", "unit": "x", "x": 2.0,
                         "y_label": "and P/E at least × its usual (blank = ignore)",
                         "help": "The price is X times the estimated value or more (and, if Y is set, the P/E is "
                                 "Y times the company's usual P/E or more)."},
    "pe_over_usual": {"group": "Value", "label": "P/E at least × its usual", "unit": "x", "x": 2.0,
                      "help": "The P/E is X times the company's own usual P/E or more."},
    "exp_return_below": {"group": "Value", "label": "Expected 10-yr return below %", "unit": "pct", "x": 0.05,
                         "help": "The return the valuation expects a year from today's price is under X."},
    # ---- the business, from the latest 10-Q
    "q_revenue": {"group": "Business (10-Q)", "label": "Sales shrinking", "unit": None,
                  "help": "The latest 10-Q shows sales over the last 12 months below a year earlier."},
    "q_profits": {"group": "Business (10-Q)", "label": "Profits falling", "unit": None,
                  "help": "The latest 10-Q shows net income over the last 12 months well below a year earlier."},
    "q_loss": {"group": "Business (10-Q)", "label": "Loss over the last 12 months", "unit": None,
               "help": "The latest 10-Q shows a loss over the last 12 months."},
    "q_hard": {"group": "Business (10-Q)", "label": "Any hard 10-Q warning", "unit": None,
               "help": "A hard quarterly warning (a loss, ROE or debt payback out of bounds; with the business health "
                       "check on, also a severe health slide)."},
    "q_soft": {"group": "Business (10-Q)", "label": "Any other 10-Q warning", "unit": None,
               "help": "A soft quarterly warning (margins, receivables, inventory, stock pay, acquisitions…)."},
    # ---- price and portfolio
    "weight_above": {"group": "Price and portfolio", "label": "Share of the portfolio above %", "unit": "pct",
                     "x": None, "help": "The holding is more than X of the portfolio (blank = the automatic limit: "
                                        "the position cap, at least 1.5 equal slices, or the max position plus 5 "
                                        "points)."},
    "gain_above": {"group": "Price and portfolio", "label": "Up from average cost %", "unit": "pct", "x": 1.0,
                   "help": "The price is X or more above the average cost."},
    "loss_below": {"group": "Price and portfolio", "label": "Down from average cost %", "unit": "pct", "x": 0.30,
                   "help": "The price is X or more below the average cost."},
    "trail_stop": {"group": "Price and portfolio", "label": "Down from its high since bought %", "unit": "pct",
                   "x": 0.35, "help": "The price is X or more below its highest close since the first purchase."},
    "below_200d": {"group": "Price and portfolio", "label": "Below its 200-day average", "unit": None,
                   "help": "The price closed below its 200-day average."},
    # ---- switching
    "better_idea": {"group": "Switch", "label": "A stock you don't own is clearly better", "unit": "num", "x": 15,
                    "y_label": "and its expected return at least this much higher (%)",
                    "help": "A Buy Zone stock you don't own beats this, the weakest holding, by X Oracle Rank "
                            "points and Y points of expected return. Only when every slot is full."},
}

ACTIONS = {"sell": "Sell all", "trim": "Trim %", "trim_to": "Trim back to % of portfolio", "switch": "Switch",
           "warn": "Warn only (REVIEW)"}
CONFIRMS = {"now": "At once", "checks": "After N checks in a row", "filing": "When the next filing agrees"}

PRESETS = {
    "classic": "Buffett classic (sell on a broken thesis; REVIEW, then sell if the next filing confirms; trim "
               "nose-bleed prices and oversized holdings; switch for a clearly better idea)",
    "ranked": "Top-ranked, as tested (sell below rank 8 or when sales shrink)",
}


def rule(when, do="sell", x=None, y=None, amount=None, confirm="now", checks=1, min_days=0, on=True) -> dict:
    return {"on": on, "when": when, "x": x, "y": y, "do": do, "amount": amount, "confirm": confirm,
            "checks": checks, "min_days": min_days}


def legacy_rules(st: dict) -> list[dict]:
    """The rule list that does exactly what a strategy's pre-0.19 settings did."""
    if st.get("buy_rule") == "top_ranked" and st.get("portfolio_mode", "focused") == "focused":
        n = int(st.get("ranked_buy_count") or 4)
        out = [rule("rank_below", "sell", x=int(st.get("ranked_sell_outside") or 2 * n))]
        if st.get("ranked_sell_on_revenue", True):
            out.append(rule("q_revenue", "sell"))
        return out
    return [
        rule("fails_rules", "sell"),
        rule("quality_below", "sell", x=st.get("sell_quality", 55)),
        rule("quality_drop", "sell", x=st.get("review_quality_drop", 10), confirm="filing"),
        rule("q_hard", "sell", confirm="filing"),
        rule("q_soft", "warn"),
        rule("near_limit", "warn", x=st.get("review_gate_margin", 10)),
        rule("price_over_value", "trim", x=st.get("nosebleed_iv_mult", 2.0), y=st.get("nosebleed_pe_mult"),
             amount=st.get("trim_fraction", 0.33), confirm="checks", checks=int(st.get("nosebleed_days", 3) or 1)),
        rule("weight_above", "trim_to", x=None, amount=None),
        rule("better_idea", "switch", x=st.get("swap_rank_gap", 15), y=st.get("swap_er_gap", 0.04),
             min_days=int(st.get("swap_min_hold_days", 0) or 0)),
    ]


def preset(name: str, st: dict) -> list[dict]:
    if name == "ranked":
        return legacy_rules({**st, "buy_rule": "top_ranked", "portfolio_mode": "focused"})
    return legacy_rules({**st, "buy_rule": "buy_zone"})


def rules_of(st: dict) -> list[dict]:
    """The strategy's exit rules (its own list, or the one its older settings imply)."""
    r = st.get("exit_rules")
    if r is None:
        return legacy_rules(st)
    return [_clean(x) for x in r if x.get("when") in TRIGGERS]


def _clean(r: dict) -> dict:
    out = rule(r["when"])
    out.update({k: r.get(k, out[k]) for k in out})
    if out["do"] not in ACTIONS:
        out["do"] = "warn"
    if out["confirm"] not in CONFIRMS:
        out["confirm"] = "now"
    return out


def _num(v, unit):
    if v is None:
        return "auto"
    if unit == "pct":
        return f"{float(v) * 100:.0f}%"
    if unit == "x":
        return f"{float(v):g}×"
    if unit == "rank":
        return f"#{int(v)}"
    return f"{float(v):g}"


SAY = {
    "quality_below": "Quality falls below {x}", "quality_drop": "Quality is down {x} points since bought",
    "wonder_below": "Wonderfulness falls below {x}", "wonder_drop": "Wonderfulness is down {x} points since bought",
    "combined_below": "the combined score falls below {x}", "rank_below": "it ranks below {x} by combined score",
    "fails_rules": "it fails a must-pass rule", "near_limit": "a must-pass rule is within {x} points of failing",
    "price_over_value": "the price is {x} the estimated value or more", "pe_over_usual": "the P/E is {x} its usual",
    "exp_return_below": "the expected 10-yr return is below {x}",
    "q_revenue": "the latest 10-Q shows sales shrinking", "q_profits": "the latest 10-Q shows profits falling",
    "q_loss": "the latest 10-Q shows a loss over 12 months", "q_hard": "a hard 10-Q warning is open",
    "q_soft": "any other 10-Q warning is open", "weight_above": "it is more than {x} of the portfolio",
    "gain_above": "it is up {x} from average cost", "loss_below": "it is down {x} from average cost",
    "trail_stop": "it is {x} below its high since bought", "below_200d": "the price is below its 200-day average",
    "better_idea": "a stock you don't own beats it by {x} Oracle Rank points and {y} of expected return",
}


def describe_rule(r: dict) -> str:
    """One plain line: 'Sell all when Quality falls below 55'."""
    t = TRIGGERS.get(r["when"], {"unit": None})
    x = _num(r.get("x"), t.get("unit")) if r.get("x") is not None else "the automatic limit"
    if r["when"] == "better_idea":
        cond = SAY["better_idea"].format(x=f"{float(r.get('x') or 0):g}", y=f"{float(r.get('y') or 0) * 100:.0f}%")
    else:
        cond = SAY.get(r["when"], r["when"]).format(x=x)
        if r["when"] == "price_over_value" and r.get("y") is not None:
            cond += f" and the P/E is {float(r['y']):g}× its usual"
        if r["when"] == "pe_over_usual":
            cond += " or more"
    act = {"sell": "Sell all", "switch": "Switch for the better stock", "warn": "Warn (REVIEW)",
           "trim": f"Trim {float(r.get('amount') or 0.33) * 100:.0f}%",
           "trim_to": ("Trim back to " + (f"{float(r['amount']) * 100:.0f}% of the portfolio" if r.get("amount")
                                          else "the automatic target"))}[r["do"]]
    conf = {"now": "", "checks": f", after {int(r.get('checks') or 1)} checks in a row",
            "filing": ", once the next filing agrees (REVIEW until then)"}[r.get("confirm", "now")]
    held = f", held at least {int(r['min_days'])} days" if r.get("min_days") else ""
    return f"{act} when {cond}{conf}{held}"


def copy_rules(rs: list[dict]) -> list[dict]:
    return copy.deepcopy(rs)

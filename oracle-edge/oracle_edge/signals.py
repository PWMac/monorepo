"""Signal ladder + model portfolio + episodes + buy-zone stints.

Badges: STRONG_BUY, BUY, ADD, HOLD, WATCH, REVIEW, SELL, SWAP, TRIM.
Signals are generated after a close and filled at the NEXT trading day's open.

Two portfolio modes:
* focused (default): one pot of money (starts at 1.0, all cash). Holds at most N stocks, the best-ranked ones.
  New buys and top-ups are paid from cash; sells and trims return cash, which is reinvested in the best
  holdings still in the Buy Zone or the next best candidate. When full, a much better idea can SWAP out the
  weakest holding (owned at least swap_min_hold_days).
* variable (legacy): every BUY is new money, every SELL takes it out; no limit unless one is set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import db
from . import exits
from .engine import Eval, PriceBook
from .rules import Profile

BENCH = "SPY"
REENTRY_DAYS = 30  # a Buy Zone re-entry within this many days of leaving continues the earlier entry

BADGE_LABEL = {
    "STRONG_BUY": "▲▲ STRONG BUY", "BUY": "▲ BUY", "ADD": "＋ ADD", "HOLD": "● HOLD", "WATCH": "◎ WATCH",
    "REVIEW": "⚠ REVIEW", "SELL": "▼ SELL", "SWAP": "⇄ SWAP", "TRIM": "✂ TRIM",
}


def ensure_portfolio(run_id: int, profile: Profile, name: str = "Model portfolio", mode: str | None = None,
                     max_holdings: int | None = None, sizing: str | None = None, policy: str | None = None,
                     overrides: dict | None = None) -> int:
    """Create the run's model portfolio on first use. Overrides (mode / holdings / sizing) are for backtests and
    holdings tests; `overrides` replaces any profile setting for this portfolio only (experiments).
    Otherwise the profile's portfolio settings apply."""
    r = db.one("SELECT id FROM portfolios WHERE run_id=? AND kind='model'", (run_id,))
    if r:
        return r["id"]
    st = {**profile.settings, **(overrides or {})}
    mode = mode or st.get("portfolio_mode", "variable")
    weekly = 0.0
    if mode == "focused":
        mh = max_holdings if max_holdings is not None else st.get("max_holdings")
        max_pos = None if mh == 0 else int(mh or 15)  # 0 = no limit on the number of holdings (0.19)
        ic = st.get("initial_capital")
        cash = last_value = float(10000.0 if ic is None else ic)  # $0 is allowed: start from new money only
        weekly = float(st.get("weekly_contribution") or 0.0)
    else:
        max_pos = max_holdings or db.cfg("max_positions")  # None = variable holdings
        cash, last_value = 0.0, 0.0
    cur = db.get().execute(
        "INSERT INTO portfolios(run_id,name,kind,max_positions,position_cap,trim_to,sizing,cash,last_value,last_nav,"
        "mode,contribution,policy,weekly,contributed) VALUES(?,?,?,?,?,?,?,?,?,1.0,?,0,?,?,?)",
        (run_id, name, "model", max_pos, st["position_cap"], st["trim_to"], sizing or st.get("sizing", "equal"), cash,
         last_value, mode, policy or (st.get("new_money", "best_value") + ("+fund" if st.get("fund_strong") else "")),
         weekly, cash),
    )
    if overrides:
        db.get().execute("UPDATE portfolios SET settings_json=? WHERE id=?", (json.dumps(overrides), cur.lastrowid))
    return cur.lastrowid


@dataclass
class DayResult:
    signals: list[dict] = field(default_factory=list)
    badges: dict[int, tuple[str, str]] = field(default_factory=dict)


class PortfolioSim:
    def __init__(self, run_id: int, portfolio_id: int, profile: Profile, pb: PriceBook, tickers: dict[int, str]):
        self.run_id, self.pid, self.p, self.pb, self.tk = run_id, portfolio_id, profile, pb, tickers
        self.st = profile.settings
        self.fmt = {r["id"]: r.get("fmt", "") for r in profile.all_rules()}
        self.target = {r["id"]: r.get("target") for r in profile.all_rules()}
        pr = db.one("SELECT * FROM portfolios WHERE id=?", (portfolio_id,))
        keys = pr.keys()
        ov = json.loads(pr["settings_json"]) if ("settings_json" in keys and pr["settings_json"]) else {}
        if ov:
            self.st = {**profile.settings, **ov}  # this portfolio's experiment settings
        self.traded = (pr["traded"] or 0.0) if "traded" in keys else 0.0  # $ bought + sold (turnover)
        self.costs = (pr["costs"] or 0.0) if "costs" in keys else 0.0    # $ paid in trading costs
        self.macro = None          # set by the replay: VIX for the market overlay
        self.overlay = "normal"    # market overlay state on the last signal day
        self.mos_gate = self.st.get("mos_gate", 0.25)
        self._ret_cache: dict = {}
        self._corr_cache: dict = {}
        self._sectors: dict | None = None
        self.max_positions = pr["max_positions"]
        self.position_cap = pr["position_cap"] or self.st["position_cap"]
        self.trim_to = pr["trim_to"] or self.st["trim_to"]
        self.last_value = pr["last_value"] or 0.0
        self.last_nav = pr["last_nav"] or 1.0
        self.last_bench = pr["last_bench"]
        self.mode = pr["mode"] or "variable"
        self.focused = self.mode == "focused"
        self.cash = pr["cash"] or 0.0
        self.sizing = pr["sizing"] or "equal"
        self.contribution = pr["contribution"] or 0.0  # v0.4 monthly (fraction); superseded by weekly dollars
        self.weekly = pr["weekly"] or 0.0
        pol = pr["policy"] or "best_value"
        self.policy = pol.split("+")[0]   # where new money goes
        self.fund = "+fund" in pol        # sell or trim weaker holdings to fund a strong buy when slots are full
        self.contributed = pr["contributed"] or (self.cash if self.focused else 0.0)
        self.bench_units = pr["bench_units"]
        self.last_deploy = pr["last_deploy"]
        self.reserve = pr["reserve"] or 0.0 if "reserve" in pr.keys() else 0.0  # cash set aside for strong buys
        self.last_date = pr["last_date"]
        self.positions = {r["cik"]: dict(r) for r in db.rows("SELECT * FROM positions WHERE portfolio_id=?", (portfolio_id,))}
        self.state = {r["cik"]: dict(r) for r in db.rows(
            "SELECT * FROM signal_state WHERE run_id=? AND portfolio_id=?", (run_id, portfolio_id))}
        self.open_stints = {r["cik"]: dict(r) for r in db.rows(
            "SELECT * FROM stints WHERE run_id=? AND status='open'", (run_id,))}
        self._first_stint_day: str | None = None

    # ------------------------------------------------------------ helpers
    def _st(self, cik):
        return self.state.setdefault(cik, {"run_id": self.run_id, "portfolio_id": self.pid, "cik": cik, "buy_streak": 0,
                                           "nose_streak": 0, "review": 0, "review_filed": None, "last_add": None,
                                           "review_reason": None, "adds": 0})

    def value_on(self, d):
        return sum(p["units"] * (self.pb.adj(self.tk.get(c), d) or 0) for c, p in self.positions.items())

    def total_on(self, d):
        """Everything the portfolio is worth: holdings plus (focused mode) cash."""
        return self.value_on(d) + (self.cash if self.focused else 0.0)

    # ------------------------------------------------------------ 1. fills at today's open
    def fill_pending(self, d: str) -> tuple[float, float]:
        flows_in = flows_out = 0.0
        pending = db.rows(
            "SELECT * FROM signals WHERE run_id=? AND portfolio_id=? AND fill_price IS NULL AND date<? ORDER BY id",
            (self.run_id, self.pid, d),
        )
        con = db.get()
        if self.focused:
            # sells first, so their cash pays for today's buys
            pending = sorted(pending, key=lambda s: (0 if s["type"] in ("SELL", "SWAP", "TRIM") else 1, s["id"]))
            first = not self.last_date and not self.contributed  # started with $0: the first week's money now
            if self.weekly and (first or (self.last_date and _week(d) != _week(self.last_date))):
                self.cash += self.weekly  # this week's new money
                flows_in += self.weekly
                self.contributed += self.weekly
                b = self.pb.adj(BENCH, d)
                if b and self.bench_units is not None:
                    self.bench_units += self.weekly / b  # the same money into the S&P 500, same day
            elif self.contribution and self.last_date and d[:7] != self.last_date[:7]:
                self.cash += self.contribution
                flows_in += self.contribution
                self.contributed += self.contribution
        for s in pending:
            t = self.tk.get(s["cik"])
            px = self.pb.open_adj_on(t, d) if t else None
            if px is None or px <= 0:
                # no bar today: try again tomorrow. After 10 trading days (a delisting, a long halt) a sale fills
                # at the last price there is and a purchase is dropped, so cash isn't tied up forever.
                if t and len(self.pb.trading_days(BENCH, s["date"], d)) > 10:
                    if s["type"] in ("SELL", "SWAP", "TRIM") and self.positions.get(s["cik"]):
                        px = self.pb.adj(t, d)
                    else:
                        con.execute("UPDATE signals SET fill_price=0, units=0 WHERE id=?", (s["id"],))
                        continue
                if px is None or px <= 0:
                    continue
            typ = s["type"]
            cik = s["cik"]
            pos = self.positions.get(cik)
            if typ in ("BUY", "STRONG_BUY", "ADD"):
                notional = s["units"] or 1.0
                if self.focused:
                    notional = min(notional, self.cash)
                    if notional <= 1e-9:
                        con.execute("UPDATE signals SET fill_price=?, units=0 WHERE id=?", (px, s["id"]))
                        continue
                    self.cash -= notional
                cost = notional * float(self.st.get("trade_cost_bps", 0) or 0) / 10000.0
                self.costs += cost
                self.traded += notional
                units = (notional - cost) / px
                if pos:
                    tot = pos["units"] + units
                    pos["avg_cost"] = (pos["avg_cost"] * pos["units"] + px * units) / tot
                    pos["units"] = tot
                else:
                    rj = json.loads(s["reason_json"] or "{}")
                    pos = {"portfolio_id": self.pid, "cik": cik, "units": units, "avg_cost": px, "opened_on": d,
                           "quality_at_buy": rj.get("quality")}
                    self.positions[cik] = pos
                    con.execute(
                        "INSERT INTO episodes(run_id,portfolio_id,cik,entry_date,entry_px,entry_rank,status,invested,"
                        "proceeds) VALUES(?,?,?,?,?,?, 'open', 0, 0)",
                        (self.run_id, self.pid, cik, d, px, rj.get("rank")),
                    )
                con.execute(
                    "UPDATE episodes SET invested=invested+? WHERE run_id=? AND portfolio_id=? AND cik=? AND status='open'",
                    (notional, self.run_id, self.pid, cik),
                )
                if not self.focused:
                    flows_in += notional
            elif typ in ("SELL", "SWAP", "TRIM") and pos:
                frac = 1.0 if typ in ("SELL", "SWAP") else min(max(s["units"] or 0.33, 0.0), 1.0)
                units = pos["units"] * frac
                proceeds = units * px
                self.traded += proceeds
                cost = proceeds * float(self.st.get("trade_cost_bps", 0) or 0) / 10000.0
                self.costs += cost
                proceeds -= cost
                pos["units"] -= units
                if self.focused:
                    self.cash += proceeds
                else:
                    flows_out += proceeds
                con.execute(
                    "UPDATE episodes SET proceeds=proceeds+? WHERE run_id=? AND portfolio_id=? AND cik=? AND status='open'",
                    (proceeds, self.run_id, self.pid, cik),
                )
                if typ in ("SELL", "SWAP") or pos["units"] <= 1e-12:
                    self._close_episode(cik, d, px, s["reason"] or typ)
                    self.positions.pop(cik, None)
                    stx = self._st(cik)
                    stx["adds"], stx["last_add"] = 0, None
            con.execute("UPDATE signals SET fill_price=? WHERE id=?", (px, s["id"]))
        return flows_in, flows_out

    def _close_episode(self, cik, d, px, reason):
        ep = db.one("SELECT * FROM episodes WHERE run_id=? AND portfolio_id=? AND cik=? AND status='open'",
                    (self.run_id, self.pid, cik))
        if not ep:
            return
        t = self.tk.get(cik)
        series = self.pb.window_adj(t, ep["entry_date"], d)
        mdd = mg = None
        if series:
            peak, mdd, mg = series[0], 0.0, 0.0
            for v in series:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1)
                mg = max(mg, v / series[0] - 1)
        ret = (ep["proceeds"] / ep["invested"] - 1) if ep["invested"] else (px / ep["entry_px"] - 1)
        b0, b1 = self.pb.adj(BENCH, ep["entry_date"]), self.pb.adj(BENCH, d)
        bret = (b1 / b0 - 1) if (b0 and b1) else None
        from datetime import date as _d

        days = (_d.fromisoformat(d) - _d.fromisoformat(ep["entry_date"])).days
        db.get().execute(
            "UPDATE episodes SET exit_date=?, exit_px=?, return_pct=?, bench_return_pct=?, excess_pct=?, max_dd_pct=?,"
            " max_gain_pct=?, days_held=?, exit_reason=?, status='closed' WHERE id=?",
            (d, px, ret, bret, (ret - bret) if bret is not None else None, mdd, mg, days, reason, ep["id"]),
        )

    # ------------------------------------------------------------ 2. NAV
    def mark(self, d: str, flows_in: float, flows_out: float) -> None:
        v = self.total_on(d)
        # money added or taken out at the open (fills) is invested for the whole day: time-weighted daily return
        base = self.last_value + flows_in - flows_out
        r = ((v - self.last_value - flows_in + flows_out) / base) if base > 0 else 0.0
        self.last_nav *= 1 + r
        b = self.pb.adj(BENCH, d)
        if self.last_bench is None and b:
            self.bench_base = b
        bench_nav = None
        b0 = db.one("SELECT bench_nav, date FROM equity_curve WHERE run_id=? AND portfolio_id=? ORDER BY date LIMIT 1",
                    (self.run_id, self.pid))
        first_b = getattr(self, "_first_bench", None)
        if first_b is None:
            first_b = self.pb.adj(BENCH, b0["date"]) if b0 else b
            self._first_bench = first_b
        if b and first_b:
            bench_nav = b / first_b
        self.last_value = v
        self.last_bench = b
        self.last_date = d
        cash_w = (self.cash / v) if (self.focused and v) else None
        bench_value = None
        if self.focused and b:
            if self.bench_units is None:
                self.bench_units = self.contributed / b
            bench_value = self.bench_units * b
        db.get().execute(
            "INSERT OR REPLACE INTO equity_curve(run_id,portfolio_id,date,nav,bench_nav,holdings,cash,value,bench_value,"
            "contributed) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, self.pid, d, self.last_nav, bench_nav, len(self.positions), cash_w,
             v if self.focused else None, bench_value, self.contributed if self.focused else None),
        )

    # ------------------------------------------------------------ 3. badges & new signals
    def _target_w(self, e: Eval) -> float:
        """Focused mode: slice of the portfolio for one holding.
          equal          1/N
          conviction     1/N scaled with Rank score squared
          value_quality  1/N scaled up for a better business (Quality) at a better price (margin of safety above
                         the gate): Quality 100 vs 80 = x1.25, each 10 points of extra margin of safety = +20%,
                         overall x0.5 to x2.
          size           1/N scaled by the square root of market value vs today's median wonderful company,
                         x0.5 to x2 (see size_factor)."""
        n = max(self.max_positions or 15, 1)
        w = 1.0 / n
        if self.sizing == "conviction" and e.rank_score:
            w *= min(1.75, max(0.5, (e.rank_score / 75.0) ** 2))
        elif self.sizing == "value_quality":
            w *= self.value_quality_factor(e)
        elif self.sizing == "size":
            w *= self.size_factor(e)
        return min(w, self._cap())

    def size_factor(self, e) -> float:
        """Bigger companies get bigger slices, as in a market-value-weighted index (Buffett's holdings are
        large companies): square root of market value vs the median wonderful company today, x0.5 to x2."""
        mc = (e.detail or {}).get("mcap")
        ref = getattr(self, "_ref_mcap", None)
        if not mc or not ref:
            return 1.0
        return min(max((mc / ref) ** 0.5, 0.5), 2.0)

    def value_quality_factor(self, e) -> float:
        q = min(max((e.quality or 0.0) / 80.0, 0.8), 1.25)
        v = min(max(1.0 + 2.0 * (_mos(e) - self.mos_gate), 0.75), 1.75)
        return min(max(q * v, 0.5), 2.0)

    def _max_pos(self) -> float | None:
        mp = self.st.get("max_position")
        return float(mp) if mp else None

    def _cap(self) -> float:
        """Largest slice one holding may get: max_position when the profile sets one (a hard limit), otherwise
        the position cap but never less than one equal slice."""
        mp = self._max_pos()
        if mp:
            return mp
        return max(self.position_cap, 1.0 / max(self.max_positions or 15, 1))

    # ------------------------------------------------------------ exposure limits (sector, correlation)
    def _sector_of(self, cik):
        if self._sectors is None:
            from .sectors import sector_map

            self._sectors = sector_map()
        return self._sectors.get(cik, f"Unknown: {cik}")

    def _rets(self, t, d):
        key = (t, _week(d))
        if key not in self._ret_cache:
            self._ret_cache[key] = self.pb.returns(t, d, int(self.st.get("corr_lookback_days", 252)))
        return self._ret_cache[key]

    def corr(self, a: int, b: int, d: str) -> float | None:
        ta, tb = self.tk.get(a), self.tk.get(b)
        if not ta or not tb:
            return None
        key = (min(ta, tb), max(ta, tb), _week(d))
        if key not in self._corr_cache:
            from .sectors import correlation

            self._corr_cache[key] = correlation(self._rets(ta, d), self._rets(tb, d))
        return self._corr_cache[key]

    def limit_room(self, cik: int, d: str, V: float, held: dict[int, float]) -> float:
        """How many more dollars `cik` may receive without breaking max_sector (same sector) or max_correlated
        (holdings whose daily returns correlate >= corr_threshold with it). `held` = current $ per holding,
        including purchases already planned today. Limits apply to new money only; nothing is sold for them."""
        st = self.st
        room = float("inf")
        ms, mc = st.get("max_sector"), st.get("max_correlated")
        if ms:
            sec = self._sector_of(cik)
            exp = sum(v for c, v in held.items() if self._sector_of(c) == sec)
            room = min(room, float(ms) * V - exp)
        if mc:
            thr = float(st.get("corr_threshold", 0.7))
            group = [c for c in held if c != cik and (self.corr(cik, c, d) or 0.0) >= thr]
            if group:
                exp = sum(held[c] for c in group) + held.get(cik, 0.0)
                room = min(room, float(mc) * V - exp)
        return max(room, 0.0)

    def _fund_strong(self, d, V, e, typ, why, rj, cash_avail, holding_evals, leaving, n_owned, limit, pos_value,
                     sig, res):
        """A strong buy appeared but every slot is full. Make room from holdings whose expected return is clearly
        lower (by swap_er_gap or more):
          - while holdings < N + fund_extra_slots: use the cash reserve first, then trim slices of the weakest
            holdings (at most fund_trim_max of each), most expensive first, and add the strong buy as an extra slot;
          - otherwise sell the single weakest holding outright (it must be held fund_min_hold_days or more).
        Returns (cash_avail, n_owned) or None when nothing qualifies."""
        st = self.st
        gap = float(st.get("swap_er_gap", 0.04))
        n = max(limit or 15, 1)
        target = min(self._target_w(e) * float(st.get("strong_buy_units", 1.5)),
                     max(float(st.get("max_buy_weight", 0.25)), 1.0 / n)) * V
        donors = [(c, h) for c, h in holding_evals.items()
                  if c not in leaving and (e.er or 0) - (h.er or 0) >= gap]
        if not donors:
            return None
        donors.sort(key=lambda ch: (ch[1].er or 0))  # lowest expected return (most expensive) first
        tk = self.tk.get(e.cik) or e.ticker
        if n_owned < n + int(st.get("fund_extra_slots", 1)):
            need = max(target - cash_avail, 0.0)
            trims = []
            tmax = float(st.get("fund_trim_max", 0.34))
            for c, h in donors:
                if need <= 0:
                    break
                val = pos_value(c)
                take = min(val * tmax, need)
                if take < 0.001 * V:
                    continue
                trims.append((c, h, take / val, take))
                need -= take
            for c, h, frac, take in trims:
                w = (f"trim {frac:.0%} to fund strong buy {tk}: its exp. return {(e.er or 0):.1%} vs "
                     f"{(h.er or 0):.1%} here")
                sig(c, "TRIM", w, frac, {"fund_for": e.cik})
                res.badges[c] = ("TRIM", w)
                cash_avail += take
            size = min(target, cash_avail)
            if size < 0.001 * V:
                return None
            cash_word = "reserve" if self.policy == "reserve" else "cash"
            src = cash_word if not trims else (f"{cash_word} and trims" if size > sum(t[3] for t in trims) + 1
                                                else "trims")
            sig(e.cik, typ, f"{why} · extra slot funded by {src} · {_money(size)}", size, rj)
            res.badges[e.cik] = (typ, why)
            return cash_avail - size, n_owned + 1
        hold_min = int(st.get("fund_min_hold_days", 180))
        olds = [(c, h) for c, h in donors if _days(self.positions[c]["opened_on"], d) >= hold_min]
        if not olds:
            return None
        c, h = olds[0]
        freed = pos_value(c)
        sig(c, "SWAP", f"sell to fund strong buy {tk}: exp. return {(h.er or 0):.1%} → {(e.er or 0):.1%}", 1.0)
        res.badges[c] = ("SWAP", f"sell to fund {tk}")
        leaving.add(c)
        cash_avail += freed
        size = min(target, cash_avail)
        sig(e.cik, typ, f"{why} · replaces {self.tk.get(c) or '?'} · {_money(size)}", size, rj)
        res.badges[e.cik] = (typ, why)
        return cash_avail - size, n_owned

    def buy_strength(self, e) -> float:
        """0 at an ordinary Buy Zone price, 1 at a strong-buy price (Quality and extra margin of safety)."""
        st = self.st
        extra = float(st.get("strong_extra_mos", 0.15)) or 0.15
        if (e.quality or 0) >= st["strong_quality"] and _mos(e) >= self.mos_gate + extra:
            return 1.0
        return min(max((_mos(e) - self.mos_gate) / extra, 0.0), 0.99)

    def _allocate(self, d, V, cash_avail, ready, add_ok, leaving, n_owned, limit, pos_value, sig, res) -> float:
        """Decide where available cash goes. Options are new stocks for free slots (best first) and more of the
        holdings still in the Buy Zone with the thesis intact, never above max_buy_weight of the portfolio.
          best_value - the option with the highest expected 10-yr return (cheapest vs its value) first
          best_rank  - the highest Oracle Rank first
          spread     - split evenly across every option
          patient    - only buy at strong-buy prices (extra margin of safety); after patient_max_weeks of
                       waiting, fall back to best_value.
          reserve    - sliding reserve: buy part now and keep the rest for a better price. Of new money, an
                       ordinary Buy Zone price gets reserve_min_deploy, a strong-buy price all of it, with a sliding
                       share in between; the rest joins the reserve, which is only spent on strong buys. The reserve never grows above reserve_max of the
                       portfolio and is spent anyway after reserve_max_weeks without a strong buy.
        Cash waits when nothing qualifies, under every policy."""
        st = self.st
        min_buy = max(0.001 * V, 1e-9)
        if cash_avail < min_buy:
            return cash_avail
        n = max(limit or 15, 1)
        cap = self._max_pos() or float(st.get("max_buy_weight", 0.25))
        free = max(0, (limit or 999) - n_owned)
        f_overlay = 1.0
        if st.get("market_overlay"):
            from .market import overlay_factor

            f_overlay = overlay_factor(self.overlay, st)
        opts = []  # (kind, eval, room, extra)
        for e, typ, why, rj, strong in ready[:free]:
            w = self._target_w(e) * (st["strong_buy_units"] if strong else 1.0)
            opts.append(("new", e, min(w, cap if self._max_pos() else max(cap, 1.0 / n)) * V, (typ, why, rj)))
        for cik, e in add_ok.items():
            if cik in leaving or res.badges.get(cik, ("",))[0] != "HOLD":
                continue
            room = cap * V - pos_value(cik)
            if room > min_buy:
                opts.append(("add", e, room, None))
        policy = self.policy
        if policy == "patient":
            if self.last_deploy is None:
                self.last_deploy = d  # start the waiting clock
            waited = _days(self.last_deploy, d) // 7
            if waited < int(st.get("patient_max_weeks", 26)):
                need = self.mos_gate + float(st.get("patient_extra_mos", 0.15))
                opts = [o for o in opts if _mos(o[1]) >= need]
            policy = "best_value"
        if not opts:
            for e, *_ in ready:
                res.badges.setdefault(e.cik, ("WATCH", "Buy Zone, waiting for cash or a free slot"))
            return cash_avail
        if policy == "best_rank":
            opts.sort(key=lambda o: o[1].rank or 999)
        elif policy == "best_value":
            opts.sort(key=lambda o: -(o[1].er or 0))
        plan = []
        full_deploy = False
        if policy == "reserve":
            base = min(max(float(st.get("reserve_min_deploy", 0.5)), 0.0), 1.0)
            if self.last_deploy is None:
                self.last_deploy = d  # start the clock for the reserve's max wait
            strength = {id(o): self.buy_strength(o[1]) for o in opts}
            best = max(strength.values())
            tap = 1.0 if best >= 1.0 else 0.0  # the reserve itself only goes into strong buys
            if _days(self.last_deploy, d) // 7 >= int(st.get("reserve_max_weeks", 26)):
                tap = 1.0  # waited long enough for a strong buy: spend the reserve on the best of what's here
            R = min(self.reserve, cash_avail)
            new = cash_avail - R
            spend_new = new * (base + (1 - base) * best)
            spend_res = R * tap
            keep = R - spend_res + (new - spend_new)
            reserve_cap = float(st.get("reserve_max", 0.20)) * V
            over = max(keep - reserve_cap, 0.0)  # never sit on more than the reserve cap
            budget = (spend_new + spend_res + over) * f_overlay  # overlay: hold back part of it as plain cash
            full_deploy = tap >= 1.0
            opts.sort(key=lambda o: (-strength[id(o)], -(o[1].er or 0)))
            wts = {id(o): (0.5 + strength[id(o)]) * (self.value_quality_factor(o[1]) if self.sizing == "value_quality"
                                                     else self.size_factor(o[1]) if self.sizing == "size"
                                                     else 1.0) for o in opts}  # stronger prices get a bigger share
            left = budget
            for _ in range(2):  # weighted split; a second pass hands what didn't fit to options with room
                tot = sum(wts[id(o)] for o in opts if o[2] > 0) or 1.0
                done = dict((id(o), a) for o, a in plan)
                give = []
                for o in opts:
                    room = o[2] - done.get(id(o), 0.0)
                    if room <= 0:
                        continue
                    give.append((o, min(room, left * wts[id(o)] / tot)))
                for o, a in give:
                    done[id(o)] = done.get(id(o), 0.0) + a
                plan = [(o, done[id(o)]) for o in opts if done.get(id(o), 0.0) > 0]
                left = budget - sum(a for _, a in plan)
                if left < min_buy:
                    break
            self.reserve = min(keep - over, cash_avail - sum(a for _, a in plan if a >= min_buy))
            self.reserve = max(self.reserve, 0.0)
        elif policy == "spread":
            deploy = cash_avail * f_overlay
            if self.sizing in ("value_quality", "size"):  # a bigger share for a better / bigger company
                fw = {id(o): (self.value_quality_factor(o[1]) if self.sizing == "value_quality"
                              else self.size_factor(o[1])) for o in opts}
                tot = sum(fw.values()) or 1.0
                plan = [(o, min(deploy * fw[id(o)] / tot, o[2])) for o in opts]
            else:
                share = deploy / len(opts)
                plan = [(o, min(share, o[2])) for o in opts]
        else:
            left = cash_avail * f_overlay
            for o in opts:
                amt = min(o[2], left)
                if amt >= min_buy:
                    plan.append((o, amt))
                    left -= amt
                if left < min_buy:
                    break
        if st.get("max_sector") or st.get("max_correlated"):
            # exposure limits: trim each planned purchase to what the sector / correlated group can still take
            held = {c: pos_value(c) for c in self.positions if c not in leaving}
            limited, spare = {}, 0.0
            for o, amt in plan:
                ok = min(amt, self.limit_room(o[1].cik, d, V, held))
                if ok < amt - 1e-9 and ok < min_buy:
                    res.badges.setdefault(o[1].cik, ("WATCH", "Buy Zone, but its sector or a group of closely "
                                                              "correlated holdings is at its limit"))
                held[o[1].cik] = held.get(o[1].cik, 0.0) + ok
                limited[id(o)] = (o, ok)
                spare += amt - ok
            # money a limit turned away goes to the other options that still have room, best first
            for o in opts:
                if spare < min_buy:
                    break
                got = limited.get(id(o), (o, 0.0))[1]
                extra = min(spare, o[2] - got, self.limit_room(o[1].cik, d, V, held))
                if extra >= min_buy:
                    limited[id(o)] = (o, got + extra)
                    held[o[1].cik] = held.get(o[1].cik, 0.0) + extra
                    spare -= extra
            plan = [limited[id(o)] for o in opts if id(o) in limited]
        for (kind, e, room, extra), amt in plan:
            if amt < min_buy:
                continue
            if kind == "new":
                typ, why, rj = extra
                sig(e.cik, typ, f"{why} · buy {_money(amt)}", amt, rj)
                res.badges[e.cik] = (typ, why)
            else:
                why = (f"put {_money(amt)} into it: "
                       + {"best_rank": f"highest Oracle Rank you own in the Buy Zone (#{e.rank})",
                          "spread": "new money split across the Buy Zone",
                          "reserve": (f"strong-buy price, reserve put to work (MOS {(e.mos or 0):.0%})"
                                      if self.buy_strength(e) >= 1 else
                                      f"part of new money; the rest waits for a better price (MOS {(e.mos or 0):.0%})")}.get(
                           policy, f"best value you own (exp. 10-yr return {(e.er or 0):.1%}, MOS {(e.mos or 0):.0%})"))
                sig(e.cik, "ADD", why, amt, {"newmoney": True})
                res.badges[e.cik] = ("ADD", why)
            cash_avail -= amt
            if policy != "reserve" or full_deploy:
                self.last_deploy = d
        for e, *_ in ready:
            res.badges.setdefault(e.cik, ("WATCH", "Buy Zone, waiting for cash or a free slot"))
        return cash_avail

    def decide(self, d: str, evals: list[Eval], signal_day: bool = True, mos_gate: float | None = None) -> DayResult:
        st = self.st
        self.mos_gate = mos_gate if mos_gate is not None else st["mos_gate"]
        res = DayResult()
        by = {e.cik: e for e in evals}
        V = self.total_on(d) or 1.0
        buys = [e for e in evals if e.list == "buy"]
        caps = sorted(((e.detail or {}).get("mcap") or 0) for e in evals if e.list in ("buy", "pricey"))
        caps = [c for c in caps if c > 0]
        self._ref_mcap = caps[len(caps) // 2] if caps else None
        if st.get("market_overlay"):
            from .market import overlay_state

            self.overlay = overlay_state(self.pb, self.macro, d, st)

        def sig(cik, typ, reason, size, rj=None):
            e = by.get(cik)
            res.signals.append({"cik": cik, "type": typ, "reason": reason, "units": size,
                                "price": e.price if e else self.pb.close(self.tk.get(cik), d),
                                "reason_json": json.dumps(rj or {})})

        if not signal_day:
            return res
        if st.get("buy_rule") == "top_ranked" and self.focused:
            return self._decide_ranked(d, res, sig, V, by)

        def pos_value(cik):
            p = self.positions.get(cik)
            return p["units"] * (self.pb.adj(self.tk.get(cik), d) or 0) if p else 0.0

        cash_avail = self.cash if self.focused else 0.0  # focused: money we can still commit today
        leaving = set()                                  # positions sold today (fill at next open)
        add_ok = {}                                      # cik -> Eval, holdings eligible for top-ups
        holding_evals = {}                               # cik -> Eval, healthy holdings (could fund a strong buy)

        # ---- owned positions: the strategy's exit rules (sell, REVIEW, trim), then ADD / HOLD
        rules_ = exits.rules_of(st)
        swap_rule = next((r for r in rules_ if r["on"] and r["when"] == "better_idea" and r["do"] == "switch"), None)
        weakest = None
        n_pos = len(self.positions)
        for cik, pos in list(self.positions.items()):
            e = by.get(cik)
            stt = self._st(cik)
            t = self.tk.get(cik)
            px = self.pb.adj(t, d) or 0
            w = pos["units"] * px / V if V else 0
            if e is None:
                res.badges[cik] = ("HOLD", "no fresh evaluation today")
                continue
            if e.list == "pending":
                res.badges[cik] = ("HOLD", e.note or "valuation pending")
                continue
            act = self._exits(d, cik, e, pos, w, V, n_pos, rules_, stt, sig, res)
            if act:
                if act[0] == "sell":
                    leaving.add(cik)
                    cash_avail += pos_value(cik)
                elif act[0] == "trim":
                    cash_avail += pos_value(cik) * act[1]
                continue
            qbuy = pos.get("quality_at_buy")
            # ADD on a dip: thesis intact, limited number of times (no endless averaging down)
            thesis_ok = not st.get("add_requires_quality", True) or (
                not stt["review"] and (qbuy is None or e.quality is None or e.quality >= qbuy - 5))
            if e.list == "buy" and thesis_ok:
                add_ok[cik] = e
            # ADD on a dip (variable mode). Focused mode buys more through the new-money allocator below.
            if not self.focused and e.list == "buy" and px and px <= (1 - st["add_discount"]) * pos["avg_cost"] \
                    and thesis_ok and (stt.get("adds") or 0) < int(st.get("max_adds", 2)):
                la = stt["last_add"]
                if not la or _days(la, d) >= st["add_cooldown_days"]:
                    why = f"thesis intact and {1 - px / pos['avg_cost']:.0%} below average cost"
                    sig(cik, "ADD", why + f" (add {stt['adds'] + 1} of {st.get('max_adds', 2)})", 0.5)
                    stt["last_add"], stt["adds"] = d, (stt.get("adds") or 0) + 1
                    res.badges[cik] = ("ADD", why)
                    continue
            note = "expensive: hold, don't add" if (e.expensive_above and e.price and e.price > e.expensive_above) \
                else "business intact"
            res.badges[cik] = ("HOLD", note)
            holding_evals[cik] = e
            held_long = swap_rule is not None and _days(pos["opened_on"], d) >= int(swap_rule.get("min_days") or 0)
            if swap_rule is not None and (not self.focused or held_long) and \
                    (weakest is None or (e.rank_score or 0) < (weakest[1].rank_score or 0)):
                weakest = (cik, e)

        # ---- candidates not owned
        n_owned = len(self.positions) - len(leaving) + sum(
            1 for s in res.signals if s["type"] in ("BUY", "STRONG_BUY")) - sum(
            1 for s in res.signals if s["type"] in ("SWAP",))
        limit = self.max_positions
        min_rank = float(st.get("min_rank_score") or 0)
        ready: list = []  # focused: confirmed Buy Zone candidates waiting for the allocator
        for e in sorted(buys, key=lambda x: x.rank or 999):
            if e.cik in self.positions:
                continue
            stt = self._st(e.cik)
            hardq = [f for f in (e.qflags or []) if f.get("hard")]
            if hardq and st.get("block_buy_on_quarterly_flag", True):
                stt["buy_streak"] = 0
                res.badges[e.cik] = ("WATCH", "in the Buy Zone, but the latest 10-Q shows: " +
                                     "; ".join(f"{f['name']} ({f['detail']})" for f in hardq))
                continue
            sick = [f for f in (e.qflags or []) if f.get("health")]
            if sick and st.get("health_check"):
                stt["buy_streak"] = 0
                res.badges[e.cik] = ("WATCH", "in the Buy Zone, but the business isn't healthy right now: " +
                                     "; ".join(f"{f['name']} ({f['detail']})" for f in sick))
                continue
            mm = st.get("max_mos")
            if mm is not None and e.mos is not None and e.mos > float(mm):
                stt["buy_streak"] = 0
                res.badges[e.cik] = ("WATCH", f"price is {e.mos:.0%} below the estimated value: discounts deeper "
                                              f"than {float(mm):.0%} have mostly been value traps; find out why "
                                              "before buying")
                continue
            stt["buy_streak"] += 1
            if stt["buy_streak"] < st["buy_confirm_days"]:
                res.badges[e.cik] = ("WATCH", f"in the Buy Zone, confirming ({stt['buy_streak']}/{st['buy_confirm_days']})")
                continue
            if st.get("timing_filter") and self.pb.downtrend(e.ticker, d):
                waited = stt["buy_streak"] - st["buy_confirm_days"]
                mx = int(st.get("timing_max_wait", 60))
                if waited < mx:
                    res.badges[e.cik] = ("WATCH", "in the Buy Zone, waiting for the slide to stop: price is below a "
                                                  f"falling 50-day average ({waited + 1}/{mx})")
                    continue
            if min_rank and (e.rank_score or 0) < min_rank:
                res.badges[e.cik] = ("WATCH", f"in the Buy Zone, but Rank {e.rank_score:.0f} is below the "
                                              f"profile's floor of {min_rank:.0f}")
                continue
            strong = (e.quality or 0) >= st["strong_quality"] and _mos(e) >= self.mos_gate + st["strong_extra_mos"]
            typ = "STRONG_BUY" if strong else "BUY"
            why = (f"Rank #{e.rank} · score {(e.rank_score or 0):.1f} · buy below ${e.buy_below:,.2f}"
                   + (f" · P/E {e.pe_now:.1f} vs its usual {e.pe_median:.1f}" if st.get("price_test") == "fair_pe"
                      and e.pe_now and e.pe_median else "")
                   + (f" · MOS {e.mos:.0%}" if e.mos is not None else "")
                   + (f" · exp. return {e.er:.1%}" if e.er is not None else ""))
            rj = {"quality": e.quality, "rank": e.rank, "mos": e.mos, "er": e.er}
            if self.focused:
                if limit and n_owned >= limit:
                    if weakest and self._better(e, weakest[1], swap_rule):
                        freed = pos_value(weakest[0])
                        sig(weakest[0], "SWAP", f"swap for {e.ticker}: Rank {weakest[1].rank_score:.0f} → "
                                                f"{e.rank_score:.0f}", 1.0)
                        res.badges[weakest[0]] = ("SWAP", f"sell to fund {e.ticker}")
                        leaving.add(weakest[0])
                        cash_avail += freed
                        size = min(freed, cash_avail, self._cap() * V)
                        if st.get("max_sector") or st.get("max_correlated"):
                            held = {c: pos_value(c) for c in self.positions if c not in leaving}
                            size = min(size, self.limit_room(e.cik, d, V, held))
                        sig(e.cik, typ, why + f" · replaces {self.tk.get(weakest[0]) or '?'} · {_money(size)}", size, rj)
                        cash_avail -= size
                        res.badges[e.cik] = (typ, why)
                        weakest = None
                    elif self.fund and strong and self.overlay == "normal":
                        got = self._fund_strong(d, V, e, typ, why, rj, cash_avail, holding_evals, leaving, n_owned,
                                                limit, pos_value, sig, res)
                        if got is None:
                            res.badges[e.cik] = ("WATCH", "strong buy, but no holding is clearly worse to sell for it")
                        else:
                            cash_avail, n_owned = got
                    else:
                        res.badges[e.cik] = ("WATCH", f"Buy Zone, but all {limit} slots hold better or newer ideas")
                    continue
                ready.append((e, typ, why, rj, strong))
                continue
            # ---- variable (legacy) mode
            size = st["strong_buy_units"] if strong else 1.0
            if limit and n_owned >= limit:
                if weakest and self._better(e, weakest[1], swap_rule):
                    sig(weakest[0], "SWAP", f"swap for {e.ticker}: Rank {weakest[1].rank_score:.0f} → {e.rank_score:.0f}", 1.0)
                    res.badges[weakest[0]] = ("SWAP", f"sell to fund {e.ticker}")
                    sig(e.cik, typ, why, size, rj)
                    res.badges[e.cik] = (typ, why)
                    weakest = None
                else:
                    res.badges[e.cik] = ("WATCH", "Buy Zone, but your holdings limit is reached")
                continue
            sig(e.cik, typ, why, size, rj)
            res.badges[e.cik] = (typ, why)
            n_owned += 1
            if weakest and swap_rule and (e.rank_score or 0) - (weakest[1].rank_score or 0) >= float(swap_rule["x"] or 0):
                old = res.badges.get(weakest[0])
                if old and old[0] == "HOLD":
                    res.badges[weakest[0]] = ("HOLD", f"upgrade available: {e.ticker} ranks much higher")

        # ---- focused: put cash to work (new money, sales, trims) according to the profile's policy
        if self.focused:
            self.reserve = min(self.reserve, max(cash_avail, 0.0))  # a strong buy may have used the reserve
            cash_avail = self._allocate(d, V, cash_avail, ready, add_ok, leaving, n_owned, limit, pos_value, sig, res)
            self.reserve = min(self.reserve, max(cash_avail, 0.0))

        buy_set = {e.cik for e in buys}
        for cik, stt in self.state.items():
            if cik not in buy_set and cik not in self.positions:
                stt["buy_streak"] = 0
        for e in evals:
            if e.list == "pricey" and e.cik not in self.positions and e.cik not in res.badges:
                res.badges[e.cik] = ("WATCH", f"wonderful, but pricey: buy below ${e.buy_below:,.2f}" if e.buy_below
                                     else "wonderful, but pricey")
        return res

    # ------------------------------------------------------------ exit rules (0.19, see exits.py)
    def _streak(self, stt: dict, key: str, hit: bool) -> int:
        """Consecutive checks a rule's condition has held (stored per holding)."""
        s = stt.get("_streaks")
        if s is None:
            s = stt["_streaks"] = json.loads(stt.get("streaks_json") or "{}")
        s[key] = (s.get(key, 0) + 1) if hit else 0
        return s[key]

    def _reset_streak(self, stt: dict, key: str) -> None:
        if stt.get("_streaks") is not None:
            stt["_streaks"][key] = 0

    def _rank_info(self, d: str) -> dict:
        c = getattr(self, "_rank_cache", None)
        if c is None or c[0] != d:
            ranked = self.ranking(d)
            c = self._rank_cache = (d, {cik: i for i, (*_, cik) in enumerate(ranked)},
                                    {cik: x for x, *_, cik in ranked})
        return {"pos": c[1], "comb": c[2]}

    def _raw_flags(self, cik: int, d: str, e) -> list[dict]:
        """The latest 10-Q's flags whatever the health-check setting (top-ranked tests used these)."""
        snaps = getattr(self, "snaps", None)
        if snaps is not None:
            s = snaps.asof(cik, d)
            q = snaps.quarterly(cik, d, s["filed"] if s else None)
            return json.loads(q["flags_json"] or "[]") if q else []
        return list(e.qflags or []) if e else []

    def _trigger(self, r: dict, cik: int, e, d: str, pos: dict | None, w: float, n_pos: int) -> str | None:
        """Why the rule's condition holds for this stock today, or None (not met, or no data to tell)."""
        when, x, y = r["when"], r.get("x"), r.get("y")
        st = self.st
        snaps = getattr(self, "snaps", None)
        q = e.quality if e else (snaps.asof(cik, d)["quality"] if snaps and snaps.asof(cik, d) else None)
        if when == "fails_rules":
            hard = [f for f in (e.failed if e else []) if f.get("id") != "u1"]
            if hard and e and not e.gates_passed:
                return "thesis broken: " + ", ".join(
                    f"{f['name']} {_fmt(f.get('value'), self.fmt.get(f.get('id'), ''))} (limit "
                    f"{_fmt(self.target.get(f.get('id')), self.fmt.get(f.get('id'), ''))})" for f in hard)
            return None
        if when == "quality_below":
            return f"thesis broken: Quality {q:.0f} < {x:g}" if q is not None and x is not None and q < x else None
        if when == "quality_drop":
            qb = pos.get("quality_at_buy") if pos else None
            if qb is not None and q is not None and x is not None and qb - q >= x:
                return f"Quality down {qb - q:.0f} pts since purchase ({qb:.0f} → {q:.0f})"
            return None
        if when in ("wonder_below", "wonder_drop", "combined_below"):
            if snaps is None:
                return None
            wv = snaps.wonder_asof(cik, d)
            if wv is None or x is None:
                return None
            if when == "wonder_below":
                return f"Wonderfulness {wv:.0f} < {x:g}" if wv < x else None
            if when == "combined_below":
                if q is None:
                    return None
                c = (wv + q) / 2
                return f"combined score {c:.1f} < {x:g}" if c < x else None
            if not pos:
                return None
            w0 = snaps.wonder_asof(cik, pos["opened_on"])
            return (f"Wonderfulness down {w0 - wv:.0f} pts since purchase ({w0:.0f} → {wv:.0f})"
                    if w0 is not None and w0 - wv >= x else None)
        if when == "rank_below":
            if snaps is None or x is None:
                return None
            ri = self._rank_info(d)
            r_ = ri["pos"].get(cik)
            if r_ is None:
                return None if snaps.asof(cik, d) is None else "no longer ranked (no recent price or score)"
            k = int(x)
            return (f"fell to #{r_ + 1} by combined score ({ri['comb'][cik]:.1f}); sells outside the top {k}"
                    if r_ >= k else None)
        if when == "near_limit":
            gm = e.gate_margin if e else None
            if gm is not None and x is not None and gm < x and e.gate_margin_rule:
                g = e.gate_margin_rule
                return (f"{g['name']} close to its limit ({_fmt(g.get('value'), g.get('fmt', ''))} vs "
                        f"{_fmt(g.get('target'), g.get('fmt', ''))})")
            return None
        if when == "price_over_value":
            if not (e and e.iv and e.price and x is not None and e.price >= x * e.iv):
                return None
            if y is not None:
                if not (e.pe_now and e.pe_median and e.pe_now >= y * e.pe_median):
                    return None
                return f"nose-bleed price: {e.price / e.iv:.1f}× value, P/E {e.pe_now:.0f} vs median {e.pe_median:.0f}"
            return f"price {e.price / e.iv:.1f}× the estimated value"
        if when == "pe_over_usual":
            if e and e.pe_now and e.pe_median and x is not None and e.pe_now >= x * e.pe_median:
                return f"P/E {e.pe_now:.0f} vs its usual {e.pe_median:.0f}"
            return None
        if when == "exp_return_below":
            if e and e.er is not None and x is not None and e.er < x:
                return f"expected 10-yr return {e.er:.1%} < {x:.0%}"
            return None
        if when in ("fair_over", "growth_gap_above", "fair_return_below"):
            dt = (e.detail or {}) if e else {}
            if x is None:
                return None
            if when == "fair_over":
                fv = dt.get("fair_iv")
                return (f"price {e.price / fv:.1f}× its fair value (${fv:,.2f})"
                        if fv and e.price and e.price >= x * fv else None)
            if when == "growth_gap_above":
                im, dl = dt.get("implied_g"), dt.get("delivered_g")
                return (f"the price implies {im:.0%} growth a year vs {dl:.0%} delivered"
                        if im is not None and dl is not None and im - dl >= x else None)
            fer = dt.get("fair_er")
            return f"expected return at today's price {fer:.1%} < {x:.0%}" if fer is not None and fer < x else None
        if when == "sales_growth_below":
            sg = self._sales_growth(cik, d, e)
            return f"sales over the last 12 months {sg:+.0%} vs a year earlier" if sg is not None and x is not None \
                and sg < x else None
        if when in ("q_revenue", "q_profits", "q_loss"):
            fid = {"q_revenue": "rev_decline", "q_profits": "profit_decline", "q_loss": "ttm_loss"}[when]
            f = next((f for f in self._raw_flags(cik, d, e) if f.get("id") == fid), None)
            return f"10-Q: {f['name']} ({f['detail']})" if f else None
        if when in ("q_hard", "q_soft"):
            want = when == "q_hard"
            fl = [f for f in (e.qflags or []) if bool(f.get("hard")) == want] if e else []
            return "; ".join(f"{f['name']}: {f['detail']} (10-Q {e.qfiled})" for f in fl) if fl else None
        t = self.tk.get(cik)
        px = self.pb.adj(t, d) if t else None
        if when == "weight_above":
            if not pos or n_pos < 2:
                return None
            mp = self._max_pos()
            if x is not None:
                cap = float(x)
            elif mp:
                cap = mp + float(st.get("max_position_tolerance", 0.05))
            else:
                cap = max(self.position_cap, 1.5 / n_pos) if n_pos else 1.0
            return f"oversized: {w:.0%} of portfolio" if w > cap else None
        if when in ("gain_above", "loss_below"):
            if not pos or not px or not pos.get("avg_cost") or x is None:
                return None
            ch = px / pos["avg_cost"] - 1
            if when == "gain_above":
                return f"up {ch:.0%} from average cost" if ch >= x else None
            return f"down {-ch:.0%} from average cost" if ch <= -x else None
        if when == "trail_stop":
            if not pos or not px or x is None:
                return None
            hi = max(self.pb.window_adj(t, pos["opened_on"], d) or [px])
            return f"{1 - px / hi:.0%} below its high since bought" if px <= hi * (1 - x) else None
        if when == "lagging_market":
            from .engine import rel_return

            months = float(y or 6)
            rr = rel_return(self.pb, t, d, int(months * 30.44)) if t else None
            return (f"{-rr:.0%} behind the S&P 500 over {months:g} months" if rr is not None and x is not None
                    and rr <= -x else None)
        if when == "below_200d":
            sma = self.pb.sma(t, d, 200) if t else None
            return "below its 200-day average" if (px and sma and px < sma) else None
        return None

    def _sales_growth(self, cik, d, e):
        dt = (e.detail or {}) if e else {}
        if dt.get("sales_g") is not None:
            return dt["sales_g"]
        snaps = getattr(self, "snaps", None)
        q = snaps.latest_q(cik, d) if snaps is not None and hasattr(snaps, "latest_q") else None
        return json.loads(q["metrics_json"] or "{}").get("ttm_revenue_growth") if q else None

    def _guard(self, r, cik, e, d) -> bool:
        """The rule's 'unless' condition holds (so the rule doesn't act)."""
        g = r.get("unless")
        if not g:
            return False
        ux = r.get("ux") if r.get("ux") is not None else exits.GUARDS[g]["x"]
        if g == "sales_growth_at_least":
            sg = self._sales_growth(cik, d, e)
            return sg is not None and sg >= ux
        if g == "wonder_at_least":
            snaps = getattr(self, "snaps", None)
            wv = ((e.detail or {}).get("wonder") if e else None) or (snaps.wonder_asof(cik, d) if snaps else None)
            return wv is not None and wv >= ux
        if g == "quality_at_least":
            return e is not None and e.quality is not None and e.quality >= ux
        return False

    def _fires(self, r, cik, e, d, pos, w, n_pos):
        why = self._trigger(r, cik, e, d, pos, w, n_pos)
        return None if (why and self._guard(r, cik, e, d)) else why

    def _better(self, cand, held, rule) -> bool:
        """The switch rule: the candidate beats the weakest holding by x Oracle Rank points and y expected return."""
        if rule is None:
            return False
        return ((cand.rank_score or 0) - (held.rank_score or 0) >= float(rule.get("x") or 0)
                and (cand.er or 0) - (held.er or 0) >= float(rule.get("y") or 0))

    def _exits(self, d, cik, e, pos, w, V, n_pos, rules_, stt, sig, res):
        """Apply the exit rules to one holding (order in exits.py). Returns ("sell", 1) / ("trim", fraction) /
        ("review", 0) when it acted or put the holding under REVIEW, else None."""
        held = _days(pos["opened_on"], d) if pos else 0

        def ok(r):
            return r["on"] and held >= int(r.get("min_days") or 0)

        # 1. sell now (or after N checks in a row)
        for i, r in enumerate(rules_):
            if not (r["on"] and r["do"] in ("sell", "switch") and r["when"] != "better_idea"
                    and r["confirm"] in ("now", "checks")):
                continue
            if not ok(r):
                continue
            why = self._fires(r, cik, e, d, pos, w, n_pos)
            if r["confirm"] == "checks":
                n = self._streak(stt, f"{i}:{r['when']}", why is not None)
                if why and n < int(r.get("checks") or 1):
                    why = None
            if why:
                self._reset_streak(stt, f"{i}:{r['when']}")
                sig(cik, "SELL", why, 1.0, {"rule": r["when"], "quality": e.quality if e else None})
                res.badges[cik] = ("SELL", why)
                return ("sell", 1.0)
        # 2. REVIEW: warnings, and actions that wait for the next filing to confirm them
        hard, soft, hard_rule = [], [], None
        for r in rules_:
            if not ok(r) or r["when"] == "better_idea":
                continue
            if r["do"] == "warn":
                why = self._fires(r, cik, e, d, pos, w, n_pos)
                if why:
                    soft.append(why)
            elif r["confirm"] == "filing":
                why = self._fires(r, cik, e, d, pos, w, n_pos)
                if why:
                    hard.append(why)
                    hard_rule = hard_rule or r
        latest_filing = max((e.filed or "") if e else "", (e.qfiled or "") if e else "")
        if hard or soft:
            if hard and stt["review"] and stt["review_filed"] and latest_filing > stt["review_filed"] \
                    and (stt.get("review_reason") or "").startswith("hard"):
                why = "REVIEW confirmed by next filing: " + "; ".join(hard)
                if hard_rule["do"] in ("trim", "trim_to"):
                    frac = self._trim_frac(hard_rule, w, n_pos)
                    if frac > 0:
                        sig(cik, "TRIM", why, frac)
                        res.badges[cik] = ("TRIM", why)
                        stt["review"], stt["review_reason"] = 0, None
                        return ("trim", frac)
                else:
                    sig(cik, "SELL", why, 1.0, {"hard": hard, "soft": soft})
                    res.badges[cik] = ("SELL", why)
                    return ("sell", 1.0)
            kind = "hard" if hard else "soft"
            if not stt["review"] or (kind == "hard" and not (stt.get("review_reason") or "").startswith("hard")):
                stt["review"], stt["review_filed"] = 1, latest_filing
                stt["review_reason"] = kind + ": " + "; ".join(hard or soft)
            res.badges[cik] = ("REVIEW", "; ".join(hard + soft) +
                               (" · becomes SELL if the next filing confirms it" if hard else
                                " · watch item, not a sell trigger"))
            return ("review", 0.0)
        stt["review"], stt["review_reason"] = 0, None
        # 3. trims: the first that fires, once
        for i, r in enumerate(rules_):
            if not (r["on"] and r["do"] in ("trim", "trim_to") and r["confirm"] in ("now", "checks")):
                continue
            if not ok(r):
                continue
            why = self._fires(r, cik, e, d, pos, w, n_pos)
            if r["confirm"] == "checks":
                n = self._streak(stt, f"{i}:{r['when']}", why is not None)
                if why and n < int(r.get("checks") or 1):
                    continue
            if not why:
                continue
            frac = self._trim_frac(r, w, n_pos)
            if frac <= 0:
                continue
            self._reset_streak(stt, f"{i}:{r['when']}")
            if r["do"] == "trim_to":
                why += f" → trim to {max(w * (1 - frac), 0):.0%}"
            sig(cik, "TRIM", why, frac)
            res.badges[cik] = ("TRIM", why)
            return ("trim", frac)
        return None

    def _trim_frac(self, r, w, n_pos) -> float:
        st = self.st
        if r["do"] == "trim":
            return min(max(float(r.get("amount") if r.get("amount") is not None else st.get("trim_fraction", 0.33)),
                           0.0), 1.0)
        if r.get("amount") is not None:
            target = float(r["amount"])
        else:
            mp = self._max_pos()
            target = mp if mp else max(self.trim_to, 1.0 / max(n_pos, 1))
        return 1 - target / w if w > target else 0.0

    # ------------------------------------------------------------ top-ranked buying (0.18.1)
    def ranking(self, d: str) -> list[tuple[float, float, float, int]]:
        """Every scored company with a recent price, best first: (combined, Wonderfulness, Quality, cik), where
        combined = (Wonderfulness + Quality) / 2 as known on d. Not limited to the Buy Zone or the must-pass rules:
        this is the ranking the top-ranked rule was tested on."""
        snaps = getattr(self, "snaps", None)
        if snaps is None:
            return []
        out = []
        for cik in snaps.ciks():
            t = self.tk.get(cik)
            if not t:
                continue
            last = self.pb.last_date(t, d)
            if not last or _days(last, d) > 7:
                continue
            s = snaps.asof(cik, d)
            if s is None or s["quality"] is None:
                continue
            w = snaps.wonder_asof(cik, d)
            if w is None:
                continue
            out.append(((w + s["quality"]) / 2, w, s["quality"], cik))
        out.sort(reverse=True)
        return out

    def _decide_ranked(self, d: str, res: DayResult, sig, V: float, by: dict | None = None) -> DayResult:
        """Once a week (the first signal day of each week): apply the strategy's exit rules to every holding (by
        default: sell below rank `ranked_sell_outside` by combined score, or when the latest 10-Q shows sales
        shrinking), then split all cash (new money plus sales and trims) equally across the top
        `ranked_buy_count` that no immediate sell rule would sell at once, at any price."""
        st = self.st
        n_buy = max(int(st.get("ranked_buy_count") or 4), 1)
        max_w = st.get("ranked_max_weight")
        ranked = self.ranking(d)
        pos_of = {c: i for i, (*_, c) in enumerate(ranked)}
        comb = {c: x for x, *_, c in ranked}

        def pos_value(cik):
            p = self.positions.get(cik)
            return p["units"] * (self.pb.adj(self.tk.get(cik), d) or 0) if p else 0.0

        if self.last_deploy and _week(self.last_deploy) == _week(d):
            for cik in self.positions:
                r = pos_of.get(cik)
                res.badges[cik] = ("HOLD", (f"#{r + 1} by combined score ({comb[cik]:.1f})" if r is not None
                                            else "not ranked today") + " · next weekly check next week")
            return res
        self.last_deploy = d
        cash = self.cash
        leaving = set()
        by = by or {}
        rules_ = exits.rules_of(st)
        n_pos = len(self.positions)
        for cik in list(self.positions):
            if self.snaps.asof(cik, d) is None:
                res.badges[cik] = ("HOLD", "no score yet")
                continue
            pos = self.positions[cik]
            stt = self._st(cik)
            w = pos_value(cik) / V if V else 0.0
            act = self._exits(d, cik, by.get(cik), pos, w, V, n_pos, rules_, stt, sig, res)
            if act:
                if act[0] == "sell":
                    leaving.add(cik)
                    cash += pos_value(cik)
                elif act[0] == "trim":
                    cash += pos_value(cik) * act[1]
                continue
            r = pos_of.get(cik)
            res.badges[cik] = ("HOLD", (f"#{r + 1} by combined score ({comb[cik]:.1f})" if r is not None
                                        else "not ranked today") + " · no exit rule applies")
        # never buy what an immediate sell rule would sell at once (e.g. sales shrinking)
        block = [r for r in rules_ if r["on"] and r["when"] != "better_idea" and not r.get("min_days") and (
            (r["do"] in ("sell", "switch") and r["confirm"] == "now")
            or (r["do"] in ("trim", "trim_to") and r["confirm"] in ("now", "checks") and r["when"] != "weight_above"))]
        total = sum(pos_value(c) for c in self.positions if c not in leaving)  # invested, without cash
        picks = []
        for x, w, q, cik in ranked:
            if len(picks) == n_buy:
                break
            if cik in leaving:
                continue
            stop = next((y for r in block if (y := self._fires(r, cik, by.get(cik), d, self.positions.get(cik),
                                                                 0.0, n_pos))), None)
            if stop:
                if pos_of[cik] < n_buy:
                    res.badges.setdefault(cik, ("WATCH", f"top ranked, but an exit rule applies: {stop}"))
                continue
            if max_w and cik in self.positions and total > 0 and pos_value(cik) / total > float(max_w):
                res.badges[cik] = ("HOLD", f"#{pos_of[cik] + 1} by combined score · already {pos_value(cik) / total:.0%}"
                                           f" of the portfolio, so new money goes to the next one")
                continue
            picks.append((cik, x, w, q))
        if picks and cash >= 1.0:
            amt = cash / len(picks)
            for cik, x, w, q in picks:
                r = pos_of[cik] + 1
                why = (f"#{r} by combined score {x:.1f} (Wonderfulness {w:.0f}, Quality {q:.0f}) · "
                       f"{'add' if cik in self.positions else 'buy'} {_money(amt)}")
                typ = "ADD" if cik in self.positions else "BUY"
                sig(cik, typ, why, amt, {"quality": q, "rank": r, "combined": x})
                res.badges[cik] = (typ, why)
        return res

    def stints(self, d: str, evals: list[Eval]) -> None:
        """Buy Zone entries ("stints"), for the 'Is the ranking working?' table. A stock that leaves the Buy Zone
        and comes back within REENTRY_DAYS continues its earlier entry instead of starting a new one, so a price
        hovering around the buy line counts once. Entries on the run's first day are flagged: those stocks were
        already in the Buy Zone when the history began, so they didn't 'enter' with a rank of their own."""
        con = db.get()
        if self._first_stint_day is None:
            self._first_stint_day = db.scalar("SELECT MIN(entered_on) FROM stints WHERE run_id=?", (self.run_id,)) or d
        in_zone = {e.cik: e for e in evals if e.list == "buy"}
        for cik, e in in_zone.items():
            if cik in self.open_stints:
                continue
            last = db.one("SELECT id, entered_on, entry_px, left_on FROM stints WHERE run_id=? AND cik=? "
                          "AND status='closed' ORDER BY left_on DESC LIMIT 1", (self.run_id, cik))
            if last and last["left_on"] and _days(last["left_on"], d) <= REENTRY_DAYS:
                con.execute("UPDATE stints SET left_on=NULL, exit_px=NULL, return_pct=NULL, bench_return_pct=NULL, "
                            "status='open', reentries=COALESCE(reentries,0)+1 WHERE id=?", (last["id"],))
                self.open_stints[cik] = {"id": last["id"], "cik": cik, "entered_on": last["entered_on"],
                                         "entry_px": last["entry_px"]}
                continue
            px = self.pb.adj(self.tk.get(cik), d)
            cur = con.execute(
                "INSERT INTO stints(run_id,cik,entered_on,entry_px,entry_rank,status,at_start) "
                "VALUES(?,?,?,?,?, 'open', ?)",
                (self.run_id, cik, d, px, e.rank, int(d == self._first_stint_day)),
            )
            self.open_stints[cik] = {"id": cur.lastrowid, "cik": cik, "entered_on": d, "entry_px": px}
        for cik in list(self.open_stints):
            if cik not in in_zone:
                s = self.open_stints.pop(cik)
                t = self.tk.get(cik)
                px = self.pb.adj(t, d)
                ret = (px / s["entry_px"] - 1) if (px and s["entry_px"]) else None
                b0, b1 = self.pb.adj(BENCH, s["entered_on"]), self.pb.adj(BENCH, d)
                bret = (b1 / b0 - 1) if (b0 and b1) else None
                con.execute("UPDATE stints SET left_on=?, exit_px=?, return_pct=?, bench_return_pct=?, status='closed' "
                            "WHERE id=?", (d, px, ret, bret, s["id"]))

    # ------------------------------------------------------------ 5. persist
    def persist(self, d: str, res: DayResult) -> None:
        con = db.get()
        for s in res.signals:
            con.execute(
                "INSERT INTO signals(run_id,portfolio_id,cik,date,type,reason,reason_json,price,units) VALUES(?,?,?,?,?,?,?,?,?)",
                (self.run_id, self.pid, s["cik"], d, s["type"], s["reason"], s["reason_json"], s["price"], s["units"]),
            )
        con.execute("DELETE FROM badges WHERE run_id=? AND portfolio_id=? AND date=?", (self.run_id, self.pid, d))
        con.executemany(
            "INSERT OR REPLACE INTO badges(run_id,portfolio_id,date,cik,badge,reason) VALUES(?,?,?,?,?,?)",
            [(self.run_id, self.pid, d, c, b, r) for c, (b, r) in res.badges.items()],
        )
        con.execute("DELETE FROM positions WHERE portfolio_id=?", (self.pid,))
        con.executemany(
            "INSERT INTO positions(portfolio_id,cik,units,avg_cost,opened_on,quality_at_buy) VALUES(?,?,?,?,?,?)",
            [(self.pid, c, p["units"], p["avg_cost"], p["opened_on"], p.get("quality_at_buy"))
             for c, p in self.positions.items()],
        )
        con.executemany(
            "INSERT OR REPLACE INTO signal_state(run_id,portfolio_id,cik,buy_streak,nose_streak,review,review_filed,"
            "last_add,review_reason,adds,streaks_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(self.run_id, self.pid, c, s["buy_streak"], s["nose_streak"], s["review"], s["review_filed"], s["last_add"],
              s.get("review_reason"), s.get("adds") or 0,
              json.dumps(s["_streaks"]) if s.get("_streaks") else s.get("streaks_json"))
             for c, s in self.state.items() if s["buy_streak"] or s["nose_streak"] or s["review"] or s["last_add"]
             or c in self.positions],
        )
        self.save_state(d)

    def save_state(self, d: str) -> None:
        db.get().execute(
            "UPDATE portfolios SET last_value=?, last_nav=?, last_bench=?, last_date=?, cash=?, contributed=?, "
            "bench_units=?, last_deploy=?, reserve=?, traded=?, costs=? WHERE id=?",
            (self.last_value, self.last_nav, self.last_bench, d, self.cash, self.contributed, self.bench_units,
             self.last_deploy, self.reserve, self.traded, self.costs, self.pid))


def _mos(e) -> float:
    """How far below its buy price a stock is, on the margin-of-safety scale the strong-buy, reserve and sizing
    rules use: the strict rule's margin of safety, or (fair-value and growth-gap buy rules) the gate plus the
    discount to the strategy's own buy price."""
    m = (e.detail or {}).get("buy_mos") if getattr(e, "detail", None) else None
    return float(m if m is not None else (e.mos or 0.0))


def _money(v: float) -> str:
    return f"${v:,.0f}" if v >= 100 else f"{v:,.4g}"


def _week(d: str) -> tuple[int, int]:
    from datetime import date as _d

    return _d.fromisoformat(d).isocalendar()[:2]


def _days(a: str, b: str) -> int:
    from datetime import date as _d

    return (_d.fromisoformat(b) - _d.fromisoformat(a)).days


def _fmt(v, fmt: str = ""):
    if v is None:
        return "n/a"
    if fmt == "pct":
        return f"{v * 100:.1f}%"
    if fmt == "ratio":
        return f"{v * 100:.0f}%"
    if fmt == "years":
        return f"{v:.1f} yrs" if v < 90 else "never (losses)"
    if fmt == "x":
        return f"{v:.2f}×"
    if abs(v) < 5:
        return f"{v:.2f}"
    return f"{v:.1f}"

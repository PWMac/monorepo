"""Your real portfolio: accounts, purchases (lots), sales, cash, baskets, and how it all performed.

Model
- An account holds cash and purchases. A purchase (a "lot") is shares bought on one day at one price; selling closes
  purchases (first in, first out unless you pick them) and keeps them as closed lots, so realized gains are exact.
- A holding is every open purchase of one ticker, across the accounts you are looking at.
- A basket is a named group of tickers (Core, Income, ...). A ticker can be in several baskets; "Not in a basket"
  collects the rest.

Performance
- Prices are the stored daily closes; dividend-adjusted closes give total return. Dividends are worked out from the
  two (the ex-date jump between the plain and the adjusted close), so every listed stock and ETF has them.
- The portfolio's return is time-weighted (TWR): each day's return of what was held, chained, so adding or taking out
  money doesn't move it. That is the number to compare with the S&P 500 and other tickers.
- "Your money" figures are money-weighted (XIRR): what your actual purchases, on their dates, earned a year.
- "Same purchases in SPY": every purchase's cost put into the benchmark on the same day (and taken out when you sold),
  so you can see what the same money would be worth.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db
from .growth import xirr

LONG_TERM_DAYS = 366  # held more than a year
DEFAULT_COMPARE = ["SPY"]
CASH_LIKE = {"SWVXX", "SNVXX", "SNOXX", "SWTXX", "SNSXX", "SPAXX", "FDRXX", "FZFXX", "VMFXX", "VMRXX", "SPRXX",
             "SGOV", "BIL", "SHV"}


# ===================================================================== accounts
def accounts() -> list[dict]:
    return [dict(r) for r in db.rows("SELECT * FROM accounts ORDER BY id")]


def account(aid: int) -> dict | None:
    r = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    return dict(r) if r else None


def add_account(name: str, broker: str = "", source: str = "manual", tail: str = "", ext_id: str | None = None,
                cash: float = 0.0) -> int:
    cur = db.get().execute(
        "INSERT INTO accounts(name, broker, tail, source, ext_id, cash, cash_asof, created_at, lot_source) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (name.strip() or "Account", broker, tail, source, ext_id, float(cash or 0),
                                      date.today().isoformat(), db.now(), "manual"))
    return int(cur.lastrowid)


def find_account(source: str, ext_id: str) -> dict | None:
    r = db.one("SELECT * FROM accounts WHERE source=? AND ext_id=?", (source, ext_id))
    return dict(r) if r else None


def update_account(aid: int, **kw) -> None:
    ok = {"name", "broker", "tail", "cash", "cash_asof", "synced_at", "source", "ext_id", "lot_source"}
    kv = {k: v for k, v in kw.items() if k in ok}
    if "cash" in kv and "cash_asof" not in kv:
        kv["cash_asof"] = date.today().isoformat()
    if kv:
        db.get().execute(f"UPDATE accounts SET {', '.join(k + '=?' for k in kv)} WHERE id=?", (*kv.values(), aid))


def delete_account(aid: int) -> None:
    with db.tx() as con:
        for t in ("lots", "closed_lots", "txns", "broker_positions"):
            con.execute(f"DELETE FROM {t} WHERE account_id=?", (aid,))
        con.execute("DELETE FROM accounts WHERE id=?", (aid,))


def ensure_manual_account() -> int:
    r = db.one("SELECT id FROM accounts ORDER BY id LIMIT 1")
    return int(r["id"]) if r else add_account("My account")


# ===================================================================== purchases (lots) and sales
def _t(ticker: str) -> str:
    return (ticker or "").strip().upper().replace("/", ".")


def lots(account_ids: list[int] | None = None, tickers: list[str] | None = None, closed: bool = False) -> list[dict]:
    q = f"SELECT * FROM {'closed_lots' if closed else 'lots'} WHERE shares > 1e-9"
    args: list = []
    if account_ids is not None:
        q += f" AND account_id IN ({','.join('?' * len(account_ids)) or 'NULL'})"
        args += account_ids
    if tickers is not None:
        q += f" AND ticker IN ({','.join('?' * len(tickers)) or 'NULL'})"
        args += tickers
    return [dict(r) for r in db.rows(q + " ORDER BY ticker, open_date, id", args)]


def add_lot(aid: int, ticker: str, open_date: str | None, shares: float, cost_ps: float, source: str = "manual",
            note: str = "", record_txn: bool = True) -> int:
    known = bool(open_date)
    od = open_date or date.today().isoformat()
    cur = db.get().execute(
        "INSERT INTO lots(account_id, ticker, open_date, shares, cost_ps, date_known, source, note, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (aid, _t(ticker), od, float(shares), float(cost_ps or 0), int(known), source,
                                      note, db.now()))
    if record_txn and source == "manual":
        add_txn(aid, od, "buy", ticker, shares, cost_ps, -float(shares) * float(cost_ps or 0), 0.0, "Added by hand",
                "manual")
    return int(cur.lastrowid)


def update_lot(lot_id: int, **kw) -> None:
    ok = {"open_date", "shares", "cost_ps", "note", "account_id", "ticker"}
    kv = {k: v for k, v in kw.items() if k in ok}
    if "ticker" in kv:
        kv["ticker"] = _t(kv["ticker"])
    if "open_date" in kv:
        kv["date_known"] = 1 if kv["open_date"] else 0
        kv["open_date"] = kv["open_date"] or date.today().isoformat()
    if kv:
        db.get().execute(f"UPDATE lots SET {', '.join(k + '=?' for k in kv)} WHERE id=?", (*kv.values(), lot_id))


def delete_lot(lot_id: int, closed: bool = False) -> None:
    db.get().execute(f"DELETE FROM {'closed_lots' if closed else 'lots'} WHERE id=?", (lot_id,))


def sell(aid: int, ticker: str, sell_date: str, shares: float, price: float, method: str = "fifo",
         lot_ids: list[int] | None = None, fees: float = 0.0, source: str = "manual", record_txn: bool = True) -> dict:
    """Close purchases for a sale. method: fifo (oldest first), lifo, hifo (highest cost first: smallest taxable
    gain), lofo, or the given lot_ids in that order. Returns {"sold", "cost", "proceeds", "gain", "short"}."""
    tk = _t(ticker)
    open_ = lots([aid], [tk])
    if lot_ids:
        by = {x["id"]: x for x in open_}
        open_ = [by[i] for i in lot_ids if i in by]
    elif method == "lifo":
        open_.sort(key=lambda x: (x["open_date"], x["id"]), reverse=True)
    elif method == "hifo":
        open_.sort(key=lambda x: -x["cost_ps"])
    elif method == "lofo":
        open_.sort(key=lambda x: x["cost_ps"])
    left = float(shares)
    out = {"sold": 0.0, "cost": 0.0, "proceeds": 0.0, "gain": 0.0, "short": 0.0}
    net_px = float(price) - (float(fees) / float(shares) if shares else 0.0)
    with db.tx() as con:
        for x in open_:
            if left <= 1e-9:
                break
            q = min(left, x["shares"])
            left -= q
            con.execute("INSERT INTO closed_lots(account_id, ticker, open_date, close_date, shares, cost_ps, price, "
                        "source, note) VALUES(?,?,?,?,?,?,?,?,?)",
                        (aid, tk, x["open_date"], sell_date, q, x["cost_ps"], net_px, source, x.get("note") or ""))
            if x["shares"] - q <= 1e-9:
                con.execute("DELETE FROM lots WHERE id=?", (x["id"],))
            else:
                con.execute("UPDATE lots SET shares=? WHERE id=?", (x["shares"] - q, x["id"]))
            g = q * (net_px - x["cost_ps"])
            out["sold"] += q
            out["cost"] += q * x["cost_ps"]
            out["proceeds"] += q * net_px
            out["gain"] += g
            if (date.fromisoformat(sell_date) - date.fromisoformat(x["open_date"])).days < LONG_TERM_DAYS:
                out["short"] += g
    if record_txn and source == "manual":
        add_txn(aid, sell_date, "sell", tk, out["sold"], price, out["sold"] * price - fees, fees, "Sold by hand",
                "manual")
    return out


def replace_lots(aid: int, new: list[dict], source: str) -> int:
    """A broker's list of open purchases is the truth for that account: replace what's there (closed lots stay)."""
    with db.tx() as con:
        con.execute("DELETE FROM lots WHERE account_id=?", (aid,))
        for x in new:
            con.execute(
                "INSERT INTO lots(account_id, ticker, open_date, shares, cost_ps, date_known, source, note, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (aid, _t(x["ticker"]), x.get("open_date") or date.today().isoformat(), float(x["shares"]),
                 float(x.get("cost_ps") or 0), int(bool(x.get("open_date"))), source, x.get("note") or "", db.now()))
        con.execute("UPDATE accounts SET lot_source=? WHERE id=?", (source, aid))
    return len(new)


# ===================================================================== transactions
TXN_KINDS = {"buy": "Buy", "sell": "Sell", "div": "Dividend", "interest": "Interest", "deposit": "Deposit",
             "withdraw": "Withdrawal", "fee": "Fee", "split": "Split", "transfer_in": "Shares in",
             "transfer_out": "Shares out", "reinvest": "Dividend reinvested", "other": "Other"}


def add_txn(aid: int, d: str, kind: str, ticker: str | None, shares: float | None, price: float | None,
            amount: float | None, fees: float | None, description: str = "", source: str = "manual",
            ext_id: str | None = None) -> bool:
    """Stores one transaction; a repeat of the same broker transaction (same ext_id) is ignored. True if new."""
    cur = db.get().execute(
        "INSERT OR IGNORE INTO txns(account_id, date, kind, ticker, shares, price, amount, fees, description, source,"
        " ext_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (aid, d, kind, _t(ticker) if ticker else None, shares, price, amount, fees, description[:300], source, ext_id))
    return cur.rowcount > 0


def txns(account_ids: list[int] | None = None, kinds: list[str] | None = None, since: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM txns WHERE 1=1", []
    if account_ids is not None:
        q += f" AND account_id IN ({','.join('?' * len(account_ids)) or 'NULL'})"
        args += account_ids
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    if since:
        q += " AND date >= ?"
        args.append(since)
    return [dict(r) for r in db.rows(q + " ORDER BY date DESC, id DESC", args)]


def rebuild_lots_from_txns(aid: int, method: str = "fifo") -> dict:
    """Purchases and sales rebuilt from the account's buy, sell, reinvest and share-transfer history (oldest first),
    replacing its purchases and closed purchases. Splits in the history scale the open purchases."""
    hist = sorted(txns([aid], ["buy", "sell", "reinvest", "transfer_in", "transfer_out", "split"]),
                  key=lambda t: (t["date"], 0 if t["kind"] in ("buy", "reinvest", "transfer_in", "split") else 1,
                                 t["id"]))
    with db.tx() as con:
        con.execute("DELETE FROM lots WHERE account_id=?", (aid,))
        con.execute("DELETE FROM closed_lots WHERE account_id=?", (aid,))
    unmatched = 0.0
    for t in hist:
        if not t["ticker"] or not t["shares"]:
            continue
        q = abs(float(t["shares"]))
        if t["kind"] in ("buy", "reinvest", "transfer_in"):
            px = t["price"] if t["price"] else (abs(t["amount"] or 0) / q if q else 0)
            fee_ps = (t["fees"] or 0) / q if q else 0
            add_lot(aid, t["ticker"], t["date"], q, (px or 0) + fee_ps, source="history", record_txn=False)
        elif t["kind"] == "split":
            # a split row carries the extra shares received; scale every open purchase of that ticker
            open_ = lots([aid], [t["ticker"]])
            have = sum(x["shares"] for x in open_)
            if have > 0:
                f = (have + float(t["shares"])) / have
                for x in open_:
                    update_lot(x["id"], shares=x["shares"] * f, cost_ps=x["cost_ps"] / f)
        else:
            have = sum(x["shares"] for x in lots([aid], [t["ticker"]]))
            if have + 1e-6 < q:
                unmatched += q - have
            px = t["price"] if t["price"] else (abs(t["amount"] or 0) / q if q else 0)
            if have > 0:
                sell(aid, t["ticker"], t["date"], min(q, have), px or 0, method, fees=t["fees"] or 0,
                     source="history", record_txn=False)
    update_account(aid, lot_source="history")
    return {"open": len(lots([aid])), "closed": len(lots([aid], closed=True)), "unmatched_shares": unmatched}


# ===================================================================== baskets
def baskets() -> list[dict]:
    out = [dict(r) for r in db.rows("SELECT * FROM baskets ORDER BY COALESCE(sort, id), id")]
    mem = defaultdict(list)
    for r in db.rows("SELECT basket_id, ticker FROM basket_members ORDER BY ticker"):
        mem[r["basket_id"]].append(r["ticker"])
    for b in out:
        b["tickers"] = mem.get(b["id"], [])
    return out


def add_basket(name: str, color: str = "", note: str = "", target: float | None = None) -> int:
    n = db.scalar("SELECT COALESCE(MAX(sort), 0) + 1 FROM baskets", default=1)
    cur = db.get().execute("INSERT INTO baskets(name, color, note, target, sort) VALUES(?,?,?,?,?)",
                           (name.strip() or "Basket", color, note, target, n))
    return int(cur.lastrowid)


def update_basket(bid: int, **kw) -> None:
    kv = {k: v for k, v in kw.items() if k in {"name", "color", "note", "target", "sort"}}
    if kv:
        db.get().execute(f"UPDATE baskets SET {', '.join(k + '=?' for k in kv)} WHERE id=?", (*kv.values(), bid))


def delete_basket(bid: int) -> None:
    with db.tx() as con:
        con.execute("DELETE FROM basket_members WHERE basket_id=?", (bid,))
        con.execute("DELETE FROM baskets WHERE id=?", (bid,))


def set_members(bid: int, tickers: list[str]) -> None:
    with db.tx() as con:
        con.execute("DELETE FROM basket_members WHERE basket_id=?", (bid,))
        for t in sorted({_t(x) for x in tickers if x}):
            con.execute("INSERT INTO basket_members(basket_id, ticker) VALUES(?,?)", (bid, t))


def toggle_member(bid: int, ticker: str, on: bool) -> None:
    if on:
        db.get().execute("INSERT OR IGNORE INTO basket_members(basket_id, ticker) VALUES(?,?)", (bid, _t(ticker)))
    else:
        db.get().execute("DELETE FROM basket_members WHERE basket_id=? AND ticker=?", (bid, _t(ticker)))


def baskets_of() -> dict[str, list[int]]:
    out = defaultdict(list)
    for r in db.rows("SELECT basket_id, ticker FROM basket_members"):
        out[r["ticker"]].append(r["basket_id"])
    return out


@dataclass
class Scope:
    """What the page is looking at: some accounts (None = all) and a basket (None = all, 0 = not in a basket)."""
    account_ids: list[int] | None = None
    basket_id: int | None = None
    label: str = "All holdings"

    def tickers(self, all_tickers: set[str]) -> set[str]:
        if self.basket_id is None:
            return set(all_tickers)
        if self.basket_id == 0:
            inb = {r["ticker"] for r in db.rows("SELECT DISTINCT ticker FROM basket_members")}
            return {t for t in all_tickers if t not in inb}
        mem = {r["ticker"] for r in db.rows("SELECT ticker FROM basket_members WHERE basket_id=?", (self.basket_id,))}
        return {t for t in all_tickers if t in mem}

    @property
    def is_basket(self) -> bool:
        return self.basket_id is not None


# ===================================================================== prices and dividends
class Px:
    """Daily closes and dividend-adjusted closes for a set of tickers, loaded once."""

    def __init__(self, tickers):
        self.d: dict[str, list[str]] = {}
        self.c: dict[str, list[float]] = {}
        self.a: dict[str, list[float]] = {}
        tk = sorted({t for t in tickers if t})
        for t in tk:
            self.d[t], self.c[t], self.a[t] = [], [], []
        if tk:
            for r in db.rows(f"SELECT ticker, date, close, adj_close FROM prices WHERE ticker IN "
                             f"({','.join('?' * len(tk))}) AND close > 0 ORDER BY ticker, date", tk):
                t = r["ticker"]
                self.d[t].append(r["date"])
                self.c[t].append(r["close"])
                self.a[t].append(r["adj_close"] or r["close"])
        self._div: dict[str, list[tuple[str, float]]] = {}

    def has(self, t) -> bool:
        return bool(self.d.get(t))

    def _i(self, t, d) -> int:
        return bisect.bisect_right(self.d.get(t, []), d) - 1

    def close(self, t, d=None) -> float | None:
        if not self.has(t):
            return None
        i = len(self.d[t]) - 1 if d is None else self._i(t, d)
        return self.c[t][i] if i >= 0 else None

    def adj(self, t, d) -> float | None:
        i = self._i(t, d) if self.has(t) else -1
        return self.a[t][i] if i >= 0 else None

    def last_date(self, t) -> str | None:
        return self.d[t][-1] if self.has(t) else None

    def prev_close(self, t) -> float | None:
        return self.c[t][-2] if self.has(t) and len(self.c[t]) > 1 else None

    def dividends(self, t) -> list[tuple[str, float]]:
        """(ex-date, cash per share) from the jumps between the plain and the dividend-adjusted close."""
        if t in self._div:
            return self._div[t]
        out = []
        d, c, a = self.d.get(t, []), self.c.get(t, []), self.a.get(t, [])
        for i in range(1, len(d)):
            if c[i - 1] and a[i - 1] and c[i] and a[i]:
                f = (a[i] / a[i - 1]) / (c[i] / c[i - 1])
                if 1.0004 < f < 1.5:  # a jump of 0.04%+ is a payout; above 50% is bad data
                    out.append((d[i], round(c[i - 1] * (f - 1), 4)))
        self._div[t] = out
        return out

    def ttm_dividend(self, t, d=None) -> float:
        d = d or self.last_date(t) or date.today().isoformat()
        start = (date.fromisoformat(d) - timedelta(days=365)).isoformat()
        return sum(v for x, v in self.dividends(t) if start < x <= d)

    def divs_between(self, t, after: str, until: str | None = None) -> float:
        """Cash per share paid with ex-dates after a purchase date (exclusive) up to a date (inclusive)."""
        return sum(v for x, v in self.dividends(t) if x > after and (until is None or x <= until))


# ===================================================================== holdings and lots, now
def _years(a: str, b: str) -> float:
    return max((date.fromisoformat(b) - date.fromisoformat(a)).days, 0) / 365.25


def _ann(total_ret: float | None, years: float) -> float | None:
    """Yearly rate, only for a year or more (annualizing a few months' move gives meaningless numbers)."""
    if total_ret is None or years < 0.99 or total_ret <= -1:
        return None
    return (1 + total_ret) ** (1 / years) - 1


def lot_metrics(x: dict, px: Px, today: str) -> dict:
    p = px.close(x["ticker"])
    cost = x["shares"] * x["cost_ps"]
    val = x["shares"] * p if p is not None else None
    held = (date.fromisoformat(today) - date.fromisoformat(x["open_date"])).days
    divs = x["shares"] * px.divs_between(x["ticker"], x["open_date"]) if x.get("date_known", 1) else 0.0
    gain = val - cost if val is not None else None
    tot = (gain + divs) / cost if (gain is not None and cost > 0) else None
    long_term = held >= LONG_TERM_DAYS
    return {**x, "price": p, "cost": cost, "value": val, "gain": gain, "gain_pct": gain / cost if gain is not None
            and cost > 0 else None, "divs": divs, "total_ret": tot, "days": held, "long_term": long_term,
            "days_to_long": None if long_term else LONG_TERM_DAYS - held,
            "ann": _ann(tot, held / 365.25) if x.get("date_known", 1) else None}


@dataclass
class Holding:
    ticker: str
    name: str = ""
    cik: int | None = None
    shares: float = 0.0
    cost: float = 0.0
    price: float | None = None
    prev: float | None = None
    value: float | None = None
    day_change: float | None = None
    gain: float | None = None
    divs: float = 0.0
    realized: float = 0.0
    ttm_div_ps: float = 0.0
    first_date: str | None = None
    accounts: list[str] = field(default_factory=list)
    lots: list[dict] = field(default_factory=list)
    weight: float | None = None
    irr: float | None = None
    baskets: list[int] = field(default_factory=list)
    cash_like: bool = False

    @property
    def avg_cost(self):
        return self.cost / self.shares if self.shares else None

    @property
    def gain_pct(self):
        return self.gain / self.cost if (self.gain is not None and self.cost > 0) else None

    @property
    def total_ret(self):
        return (self.gain + self.divs) / self.cost if (self.gain is not None and self.cost > 0) else None

    @property
    def day_pct(self):
        return (self.price / self.prev - 1) if (self.price and self.prev) else None

    @property
    def div_yield(self):
        return self.ttm_div_ps / self.price if self.price else None

    @property
    def yield_on_cost(self):
        return self.ttm_div_ps / self.avg_cost if self.avg_cost else None

    @property
    def income(self):
        return self.ttm_div_ps * self.shares


def holdings(scope: Scope | None = None, px: Px | None = None) -> tuple[list[Holding], Px]:
    scope = scope or Scope()
    open_ = lots(scope.account_ids)
    want = scope.tickers({x["ticker"] for x in open_})
    open_ = [x for x in open_ if x["ticker"] in want]
    closed = [x for x in lots(scope.account_ids, closed=True) if x["ticker"] in want]
    px = px or Px({x["ticker"] for x in open_})
    names = {r["ticker"]: (r["name"], r["cik"]) for r in db.rows("SELECT ticker, name, cik FROM securities")}
    accn = {a["id"]: a["name"] for a in accounts()}
    bof = baskets_of()
    today = date.today().isoformat()
    by: dict[str, Holding] = {}
    for x in open_:
        t = x["ticker"]
        h = by.get(t)
        if h is None:
            nm, cik = names.get(t, ("", None))
            h = by[t] = Holding(t, nm, cik, price=px.close(t), prev=px.prev_close(t), ttm_div_ps=px.ttm_dividend(t),
                                baskets=bof.get(t, []), cash_like=t in CASH_LIKE)
        m = lot_metrics(x, px, today)
        m["account"] = accn.get(x["account_id"], "")
        h.lots.append(m)
        h.shares += x["shares"]
        h.cost += m["cost"]
        h.divs += m["divs"]
        if m["account"] and m["account"] not in h.accounts:
            h.accounts.append(m["account"])
        if x.get("date_known", 1) and (h.first_date is None or x["open_date"] < h.first_date):
            h.first_date = x["open_date"]
    for x in closed:
        if x["ticker"] in by:
            by[x["ticker"]].realized += x["shares"] * (x["price"] - x["cost_ps"])
    tot = 0.0
    for h in by.values():
        if h.price is not None:
            h.value = h.shares * h.price
            h.gain = h.value - h.cost
            tot += h.value
            if h.prev:
                h.day_change = h.shares * (h.price - h.prev)
        flows = [(date.fromisoformat(m["open_date"]), -m["cost"]) for m in h.lots if m.get("date_known", 1)]
        if flows and h.value and (date.fromisoformat(today) - min(f[0] for f in flows)).days >= 361:
            known_val = sum(m["value"] or 0 for m in h.lots if m.get("date_known", 1))
            known_div = sum(m["divs"] for m in h.lots if m.get("date_known", 1))
            h.irr = xirr(sorted(flows) + [(date.fromisoformat(today), known_val + known_div)])
    for h in by.values():
        h.weight = h.value / tot if (tot and h.value is not None) else None
    return sorted(by.values(), key=lambda h: -(h.value or 0)), px


def cash(scope: Scope | None = None) -> float:
    scope = scope or Scope()
    if scope.is_basket:
        return 0.0  # cash belongs to accounts, not baskets
    q, args = "SELECT COALESCE(SUM(cash), 0) FROM accounts", []
    if scope.account_ids is not None:
        q += f" WHERE id IN ({','.join('?' * len(scope.account_ids)) or 'NULL'})"
        args = scope.account_ids
    return float(db.scalar(q, args, default=0.0))


def summary(hs: list[Holding], scope: Scope | None = None) -> dict:
    scope = scope or Scope()
    inv = sum(h.value or 0 for h in hs)
    cost = sum(h.cost for h in hs)
    c = cash(scope)
    day = sum(h.day_change or 0 for h in hs)
    divs = sum(h.divs for h in hs)
    income = sum(h.income for h in hs)
    ytd0 = f"{date.today().year}-01-01"
    rows = [x for x in lots(scope.account_ids, closed=True) if x["close_date"] >= ytd0]
    want = scope.tickers({x["ticker"] for x in rows})
    real_ytd = sum(x["shares"] * (x["price"] - x["cost_ps"]) for x in rows if x["ticker"] in want)
    real_ytd_short = sum(x["shares"] * (x["price"] - x["cost_ps"]) for x in rows if x["ticker"] in want and
                         (date.fromisoformat(x["close_date"]) - date.fromisoformat(x["open_date"])).days
                         < LONG_TERM_DAYS)
    un_short = sum(m["gain"] or 0 for h in hs for m in h.lots if not m["long_term"])
    un_long = sum(m["gain"] or 0 for h in hs for m in h.lots if m["long_term"])
    losers = [m for h in hs for m in h.lots if (m["gain"] or 0) < 0]
    w = [h.weight or 0 for h in hs]
    return {"value": inv + c, "invested": inv, "cash": c, "cost": cost, "gain": inv - cost,
            "gain_pct": (inv - cost) / cost if cost else None, "day": day,
            "day_pct": day / (inv - day) if (inv - day) else None, "divs": divs,
            "income": income, "yield": income / inv if inv else None, "yoc": income / cost if cost else None,
            "realized_ytd": real_ytd, "realized_ytd_short": real_ytd_short, "unreal_short": un_short,
            "unreal_long": un_long, "harvest": sum(m["gain"] for m in losers), "harvest_n": len(losers),
            "n": len(hs), "top_w": max(w) if w else None, "top5_w": sum(sorted(w, reverse=True)[:5]) if w else None,
            "eff_n": 1 / sum(x * x for x in w) if any(w) else None,
            "cash_pct": c / (inv + c) if (inv + c) else None}


def upcoming_income(hs: list[Holding], px: Px) -> list[dict]:
    """Expected payouts over the next 12 months: each dividend paid in the last 12 months, a year later, at today's
    share count (a projection; companies change dividends)."""
    out = []
    today = date.today()
    for h in hs:
        for d, v in px.dividends(h.ticker):
            dd = date.fromisoformat(d)
            if today - timedelta(days=365) < dd <= today:
                nxt = dd + timedelta(days=365)
                out.append({"ticker": h.ticker, "date": nxt.isoformat(), "per_share": v, "amount": v * h.shares})
    return sorted(out, key=lambda x: x["date"])


def income_by_month(scope: Scope, px: Px | None = None, months: int = 24) -> list[tuple[str, float]]:
    """Dividends received per month (shares held on each ex-date × cash per share), oldest first."""
    open_ = lots(scope.account_ids)
    closed = lots(scope.account_ids, closed=True)
    want = scope.tickers({x["ticker"] for x in open_ + closed})
    allx = [x for x in open_ + closed if x["ticker"] in want and x.get("date_known", 1) != 0]
    px = px or Px({x["ticker"] for x in allx})
    start = (date.today().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1).isoformat()
    by = defaultdict(float)
    for x in allx:
        until = x.get("close_date")
        for d, v in px.dividends(x["ticker"]):
            if d > x["open_date"] and (until is None or d <= until) and d >= start:
                by[d[:7]] += v * x["shares"]
    keys = []
    m = date.fromisoformat(start)
    while m <= date.today():
        keys.append(m.isoformat()[:7])
        m = (m + timedelta(days=32)).replace(day=1)
    return [(k, by.get(k, 0.0)) for k in keys]


# ===================================================================== performance over time
PERIODS = {"1M": 30, "3M": 91, "YTD": None, "1Y": 365, "3Y": 365 * 3, "5Y": 365 * 5, "All": 0}


def _period_start(period: str, first: str) -> str:
    today = date.today()
    if period == "YTD":
        s = date(today.year, 1, 1).isoformat()
    elif PERIODS.get(period):
        s = (today - timedelta(days=PERIODS[period])).isoformat()
    else:
        s = first
    return max(s, first)


def history(scope: Scope | None = None, period: str = "All", compare: list[str] | None = None) -> dict:
    """Daily value and time-weighted growth of the holdings in scope (cash excluded), with each comparison ticker's
    growth over the same days. {"dates", "value", "cost", "twr" (growth of 1), "bench": {ticker: growth of 1},
    "start", "missing": [tickers without prices]}."""
    scope = scope or Scope()
    compare = [_t(c) for c in (compare if compare is not None else DEFAULT_COMPARE) if c]
    open_ = lots(scope.account_ids)
    closed = lots(scope.account_ids, closed=True)
    want = scope.tickers({x["ticker"] for x in open_ + closed})
    items = [x for x in open_ + closed if x["ticker"] in want]
    if not items:
        return {"dates": [], "value": [], "twr": [], "bench": {}, "missing": []}
    # a purchase with an unknown date carries the day it was entered, so its history starts then
    tickers = {x["ticker"] for x in items}
    px = Px(tickers | set(compare) | {"SPY"})
    cal = px.d.get("SPY") or sorted({d for t in tickers for d in px.d.get(t, [])})
    first = min(x["open_date"] for x in items)
    s = _period_start(period, first)
    lo = bisect.bisect_left(cal, first)
    dates = cal[lo:]
    if len(dates) < 2:
        return {"dates": dates, "value": [], "twr": [], "bench": {}, "missing": []}
    # shares of each ticker held at each day's close
    n = len(dates)
    delta: dict[str, list[float]] = {t: [0.0] * (n + 1) for t in tickers}
    cost_delta = [0.0] * (n + 1)
    for x in items:
        i = bisect.bisect_left(dates, x["open_date"])
        j = bisect.bisect_left(dates, x["close_date"]) if x.get("close_date") else n
        if i >= n or j <= i:
            continue
        delta[x["ticker"]][i] += x["shares"]
        delta[x["ticker"]][j] -= x["shares"]
        cost_delta[i] += x["shares"] * x["cost_ps"]
        cost_delta[j] -= x["shares"] * x["cost_ps"]
    sh = {}
    for t in tickers:
        run, arr = 0.0, []
        for k in range(n):
            run += delta[t][k]
            arr.append(run)
        sh[t] = arr
    cl = {t: [px.close(t, d) for d in dates] for t in tickers}
    aj = {t: [px.adj(t, d) for d in dates] for t in tickers}
    value, cost, twr = [], [], [1.0]
    run_cost = 0.0
    for k in range(n):
        run_cost += cost_delta[k]
        value.append(sum(sh[t][k] * (cl[t][k] or 0) for t in tickers))
        cost.append(run_cost)
        if k == 0:
            continue
        base = sum(sh[t][k - 1] * (cl[t][k - 1] or 0) for t in tickers)
        if base <= 0:
            twr.append(twr[-1])
            continue
        gain = sum(sh[t][k - 1] * (cl[t][k - 1] or 0) * ((aj[t][k] / aj[t][k - 1] - 1) if aj[t][k] and aj[t][k - 1]
                                                         else 0) for t in tickers)
        twr.append(twr[-1] * (1 + gain / base))
    # cut to the period and rebase to 1 at its start
    a = bisect.bisect_left(dates, s)
    a = min(a, n - 1)
    dates, value, cost, twr = dates[a:], value[a:], cost[a:], twr[a:]
    t0 = twr[0] or 1.0
    twr = [v / t0 for v in twr]
    bench, missing = {}, []
    for c in compare:
        if not px.has(c):
            missing.append(c)
            continue
        b0 = px.adj(c, dates[0])
        if not b0:
            missing.append(c)
            continue
        bench[c] = [(px.adj(c, d) or b0) / b0 for d in dates]
    return {"dates": dates, "value": value, "cost": cost, "twr": twr, "bench": bench, "missing": missing,
            "start": dates[0], "first": first}


def same_money(scope: Scope, ticker: str = "SPY") -> dict | None:
    """Every purchase's cost put into `ticker` on the same day, and taken out on the day it was sold: what that would
    be worth now, against what your purchases are worth (plus sale proceeds and dividends)."""
    open_ = lots(scope.account_ids)
    closed = lots(scope.account_ids, closed=True)
    want = scope.tickers({x["ticker"] for x in open_ + closed})
    items = [x for x in open_ + closed if x["ticker"] in want and x.get("date_known", 1)]
    if not items:
        return None
    t = _t(ticker)
    px = Px({x["ticker"] for x in items} | {t})
    if not px.has(t):
        return None
    last = px.last_date(t)
    mine, theirs, put = 0.0, 0.0, 0.0
    flows_m, flows_b = [], []
    for x in items:
        b0 = px.adj(t, x["open_date"])
        if not b0:
            continue
        c = x["shares"] * x["cost_ps"]
        put += c
        units = c / b0
        od = date.fromisoformat(x["open_date"])
        flows_m.append((od, -c))
        flows_b.append((od, -c))
        if x.get("close_date"):
            proceeds = x["shares"] * x["price"] + x["shares"] * px.divs_between(x["ticker"], x["open_date"],
                                                                                 x["close_date"])
            b = units * (px.adj(t, x["close_date"]) or b0)
            cd = date.fromisoformat(x["close_date"])
            flows_m.append((cd, proceeds))
            flows_b.append((cd, b))
        else:
            p = px.close(x["ticker"]) or 0
            proceeds = x["shares"] * p + x["shares"] * px.divs_between(x["ticker"], x["open_date"])
            b = units * (px.adj(t, last) or b0)
            flows_m.append((date.fromisoformat(last), proceeds))
            flows_b.append((date.fromisoformat(last), b))
        mine += proceeds
        theirs += b
    return {"ticker": t, "put_in": put, "mine": mine, "theirs": theirs,
            "irr_mine": xirr(sorted(flows_m)), "irr_theirs": xirr(sorted(flows_b))}


# ===================================================================== statistics
def _rets(g: list[float]) -> list[float]:
    return [g[i] / g[i - 1] - 1 for i in range(1, len(g)) if g[i - 1]]


def _month_ends(dates: list[str], g: list[float]) -> list[tuple[str, float]]:
    out = []
    for i, d in enumerate(dates):
        if i + 1 == len(dates) or dates[i + 1][:7] != d[:7]:
            out.append((d[:7], g[i]))
    return out


def risk_free() -> float:
    """Yearly risk-free rate for Sharpe and Sortino: the latest 3-month Treasury bill yield if the app has it, else 4%."""
    v = db.scalar("SELECT value FROM macro WHERE series IN ('DTB3','DGS3MO') AND value IS NOT NULL ORDER BY date DESC "
                  "LIMIT 1")
    return float(v) / 100 if v is not None else 0.04


def stats(dates: list[str], g: list[float], bench: list[float] | None = None) -> dict:
    """Return and risk statistics of a growth-of-1 series (daily), optionally against a benchmark's."""
    if len(g) < 3:
        return {}
    yrs = max(_years(dates[0], dates[-1]), 1 / 365)
    tot = g[-1] / g[0] - 1
    r = _rets(g)
    mu = sum(r) / len(r)
    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / max(len(r) - 1, 1))
    vol = sd * math.sqrt(252)
    cagr = (1 + tot) ** (1 / yrs) - 1 if yrs >= 0.99 else None
    ann = cagr if cagr is not None else (1 + tot) ** (1 / yrs) - 1
    rf = risk_free()
    down = [min(x - rf / 252, 0) for x in r]
    dd_dev = math.sqrt(sum(x * x for x in down) / len(down)) * math.sqrt(252)
    peak, mdd, mdd_at, cur_dd = g[0], 0.0, dates[0], 0.0
    for d, v in zip(dates, g):
        peak = max(peak, v)
        x = v / peak - 1
        if x < mdd:
            mdd, mdd_at = x, d
        cur_dd = x
    me = _month_ends(dates, g)
    mr = [(me[i][0], me[i][1] / me[i - 1][1] - 1) for i in range(1, len(me))]
    out = {"total": tot, "cagr": cagr, "ann": ann, "vol": vol, "max_dd": mdd, "max_dd_at": mdd_at, "dd_now": cur_dd,
           "sharpe": (ann - rf) / vol if vol else None, "sortino": (ann - rf) / dd_dev if dd_dev else None,
           "best_month": max(mr, key=lambda x: x[1]) if mr else None,
           "worst_month": min(mr, key=lambda x: x[1]) if mr else None,
           "pos_months": sum(1 for _, x in mr if x > 0) / len(mr) if mr else None, "rf": rf, "years": yrs}
    if bench and len(bench) == len(g):
        rb = _rets(bench)
        k = min(len(r), len(rb))
        r2, rb2 = r[-k:], rb[-k:]
        mb = sum(rb2) / k
        cov = sum((a - mu) * (b - mb) for a, b in zip(r2, rb2)) / max(k - 1, 1)
        vb = sum((b - mb) ** 2 for b in rb2) / max(k - 1, 1)
        sdb = math.sqrt(vb)
        beta = cov / vb if vb else None
        btot = bench[-1] / bench[0] - 1
        bann = (1 + btot) ** (1 / yrs) - 1
        mb_ = _month_ends(dates, bench)
        mrb = [mb_[i][1] / mb_[i - 1][1] - 1 for i in range(1, len(mb_))]
        pairs = list(zip([x for _, x in mr], mrb))
        up = [(a, b) for a, b in pairs if b > 0]
        dn = [(a, b) for a, b in pairs if b < 0]

        def cap(ps):
            if not ps:
                return None
            ga = math.prod(1 + a for a, _ in ps) ** (1 / len(ps)) - 1
            gb = math.prod(1 + b for _, b in ps) ** (1 / len(ps)) - 1
            return ga / gb if gb else None
        te = math.sqrt(sum((a - b - (mu - mb)) ** 2 for a, b in zip(r2, rb2)) / max(k - 1, 1)) * math.sqrt(252)
        out.update({"bench_total": btot, "bench_ann": bann, "excess": ann - bann, "beta": beta,
                    "corr": cov / (sd * sdb) if sd and sdb else None,
                    "alpha": (ann - rf) - beta * (bann - rf) if beta is not None else None,
                    "up_capture": cap(up), "down_capture": cap(dn), "tracking_error": te,
                    "info_ratio": (ann - bann) / te if te else None})
    return out


def sector_mix(hs: list[Holding]) -> list[tuple[str, float]]:
    from .sectors import sector_of_sic

    sic = {r["ticker"]: r["sic"] for r in db.rows("SELECT ticker, sic FROM securities")}
    by = defaultdict(float)
    for h in hs:
        if h.value is None:
            continue
        if h.cash_like:
            k = "Cash-like"
        elif h.ticker in sic:
            k = sector_of_sic(sic[h.ticker])
            k = "Other" if k in ("Unknown",) else k
        else:
            k = "Funds & ETFs"
        by[k] += h.value
    tot = sum(by.values()) or 1
    return sorted(((k, v / tot) for k, v in by.items()), key=lambda x: -x[1])


def held_tickers() -> list[str]:
    return [r["ticker"] for r in db.rows("SELECT DISTINCT ticker FROM lots WHERE shares > 0 ORDER BY ticker")]


def compare_tickers() -> list[str]:
    return list(db.cfg("holdings_compare", DEFAULT_COMPARE) or [])


def reconcile() -> list[dict]:
    """Where a broker's share count and the purchases on file disagree (purchases missing or out of date)."""
    have = defaultdict(float)
    for r in db.rows("SELECT account_id, ticker, SUM(shares) s FROM lots GROUP BY account_id, ticker"):
        have[(r["account_id"], r["ticker"])] = r["s"]
    out = []
    for r in db.rows("SELECT * FROM broker_positions"):
        h = have.get((r["account_id"], r["ticker"]), 0.0)
        if abs(h - (r["shares"] or 0)) > 1e-3:
            out.append({"account_id": r["account_id"], "ticker": r["ticker"], "broker": r["shares"], "lots": h,
                        "avg_cost": r["avg_cost"]})
    return out


def apply_positions(aid: int, positions: list[dict], source: str, fill_missing: bool = True) -> dict:
    """Store a broker's current positions for an account. Tickers with no purchases on file get one purchase at the
    broker's average cost with the date unknown, so they show up straight away; share counts that disagree with the
    purchases on file are listed for review (reconcile())."""
    asof = date.today().isoformat()
    with db.tx() as con:
        con.execute("DELETE FROM broker_positions WHERE account_id=?", (aid,))
        for p in positions:
            con.execute("INSERT OR REPLACE INTO broker_positions(account_id, ticker, shares, avg_cost, market_value, "
                        "asof) VALUES(?,?,?,?,?,?)", (aid, _t(p["ticker"]), p["shares"], p.get("avg_cost"),
                                                      p.get("market_value"), asof))
    added = 0
    if fill_missing:
        have = {x["ticker"] for x in lots([aid])}
        for p in positions:
            if _t(p["ticker"]) not in have and p["shares"] > 0:
                add_lot(aid, p["ticker"], None, p["shares"], p.get("avg_cost") or 0, source=source,
                        note="from positions; purchase date unknown", record_txn=False)
                added += 1
    return {"positions": len(positions), "added": added, "mismatch": len([r for r in reconcile()
                                                                          if r["account_id"] == aid])}


def fix_mismatch(aid: int, ticker: str) -> None:
    """Make the purchases match the broker's share count: scale them (shares and cost per share kept at the broker's
    average) when there are some, or add one undated purchase."""
    bp = db.one("SELECT * FROM broker_positions WHERE account_id=? AND ticker=?", (aid, ticker))
    if not bp:
        return
    open_ = lots([aid], [ticker])
    have = sum(x["shares"] for x in open_)
    want = bp["shares"] or 0
    if want <= 0:
        for x in open_:
            delete_lot(x["id"])
    elif have <= 0:
        add_lot(aid, ticker, None, want, bp["avg_cost"] or 0, source="positions", record_txn=False)
    elif want > have:
        add_lot(aid, ticker, None, want - have, bp["avg_cost"] or 0, source="positions",
                note="added to match the broker's share count; purchase date unknown", record_txn=False)
    else:
        f = want / have
        for x in open_:
            update_lot(x["id"], shares=x["shares"] * f)

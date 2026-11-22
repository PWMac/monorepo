"""Getting holdings in: brokerage files (CSV / JSON exports), the Schwab Trader API, and SnapTrade.

Files
- Positions (any broker; Schwab's "Positions" export, Fidelity's "Portfolio Positions", or the app's template):
  current shares, average cost and cash per account.
- Purchases with lots (Schwab's lot-details export, StreetSmart's "Export Positions with Lots", or the template):
  every open purchase with its date and cost per share. These replace the account's purchases.
- Transactions (Schwab's History export as CSV or JSON, or the template): buys, sells, dividends, deposits...
  Stored once (re-importing an overlapping file adds nothing twice); purchases can be rebuilt from them.

Schwab Trader API (developer.schwab.com, "Trader API - Individual")
- OAuth: you sign in at Schwab, it sends the browser to your callback URL with a code, and you paste that address
  back into the app. The connection lasts 7 days (Schwab's limit), then you sign in again.
- Gives accounts, positions (average cost, not purchase dates), cash, and up to a year of transactions per call.

SnapTrade Personal (snaptrade.com/personal, free for personal use)
- Connect Fidelity, Vanguard, Robinhood, E*TRADE, Schwab and others in SnapTrade's dashboard, then use your Personal
  client ID and consumer key here. Gives accounts, positions (with purchase lots where the broker shares them),
  cash and activity.

Nothing here places orders.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import db, holdings, secrets


# ===================================================================== parsing helpers
def num(v) -> float | None:
    """'$1,234.56' -> 1234.56; '(12.30)' and '-$12.30' -> -12.3; '--', 'N/A', '' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "--", "-", "N/A", "n/a", "NA"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def pdate(v) -> str | None:
    """'11/20/2026', '11/20/2026 as of 11/19/2026', '2026-11-20', '2026/11/20', '20-Nov-2026' -> '2026-11-20'."""
    if not v:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        mo, d, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    for f in ("%d-%b-%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s[:12].strip(), f).date().isoformat()
        except ValueError:
            continue
    return None


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9/]+", " ", (h or "").lower()).strip()


COLS = {
    "symbol": ["symbol", "ticker", "security", "symbol cusip"],
    "account": ["account name", "account", "account number", "acct"],
    "qty": ["qty quantity", "quantity", "qty", "shares", "units", "share quantity"],
    "price": ["price", "last price", "current price", "market price"],
    "value": ["mkt val market value", "market value", "current value", "value"],
    "cost_ps": ["cost/share", "cost per share", "unit cost", "average cost basis", "avg cost", "average cost",
                "price paid", "purchase price", "cost share", "share cost", "trans cost/share", "average price"],
    "cost_total": ["cost basis", "cost basis total", "total cost", "cost total", "cost", "trans cost total"],
    "date": ["open date", "date acquired", "acquired date", "acquired", "acquisition date", "purchase date",
             "trade date", "date", "opened"],
    "type": ["security type", "type", "asset type"],
    "action": ["action", "transaction", "activity", "type"],
    "fees": ["fees comm", "fees & comm", "fees", "commission", "fee"],
    "amount": ["amount", "net amount", "total"],
    "desc": ["description", "name", "security description"],
}


def _find(header: list[str], key: str, avoid: set[int] = frozenset()) -> int | None:
    hs = [_norm(h) for h in header]
    for cand in COLS[key]:
        c = _norm(cand)
        for i, h in enumerate(hs):
            if i not in avoid and h == c:
                return i
    for cand in COLS[key]:
        c = _norm(cand)
        for i, h in enumerate(hs):
            if i not in avoid and h.startswith(c + " "):
                return i
    return None


def _rows(text: str) -> list[list[str]]:
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]


def _is_header(r: list[str]) -> bool:
    return _find(r, "symbol") is not None and (_find(r, "qty") is not None or _find(r, "action") is not None)


def _clean_symbol(s: str) -> str | None:
    s = (s or "").strip().upper()
    if not s or s in ("--", "CASH & CASH INVESTMENTS", "ACCOUNT TOTAL", "TOTAL", "PENDING ACTIVITY") \
            or s.startswith("CASH") or s.startswith("ACCOUNT TOTAL") or " " in s and not re.match(r"^[A-Z.]+ [A-Z]$", s):
        return None
    s = s.replace("/", ".").replace("**", "").strip("* ")
    return s if re.match(r"^[A-Z0-9.\-]{1,12}$", s) else None


def _acct_label(r: list[str]) -> str | None:
    """A line naming an account in a multi-account export: 'Individual ...123', 'Positions for account Roth ...456'."""
    cells = [c for c in r if c]
    if len(cells) != 1:
        return None
    s = cells[0]
    m = re.search(r"(?:positions for (?:account )?)?(.+?)(?: as of .*)?$", s, re.I)
    lab = (m[1] if m else s).strip()
    return lab if lab and len(lab) < 80 else None


def tail_of(label: str) -> str:
    m = re.search(r"(\d{3,4})\s*$", label or "")
    return m[1] if m else ""


# ===================================================================== file kinds
def sniff(text: str, filename: str = "") -> str:
    """positions | lots | transactions | json_transactions | unknown"""
    t = text.lstrip("﻿").strip()
    if t.startswith("{") or t.startswith("["):
        return "json_transactions"
    for r in _rows(text)[:60]:
        if _is_header(r):
            if _find(r, "action") is not None and _norm(r[_find(r, "action")]) == "action":
                return "transactions"
            di = _find(r, "date")
            if di is not None and _find(r, "qty") is not None and _find(r, "action") is None:
                return "lots"
            if _find(r, "action") is not None and _find(r, "amount") is not None and _find(r, "date") is not None:
                return "transactions"
            return "positions"
    return "unknown"


def parse_positions(text: str) -> dict:
    """{account label: {"positions": [{ticker, shares, avg_cost, market_value}], "cash": float|None}}"""
    out: dict[str, dict] = {}
    cur = "Imported account"
    hdr = None
    for r in _rows(text):
        if not any(r):
            continue
        if _is_header(r):
            hdr = r
            continue
        if hdr is None:
            lab = _acct_label(r)
            if lab:
                cur = lab
            continue
        lab = _acct_label(r)
        if lab and not _clean_symbol(lab):
            cur, hdr = lab, None  # a new account section; its header follows
            continue
        si = _find(hdr, "symbol")
        ai = _find(hdr, "account")
        acct = (r[ai] if ai is not None and ai < len(r) and r[ai] else cur)
        a = out.setdefault(acct, {"positions": [], "cash": None})
        raw = r[si] if si < len(r) else ""
        if raw.upper().startswith("CASH") or "MONEY MARKET" in raw.upper() or "CORE" in raw.upper():
            vi = _find(hdr, "value")
            v = num(r[vi]) if vi is not None and vi < len(r) else None
            if v is not None:
                a["cash"] = (a["cash"] or 0) + v
            continue
        sym = _clean_symbol(raw)
        if not sym:
            continue
        g = lambda k: (num(r[i]) if (i := _find(hdr, k)) is not None and i < len(r) else None)  # noqa: E731
        q = g("qty")
        if not q:
            continue
        cps = g("cost_ps")
        ct = g("cost_total")
        if cps is None and ct is not None and q:
            cps = ct / q
        a["positions"].append({"ticker": sym, "shares": q, "avg_cost": cps, "market_value": g("value")})
    return {k: v for k, v in out.items() if v["positions"] or v["cash"] is not None}


def parse_lots(text: str) -> dict:
    """{account label: [{ticker, open_date, shares, cost_ps}]}. Rows without a date are position totals (they name
    the ticker for the lot rows under them)."""
    out: dict[str, list] = {}
    cur = "Imported account"
    hdr = None
    sym = None
    for r in _rows(text):
        if not any(r):
            continue
        if _is_header(r):
            hdr = r
            continue
        if hdr is None:
            lab = _acct_label(r)
            if lab:
                cur = lab
            continue
        lab = _acct_label(r)
        if lab and not _clean_symbol(lab):
            cur, hdr = lab, None
            continue
        g = lambda k: (r[i] if (i := _find(hdr, k)) is not None and i < len(r) else "")  # noqa: E731
        s = _clean_symbol(g("symbol"))
        if s:
            sym = s
        d = pdate(g("date"))
        if not d or not sym:
            continue
        q = num(g("qty"))
        if not q:
            continue
        cps = num(g("cost_ps"))
        ct = num(g("cost_total"))
        if cps is None and ct is not None:
            cps = ct / q
        acct = g("account") or cur
        out.setdefault(acct, []).append({"ticker": sym, "open_date": d, "shares": q, "cost_ps": cps or 0.0})
    return out


ACTIONS = [  # (pattern, kind); first match wins
    (r"reinvest shares|qual div reinvest share|buy.*reinvest", "reinvest"),
    (r"^buy|bought|purchase|^buy to", "buy"),
    (r"^sell|sold|^sell to", "sell"),
    (r"stock split|^split|forward split", "split"),
    (r"dividend|div\b|cap gain|capital gain|distribution", "div"),
    (r"interest", "interest"),
    (r"fee|adr mgmt|foreign tax", "fee"),
    (r"journaled shares|security transfer|shares (in|out)|receive and deliver|transfer of security|internal transfer"
     r"|spin.?off|stock merger", "shares"),
    (r"moneylink|wire|funds received|deposit|transfer|journal|contribution|ach|electronic", "cash"),
]


def _kind(action: str, qty: float | None, amount: float | None) -> str:
    a = (action or "").lower()
    for pat, k in ACTIONS:
        if re.search(pat, a):
            if k == "shares":
                return "transfer_in" if (qty or 0) >= 0 else "transfer_out"
            if k == "cash":
                if qty and not amount:
                    return "transfer_in" if qty > 0 else "transfer_out"
                return "deposit" if (amount or 0) >= 0 else "withdraw"
            return k
    return "other"


def _txn_rows(records: list[dict]) -> list[dict]:
    out, seen = [], {}
    for rec in records:
        d = pdate(rec.get("Date") or rec.get("date") or rec.get("Trade Date") or rec.get("Run Date"))
        if not d:
            continue
        act = rec.get("Action") or rec.get("action") or rec.get("Type") or ""
        sym = _clean_symbol(rec.get("Symbol") or rec.get("symbol") or "")
        q = num(rec.get("Quantity") or rec.get("Qty") or rec.get("Shares"))
        p = num(rec.get("Price") or rec.get("Price ($)"))
        f = num(rec.get("Fees & Comm") or rec.get("Fees") or rec.get("Commission")) or 0.0
        amt = num(rec.get("Amount") or rec.get("Amount ($)") or rec.get("Net Amount"))
        k = _kind(act, q, amt)
        if k == "sell" and q:
            q = abs(q)
        if k in ("buy", "reinvest") and q:
            q = abs(q)
        key = "|".join(str(x) for x in (d, act, sym, q, p, amt))
        seen[key] = seen.get(key, 0) + 1
        out.append({"date": d, "kind": k, "ticker": sym, "shares": q, "price": p, "amount": amt, "fees": f,
                    "description": f"{act} {rec.get('Description') or ''}".strip(),
                    "ext_id": "file:" + hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:16]})
    return out


def parse_transactions(text: str) -> list[dict]:
    t = text.lstrip("﻿").strip()
    if t.startswith("{") or t.startswith("["):
        js = json.loads(t)
        recs = js.get("BrokerageTransactions") or js.get("transactions") or [] if isinstance(js, dict) else js
        return _txn_rows(recs)
    rows = _rows(text)
    hdr, recs = None, []
    for r in rows:
        if not any(r):
            continue
        if hdr is None:
            if _find(r, "date") is not None and (_find(r, "action") is not None or _find(r, "amount") is not None):
                hdr = r
            continue
        if r[0].lower().startswith("transactions total") or len(r) < 3:
            continue
        recs.append({hdr[i]: (r[i] if i < len(r) else "") for i in range(len(hdr))})
    return _txn_rows(recs)


# ===================================================================== importing
def _account_for(label: str, source: str, account_id: int | None, broker: str) -> int:
    if account_id:
        return account_id
    tail = tail_of(label)
    for a in holdings.accounts():
        if a["name"] == label or (tail and a["tail"] == tail and a["broker"] == broker):
            return a["id"]
    return holdings.add_account(label, broker, source, tail)


def import_file(text: str, filename: str = "", account_id: int | None = None, broker: str = "",
                kind: str | None = None, rebuild: bool = False) -> dict:
    """Import one export. account_id puts everything into that account (single-account files); otherwise accounts
    are matched by name or the last digits of the number, and created when new."""
    kind = kind or sniff(text, filename)
    res = {"kind": kind, "accounts": [], "rows": 0, "messages": []}
    if kind == "positions":
        data = parse_positions(text)
        for lab, v in data.items():
            aid = _account_for(lab, "file", account_id, broker)
            r = holdings.apply_positions(aid, v["positions"], "file")
            if v["cash"] is not None:
                holdings.update_account(aid, cash=v["cash"])
            holdings.update_account(aid, synced_at=db.now())
            res["accounts"].append(aid)
            res["rows"] += r["positions"]
            res["messages"].append(f"{lab}: {r['positions']} positions" + (f", cash {v['cash']:,.2f}" if v["cash"]
                                   is not None else "") + (f"; {r['added']} without purchase dates yet" if r["added"]
                                                            else "") + (f"; {r['mismatch']} share counts to check"
                                                                        if r["mismatch"] else ""))
    elif kind == "lots":
        data = parse_lots(text)
        for lab, ls in data.items():
            aid = _account_for(lab, "file", account_id, broker)
            holdings.replace_lots(aid, ls, "lots file")
            res["accounts"].append(aid)
            res["rows"] += len(ls)
            res["messages"].append(f"{lab}: {len(ls)} purchases across {len({x['ticker'] for x in ls})} holdings")
    elif kind in ("transactions", "json_transactions"):
        ts = parse_transactions(text)
        aid = account_id or _account_for("Imported account", "file", None, broker)
        new = sum(1 for t in ts if holdings.add_txn(aid, t["date"], t["kind"], t["ticker"], t["shares"], t["price"],
                                                   t["amount"], t["fees"], t["description"], "file", t["ext_id"]))
        res["accounts"].append(aid)
        res["rows"] = new
        span = f" from {min(t['date'] for t in ts)} to {max(t['date'] for t in ts)}" if ts else ""
        res["messages"].append(f"{new} new transactions{span} ({len(ts) - new} already on file)")
        if rebuild:
            r = holdings.rebuild_lots_from_txns(aid)
            res["messages"].append(f"Purchases rebuilt from the history: {r['open']} open, {r['closed']} sold"
                                   + (f"; {r['unmatched_shares']:g} shares sold that the history never bought (it "
                                      "starts too late)" if r["unmatched_shares"] else ""))
    else:
        res["messages"].append("Couldn't tell what this file is: no header row with a Symbol and a Quantity or "
                               "Action column was found.")
    return res


TEMPLATE_LOTS = "Account,Symbol,Date acquired,Shares,Cost per share\nMy account,KO,2021-03-15,25,51.20\n" \
                "My account,KO,2023-08-01,10,60.05\n"
TEMPLATE_TXNS = "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n" \
                "03/15/2021,Buy,KO,COCA COLA CO,25,51.20,0,-1280.00\n04/01/2021,Qualified Dividend,KO,COCA COLA CO,,,," \
                "10.50\n"


# ===================================================================== Schwab Trader API
class SchwabError(Exception):
    pass


class Schwab:
    AUTH = "https://api.schwabapi.com/v1/oauth/authorize"
    TOKEN = "https://api.schwabapi.com/v1/oauth/token"
    BASE = "https://api.schwabapi.com/trader/v1"
    REFRESH_DAYS = 7
    TXN_TYPES = ("TRADE,RECEIVE_AND_DELIVER,DIVIDEND_OR_INTEREST,ACH_RECEIPT,ACH_DISBURSEMENT,CASH_RECEIPT,"
                 "CASH_DISBURSEMENT,ELECTRONIC_FUND,WIRE_OUT,WIRE_IN,JOURNAL")

    def __init__(self, session=None):
        import requests

        self.http = session or requests.Session()

    # -- settings (keys in Windows Credential Manager) --
    @staticmethod
    def app_key():
        return secrets.get("schwab_app_key")

    @staticmethod
    def callback():
        return db.cfg("schwab_callback", "https://127.0.0.1:8182")

    @staticmethod
    def save_app(key: str, secret: str, callback: str) -> None:
        secrets.set("schwab_app_key", key.strip() or None)
        if secret:
            secrets.set("schwab_app_secret", secret.strip())
        db.set_cfg("schwab_callback", callback.strip() or "https://127.0.0.1:8182")

    _access_cache: dict = {}  # the 30-minute access token stays in memory (Credential Manager holds the refresh token)

    @classmethod
    def tokens(cls) -> dict:
        t = secrets.get("schwab_tokens")
        return {**(json.loads(t) if t else {}), **cls._access_cache}

    @classmethod
    def _save_tokens(cls, t: dict) -> None:
        cls._access_cache.clear()
        if t.get("access_token"):
            cls._access_cache.update(access_token=t["access_token"], expires_at=t.get("expires_at", 0))
        keep = {k: t[k] for k in ("refresh_token", "refresh_issued") if t.get(k)}
        secrets.set("schwab_tokens", json.dumps(keep) if keep else None)

    @classmethod
    def disconnect(cls) -> None:
        cls._save_tokens({})

    @classmethod
    def status(cls) -> dict:
        t = cls.tokens()
        if not cls.app_key():
            return {"state": "no_app", "text": "Not set up"}
        if not t.get("refresh_token"):
            return {"state": "signed_out", "text": "Sign in needed"}
        left = t.get("refresh_issued", 0) + cls.REFRESH_DAYS * 86400 - time.time()
        if left <= 0:
            return {"state": "expired", "text": "Sign-in expired (Schwab allows 7 days): sign in again"}
        return {"state": "ok", "text": f"Connected · sign in again within {left / 86400:.1f} days",
                "days_left": left / 86400}

    def authorize_url(self) -> str:
        return f"{self.AUTH}?client_id={quote(self.app_key() or '')}&redirect_uri={quote(self.callback(), safe='')}"

    def _token(self, data: dict) -> dict:
        key, sec = self.app_key(), secrets.get("schwab_app_secret")
        if not key or not sec:
            raise SchwabError("Enter the app key and secret first.")
        auth = base64.b64encode(f"{key}:{sec}".encode()).decode()
        r = self.http.post(self.TOKEN, data=data, headers={"Authorization": f"Basic {auth}",
                                                            "Content-Type": "application/x-www-form-urlencoded"},
                           timeout=30)
        if r.status_code != 200:
            raise SchwabError(f"Schwab refused the sign-in ({r.status_code}): {r.text[:200]}")
        return r.json()

    def finish_login(self, redirected: str) -> None:
        """Exchange the code in the address Schwab sent the browser to (pasted by the user) for tokens."""
        s = (redirected or "").strip()
        code = None
        if "code=" in s:
            q = parse_qs(urlparse(s).query)
            code = (q.get("code") or [None])[0]
            if code is None:
                m = re.search(r"code=([^&]+)", s)
                code = unquote(m[1]) if m else None
        else:
            code = unquote(s)
        if not code:
            raise SchwabError("No code found: paste the whole address from the browser's address bar.")
        js = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.callback()})
        now = time.time()
        self._save_tokens({"access_token": js["access_token"], "refresh_token": js["refresh_token"],
                           "expires_at": now + float(js.get("expires_in", 1800)) - 60, "refresh_issued": now})

    def _access(self) -> str:
        t = self.tokens()
        if not t.get("refresh_token"):
            raise SchwabError("Not signed in to Schwab.")
        if time.time() > t.get("refresh_issued", 0) + self.REFRESH_DAYS * 86400:
            raise SchwabError("The Schwab sign-in expired (Schwab allows 7 days). Sign in again.")
        if time.time() < t.get("expires_at", 0):
            return t["access_token"]
        js = self._token({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        t.update({"access_token": js["access_token"], "expires_at": time.time() + float(js.get("expires_in", 1800))
                  - 60})
        if js.get("refresh_token") and js["refresh_token"] != t["refresh_token"]:
            t["refresh_token"] = js["refresh_token"]
        self._save_tokens(t)
        return t["access_token"]

    def get(self, path: str, params: dict | None = None):
        r = self.http.get(self.BASE + path, params=params or {}, timeout=60,
                          headers={"Authorization": f"Bearer {self._access()}", "Accept": "application/json"})
        if r.status_code == 401:
            raise SchwabError("Schwab says the sign-in is no longer valid. Sign in again.")
        if r.status_code != 200:
            raise SchwabError(f"Schwab error {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    # -- data --
    @staticmethod
    def parse_account(sa: dict) -> dict:
        pos = []
        for p in sa.get("positions") or []:
            ins = p.get("instrument") or {}
            sym = _clean_symbol(ins.get("symbol") or "")
            if not sym or ins.get("assetType") in ("OPTION", "CURRENCY"):
                continue
            q = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
            if abs(q) < 1e-9:
                continue
            pos.append({"ticker": sym, "shares": q, "avg_cost": p.get("averagePrice") or p.get("averageLongPrice"),
                        "market_value": p.get("marketValue"), "asset_type": ins.get("assetType")})
        bal = sa.get("currentBalances") or {}
        c = bal.get("cashBalance")
        if c is None:
            c = bal.get("totalCash", bal.get("availableFunds"))
        return {"number": str(sa.get("accountNumber") or ""), "type": sa.get("type") or "", "positions": pos,
                "cash": float(c) if c is not None else None, "value": bal.get("liquidationValue")}

    @staticmethod
    def parse_txn(t: dict) -> dict | None:
        typ = t.get("type") or ""
        d = (t.get("tradeDate") or t.get("time") or "")[:10]
        if not d:
            return None
        items = t.get("transferItems") or []
        sec = next((i for i in items if (i.get("instrument") or {}).get("assetType") not in (None, "CURRENCY")), None)
        fees = sum(abs(float(i.get("cost") or 0)) for i in items if i.get("feeType"))
        net = t.get("netAmount")
        sym = _clean_symbol(((sec or {}).get("instrument") or {}).get("symbol") or "")
        q = float(sec.get("amount") or 0) if sec else None
        px = sec.get("price") if sec else None
        desc = t.get("description") or ""
        if typ == "TRADE" and sec:
            kind = "buy" if (net or 0) < 0 or sec.get("positionEffect") == "OPENING" and (q or 0) > 0 else "sell"
            q = abs(q or 0)
        elif typ == "DIVIDEND_OR_INTEREST":
            kind = "interest" if "INTEREST" in desc.upper() and not sym else "div"
        elif typ == "RECEIVE_AND_DELIVER" and sec:
            kind = "split" if "SPLIT" in desc.upper() else ("transfer_in" if (q or 0) >= 0 else "transfer_out")
            q = q if kind == "split" else abs(q or 0)
        elif typ in ("ACH_RECEIPT", "CASH_RECEIPT", "WIRE_IN", "ACH_DISBURSEMENT", "CASH_DISBURSEMENT", "WIRE_OUT",
                     "ELECTRONIC_FUND", "JOURNAL"):
            kind = "deposit" if (net or 0) >= 0 else "withdraw"
        else:
            kind = "other"
        return {"date": d, "kind": kind, "ticker": sym, "shares": q, "price": px, "amount": net, "fees": fees,
                "description": f"{typ} {desc}".strip(), "ext_id": f"schwab:{t.get('activityId')}"}

    def sync(self, days: int = 365, progress=None) -> dict:
        """Accounts, positions and cash, plus the last `days` of transactions (a year per request)."""
        nums = {str(x["accountNumber"]): x["hashValue"] for x in self.get("/accounts/accountNumbers")}
        accts = self.get("/accounts", {"fields": "positions"})
        msgs, ids = [], []
        for k, a in enumerate(accts):
            sa = self.parse_account(a.get("securitiesAccount") or {})
            h = nums.get(sa["number"])
            tail = sa["number"][-3:]
            ex = holdings.find_account("schwab", h) if h else None
            aid = ex["id"] if ex else holdings.add_account(f"Schwab {sa['type'].title()} …{tail}".strip(), "Schwab",
                                                           "schwab", tail, h)
            r = holdings.apply_positions(aid, sa["positions"], "schwab")
            if sa["cash"] is not None:
                holdings.update_account(aid, cash=sa["cash"])
            new = 0
            end = datetime.utcnow()
            left = days
            while left > 0 and h:
                span = min(left, 365)
                start = end - timedelta(days=span)
                js = self.get(f"/accounts/{h}/transactions", {
                    "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "endDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "types": self.TXN_TYPES})
                for t in js if isinstance(js, list) else []:
                    p = self.parse_txn(t)
                    if p and holdings.add_txn(aid, p["date"], p["kind"], p["ticker"], p["shares"], p["price"],
                                              p["amount"], p["fees"], p["description"], "schwab", p["ext_id"]):
                        new += 1
                end, left = start, left - span
            holdings.update_account(aid, synced_at=db.now())
            ids.append(aid)
            msgs.append(f"…{tail}: {r['positions']} positions, cash {sa['cash'] or 0:,.2f}, {new} new transactions"
                        + (f"; {r['added']} holdings need purchase dates (import the lots file)" if r["added"] else ""))
            if progress:
                progress((k + 1) / max(len(accts), 1), msgs[-1])
        return {"accounts": ids, "messages": msgs}


# ===================================================================== SnapTrade (Personal API key)
class SnapTradeError(Exception):
    pass


class SnapTrade:
    BASE = "https://api.snaptrade.com"

    def __init__(self, session=None):
        import requests

        self.http = session or requests.Session()
        self.cid = secrets.get("snaptrade_client_id")
        self.key = secrets.get("snaptrade_consumer_key")

    @staticmethod
    def save(client_id: str, consumer_key: str) -> None:
        secrets.set("snaptrade_client_id", client_id.strip() or None)
        if consumer_key:
            secrets.set("snaptrade_consumer_key", consumer_key.strip())

    @staticmethod
    def configured() -> bool:
        return bool(secrets.get("snaptrade_client_id") and secrets.get("snaptrade_consumer_key"))

    @staticmethod
    def sign(path: str, query: str, body, consumer_key: str) -> str:
        payload = {"content": None if body is None or body == {} else body, "path": path, "query": query}
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return base64.b64encode(hmac.new(consumer_key.encode(), canonical.encode(), hashlib.sha256).digest()).decode()

    def get(self, path: str, params: dict | None = None):
        if not (self.cid and self.key):
            raise SnapTradeError("Enter your SnapTrade Personal client ID and consumer key first.")
        q = urlencode({"clientId": self.cid, "timestamp": int(time.time()), **(params or {})})
        r = self.http.get(f"{self.BASE}{path}?{q}", timeout=60,
                          headers={"Signature": self.sign(path, q, None, self.key), "Accept": "application/json"})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise SnapTradeError(f"SnapTrade error {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _sym(o) -> str | None:
        """The ticker inside SnapTrade's nested symbol objects."""
        for _ in range(4):
            if isinstance(o, str):
                return _clean_symbol(o)
            if not isinstance(o, dict):
                return None
            o = o.get("symbol") or o.get("raw_symbol") or o.get("ticker")
        return _clean_symbol(o) if isinstance(o, str) else None

    @classmethod
    def parse_positions(cls, js) -> tuple[list[dict], list[dict]]:
        """(positions, lots) from /accounts/{id}/positions/all (or the older /positions list)."""
        items = js.get("results", []) if isinstance(js, dict) else (js or [])
        pos, lts = [], []
        for p in items:
            if p.get("cash_equivalent"):
                continue
            sym = cls._sym(p.get("instrument") or p.get("symbol"))
            q = num(p.get("units"))
            if not sym or not q:
                continue
            cb = num(p.get("cost_basis") or p.get("average_purchase_price"))
            pos.append({"ticker": sym, "shares": q, "avg_cost": cb, "market_value": (num(p.get("price")) or 0) * q})
            for t in p.get("tax_lots") or []:
                d = pdate(t.get("original_purchase_date"))
                lq = num(t.get("quantity"))
                if not lq:
                    continue
                cps = num(t.get("purchased_price"))
                if cps is None and num(t.get("cost_basis")) is not None:
                    cps = num(t.get("cost_basis")) / lq
                lts.append({"ticker": sym, "open_date": d, "shares": lq, "cost_ps": cps or cb or 0})
        return pos, lts

    @classmethod
    def parse_activity(cls, a: dict) -> dict | None:
        d = pdate(a.get("trade_date") or a.get("settlement_date"))
        if not d:
            return None
        t = (a.get("type") or "").upper()
        kind = {"BUY": "buy", "SELL": "sell", "DIVIDEND": "div", "REI": "reinvest", "STOCK_DIVIDEND": "div",
                "INTEREST": "interest", "CONTRIBUTION": "deposit", "DEPOSIT": "deposit", "WITHDRAWAL": "withdraw",
                "FEE": "fee", "TAX": "fee", "SPLIT": "split"}.get(t, "other")
        q = num(a.get("units"))
        amt = num(a.get("amount"))
        if t in ("TRANSFER", "EXTERNAL_ASSET_TRANSFER_IN", "EXTERNAL_ASSET_TRANSFER_OUT", "OPTIONEXPIRATION"):
            kind = "transfer_in" if (q or 0) > 0 else ("transfer_out" if q else ("deposit" if (amt or 0) >= 0
                                                                                 else "withdraw"))
        if kind in ("buy", "sell", "reinvest", "transfer_in", "transfer_out") and q:
            q = abs(q)
        return {"date": d, "kind": kind, "ticker": cls._sym(a.get("symbol")), "shares": q, "price": num(a.get("price")),
                "amount": amt, "fees": num(a.get("fee")) or 0.0, "description": f"{t} {a.get('description') or ''}"[:300],
                "ext_id": f"snaptrade:{a.get('id')}"}

    def sync(self, days: int = 365 * 5, progress=None) -> dict:
        accts = self.get("/accounts") or self.get("/api/v1/accounts") or []
        msgs, ids = [], []
        for k, a in enumerate(accts):
            if (a.get("account_category") or "investment") not in ("investment", None) or a.get("is_paper"):
                continue
            ext = str(a["id"])
            tail = str(a.get("number") or "")[-3:]
            ex = holdings.find_account("snaptrade", ext)
            inst = a.get("institution_name") or "Brokerage"
            aid = ex["id"] if ex else holdings.add_account(f"{inst} {a.get('name') or ''} …{tail}".replace("  ", " "),
                                                           inst, "snaptrade", tail, ext)
            js = self.get(f"/accounts/{ext}/positions/all")
            if js is None:
                js = self.get(f"/api/v1/accounts/{ext}/positions") or []
            pos, lts = self.parse_positions(js)
            r = holdings.apply_positions(aid, pos, "snaptrade", fill_missing=not lts)
            if lts and all(x["open_date"] for x in lts):
                holdings.replace_lots(aid, lts, "snaptrade lots")
            bal = self.get(f"/accounts/{ext}/balances") or []
            c = sum(num(b.get("cash")) or 0 for b in bal if isinstance(b, dict))
            holdings.update_account(aid, cash=c)
            new, off = 0, 0
            start = (date.today() - timedelta(days=days)).isoformat()
            while True:
                js = self.get(f"/accounts/{ext}/activities", {"startDate": start, "endDate": date.today().isoformat(),
                                                              "offset": off, "limit": 1000})
                data = (js.get("data") if isinstance(js, dict) else js) or []
                for x in data:
                    p = self.parse_activity(x)
                    if p and holdings.add_txn(aid, p["date"], p["kind"], p["ticker"], p["shares"], p["price"],
                                              p["amount"], p["fees"], p["description"], "snaptrade", p["ext_id"]):
                        new += 1
                tot = (js.get("pagination") or {}).get("total") if isinstance(js, dict) else None
                off += len(data)
                if not data or tot is None or off >= tot:
                    break
            holdings.update_account(aid, synced_at=db.now())
            ids.append(aid)
            msgs.append(f"{inst} …{tail}: {r['positions']} positions" + (f", {len(lts)} purchases" if lts else "")
                        + f", cash {c:,.2f}, {new} new transactions")
            if progress:
                progress((k + 1) / max(len(accts), 1), msgs[-1])
        return {"accounts": ids, "messages": msgs}

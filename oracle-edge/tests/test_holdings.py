"""The real portfolio (0.22): purchases and sales, imports, performance, statistics and the order plan."""

import json
from datetime import date, timedelta

import pytest

from oracle_edge import brokers, db, holdings, orderplan


def _prices(t, start, days, p0, step=0.0, div_every=0, div=0.0):
    """Daily prices; every div_every-th day an ex-dividend (the adjusted close jumps relative to the close)."""
    con = db.get()
    d = date.fromisoformat(start)
    p, factor = p0, 1.0
    out = []
    for i in range(days):
        if d.weekday() < 5:
            if div_every and i and i % div_every == 0:
                factor *= 1 + div / p
            con.execute("INSERT OR REPLACE INTO prices(ticker,date,close,adj_close,source) VALUES(?,?,?,?,?)",
                        (t, d.isoformat(), p, p * factor, "test"))
            out.append(d.isoformat())
            p += step
        d += timedelta(days=1)
    return out


@pytest.fixture
def clean():
    def wipe():
        for t in ("accounts", "lots", "closed_lots", "txns", "broker_positions", "baskets", "basket_members"):
            db.get().execute(f"DELETE FROM {t}")
    wipe()
    yield
    wipe()


def test_real_holdings_is_a_view_over_purchases(clean):
    db.get().execute("INSERT OR IGNORE INTO securities(cik,ticker,name) VALUES(990001,'ZZQ','Zed Q')")
    a = holdings.add_account("Test")
    holdings.add_lot(a, "ZZQ", "2024-01-02", 10, 20)
    holdings.add_lot(a, "ZZQ", "2024-06-03", 10, 30)
    r = db.one("SELECT * FROM real_holdings WHERE cik=990001")
    assert r["shares"] == 20 and r["avg_cost"] == pytest.approx(25)


def test_sell_fifo_hifo_and_realized(clean):
    a = holdings.add_account("Test")
    holdings.add_lot(a, "ZZA", "2020-01-02", 10, 10)
    holdings.add_lot(a, "ZZA", "2025-06-02", 10, 30)
    r = holdings.sell(a, "ZZA", "2025-09-01", 5, 40, "hifo")
    assert r["gain"] == pytest.approx(50) and r["short"] == pytest.approx(50)  # the $30 lot, held < a year
    r = holdings.sell(a, "ZZA", "2025-09-01", 12, 40, "fifo")
    assert r["cost"] == pytest.approx(10 * 10 + 2 * 30) and r["short"] == pytest.approx(2 * 10)
    left = holdings.lots([a])
    assert len(left) == 1 and left[0]["shares"] == pytest.approx(3) and left[0]["cost_ps"] == 30
    assert len(holdings.lots([a], closed=True)) == 3
    assert {t["kind"] for t in holdings.txns([a])} == {"buy", "sell"}


def test_holding_and_lot_metrics_with_dividends(clean):
    days = _prices("ZZD", "2024-01-01", 800, 50.0, step=0.02, div_every=60, div=0.5)
    a = holdings.add_account("Test")
    holdings.add_lot(a, "ZZD", days[0], 100, 50.0)
    holdings.add_lot(a, "ZZD", days[-1], 10, 60.0, record_txn=False)
    hs, px = holdings.holdings(holdings.Scope([a]))
    h = hs[0]
    assert h.shares == 110 and len(h.lots) == 2
    assert px.dividends("ZZD") and abs(px.dividends("ZZD")[0][1] - 0.5) < 0.02
    old = [m for m in h.lots if m["open_date"] == days[0]][0]
    assert old["divs"] > 0 and old["long_term"] and old["ann"] is not None
    new = [m for m in h.lots if m["open_date"] == days[-1]][0]
    assert new["divs"] == 0 and not new["long_term"] and new["days_to_long"] > 0
    assert h.ttm_div_ps > 0 and h.div_yield and h.yield_on_cost
    s = holdings.summary(hs, holdings.Scope([a]))
    assert s["invested"] == pytest.approx(h.value) and s["unreal_long"] > 0


def test_history_twr_matches_the_stock_and_ignores_new_money(clean):
    days = _prices("ZZT", "2024-01-01", 500, 100.0, step=0.1)
    _prices("SPY", "2024-01-01", 500, 400.0, step=0.2)
    a = holdings.add_account("Test")
    holdings.add_lot(a, "ZZT", days[0], 10, 100.0, record_txn=False)
    holdings.add_lot(a, "ZZT", days[200], 1000, 120.0, record_txn=False)  # a big top-up doesn't move the TWR
    hi = holdings.history(holdings.Scope([a]), "All", ["SPY", "NOPE"])
    p0 = db.scalar("SELECT close FROM prices WHERE ticker='ZZT' AND date=?", (hi["dates"][0],))
    p1 = db.scalar("SELECT close FROM prices WHERE ticker='ZZT' AND date=?", (hi["dates"][-1],))
    assert hi["twr"][-1] == pytest.approx(p1 / p0, rel=1e-6)
    assert "SPY" in hi["bench"] and hi["missing"] == ["NOPE"]
    st = holdings.stats(hi["dates"], hi["twr"], hi["bench"]["SPY"])
    assert st["total"] > 0 and st["max_dd"] == 0 and st["beta"] is not None
    sm = holdings.same_money(holdings.Scope([a]), "SPY")
    assert sm["put_in"] == pytest.approx(1000 + 120000) and sm["theirs"] > sm["put_in"]


def test_baskets_scope(clean):
    a = holdings.add_account("Test")
    for t in ("ZZA", "ZZB", "ZZC"):
        holdings.add_lot(a, t, "2024-01-02", 1, 1, record_txn=False)
    b = holdings.add_basket("Core")
    holdings.set_members(b, ["zza", "ZZB"])
    hs, _ = holdings.holdings(holdings.Scope(None, b))
    assert {h.ticker for h in hs} == {"ZZA", "ZZB"}
    hs, _ = holdings.holdings(holdings.Scope(None, 0))
    assert {h.ticker for h in hs} == {"ZZC"}
    assert holdings.cash(holdings.Scope(None, b)) == 0


SCHWAB_POSITIONS = '''"Positions for account Individual ...123 as of 09:15 PM ET, 2026/11/20","","","",""
"","","","",""
"Symbol","Description","Qty (Quantity)","Price","Mkt Val (Market Value)","Cost Basis","Security Type"
"KO","COCA COLA CO","25","$70.10","$1,752.50","$1,280.00","Equity"
"BRK/B","BERKSHIRE HATHAWAY","2","$480.00","$960.00","$700.00","Equity"
"Cash & Cash Investments","--","--","--","$1,234.56","--","Cash and Money Market"
"Account Total","--","--","--","$3,947.06","$1,980.00","--"
'''

SCHWAB_LOTS = '''"Symbol","Name","Open Date","Quantity","Cost/Share","Cost Basis","Market Value"
"KO","COCA COLA CO","","25","","$1,280.00","$1,752.50"
"","","03/15/2021","15","$50.00","$750.00",""
"","","08/01/2023","10","$53.00","$530.00",""
'''

SCHWAB_TXNS = {"FromDate": "01/01/2021", "ToDate": "12/31/2021", "BrokerageTransactions": [
    {"Date": "03/15/2021", "Action": "Buy", "Symbol": "KO", "Description": "COCA COLA", "Quantity": "15",
     "Price": "$50.00", "Fees & Comm": "", "Amount": "-$750.00"},
    {"Date": "04/01/2021", "Action": "Qualified Dividend", "Symbol": "KO", "Description": "COCA COLA",
     "Quantity": "", "Price": "", "Fees & Comm": "", "Amount": "$6.30"},
    {"Date": "06/01/2021 as of 05/28/2021", "Action": "Sell", "Symbol": "KO", "Description": "COCA COLA",
     "Quantity": "5", "Price": "$55.00", "Fees & Comm": "$0.10", "Amount": "$274.90"},
    {"Date": "06/02/2021", "Action": "MoneyLink Transfer", "Symbol": "", "Description": "Tfr BANK",
     "Quantity": "", "Price": "", "Fees & Comm": "", "Amount": "$1,000.00"}]}


def test_parse_schwab_positions_lots_and_transactions(clean):
    assert brokers.sniff(SCHWAB_POSITIONS) == "positions"
    pos = brokers.parse_positions(SCHWAB_POSITIONS)
    (lab, v), = pos.items()
    assert "123" in lab and v["cash"] == pytest.approx(1234.56)
    assert [(p["ticker"], p["shares"]) for p in v["positions"]] == [("KO", 25), ("BRK.B", 2)]
    assert v["positions"][0]["avg_cost"] == pytest.approx(51.2)
    assert brokers.sniff(SCHWAB_LOTS) == "lots"
    lots = brokers.parse_lots(SCHWAB_LOTS)
    (ls,) = lots.values()
    assert [(x["open_date"], x["shares"], x["cost_ps"]) for x in ls] == [("2021-03-15", 15, 50.0),
                                                                        ("2023-08-01", 10, 53.0)]
    ts = brokers.parse_transactions(json.dumps(SCHWAB_TXNS))
    assert [t["kind"] for t in ts] == ["buy", "div", "sell", "deposit"]
    assert ts[2]["date"] == "2021-06-01" and ts[2]["fees"] == pytest.approx(0.1)

    r = brokers.import_file(SCHWAB_POSITIONS)
    aid = r["accounts"][0]
    assert holdings.account(aid)["cash"] == pytest.approx(1234.56) and holdings.account(aid)["tail"] == "123"
    assert {x["ticker"]: x["date_known"] for x in holdings.lots([aid])} == {"KO": 0, "BRK.B": 0}
    brokers.import_file(SCHWAB_LOTS, account_id=aid)
    assert sorted(x["shares"] for x in holdings.lots([aid])) == [10, 15]  # lots file replaced the account's
    assert [r["ticker"] for r in holdings.reconcile()] == ["BRK.B"]  # broker has BRK.B, the lots file doesn't
    holdings.fix_mismatch(aid, "BRK.B")
    assert not holdings.reconcile()
    r1 = brokers.import_file(json.dumps(SCHWAB_TXNS), account_id=aid)
    r2 = brokers.import_file(json.dumps(SCHWAB_TXNS), account_id=aid)
    assert r1["rows"] == 4 and r2["rows"] == 0  # importing twice adds nothing


def test_rebuild_purchases_from_history(clean):
    a = holdings.add_account("Hist")
    brokers.import_file(json.dumps(SCHWAB_TXNS), account_id=a, rebuild=True)
    (x,) = holdings.lots([a])
    assert x["shares"] == pytest.approx(10) and x["cost_ps"] == pytest.approx(50)
    (c,) = holdings.lots([a], closed=True)
    assert c["shares"] == 5 and c["price"] == pytest.approx(55 - 0.02)


def test_schwab_api_parsing():
    sa = brokers.Schwab.parse_account({"accountNumber": "12345678", "type": "MARGIN", "positions": [
        {"longQuantity": 10, "shortQuantity": 0, "averagePrice": 51.2, "marketValue": 701,
         "instrument": {"symbol": "KO", "assetType": "EQUITY"}},
        {"longQuantity": 1, "averagePrice": 2, "instrument": {"symbol": "KO  261218C00070000", "assetType": "OPTION"}}],
        "currentBalances": {"cashBalance": 99.5, "liquidationValue": 800.5}})
    assert sa["cash"] == 99.5 and [p["ticker"] for p in sa["positions"]] == ["KO"]
    t = brokers.Schwab.parse_txn({"activityId": 7, "type": "TRADE", "tradeDate": "2026-03-02T14:30:00+0000",
                                  "netAmount": -512.65, "transferItems": [
                                      {"instrument": {"assetType": "EQUITY", "symbol": "KO"}, "amount": 10,
                                       "price": 51.2, "cost": -512.0, "positionEffect": "OPENING"},
                                      {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
                                       "feeType": "COMMISSION", "cost": -0.65}]})
    assert (t["kind"], t["ticker"], t["shares"], t["fees"], t["ext_id"]) == ("buy", "KO", 10, 0.65, "schwab:7")
    d = brokers.Schwab.parse_txn({"activityId": 8, "type": "DIVIDEND_OR_INTEREST", "time": "2026-04-01",
                                  "netAmount": 5.1, "description": "QUALIFIED DIVIDEND",
                                  "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "KO"}}]})
    assert d["kind"] == "div" and d["ticker"] == "KO"


def test_schwab_login_url_and_code(monkeypatch):
    store = {}
    monkeypatch.setattr(brokers.secrets, "get", lambda k: store.get(k))
    monkeypatch.setattr(brokers.secrets, "set", lambda k, v: store.__setitem__(k, v))
    brokers.Schwab.save_app("KEY", "SECRET", "https://127.0.0.1:8182")

    class R:
        status_code = 200

        def json(self):
            return {"access_token": "A", "refresh_token": "R", "expires_in": 1800}

    class S:
        def post(self, url, data, headers, timeout):
            self.data = data
            return R()
    s = S()
    api = brokers.Schwab(s)
    assert "client_id=KEY" in api.authorize_url() and "redirect_uri=https%3A%2F%2F127.0.0.1%3A8182" in \
        api.authorize_url()
    api.finish_login("https://127.0.0.1:8182/?code=C0DE%40&session=xyz")
    assert s.data["code"] == "C0DE@" and brokers.Schwab.status()["state"] == "ok"


def test_snaptrade_signature_and_parsing():
    sig = brokers.SnapTrade.sign("/accounts", "clientId=X&timestamp=1", None, "K")
    import base64
    import hashlib
    import hmac
    want = base64.b64encode(hmac.new(b"K", b'{"content":null,"path":"/accounts","query":"clientId=X&timestamp=1"}',
                                     hashlib.sha256).digest()).decode()
    assert sig == want
    pos, lts = brokers.SnapTrade.parse_positions({"results": [
        {"instrument": {"kind": "stock", "symbol": "KO"}, "units": "25", "price": "70.1", "cost_basis": "51.2",
         "tax_lots": [{"original_purchase_date": "2021-03-15", "quantity": "15", "purchased_price": "50"},
                      {"original_purchase_date": "2023-08-01", "quantity": "10", "cost_basis": "530"}]},
        {"instrument": {"symbol": "SWVXX"}, "units": "100", "cash_equivalent": True}]})
    assert [p["ticker"] for p in pos] == ["KO"] and [x["cost_ps"] for x in lts] == [50.0, 53.0]
    a = brokers.SnapTrade.parse_activity({"id": "u1", "type": "BUY", "trade_date": "2026-01-05T00:00:00Z",
                                          "symbol": {"symbol": "KO"}, "units": 3, "price": 60, "amount": -180,
                                          "fee": 0})
    assert (a["kind"], a["ticker"], a["shares"], a["date"]) == ("buy", "KO", 3, "2026-01-05")


def test_order_plan_reserve_sells_and_buys(clean, monkeypatch):
    days = _prices("ZZH", "2025-01-01", 300, 100.0)
    _prices("ZZX", "2025-01-01", 300, 50.0)
    _prices("ZZY", "2025-01-01", 300, 20.0)
    a = holdings.add_account("Plan", cash=10000)
    holdings.add_lot(a, "ZZH", days[0], 10, 80.0, record_txn=False)
    monkeypatch.setattr(orderplan, "market_view", lambda st=None: {"date": days[-1], "state": "normal", "zone": "Fair",
                                                                   "reserve": 0.10, "why": []})
    evals = {"evals": {
        1: {"ticker": "ZZH", "list": "out", "gates_passed": False, "failed": ["Debt"], "price": 100.0},
        2: {"ticker": "ZZX", "list": "buy", "price": 50.0, "buy_below": 48.0, "rank": 1, "quality": 80, "iv": 70},
        3: {"ticker": "ZZY", "list": "buy", "price": 20.0, "buy_below": None, "rank": 2}}}
    from oracle_edge import rules
    rules.seed_profiles()
    p = orderplan.build(a, max_names=2, evals=evals)
    acts = {(r["action"], r["ticker"]) for r in p["rows"]}
    assert ("SELL", "ZZH") in acts and ("BUY", "ZZX") in acts and ("BUY", "ZZY") in acts
    total = 10000 + 1000
    assert p["reserve"] == pytest.approx(total * 0.10)
    buy = [r for r in p["rows"] if r["ticker"] == "ZZX"][0]
    assert buy["limit"] == 48.0  # the lower of today's price and the strategy's buy price
    assert p["buys"] <= p["cash"] + p["proceeds"] - p["reserve"] + 1e-6
    assert "ZZX" in orderplan.to_csv(p) and "SELL" in orderplan.to_text(p)


def test_sync_failures_are_reported_and_logged(monkeypatch):
    monkeypatch.setattr(brokers.SnapTrade, "get", lambda self, path, params=None: [])
    r = brokers.run_sync("snaptrade")
    assert not r["ok"] and "no brokerage account connected" in r["messages"][0]
    assert brokers.sync_log(1)[0]["ok"] == 0

    def boom(self, path, params=None):
        raise brokers.SnapTradeError("SnapTrade error 401 on /accounts: bad signature")
    monkeypatch.setattr(brokers.SnapTrade, "get", boom)
    r = brokers.run_sync("snaptrade")
    assert not r["ok"] and "401" in r["messages"][0] and "401" in brokers.sync_log(1)[0]["detail"]


def test_snaptrade_sync_brings_in_every_investment_account(clean, monkeypatch):
    resp = {
        "/accounts": [{"id": "acc-1", "name": "Individual", "number": "ABC123", "institution_name": "Vanguard US",
                       "account_category": "INVESTMENT"},
                      {"id": "acc-2", "name": "Checking", "number": "9", "institution_name": "Bank",
                       "account_category": "banking"}],
        "/accounts/acc-1/positions/all": {"results": [
            {"instrument": {"kind": "stock", "symbol": "VTI"}, "units": "10", "price": "300", "cost_basis": "200"}]},
        "/accounts/acc-1/balances": [{"currency": {"code": "USD"}, "cash": 55.5}],
        "/accounts/acc-1/activities": {"data": [], "pagination": {"offset": 0, "limit": 1000, "total": 0}}}
    monkeypatch.setattr(brokers.SnapTrade, "get", lambda self, path, params=None: resp.get(path))
    r = brokers.run_sync("snaptrade")
    assert r["ok"] and "Vanguard US …123: 1 positions" in r["messages"][0] and "Checking" in r["messages"][-1]
    (a,) = holdings.accounts()
    assert a["cash"] == 55.5 and [x["ticker"] for x in holdings.lots([a["id"]])] == ["VTI"]

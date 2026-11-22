"""ETFs (and other funds): who they are, everything they hold, and the data to judge them.

Sources
- Holdings: the SEC's Form N-PORT. Every registered fund files its complete portfolio (each position's identifiers,
  value and share of net assets); the third month of each quarter is made public about 60 days after it ends. Ticker ->
  fund series comes from the SEC's mutual-fund/ETF ticker file; the series' latest NPORT-P filing is found through
  EDGAR. Funds that aren't registered investment companies (gold and bitcoin trusts) have no N-PORT; Yahoo's top 10
  holdings stand in.
- Fund facts: Yahoo Finance via yfinance (category, issuer, expense ratio and its category average, turnover, assets,
  inception, yield, NAV, top holdings, sectors).
- Prices: the app's usual daily prices.

Each holding is matched to a company the app scores: by the ticker in the filing, then by CUSIP (learned from other
funds' filings that list both), then by name.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from . import db, paths

SERIES_URL = "https://www.sec.gov/files/company_tickers_mf.json"
BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# A starting universe of widely held US-listed funds; your own ETFs and any you add join it.
SEED = """
SPY IVV VOO VTI ITOT SCHB SPLG RSP DIA QQQ QQQM ONEQ IWM IJH IJR MDY VB VO VV MGK VUG VTV IWF IWD SCHG SCHX SCHD
VIG DGRO NOBL SDY VYM HDV DVY SPYD JEPI JEPQ QUAL MTUM USMV VLUE MOAT COWZ CALF AVUV VBR VOT IWO IWN SPMO SPHQ
SPLV SPGP DGRW FNDX PRF RPV RPG XLK XLF XLV XLE XLI XLY XLP XLU XLB XLRE XLC VGT VHT VFH VDE VIS VCR VDC VPU VAW VNQ
VOX FTEC FHLC FNCL FDIS FSTA FIDU FENY FUTY FMAT FREL FCOM SMH SOXX XSD PSI SOXQ FTXL IGV SKYY CIBR HACK BOTZ ROBO
ARKK ARKW ARKG ICLN TAN LIT IBB XBI KRE KBE ITA XAR PAVE IYT JETS GDX XME VXUS VEA VWO IEFA IEMG EFA EEM ACWI VT
IXUS SCHF SCHE EWJ INDA MCHI FXI EWZ VGK EWT EWY BND AGG BNDX TLT IEF SHY LQD HYG TIP VTIP SGOV BIL MUB VCIT VCSH
GLD IAU SLV IBIT FBTC
""".split()

FOREIGN_WORDS = ("foreign", "international", "emerging", "europe", "japan", "china", "india", "pacific", "latin",
                 "diversified emerging", "ex-us", "korea", "taiwan", "brazil")
BOND_WORDS = ("bond", "treasury", "government", "muni", "corporate", "ultrashort", "inflation", "high yield",
              "bank loan", "short-term", "intermediate")


# ===================================================================== universe
def universe() -> list[str]:
    """The seed list, every fund you own or added, and every fund already on file."""
    from .holdings import held_tickers

    companies = {r["ticker"] for r in db.rows("SELECT ticker FROM securities WHERE ticker IS NOT NULL")}
    mine = [t for t in held_tickers() if t not in companies]
    have = [r["ticker"] for r in db.rows("SELECT ticker FROM etfs")]
    out = list(dict.fromkeys(mine + list(db.cfg("etf_watch", []) or []) + have + SEED))
    removed = set(db.cfg("etf_removed", []) or [])
    return [t for t in out if t not in removed or t in mine]


def add(ticker: str) -> str:
    t = re.sub(r"[^A-Za-z0-9.\-]", "", ticker or "").upper()
    if not t:
        return ""
    w = list(db.cfg("etf_watch", []) or [])
    if t not in w:
        db.set_cfg("etf_watch", w + [t])
    db.set_cfg("etf_removed", [x for x in (db.cfg("etf_removed", []) or []) if x != t])
    db.get().execute("INSERT OR IGNORE INTO etfs(ticker, added_by) VALUES(?, 'you')", (t,))
    return t


def remove(ticker: str) -> None:
    db.set_cfg("etf_watch", [x for x in (db.cfg("etf_watch", []) or []) if x != ticker])
    db.set_cfg("etf_removed", list(dict.fromkeys((db.cfg("etf_removed", []) or []) + [ticker])))
    with db.tx() as con:
        for t in ("etfs", "etf_holdings", "etf_scores", "etf_aum"):
            con.execute(f"DELETE FROM {t} WHERE {'ticker' if t == 'etfs' else 'etf'}=?", (ticker,))


def get(ticker: str) -> dict | None:
    r = db.one("SELECT * FROM etfs WHERE ticker=?", (ticker.upper(),))
    if not r:
        return None
    x = dict(r)
    for k in ("sectors_json", "assets_json", "top_json"):
        x[k[:-5]] = json.loads(x[k]) if x.get(k) else None
    return x


def is_fund(ticker: str) -> bool:
    """A ticker the app treats as a fund: on file as one, or held/seeded and not a company it scores."""
    t = (ticker or "").upper()
    if db.scalar("SELECT 1 FROM etfs WHERE ticker=?", (t,)):
        return True
    return t in SEED and not db.scalar("SELECT 1 FROM securities WHERE ticker=?", (t,))


def benchmark_for(category: str | None, ticker: str = "") -> str:
    """What the fund is measured against: the S&P 500 for US stock funds (the Buffett yardstick: did it beat simply
    owning the index?), total international (VXUS) for foreign stock funds, the world (VT) for global funds, and the
    US bond market (AGG) for bond funds."""
    c = (category or "").lower()
    if any(w in c for w in BOND_WORDS) and "equity" not in c:
        return "AGG" if ticker != "AGG" else "BND"
    if "world" in c or "global" in c:
        return "VT" if ticker != "VT" else "ACWI"
    if any(w in c for w in FOREIGN_WORDS):
        return "VXUS" if ticker != "VXUS" else "VEA"
    return "SPY" if ticker != "SPY" else "VOO"


# ===================================================================== SEC: ticker -> fund series -> N-PORT
def _sec():
    from .sources import sec

    return sec.http()


def series_map(refresh: bool = False) -> dict[str, dict]:
    """ticker -> {cik, series, class} from the SEC's fund ticker file (cached for a week)."""
    f = paths.DATA_DIR / "cache" / "company_tickers_mf.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not f.exists() or time.time() - f.stat().st_mtime > 7 * 86400:
        r = _sec().get(SERIES_URL)
        r.raise_for_status()
        f.write_bytes(r.content)
    js = json.loads(f.read_text(encoding="utf-8"))
    return parse_series_map(js)


def parse_series_map(js: dict) -> dict[str, dict]:
    fields = js.get("fields") or ["cik", "seriesId", "classId", "symbol"]
    i = {k: fields.index(k) for k in ("cik", "seriesId", "classId", "symbol") if k in fields}
    out = {}
    for row in js.get("data") or []:
        sym = str(row[i["symbol"]] or "").upper().strip()
        if sym:
            out[sym] = {"cik": int(row[i["cik"]]), "series": row[i["seriesId"]], "class": row[i["classId"]]}
    return out


def parse_browse_atom(text: str) -> list[dict]:
    """Filings from EDGAR's company browse (atom output): [{acc, date}] newest first."""
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        acc = re.search(r"<accession-number>([^<]+)</accession-number>", e) or \
            re.search(r"accession-number=([0-9-]+)", e) or re.search(r"/(\d{10}-\d{2}-\d{6})-index", e)
        d = re.search(r"<filing-date>([^<]+)</filing-date>", e)
        typ = re.search(r"<filing-type>([^<]+)</filing-type>", e)
        if acc and (not typ or typ[1].strip().upper() == "NPORT-P"):
            out.append({"acc": acc[1].strip(), "date": d[1].strip() if d else ""})
    return out


def latest_filings(series: str, cik: int, n: int = 8) -> list[dict]:
    """The series' most recent NPORT-P filings (EDGAR accepts a series id as the company)."""
    r = _sec().get(BROWSE_URL, params={"action": "getcompany", "CIK": series, "type": "NPORT-P", "dateb": "",
                                       "owner": "include", "count": str(n), "output": "atom"})
    out = parse_browse_atom(r.text) if r.status_code == 200 else []
    if out:
        return out
    # fallback: the trust's filing list (one series per trust works directly; others are checked by series id)
    r = _sec().get(SUBMISSIONS_URL.format(cik=cik))
    if r.status_code != 200:
        return []
    rec = (r.json().get("filings") or {}).get("recent") or {}
    return [{"acc": a, "date": d} for a, f, d in zip(rec.get("accessionNumber", []), rec.get("form", []),
                                                     rec.get("filingDate", [])) if f == "NPORT-P"][:40]


def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_nport(xml: bytes | str) -> dict:
    """The parts of an NPORT-P primary_doc.xml the app uses: the series, the report date, net assets, and every
    position (name, CUSIP, ISIN, ticker, value, % of net assets, asset category, country)."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = _strip(el.tag)

    def txt(parent, *path):
        cur = parent
        for p in path:
            cur = cur.find(p) if cur is not None else None
        return (cur.text or "").strip() if cur is not None and cur.text else None

    gen = root.find(".//genInfo")
    fund = root.find(".//fundInfo")

    def fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    out = {"series_id": txt(gen, "seriesId"), "series_name": txt(gen, "seriesName"),
           "reg_name": txt(gen, "regName"), "rep_date": txt(gen, "repPdDate"),
           "period_end": txt(gen, "repPdEnd"), "net_assets": fnum(txt(fund, "netAssets")),
           "tot_assets": fnum(txt(fund, "totAssets")), "holdings": []}
    for s in root.iter("invstOrSec"):
        ids = s.find("identifiers")
        isin = tick = None
        if ids is not None:
            e = ids.find("isin")
            isin = e.get("value") if e is not None else None
            e = ids.find("ticker")
            tick = e.get("value") if e is not None else None
        cusip = txt(s, "cusip")
        out["holdings"].append({
            "name": txt(s, "name"), "title": txt(s, "title"), "cusip": None if cusip in (None, "N/A", "000000000")
            else cusip, "isin": isin, "ticker": (tick or "").upper().strip() or None,
            "value": fnum(txt(s, "valUSD")), "weight": fnum(txt(s, "pctVal")), "units": txt(s, "units"),
            "payoff": txt(s, "payoffProfile"), "asset_cat": txt(s, "assetCat") or (
                "other" if s.find("assetConditional") is not None else None),
            "country": txt(s, "invCountry")})
    return out


def fetch_holdings(ticker: str, force: bool = False) -> str:
    """Download and store the fund's latest N-PORT holdings. Returns a short status."""
    t = ticker.upper()
    sm = series_map().get(t)
    if not sm:
        return "not in the SEC's fund list (not a registered fund: Yahoo's top 10 holdings are used)"
    row = get(t) or {}
    db.get().execute("INSERT OR IGNORE INTO etfs(ticker) VALUES(?)", (t,))
    db.get().execute("UPDATE etfs SET cik=?, series_id=?, class_id=? WHERE ticker=?",
                     (sm["cik"], sm["series"], sm["class"], t))
    fl = latest_filings(sm["series"], sm["cik"])
    if not fl:
        return "no N-PORT filing found"
    for f in fl[:12]:
        acc = f["acc"]
        if acc == row.get("holdings_acc") and not force:
            return f"holdings unchanged (as of {row.get('holdings_asof')})"
        r = _sec().get(DOC_URL.format(cik=sm["cik"], acc=acc.replace("-", "")))
        if r.status_code != 200:
            continue
        doc = parse_nport(r.content)
        if doc["series_id"] and doc["series_id"] != sm["series"]:
            continue  # another fund of the same trust
        store_holdings(t, doc, acc, "SEC N-PORT")
        return f"{len(doc['holdings'])} holdings as of {doc['rep_date']}"
    return "no N-PORT filing for this fund among the trust's recent filings"


def store_holdings(t: str, doc: dict, acc: str, source: str) -> None:
    asof = doc.get("rep_date") or date.today().isoformat()
    hs = [h for h in doc["holdings"] if h.get("weight") is not None]
    with db.tx() as con:
        con.execute("DELETE FROM etf_holdings WHERE etf=?", (t,))
        con.executemany(
            "INSERT INTO etf_holdings(etf, asof, name, title, cusip, isin, ticker, weight, value_usd, asset_cat, "
            "country) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(t, asof, h.get("name"), h.get("title"), h.get("cusip"), h.get("isin"), h.get("ticker"),
              h["weight"], h.get("value"), h.get("asset_cat"), h.get("country")) for h in hs])
        con.execute("UPDATE etfs SET holdings_asof=?, holdings_acc=?, holdings_at=?, holdings_source=?, "
                    "net_assets=COALESCE(?, net_assets), n_holdings=? WHERE ticker=?",
                    (asof, acc, db.now(), source, doc.get("net_assets"), len(hs), t))
        if doc.get("net_assets"):
            con.execute("INSERT OR REPLACE INTO etf_aum(etf, date, net_assets) VALUES(?,?,?)",
                        (t, asof, doc["net_assets"]))
    match_holdings(t)


# ===================================================================== Yahoo: fund facts
def parse_facts(info: dict, fd=None) -> dict:
    """Fund facts under stable names from yfinance's info and funds_data (either may be missing)."""
    info = info or {}

    def num(x):
        try:
            v = float(x)
            return v if v == v else None
        except (TypeError, ValueError):
            return None
    inc = info.get("fundInceptionDate")
    out = {"name": info.get("longName") or info.get("shortName"), "family": info.get("fundFamily"),
           "category": info.get("category"), "legal_type": info.get("legalType") or info.get("quoteType"),
           # info's netExpenseRatio is in percent (0.03 = 0.03%); funds_data's ratio (below) is a fraction
           "expense": num(info.get("netExpenseRatio")) / 100 if num(info.get("netExpenseRatio")) is not None
           else None, "aum": num(info.get("totalAssets")),
           "inception": datetime.fromtimestamp(inc, timezone.utc).date().isoformat() if isinstance(inc, (int, float)) else None,
           "yield": num(info.get("yield")) if info.get("yield") is not None else (
               num(info.get("dividendYield")) / 100 if num(info.get("dividendYield")) is not None else None),
           "nav": num(info.get("navPrice")), "beta3y": num(info.get("beta3Year")),
           "description": info.get("longBusinessSummary")}
    if fd is not None:
        try:
            ov = fd.fund_overview or {}
            out["category"] = out["category"] or ov.get("categoryName")
            out["family"] = out["family"] or ov.get("family")
            out["legal_type"] = ov.get("legalType") or out["legal_type"]
        except Exception:
            pass
        try:
            ops = fd.fund_operations
            col = [c for c in ops.columns if c != "Category Average"][0]
            er = num(ops.loc["Annual Report Expense Ratio", col])
            out["expense"] = er if er is not None else out["expense"]
            out["expense_cat"] = num(ops.loc["Annual Report Expense Ratio", "Category Average"])
            out["turnover"] = num(ops.loc["Annual Holdings Turnover", col])
            out["turnover_cat"] = num(ops.loc["Annual Holdings Turnover", "Category Average"])
            out["aum"] = out["aum"] or num(ops.loc["Total Net Assets", col])
        except Exception:
            pass
        try:
            eh = fd.equity_holdings
            col = [c for c in eh.columns if c != "Category Average"][0]
            out["pe"] = num(eh.loc["Price/Earnings", col])
            out["pe_cat"] = num(eh.loc["Price/Earnings", "Category Average"])
        except Exception:
            pass
        try:
            out["sectors"] = {k: float(v) for k, v in (fd.sector_weightings or {}).items() if num(v)}
            out["assets"] = {k: float(v) for k, v in (fd.asset_classes or {}).items() if num(v) is not None}
            th = fd.top_holdings
            out["top"] = [{"ticker": str(i), "name": str(r.get("Name") or ""), "weight": float(r.get("Holding Percent"))
                           } for i, r in th.iterrows()] if th is not None and not th.empty else []
            if not out.get("description"):
                out["description"] = fd.description
        except Exception:
            pass
    return out


def fetch_facts(ticker: str) -> str:
    import yfinance as yf

    t = ticker.upper()
    tk = yf.Ticker(t)
    info = tk.info or {}
    fd = None
    try:
        fd = tk.funds_data
        _ = fd.fund_overview
    except Exception:
        fd = None
    f = parse_facts(info, fd)
    if not (f.get("name") or f.get("expense") or f.get("category")):
        return "no fund facts from Yahoo"
    store_facts(t, f)
    return f"{f.get('category') or 'fund'}, expense {f['expense'] * 100:.2f}%" if f.get("expense") is not None \
        else (f.get("category") or "facts stored")


def store_facts(t: str, f: dict) -> None:
    db.get().execute("INSERT OR IGNORE INTO etfs(ticker) VALUES(?)", (t,))
    cols = ["name", "family", "category", "legal_type", "expense", "expense_cat", "turnover", "turnover_cat", "aum",
            "inception", "yield", "nav", "beta3y", "pe", "pe_cat", "description"]
    kv = {c: f.get(c) for c in cols if f.get(c) is not None}
    for k in ("sectors", "assets", "top"):
        if f.get(k) is not None:
            kv[k + "_json"] = json.dumps(f[k])
    kv["info_at"] = db.now()
    cur = get(t) or {}
    if not cur.get("benchmark") or cur.get("benchmark") == benchmark_for(cur.get("category"), t):
        kv["benchmark"] = benchmark_for(f.get("category") or cur.get("category"), t)
    db.get().execute(f"UPDATE etfs SET {', '.join(k + '=?' for k in kv)} WHERE ticker=?", (*kv.values(), t))
    # no N-PORT (a gold or bitcoin trust, or the SEC couldn't be reached): Yahoo's top holdings stand in
    if f.get("top") and not (cur.get("holdings_source") or "").startswith("SEC"):
        store_holdings(t, {"rep_date": date.today().isoformat(), "net_assets": None, "holdings": [
            {"name": x["name"], "ticker": x["ticker"], "weight": x["weight"] * 100, "asset_cat": "EC"}
            for x in f["top"]]}, "", "Yahoo top 10")


# ===================================================================== matching holdings to companies
_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "sa", "nv", "ag",
           "the", "class", "cl", "a", "b", "c", "com", "common", "stock", "shs", "new", "de", "lp", "llc", "ord",
           "adr", "ads", "sponsored", "se", "holding", "holdings", "group", "grp", "intl"}


def norm_name(s: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower().replace("&", " and "))
    w = [x for x in s.split() if x not in _SUFFIX]
    return " ".join(w)


def _name_index() -> dict[str, int]:
    idx, dup = {}, set()
    for r in db.rows("SELECT cik, name FROM securities WHERE name IS NOT NULL AND ticker IS NOT NULL"):
        k = norm_name(r["name"])
        if not k:
            continue
        if k in idx and idx[k] != r["cik"]:
            dup.add(k)
        idx[k] = r["cik"]
    for k in dup:
        idx.pop(k, None)
    return idx


def match_holdings(etf: str | None = None) -> int:
    """Link each holding to a scored company: filing ticker, then CUSIP (learned across funds), then name. Also
    remembers CUSIP -> company from every holding matched by ticker, for funds whose filings leave the ticker out."""
    tick = {r["ticker"]: r["cik"] for r in db.rows("SELECT ticker, cik FROM securities WHERE ticker IS NOT NULL")}
    tick.update({t.replace(".", "-"): c for t, c in list(tick.items())})
    cmap = {r["cusip"]: r["cik"] for r in db.rows("SELECT cusip, cik FROM cusip_map WHERE cik IS NOT NULL")}
    names = None
    q = "SELECT rowid, * FROM etf_holdings" + (" WHERE etf=?" if etf else "")
    rows = db.rows(q, (etf,) if etf else ())
    upd, learn, n = [], [], 0
    for h in rows:
        cik, how = None, None
        t = (h["ticker"] or "").upper().replace("/", ".").replace(" ", ".")
        if t and t in tick:
            cik, how = tick[t], "ticker"
            if h["cusip"]:
                learn.append((h["cusip"], t, cik, "nport"))
        elif h["cusip"] and h["cusip"] in cmap:
            cik, how = cmap[h["cusip"]], "cusip"
        elif (h["asset_cat"] or "EC") in ("EC", "EP", None) and (h["name"] or h["title"]):
            if names is None:
                names = _name_index()
            for nm in (h["name"], h["title"]):
                k = norm_name(nm)
                if k and k in names:
                    cik, how = names[k], "name"
                    break
        if cik:
            n += 1
        upd.append((cik, how, h["rowid"]))
    with db.tx() as con:
        con.executemany("UPDATE etf_holdings SET cik=?, matched_by=? WHERE rowid=?", upd)
        con.executemany("INSERT OR REPLACE INTO cusip_map(cusip, ticker, cik, source) VALUES(?,?,?,?)", learn)
    return n


# ===================================================================== refreshing
def refresh(tickers: list[str] | None = None, progress=None, holdings_too: bool = True, facts_too: bool = True,
            prices_too: bool = True, force: bool = False) -> dict:
    """Fund facts (weekly), holdings (when a newer N-PORT exists), prices, then scores. Each fund on its own: one
    failing never stops the rest; problems go to the data log."""
    from . import etfscore

    tks = [t.upper() for t in (tickers or universe())]
    done, errors = 0, []
    bms = ["SPY", "VXUS", "VT", "AGG"]
    want = list(dict.fromkeys(tks + bms))
    if not prices_too:  # the daily update brings prices; only funds new to the app need theirs now
        have = {r["ticker"] for r in db.rows("SELECT DISTINCT ticker FROM prices")}
        want = [t for t in want if t not in have]
    if want:
        try:
            from .pipeline import fetch_prices_for

            fetch_prices_for(want)
        except Exception as e:
            errors.append(f"prices: {e}")
    for i, t in enumerate(tks):
        row = get(t) or {}
        msgs = []
        if facts_too and (force or not row.get("info_at") or
                          datetime.fromisoformat(row["info_at"]) < datetime.now() - timedelta(days=7)):
            try:
                msgs.append(fetch_facts(t))
            except Exception as e:
                errors.append(f"{t} facts: {e}")
        if holdings_too and (force or not row.get("holdings_at") or
                             datetime.fromisoformat(row["holdings_at"]) < datetime.now() - timedelta(days=7)):
            try:
                msgs.append(fetch_holdings(t, force))
            except Exception as e:
                errors.append(f"{t} holdings: {e}")
        done += 1
        if progress:
            progress((i + 1) / len(tks), f"{t}: " + "; ".join(m for m in msgs if m))
    n = etfscore.score_all()
    for e in errors[:50]:
        db.log("etf", "refresh", False, e[:300])
    db.log("etf", "refresh", True, f"{done} funds checked, {n} scored, {len(errors)} problems")
    return {"funds": done, "scored": n, "errors": errors}

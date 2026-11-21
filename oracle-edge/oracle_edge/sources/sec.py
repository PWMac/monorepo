"""SEC EDGAR adapter: ticker map, bulk + per-company XBRL facts, SIC codes, new-filing detection.

Rules from the SEC (https://www.sec.gov/os/accessing-edgar-data): max 10 requests/second and a
User-Agent that names you and gives an email. Both are respected here (we use 8/s).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from .. import db, paths
from .http import Http

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTER_FORMS = {"10-Q", "10-Q/A"}
EIGHTK_FORMS = {"8-K", "8-K/A"}  # current reports; item 5.02 = a director or officer leaving or being appointed
# items tracked from 10-Qs for trailing-12-month early warnings
QUARTER_ITEMS = {"revenue", "net_income", "ocf", "capex", "operating_income", "eps_diluted", "shares_diluted",
                 "lt_debt", "st_debt", "equity", "total_assets", "cash", "treasury_stock",
                 "gross_profit", "cogs", "receivables", "inventory", "sbc", "rnd", "st_investments",
                 "op_lease_liability", "pretax_income", "income_tax"}
FOREIGN_FORMS = {"20-F", "20-F/A", "40-F", "40-F/A"}

BANK_SIC = {6021, 6022, 6029, 6035, 6036}
INSURER_SIC = {6311, 6331, 6351, 6361, 6399, 6411}
EXCLUDED_SIC = {6798: "REIT", 6770: "blank check / SPAC", 6726: "fund / BDC"}

_http: Http | None = None


def user_agent() -> str:
    ua = db.cfg("sec_user_agent")
    if not ua:
        raise RuntimeError("SEC identity not set. Open Settings and enter your name and email (the SEC requires it).")
    return ua


def http() -> Http:
    global _http
    if _http is None:
        _http = Http(user_agent(), per_second=8)
    return _http


def reset_http() -> None:
    global _http
    _http = None


@lru_cache(maxsize=1)
def concept_map() -> dict:
    with open(Path(__file__).resolve().parent.parent / "concept_map.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- ticker map
def load_ticker_map(exchanges: list[str]) -> int:
    """Download the SEC ticker/exchange map and upsert into `securities`. Returns count."""
    r = http().get(TICKERS_URL)
    r.raise_for_status()
    payload = r.json()
    return upsert_tickers(payload, exchanges)


def upsert_tickers(payload: dict, exchanges: list[str]) -> int:
    fields = payload["fields"]
    idx = {f: i for i, f in enumerate(fields)}
    seen: set[int] = set()
    allowed = {e.lower() for e in exchanges}
    n = 0
    with db.tx() as con:
        for row in payload["data"]:
            cik = int(row[idx["cik"]])
            exch = (row[idx["exchange"]] or "").strip()
            ticker = (row[idx["ticker"]] or "").strip().upper()
            if not ticker or cik in seen or exch.lower() not in allowed:
                continue
            seen.add(cik)  # first listed ticker is the primary share class
            con.execute(
                """INSERT INTO securities(cik,ticker,name,exchange,updated_at,active) VALUES(?,?,?,?,?,1)
                   ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker,name=excluded.name,
                   exchange=excluded.exchange,updated_at=excluded.updated_at,active=1""",
                (cik, ticker, row[idx["name"]], exch, db.now()),
            )
            n += 1
    return n


# ---------------------------------------------------------------- facts parsing
def _pick_unit(units: dict, unit_kind: str | None) -> tuple[str, list] | None:
    if not units:
        return None
    if unit_kind == "per_share":
        cands = [u for u in units if "/shares" in u.lower()]
    elif unit_kind == "shares":
        cands = [u for u in units if u.lower() == "shares"]
    elif unit_kind == "pure":
        cands = [u for u in units if u.lower() == "pure"]
    else:
        cands = [u for u in units if "/" not in u and u.lower() not in ("shares", "pure")]
    if not cands:
        return None
    if "USD" in cands or "USD/shares" in cands:
        u = "USD" if "USD" in cands else "USD/shares"
    else:
        u = max(cands, key=lambda c: len(units[c]))
    return u, units[u]


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def parse_companyfacts(doc: dict) -> dict:
    """Turn one companyfacts JSON document into normalized annual facts.

    Returns {"facts": [(item, fy_end, value, filed, form, unit)], "shares": [(as_of, value, filed)],
             "currency": str, "foreign": bool, "last_filed": str|None}
    """
    facts_all = doc.get("facts", {})
    cmap = concept_map()
    out: list[tuple] = []
    currencies: dict[str, int] = {}
    foreign = "ifrs-full" in facts_all
    last_filed = None

    # fiscal-year ends from annual duration items (used to keep instants aligned to FY ends)
    fy_ends: set[str] = set()

    item_rows: dict[str, dict[str, list]] = {}
    qout: list[tuple] = []
    for item, spec in cmap.items():
        per_end: dict[str, list] = {}
        q_per: dict[tuple, list] = {}
        for tag in spec["tags"]:
            tax, concept = tag.split(":", 1)
            node = facts_all.get(tax, {}).get(concept)
            if not node:
                continue
            picked = _pick_unit(node.get("units", {}), spec.get("unit"))
            if not picked:
                continue
            unit, entries = picked
            this_tag: dict[str, list] = {}
            q_this: dict[tuple, list] = {}
            for e in entries:
                form = e.get("form", "")
                if form in QUARTER_FORMS and item in QUARTER_ITEMS and e.get("end") and e.get("val") is not None:
                    st_ = e.get("start") or ""
                    if spec["kind"] == "duration" and (not st_ or not (80 <= _days(st_, e["end"]) <= 290)):
                        continue
                    q_this.setdefault((st_, e["end"]), []).append((float(e["val"]), e.get("filed") or e["end"], form))
                    continue
                if form not in ANNUAL_FORMS:
                    continue
                if form in FOREIGN_FORMS:
                    foreign = True
                end = e.get("end")
                if not end or e.get("val") is None:
                    continue
                if spec["kind"] == "duration":
                    start = e.get("start")
                    if not start or not (330 <= _days(start, end) <= 380):
                        continue
                filed = e.get("filed") or end
                this_tag.setdefault(end, []).append((float(e["val"]), filed, form, unit))
                if last_filed is None or filed > last_filed:
                    last_filed = filed
            # earlier tags win per fiscal year; later tags only fill years that are still missing
            for end, vals in this_tag.items():
                if end not in per_end:
                    per_end[end] = vals
            for key, vals in q_this.items():
                if key not in q_per:
                    q_per[key] = vals
            if unit not in ("shares", "pure") and "/" not in unit:
                currencies[unit] = currencies.get(unit, 0) + len(entries)
        item_rows[item] = per_end
        for (st_, end), vals in q_per.items():
            seen_q = set()
            for val, filed, form in vals:
                if filed not in seen_q:
                    seen_q.add(filed)
                    qout.append((item, st_, end, val, filed, form))
        if spec["kind"] == "duration" and item in ("net_income", "revenue"):
            fy_ends.update(per_end.keys())

    for item, per_end in item_rows.items():
        kind = cmap[item]["kind"]
        for end, vals in per_end.items():
            if kind == "instant" and fy_ends and end not in fy_ends:
                continue
            seen_filed = set()
            for val, filed, form, unit in vals:
                if filed in seen_filed:
                    continue
                seen_filed.add(filed)
                out.append((item, end, val, filed, form, unit))

    shares: list[tuple] = []
    dei = facts_all.get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
    for e in dei.get("units", {}).get("shares", []):
        if e.get("end") and e.get("val"):
            shares.append((e["end"], float(e["val"]), e.get("filed") or e["end"]))

    currency = max(currencies, key=currencies.get) if currencies else "USD"
    return {"facts": out, "shares": shares, "currency": currency, "foreign": foreign, "last_filed": last_filed,
            "qfacts": qout}


def store_parsed(cik: int, parsed: dict, con=None) -> None:
    con = con or db.get()
    con.execute("DELETE FROM facts WHERE cik=?", (cik,))
    con.executemany(
        "INSERT OR REPLACE INTO facts(cik,item,fy_end,value,filed,form,unit) VALUES(?,?,?,?,?,?,?)",
        [(cik, *f) for f in parsed["facts"]],
    )
    con.executemany(
        "INSERT OR REPLACE INTO shares_out(cik,as_of,value,filed) VALUES(?,?,?,?)",
        [(cik, *s) for s in parsed["shares"]],
    )
    con.execute("DELETE FROM qfacts WHERE cik=?", (cik,))
    con.executemany(
        "INSERT OR REPLACE INTO qfacts(cik,item,start,end,value,filed,form) VALUES(?,?,?,?,?,?,?)",
        [(cik, *q) for q in parsed.get("qfacts", [])],
    )
    con.execute(
        "UPDATE securities SET currency=?, is_foreign=?, facts_loaded_at=?, last_filed=? WHERE cik=?",
        (parsed["currency"], int(parsed["foreign"]), db.now(), parsed["last_filed"], cik),
    )


# ---------------------------------------------------------------- bulk + per company
def bulk_zip_path() -> Path:
    return paths.CACHE_DIR / "companyfacts.zip"


def download_bulk(progress=None) -> Path:
    return http().download(BULK_URL, bulk_zip_path(), progress=progress)


def ingest_bulk(zip_path: Path, progress=None) -> int:
    """Parse every wanted company in the bulk ZIP. Returns number of companies loaded."""
    wanted = {r["cik"] for r in db.rows("SELECT cik FROM securities WHERE active=1")}
    con = db.get()
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        names = [nm for nm in zf.namelist() if nm.startswith("CIK") and nm.endswith(".json")]
        names = [nm for nm in names if int(re.sub(r"\D", "", nm) or 0) in wanted]
        total = len(names) or 1
        batch = 0
        con.execute("BEGIN IMMEDIATE")
        for i, nm in enumerate(names):
            cik = int(re.sub(r"\D", "", nm))
            try:
                with zf.open(nm) as fh:
                    doc = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
                store_parsed(cik, parse_companyfacts(doc), con)
                n += 1
            except Exception as e:  # one bad file must not stop the load
                db.log("sec", "bulk_parse", False, f"CIK {cik}: {e}")
            batch += 1
            if batch >= 200:
                con.execute("COMMIT")
                con.execute("BEGIN IMMEDIATE")
                batch = 0
            if progress and i % 50 == 0:
                progress(i / total)
        con.execute("COMMIT")
    return n


def refresh_company(cik: int) -> bool:
    r = http().get(FACTS_URL.format(cik=cik))
    if r.status_code == 404:
        return False
    r.raise_for_status()
    with db.tx() as con:
        store_parsed(cik, parse_companyfacts(r.json()), con)
    return True


def fetch_sic(cik: int) -> tuple[int | None, str]:
    r = http().get(SUBMISSIONS_URL.format(cik=cik))
    if r.status_code != 200:
        return None, ""
    d = r.json()
    try:
        return int(d.get("sic") or 0) or None, d.get("sicDescription") or ""
    except ValueError:
        return None, d.get("sicDescription") or ""


def route_category(sic: int | None) -> str:
    if sic in BANK_SIC:
        return "bank"
    if sic in INSURER_SIC:
        return "insurer"
    if sic in EXCLUDED_SIC:
        return "excluded"
    return "standard"


def set_sic(cik: int, sic: int | None, desc: str) -> None:
    db.get().execute(
        "UPDATE securities SET sic=?, sic_desc=?, category=? WHERE cik=?", (sic, desc, route_category(sic), cik)
    )


# ---------------------------------------------------------------- new filings
_IDX_LINE = re.compile(r"^(?P<form>\S.*?)\s{2,}(?P<name>.+?)\s{2,}(?P<cik>\d+)\s{2,}(?P<date>\d{8}|\d{4}-\d{2}-\d{2})\s{2,}\S+\s*$")


def parse_daily_index(text: str, forms: set[str] | None = None) -> list[tuple[str, int, str]]:
    forms = forms or (ANNUAL_FORMS | QUARTER_FORMS)
    out = []
    for line in text.splitlines():
        m = _IDX_LINE.match(line)
        if m and m.group("form").strip() in forms:
            d = m.group("date").replace("-", "")
            out.append((m.group("form").strip(), int(m.group("cik")), f"{d[:4]}-{d[4:6]}-{d[6:]}"))
    return out


def annual_filings_since(since: date, until: date | None = None, forms: set[str] | None = None
                         ) -> list[tuple[str, int, str]]:
    until = until or date.today()
    found: list[tuple[str, int, str]] = []
    d = since
    while d <= until:
        if d.weekday() < 5:
            q = (d.month - 1) // 3 + 1
            url = DAILY_INDEX_URL.format(y=d.year, q=q, ymd=d.strftime("%Y%m%d"))
            try:
                r = http().get(url)
                if r.status_code == 200:
                    found.extend(parse_daily_index(r.text, forms))
            except Exception as e:
                db.log("sec", "daily_index", False, f"{d}: {e}")
        d += timedelta(days=1)
    return found


def officer_changes(cik: int, since: str) -> list[str]:
    """Filing dates of the company's 8-Ks since `since` that report item 5.02 (departure or appointment of
    directors or certain officers, the CEO among them), newest first. From the SEC submissions file."""
    r = http().get(SUBMISSIONS_URL.format(cik=cik))
    if r.status_code != 200:
        return []
    return parse_officer_changes(r.json(), since)


def parse_officer_changes(doc: dict, since: str) -> list[str]:
    rec = (doc.get("filings") or {}).get("recent") or {}
    forms, dates, items = rec.get("form") or [], rec.get("filingDate") or [], rec.get("items") or []
    out = [d for f, d, it in zip(forms, dates, items + [""] * (len(forms) - len(items)))
           if f in EIGHTK_FORMS and d >= since and "5.02" in (it or "")]
    return sorted(set(out), reverse=True)


# ---------------------------------------------------------------- when the CEO started, from filings
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
_CEO_RE = re.compile(r"chief\s+executive\s+officer|\bceo\b", re.I)
_SINCE_RE = re.compile(r"\b(since|in|effective|from|as of)\s+(?:(january|february|march|april|may|june|july|august|"
                       r"september|october|november|december)\s+(?:\d{1,2},\s*)?)?((?:19|20)\d{2})\b", re.I)


def _text(html_: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&").replace("&#8217;", "'")
    return re.sub(r"\s+", " ", t)


def ceo_start_in_text(text: str, name: str) -> tuple[str, str] | None:
    """The date `name` became CEO, from a biography sentence such as "Mr. Smith has served as our Chief Executive
    Officer since March 2014" or "was appointed President and CEO in 2019". Returns (YYYY-MM-01, the sentence), the
    earliest such date found, or None. The sentence must name the person (last name) and the CEO role, and the date
    must follow the CEO wording."""
    last = [w for w in re.sub(r"[.,]", " ", name or "").split() if len(w) > 1]
    if not last:
        return None
    ln = last[-1].lower()
    if ln in ("jr", "sr", "ii", "iii", "iv") and len(last) > 1:
        ln = last[-2].lower()
    best = None
    for sent in re.split(r"(?<=[.;])\s+", text):
        low = sent.lower()
        if ln not in low or len(sent) > 600:
            continue
        for m in _CEO_RE.finditer(sent):
            tail = sent[m.end(): m.end() + 120]
            d = _SINCE_RE.search(tail)
            if not d:
                continue
            if re.search(r"\b(former|previously|prior|until|retired|interim)\b", sent[max(0, m.start() - 60): m.start()], re.I):
                continue  # a past or interim role, not the current one
            mon = _MONTHS.get((d.group(2) or "").lower(), 1)
            when = f"{d.group(3)}-{mon:02d}-01"
            if best is None or when < best[0]:
                best = (when, sent.strip()[:300])
    return best


def ceo_start_from_filings(cik: int, name: str) -> dict | None:
    """Look for when the CEO started in the latest proxy statement (DEF 14A), then the latest annual report (their
    executive-officer biographies). {start, form, filed, sentence} or None."""
    r = http().get(SUBMISSIONS_URL.format(cik=cik))
    if r.status_code != 200:
        return None
    rec = (r.json().get("filings") or {}).get("recent") or {}
    rows = list(zip(rec.get("form") or [], rec.get("filingDate") or [], rec.get("accessionNumber") or [],
                    rec.get("primaryDocument") or []))
    for forms in (("DEF 14A",), ("10-K", "20-F", "40-F")):
        doc = next((x for x in rows if x[0] in forms and x[3]), None)
        if not doc:
            continue
        resp = http().get(ARCHIVE_URL.format(cik=int(cik), acc=doc[2].replace("-", ""), doc=doc[3]))
        if resp.status_code != 200:
            continue
        hit = ceo_start_in_text(_text(resp.text), name)
        if hit:
            return {"start": hit[0], "form": doc[0], "filed": doc[1], "sentence": hit[1]}
    return None


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- ADR share ratio (foreign filers)
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty-five": 25, "thirty": 30,
              "forty": 40, "fifty": 50, "sixty": 60, "eighty": 80, "hundred": 100, "one hundred": 100,
              "one-half": 0.5, "one half": 0.5, "half": 0.5, "one-third": 1 / 3, "one-fourth": 0.25,
              "one-quarter": 0.25, "one-fifth": 0.2, "one-tenth": 0.1, "one-twentieth": 0.05, "two-thirds": 2 / 3,
              "a": 1, "an": 1}
_N = r"(?P<n>\d+(?:\.\d+)?(?:/\d+)?|[a-z]+(?:-[a-z]+)?(?:\s+(?:hundred|half))?)"
_TAIL = (r"\s*(?:\(\s*[\w\.\/\-\s]+\)\s*)?(?:of\s+(?:one\s+|an?\s+|our\s+|the\s+|its\s+))?"
         r"(?:fully\s+paid\s+)?(?:class\s+[a-z]\s+)?(?:ordinary|common|equity|underlying)?\s*shares?")
_Q = "[\"“”]?"
_ADS_RE = re.compile(
    r"(?:each|one)\s+(?:american\s+depositary\s+shares?|ads)\s*(?:\(\s*" + _Q + r"ads" + _Q + r"\s*\))?\s*,?\s*"
    r"(?:represents?|representing|equal(?:s)?\s+to|evidencing|evidences)\s+" + _N + _TAIL, re.I)
_ADS_RE2 = re.compile(
    r"(?:american\s+depositary\s+shares?|adss?)\s*(?:\(\s*" + _Q + r"adss?" + _Q + r"\s*\))?\s*,?\s*"
    r"each\s+(?:representing|represents|of\s+which\s+represents)\s+" + _N + _TAIL, re.I)


def _to_number(tok: str) -> float | None:
    tok = tok.strip().lower()
    m = re.fullmatch(r"([a-z\-]+)\s+hundred", tok)
    if m and _NUM_WORDS.get(m.group(1)):
        return _NUM_WORDS[m.group(1)] * 100.0
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    if re.fullmatch(r"\d+/\d+", tok):
        a, b = tok.split("/")
        return float(a) / float(b) if float(b) else None
    return _NUM_WORDS.get(tok)


def parse_adr_ratio(text: str) -> tuple[float | None, str]:
    """Ordinary shares per ADS from annual-report text. (1.0, ...) when no ADS program is mentioned."""
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"&nbsp;|&#160;|\xa0", " ", plain)
    plain = re.sub(r"&#822[01];|&ldquo;|&rdquo;", '"', plain)
    plain = re.sub(r"\s+", " ", plain)
    found = []
    for rx in (_ADS_RE2, _ADS_RE):
        for m in rx.finditer(plain):
            n = _to_number(m.group("n"))
            if n and 0.001 <= n <= 10000:
                found.append((n, plain[max(0, m.start() - 20): m.end() + 10].strip()))
    if found:
        counts: dict[float, int] = {}
        for n, _ in found:
            counts[n] = counts.get(n, 0) + 1
        best = max(counts, key=counts.get)
        snippet = next(sn for n, sn in found if n == best)
        return best, snippet[:220]
    if re.search(r"american\s+depositary", plain, re.I):
        return None, "ADS program mentioned but the ratio wasn't found"
    return 1.0, "no ADS program: ordinary shares trade directly"


def detect_adr_ratio(cik: int) -> tuple[float | None, str]:
    r = http().get(SUBMISSIONS_URL.format(cik=cik))
    if r.status_code != 200:
        return None, f"submissions HTTP {r.status_code}"
    rec = r.json().get("filings", {}).get("recent", {})
    forms, accs, docs = rec.get("form", []), rec.get("accessionNumber", []), rec.get("primaryDocument", [])
    for f, a, d in zip(forms, accs, docs):
        if f in ("20-F", "40-F", "10-K", "20-F/A", "40-F/A"):
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{a.replace('-', '')}/{d}"
            doc = http().get(url)
            if doc.status_code != 200:
                return None, f"annual report HTTP {doc.status_code}"
            return parse_adr_ratio(doc.text[:3_000_000])
    return None, "no annual report found"


def set_adr_ratio(cik: int, ratio: float | None, source: str) -> None:
    db.get().execute("UPDATE securities SET adr_ratio=?, adr_source=?, adr_checked_at=? WHERE cik=?",
                     (ratio, source[:300], db.now(), cik))

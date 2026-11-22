"""Who the CEO is, and since when, from the company's own SEC filings.

Since 2002 (Sarbanes-Oxley) every 10-K and 10-Q carries a certification by the principal executive officer
(Exhibit 31.1: "I, Jane Smith, certify that I have reviewed this report..."); foreign private issuers certify their
20-F the same way (Exhibit 12.1). So the latest periodic report names today's CEO, and walking back through earlier
reports finds the first one this person certified: their start, to within a quarter, even when no database records
it. Every filing's certifier is cached for good (filings don't change), so later look-ups only read new filings.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from .. import db
from .http import Http
from .sec import SUBMISSIONS_URL, _text, user_agent

PERIODIC = {"10-K", "10-Q", "20-F", "40-F", "10-KT"}
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/{acc}-index.htm"
FILES_URL = "https://data.sec.gov/submissions/{name}"
CURRENT_DAYS = 7  # re-read who certified the latest report at most weekly
PER_SECOND = 3  # its own, gentler client: the app and the daily update may both be reading the SEC (limit 10/s)
_http: Http | None = None


def http() -> Http:
    global _http
    if _http is None:
        _http = Http(user_agent(), per_second=PER_SECOND)
    return _http

_NAME = r"[^,;:()\d<>\[\]]{3,60}?(?:,\s*(?:Jr|Sr|II|III|IV)\.?)?"
_CERT = re.compile(r"\bI,\s+(?P<name>" + _NAME + r")(?:,\s+(?P<title>[^,;]{3,140}?))?,?\s+(?:do\s+)?(?:hereby\s+)?certify",
                   re.S | re.I)
_PEO = re.compile(r"chief\s+executive\s+officer|principal\s+executive\s+officer|\bceo\b", re.I)
_PFO = re.compile(r"chief\s+financial\s+officer|principal\s+financial\s+officer|\bcfo\b|principal\s+accounting",
                  re.I)
_CERT_FILE = re.compile(r"ex(?:hibit)?[-_ ]?31[-_.]?0?1\b|ex(?:hibit)?[-_ ]?311|ex(?:hibit)?[-_ ]?12[-_.]?0?1\b|"
                        r"ex(?:hibit)?[-_ ]?121(?!\d)", re.I)


def _cache_get(key: str, days: int | None = None):
    r = db.one("SELECT fetched_at, json FROM ceo_cache WHERE key=?", (key,))
    if not r:
        return None
    if days is not None and datetime.fromisoformat(r["fetched_at"]) < datetime.now() - timedelta(days=days):
        return None
    return json.loads(r["json"])


def _cache_put(key: str, val) -> None:
    db.get().execute("INSERT OR REPLACE INTO ceo_cache(key, fetched_at, json) VALUES(?,?,?)",
                     (key, datetime.now().isoformat(timespec="seconds"), json.dumps(val)))


# ---------------------------------------------------------------- parsing (pure; tested with sample documents)
def _strip(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", h).replace("&nbsp;", " ")).strip()


def exhibit_links(index_html: str) -> list[tuple[str, str]]:
    """(type, href) of every document in a filing's index page (the table: Seq, Description, Document, Type,
    Size). Type is upper-cased, e.g. "EX-31.1"; the description is used when the type cell is blank."""
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", index_html):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        m = re.search(r'href="([^"]+)"', cells[2])
        if not m:
            continue
        typ = _strip(cells[3]).upper() or _strip(cells[1]).upper()
        out.append((typ, m.group(1)))
    return out


def pick_certification(links: list[tuple[str, str]]) -> str | None:
    """The principal executive officer's certification: EX-31.1 (10-K/10-Q; EX-31 when one exhibit holds both),
    EX-12.1 (20-F), else a document whose file name says so (e.g. "...ex311.htm")."""
    for want in ("EX-31.1", "EX-31.01", "EX-31", "EX-31.A", "EX-12.1", "EX-12.01", "EX-12"):
        for t, h in links:
            if t == want:
                return h
    for t, h in links:
        if _CERT_FILE.search(h.rsplit("/", 1)[-1]) or t.startswith("EX-31"):
            return h
    return None


def _clean_name(n: str) -> str:
    n = re.sub(r"\s+", " ", n).strip().rstrip(",").strip()
    if n.endswith(".") and not re.search(r"\b(?:Jr|Sr)\.$", n, re.I):
        n = n[:-1]
    n = re.sub(r"^(?:Mr|Ms|Mrs|Dr)\.?\s+", "", n)
    if n.isupper():  # "JOHN A. SMITH" -> "John A. Smith"
        n = " ".join(w if len(w) == 2 and w.endswith(".") or w.strip(",") in ("II", "III", "IV") else w.capitalize()
                     for w in n.split())
    return n


def certifier(text: str) -> dict | None:
    """The person certifying as principal executive officer: {name, title}. An exhibit can hold more than one
    certification (CEO and CFO in one EX-31): the one whose own wording or signature names the CEO / principal
    executive role is taken; a lone certification signed only as CFO is not."""
    hits = list(_CERT.finditer(text))
    if not hits:
        return None
    fallback = None
    for i, m in enumerate(hits):
        name = _clean_name(m.group("name"))
        words = name.split()
        if not (1 < len(words) <= 6) or name.lower() in ("the undersigned", "each of the undersigned"):
            continue
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(text), m.end() + 5000)
        seg = (m.group("title") or "") + " " + text[m.end(): end][-900:]
        title = (m.group("title") or "").strip()
        if not title:
            t = re.search(r"((?:(?:President|Chairman|Chairwoman|Chair|Founder|Co-Founder|Executive|Vice|of the Board|"
                          r"and|&|,)\s*)*(?:Co-)?(?:Chief|Principal) Executive Officer(?:\s*\([^)]{0,40}\))?)", seg)
            title = re.sub(r"\s+", " ", t.group(1)).strip(" ,") if t else ""
        cand = {"name": name, "title": title}
        if _PEO.search(seg):
            return cand
        if fallback is None and not _PFO.search(seg):
            fallback = cand
    return fallback if len(hits) == 1 else None


def same_person(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    words = lambda s: [w for w in re.sub(r"[.,]", " ", s).lower().split() if len(w) > 1 and w not in  # noqa: E731
                       ("jr", "sr", "ii", "iii", "iv", "mr", "ms", "dr")]
    wa, wb = words(a), words(b)
    return bool(wa and wb and (wa[-1] in wb or wb[-1] in wa))


def find_start(filings: list[dict], who_at) -> dict:
    """filings: periodic reports, newest first. who_at(i) -> certifier name of filings[i] (None if unreadable).
    Finds the oldest report in the current CEO's unbroken run: gallop back (1, 2, 4, 8... reports), then halve the
    gap, so ~2 log2(n) reports are read. An unreadable report is replaced by a readable neighbour; with none, the
    search stops on the safe side (a later start). Returns {first, prev, prev_name}: the oldest report they certified
    and the next older report read (certified by someone else), or prev None when they certified every report seen."""
    n = len(filings)
    name = who_at(0) if n else None
    out = {"first": filings[0] if n else None, "prev": None, "prev_name": None}
    if not name or n < 2:
        return out

    def probe(i, lo, hi):  # read report i, or a readable neighbour strictly between lo and hi
        for j in (i, i + 1, i - 1, i + 2, i - 2):
            if lo < j < hi:
                nm = who_at(j)
                if nm is not None:
                    return j, same_person(nm, name)
        return None, None
    good, bad, step = 0, None, 1
    while good < n - 1:
        j, same = probe(min(good + step, n - 1), good, n)
        if j is None:
            break
        if same:
            good, step = j, step * 2
        else:
            bad = j
            break
    if bad is None:
        out["first"] = filings[good]
        return out
    lo, hi = good, bad
    while hi - lo > 1:
        j, same = probe((lo + hi) // 2, lo, hi)
        if j is None:
            break  # nothing readable in between: the change lies somewhere in (lo, hi)
        lo, hi = (j, hi) if same else (lo, j)
    out.update(first=filings[lo], prev=filings[hi], prev_name=who_at(hi))
    return out


# ---------------------------------------------------------------- network (cached)
def _periodic(page: dict) -> list[dict]:
    out = []
    forms = page.get("form") or []
    for form, filed, acc, rep in zip(forms, page.get("filingDate") or [], page.get("accessionNumber") or [],
                                     page.get("reportDate") or [""] * len(forms)):
        if form in PERIODIC and filed >= "2003-08-14":  # when Exhibit 31 certifications began
            out.append({"form": form, "filed": filed, "acc": acc, "period": rep or ""})
    return out


def periodic_filings(cik: int, older: bool = False) -> tuple[list[dict], bool]:
    """The company's 10-K / 10-Q / 20-F / 40-F reports, newest first: [{form, filed, acc, period}], and whether
    older pages of the filing list exist. older=True also reads those pages (big companies' recent list can be
    mostly insider-trading forms, reaching back only a few years)."""
    r = http().get(SUBMISSIONS_URL.format(cik=cik))
    if r.status_code != 200:
        return [], False
    doc = r.json()
    out = _periodic((doc.get("filings") or {}).get("recent") or {})
    files = [f.get("name") for f in (doc.get("filings") or {}).get("files") or [] if f.get("name")]
    if older:
        for nm in files:
            rr = http().get(FILES_URL.format(name=nm))
            if rr.status_code == 200:
                out += _periodic(rr.json())
    out = list({f["acc"]: f for f in out}.values())
    out.sort(key=lambda x: x["filed"], reverse=True)
    return out, bool(files) and not older


def certifier_of(cik: int, acc: str) -> dict | None:
    """Who certified this filing as principal executive officer: {name, title}, or None. Cached for good."""
    key = f"cert:{acc}"
    c = _cache_get(key)
    if c is not None:
        return c or None
    acc_nd = acc.replace("-", "")
    found: dict = {}
    r = http().get(INDEX_URL.format(cik=int(cik), acc_nd=acc_nd, acc=acc))
    if r.status_code == 200:
        href = pick_certification(exhibit_links(r.text))
        if href:
            url = href if href.startswith("http") else "https://www.sec.gov" + href
            e = http().get(url)
            if e.status_code == 200:
                found = certifier(_text(e.text)) or {}
    elif r.status_code != 404:
        return None  # throttled or failing: don't remember a miss
    _cache_put(key, found)
    return found or None


def run_of(cik: int, fl: list[dict]) -> dict | None:
    """The current certifier's run over these reports (newest first), as current() returns it, or None."""
    names: dict[int, str | None] = {}

    def who_at(i):
        if i not in names:
            names[i] = (certifier_of(cik, fl[i]["acc"]) or {}).get("name")
        return names[i]
    k = next((i for i in range(min(4, len(fl))) if who_at(i)), None)  # the latest report that can be read
    if k is None:
        return None
    run = find_start(fl[k:], lambda i: who_at(i + k))
    cert = certifier_of(cik, fl[k]["acc"]) or {}
    first, prev = run["first"], run["prev"]
    out = {"name": cert.get("name"), "title": cert.get("title", ""), "filed": fl[k]["filed"], "form": fl[k]["form"],
           "first_filed": first["filed"], "first_form": first["form"], "prev_name": run.get("prev_name"),
           "reads": len(names)}
    if prev:  # took over between the predecessor's last report and their own first one
        a, b = date.fromisoformat(prev["filed"]), date.fromisoformat(first["filed"])
        out["since"] = (a + (b - a) / 2).isoformat()
        out["since_range"] = [prev["filed"], first["filed"]]
    else:  # certified every report on file: CEO at least since then
        out["since"] = first["period"] or first["filed"]
        out["at_least"] = True
    return out


def current(cik: int, refresh: bool = False) -> dict | None:
    """Today's CEO per the latest periodic report and since when, from the run of reports they certified:
    {name, title, filed, form, first_filed, first_form, since, since_range | at_least, prev_name}. {} when the SEC
    has nothing readable for the company; None when the SEC couldn't be reached. Cached a week."""
    key = f"secceo:{cik}"
    if not refresh:
        c = _cache_get(key, CURRENT_DAYS)
        if c is not None:
            return c
    try:
        fl, more = periodic_filings(cik)
        out = run_of(cik, fl) if fl else None
        if out and out.get("at_least") and more:  # their run reaches past the recent list: read the older pages
            fl, _ = periodic_filings(cik, older=True)
            out = run_of(cik, fl) or out
    except Exception:
        return None  # offline: try again next time
    out = out or {}
    _cache_put(key, out)
    return out

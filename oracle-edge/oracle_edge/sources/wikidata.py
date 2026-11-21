"""Who ran a company, and when: chief executive officers (P169) with start/end dates (P580/P582) from Wikidata's
free SPARQL service, matched by SEC CIK (P5531) or ticker (P249 on the stock exchange, P414). Also the other
companies a person has run. Cached for 30 days. Wikidata is edited by volunteers: well covered for large
companies, patchy for small ones, and sometimes late after a CEO change, so the app lets you correct it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .. import db

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "OracleEdge/0.14 (personal research tool; https://github.com/PWMac/monorepo)"
MAX_AGE_DAYS = 30


def _query(q: str) -> list[dict]:
    import httpx

    r = httpx.get(ENDPOINT, params={"query": q, "format": "json"}, timeout=20,
                  headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    r.raise_for_status()
    out = []
    for b in r.json().get("results", {}).get("bindings", []):
        out.append({k: v.get("value") for k, v in b.items()})
    return out


def _cached(key: str, fn):
    r = db.one("SELECT fetched_at, json FROM ceo_cache WHERE key=?", (key,))
    if r and datetime.fromisoformat(r["fetched_at"]) > datetime.now() - timedelta(days=MAX_AGE_DAYS):
        return json.loads(r["json"])
    try:
        val = fn()
    except Exception as e:  # offline or the service is busy: keep any old copy
        db.log("wikidata", key, False, str(e)[:200])
        return json.loads(r["json"]) if r else None
    db.get().execute("INSERT OR REPLACE INTO ceo_cache(key, fetched_at, json) VALUES(?,?,?)",
                     (key, datetime.now().isoformat(timespec="seconds"), json.dumps(val)))
    return val


def _d(v: str | None) -> str | None:
    return v[:10] if v and len(v) >= 10 and v[0] != "-" else None


def parse_ceos(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        qid = (r.get("ceo") or "").rsplit("/", 1)[-1]
        key = (qid, _d(r.get("start")))
        if not qid or key in seen:
            continue
        seen.add(key)
        out.append({"qid": qid, "name": r.get("ceoLabel") or qid, "start": _d(r.get("start")), "end": _d(r.get("end")),
                    "company_qid": (r.get("company") or "").rsplit("/", 1)[-1]})
    out.sort(key=lambda x: x["start"] or "0000")
    return out


def ceos(cik: int, ticker: str | None) -> list[dict] | None:
    """Every CEO Wikidata lists for the company, oldest first: {qid, name, start, end}. None if unreachable."""
    ciks = f'"{int(cik):010d}" "{int(cik)}"'
    tk = (ticker or "").replace('"', "")
    q = f"""SELECT ?company ?ceo ?ceoLabel ?start ?end WHERE {{
      {{ VALUES ?cik {{ {ciks} }} ?company wdt:P5531 ?cik . }}
      UNION {{ ?company p:P414 ?ex . ?ex pq:P249 "{tk}" . }}
      ?company p:P169 ?st . ?st ps:P169 ?ceo .
      OPTIONAL {{ ?st pq:P580 ?start }} OPTIONAL {{ ?st pq:P582 ?end }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }} LIMIT 200"""
    val = _cached(f"ceos:{cik}", lambda: _query(q))
    return parse_ceos(val) if val is not None else None


def companies_led(qid: str) -> list[dict]:
    """Companies a person has been CEO of: {company, label, cik, start, end}."""
    q = f"""SELECT ?company ?companyLabel ?cik ?start ?end WHERE {{
      ?company p:P169 ?st . ?st ps:P169 wd:{qid} .
      OPTIONAL {{ ?st pq:P580 ?start }} OPTIONAL {{ ?st pq:P582 ?end }}
      OPTIONAL {{ ?company wdt:P5531 ?cik }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }} LIMIT 50"""
    val = _cached(f"led:{qid}", lambda: _query(q)) or []
    out, seen = [], set()
    for r in val:
        k = (r.get("company"), _d(r.get("start")))
        if k in seen:
            continue
        seen.add(k)
        cik = r.get("cik")
        out.append({"company": (r.get("company") or "").rsplit("/", 1)[-1], "label": r.get("companyLabel"),
                    "cik": int(cik) if cik and cik.isdigit() else None, "start": _d(r.get("start")),
                    "end": _d(r.get("end"))})
    return out

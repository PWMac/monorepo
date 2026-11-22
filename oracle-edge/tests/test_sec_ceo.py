"""Who the CEO is, from the SEC certifications every 10-K / 10-Q / 20-F carries (sample documents, no network)."""

import json

from oracle_edge import ceo, db
from oracle_edge.sources import sec_ceo

INDEX = """<table class="tableFile" summary="Document Format Files">
<tr><th scope="col">Seq</th><th scope="col">Description</th><th scope="col">Document</th><th scope="col">Type</th>
<th scope="col">Size</th></tr>
<tr><td scope="row">1</td><td scope="row">10-Q</td><td scope="row"><a href="/Archives/edgar/data/77/{acc}/zz-q.htm">zz-q.htm</a>
 &nbsp;&nbsp;<span class="label">iXBRL</span></td><td scope="row">10-Q</td><td scope="row">912345</td></tr>
<tr><td scope="row">2</td><td scope="row">CERTIFICATION OF CEO</td><td scope="row">
<a href="/Archives/edgar/data/77/{acc}/zz-ex311.htm">zz-ex311.htm</a></td><td scope="row">EX-31.1</td><td scope="row">9</td></tr>
<tr><td scope="row">3</td><td scope="row">CERTIFICATION OF CFO</td><td scope="row">
<a href="/Archives/edgar/data/77/{acc}/zz-ex312.htm">zz-ex312.htm</a></td><td scope="row">EX-31.2</td><td scope="row">9</td></tr>
</table>"""

CERT = """<html><body><p>Exhibit 31.1</p><p>CERTIFICATION PURSUANT TO RULE 13a-14(a)</p>
<p>I, {name}, certify that:</p><p>1. I have reviewed this quarterly report on Form 10-Q of Zed Corp.;</p>
<p>Date: {d}</p><p>/s/ {name}</p><p>{name}</p><p>President and Chief Executive Officer</p>
<p>(Principal Executive Officer)</p></body></html>"""


def test_reads_the_certifier_from_an_exhibit_31():
    assert sec_ceo.certifier(sec_ceo._text(CERT.format(name="Maria L. Ortiz", d="2026-08-01"))) == {
        "name": "Maria L. Ortiz", "title": "President and Chief Executive Officer (Principal Executive Officer)"}
    # all capitals, a suffix, and the title inside the sentence
    assert sec_ceo.certifier("I, ROBERT K. JONES, JR., certify that ... (Principal Executive Officer)")["name"] == \
        "Robert K. Jones, Jr."
    assert sec_ceo.certifier("I, Ludwig van Hoek, Chief Executive Officer of Zed N.V., certify that")["name"] == \
        "Ludwig van Hoek"
    # one exhibit with both certifications: the CEO's, not the CFO's
    both = ("I, Ann Lee, certify that ... /s/ Ann Lee Chief Financial Officer. "
            "I, Raj Patel, certify that ... /s/ Raj Patel Chief Executive Officer")
    assert sec_ceo.certifier(both)["name"] == "Raj Patel"
    # a lone certification signed only as CFO isn't the CEO's
    assert sec_ceo.certifier("I, Ann Lee, certify that ... /s/ Ann Lee Chief Financial Officer") is None


def test_finds_the_certification_in_the_filing_index():
    links = sec_ceo.exhibit_links(INDEX.format(acc="000007726000001"))
    assert ("EX-31.1", "/Archives/edgar/data/77/000007726000001/zz-ex311.htm") in links
    assert sec_ceo.pick_certification(links).endswith("zz-ex311.htm")
    assert sec_ceo.pick_certification([("EX-99", "/x/abc-exhibit121.htm")]).endswith("exhibit121.htm")  # 20-F
    assert sec_ceo.pick_certification([("EX-99", "/x/press.htm")]) is None


def test_steps_back_through_the_reports_to_the_first_one_the_ceo_signed():
    fl = [{"filed": f"{2026 - i // 4}-{(3 - i % 4) * 3 + 1:02d}-10", "acc": str(i), "period": "", "form": "10-Q"}
          for i in range(80)]
    for change, unreadable in ((13, set()), (1, set()), (79, set()), (13, {6, 7, 12}), (200, set())):
        reads = set()

        def who_at(i):
            reads.add(i)
            return None if i in unreadable else ("Maria Ortiz" if i < change else "Tom Baker")
        r = sec_ceo.find_start(fl, who_at)
        if change >= len(fl):
            assert r["prev"] is None and r["first"] is fl[-1]
        else:
            first, prev = fl.index(r["first"]), fl.index(r["prev"])
            assert first < change <= prev and who_at(prev) == "Tom Baker" and r["prev_name"] == "Tom Baker"
            assert first == change - 1 or (first + 1) in unreadable  # exact unless the reports between can't be read
        assert len(reads) <= 16  # a handful of reports, not all 80


class _R:
    def __init__(self, code, text="", js=None):
        self.status_code, self.text, self._js = code, text, js

    def json(self):
        return self._js


def _fake_sec(change_at=6, n=24, files=False):
    """A company whose CEO changed `change_at` reports ago. Returns (http stub, counter of documents read)."""
    accs = [f"0000077-{26 - i // 4:02d}-{i:06d}" for i in range(n)]
    filed = [f"{2026 - i // 4}-{(3 - i % 4) * 3 + 1:02d}-10" for i in range(n)]
    recent = {"form": ["4", "8-K"] + ["10-Q" if i % 4 else "10-K" for i in range(n)],
              "filingDate": ["2026-11-01", "2026-10-30"] + filed, "accessionNumber": ["a", "b"] + accs,
              "reportDate": ["", ""] + [""] * n}
    doc = {"filings": {"recent": recent, "files": []}}
    seen = []

    class H:
        def get(self, url, params=None):
            seen.append(url)
            if "submissions" in url:
                return _R(200, js=doc)
            for i, a in enumerate(accs):
                nd = a.replace("-", "")
                if url.endswith(f"{a}-index.htm"):
                    return _R(200, INDEX.format(acc=nd))
                if nd in url and url.endswith("zz-ex311.htm"):
                    return _R(200, CERT.format(name="Maria L. Ortiz" if i < change_at else "Tom Baker", d=filed[i]))
            return _R(404)
    return H(), seen, filed


def test_current_ceo_and_start_from_the_sec(monkeypatch):
    monkeypatch.undo()  # the real look-up (conftest stubs it out)
    h, seen, filed = _fake_sec(change_at=6)
    monkeypatch.setattr(sec_ceo, "http", lambda: h)
    r = sec_ceo.current(7701, refresh=True)
    assert r["name"] == "Maria L. Ortiz" and r["prev_name"] == "Tom Baker"
    assert r["since_range"] == [filed[6], filed[5]] and filed[6] < r["since"] < filed[5]
    n = len(seen)
    assert n < 30
    # cached: every certifier is remembered, so a fresh look reads only the filing list
    seen.clear()
    assert sec_ceo.current(7701, refresh=True)["name"] == "Maria L. Ortiz"
    assert all("submissions" in u for u in seen)
    # signed every report on file: "at least since" the oldest
    h2, _, filed2 = _fake_sec(change_at=99, n=10)
    monkeypatch.setattr(sec_ceo, "http", lambda: h2)
    db.get().execute("DELETE FROM ceo_cache WHERE key LIKE 'cert:%'")
    r = sec_ceo.current(7702, refresh=True)
    assert r["at_least"] and r["since"] == filed2[-1]


def test_sec_names_the_ceo_when_wikidata_is_stale_or_down(monkeypatch):
    from oracle_edge.sources import company, wikidata

    sec = {"name": "Maria L. Ortiz", "since": "2025-02-14", "since_range": ["2024-11-01", "2025-05-01"],
           "filed": "2026-08-01", "form": "10-Q", "first_form": "10-Q", "first_filed": "2025-05-01",
           "prev_name": "Tom Baker"}
    # Wikidata still lists the predecessor: the SEC's signer wins, start from the certifications
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: [{"qid": "Q1", "name": "Tom Baker", "start": "2012-01-01",
                                                         "end": None}])
    res = ceo.who(7703, "ZZQ", [], sec)
    assert res["current"]["name"] == "Maria L. Ortiz" and res["current"]["start"] == "2025-02-14"
    assert res["current"]["approx"] and "first signed the 10-Q filed 2025-05-01" in res["current"]["source"]
    assert res["wikidata_stale"] == "Tom Baker"
    # Wikidata has the same person: its exact start date is used
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: [{"qid": "Q2", "name": "Maria Ortiz", "start": "2025-01-06",
                                                         "end": None}])
    res = ceo.who(7703, "ZZQ", [], sec)
    assert res["current"]["start"] == "2025-01-06" and res["current"]["qid"] == "Q2"
    # Yahoo disagrees with the latest certification: shown as a conflict
    assert "conflict" in ceo.who(7703, "ZZQ", [{"name": "Zoe Quinn", "title": "Chief Executive Officer"}], sec)
    # Wikidata unreachable: the SEC's answer is still saved for the Screener
    monkeypatch.undo()
    monkeypatch.setattr(sec_ceo, "current", lambda cik, refresh=False: sec)
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: None)
    monkeypatch.setattr(wikidata, "companies_led", lambda *a, **k: [])
    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": []})
    cik = 7704
    db.get().execute("INSERT OR REPLACE INTO securities(cik, ticker, name) VALUES(?,?,?)", (cik, "ZZR", "Zed R"))
    res = ceo.assess(cik, "ZZR")
    assert res["saved"]
    g = db.one("SELECT * FROM ceo_grades WHERE cik=?", (cik,))
    assert g["name"] == "Maria L. Ortiz" and g["start"] == "2025-02-14"


def test_at_least_start_looks_for_the_biography_date(monkeypatch):
    from oracle_edge.sources import company, sec, wikidata

    monkeypatch.setattr(sec_ceo, "current", lambda cik, refresh=False: {
        "name": "Long Server", "since": "2009-06-30", "at_least": True, "filed": "2026-08-01", "form": "10-Q",
        "first_form": "10-Q", "first_filed": "2009-08-01"})
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: [])
    monkeypatch.setattr(wikidata, "companies_led", lambda *a, **k: [])
    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": []})
    monkeypatch.setattr(sec, "ceo_start_from_filings", lambda c, n: {
        "start": "2001-03-01", "form": "DEF 14A", "filed": "2026-03-20", "sentence": "CEO since March 2001"})
    cik = 7705
    db.get().execute("INSERT OR REPLACE INTO securities(cik, ticker, name) VALUES(?,?,?)", (cik, "ZZL", "Zed L"))
    db.get().execute("DELETE FROM ceo_cache WHERE key LIKE 'secstart:7705:%'")
    res = ceo.assess(cik, "ZZL")
    assert res["current"]["start"] == "2001-03-01" and not res["current"]["at_least"]
    assert "DEF 14A" in res["current"]["source"]
    json.dumps(res["current"])  # stays serialisable for the card


def test_daily_check_spots_a_new_signer_on_a_new_report(monkeypatch):
    from oracle_edge.sources import company, wikidata

    cik = 7706
    db.get().execute("INSERT OR REPLACE INTO securities(cik, ticker, name) VALUES(?,?,?)", (cik, "ZZN", "Zed N"))
    db.get().execute("INSERT OR REPLACE INTO ceo_grades(cik, name, start, note, updated_at) VALUES(?,?,?,?,?)",
                     (cik, "Tom Baker", "2012-01-01", "", db.now()))
    new = {"name": "Maria L. Ortiz", "since": "2026-09-01", "since_range": ["2026-08-01", "2026-11-05"],
           "filed": "2026-11-05", "form": "10-Q", "first_form": "10-Q", "first_filed": "2026-11-05",
           "prev_name": "Tom Baker"}
    monkeypatch.setattr(sec_ceo, "current", lambda c, refresh=False: new)
    monkeypatch.setattr(wikidata, "ceos", lambda c, t: [])
    monkeypatch.setattr(wikidata, "companies_led", lambda *a, **k: [])
    monkeypatch.setattr(company, "get", lambda t, refresh=False: {"officers": []})
    out = ceo.check_changes({}, set(), periodic={cik})
    assert out["checked_reports"] == 1 and out["officer_changes"] == 1
    ch = db.one("SELECT * FROM ceo_changes WHERE cik=?", (cik,))
    assert ch["old_name"] == "Tom Baker" and ch["new_name"] == "Maria L. Ortiz" and "certified by" in ch["source"]
    # same signer next quarter: nothing to do
    out = ceo.check_changes({}, set(), periodic={cik})
    assert out["officer_changes"] == 0

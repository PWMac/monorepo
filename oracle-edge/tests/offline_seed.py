"""Build a fully populated offline database from the synthetic market (no network).

Usage:  ORACLE_EDGE_DATA=/tmp/oe_data python -m tests.offline_seed
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracle_edge import db, paths, pipeline, rules  # noqa: E402
from oracle_edge.sources import prices, sec  # noqa: E402
from tests import synthetic  # noqa: E402


class FakeJob:
    def update(self, progress=None, step=None, message=None):
        if step:
            print(f"  [{progress if progress is not None else '':>}] {step}", flush=True)

    def sub(self, lo, hi, step):
        self.update(lo, step)
        return lambda f: None


def seed(with_backtest=True):
    t0 = time.time()
    db.get()
    db.set_cfg("sec_user_agent", "Oracle Edge test test@example.com")
    db.set_cfg("backfill_start", "2019-01-01")
    rules.seed_profiles()
    comps, tickers, docs = synthetic.build()
    sec.upsert_tickers(tickers, ["NYSE", "Nasdaq"])
    zp = paths.CACHE_DIR / "companyfacts.zip"
    synthetic.write_zip(docs, zp)
    n = sec.ingest_bulk(zp)
    print(f"ingested {n} companies in {time.time() - t0:.1f}s")
    prices.store(synthetic.price_frames(comps), "synthetic")
    with db.tx() as con:
        con.executemany("INSERT OR REPLACE INTO macro(series,date,value) VALUES(?,?,?)", synthetic.macro_rows())
    for tk, (cik, kind, _) in comps.items():
        sic = {"bank": 6022, "foreign": 2834, "insurer": 6331}.get(kind, 3571)
        sec.set_sic(cik, sic, kind)
        if kind == "foreign":
            sec.set_adr_ratio(cik, 2.0, "manual: synthetic test")
    # no network: prices already stored, SIC already set
    pipeline.fetch_prices_for = lambda tickers, progress=None: {"rows": 0, "missing": []}
    pipeline.route_sic = lambda ciks, progress=None: None
    pipeline.detect_adr = lambda ciks, progress=None: 0
    pipeline.finish_setup(FakeJob(), lo=0.5)
    db.set_cfg("initial_load_done", db.now())
    print(f"setup done in {time.time() - t0:.1f}s")
    if with_backtest:
        p = rules.default_profile()
        pipeline.run_backtest(FakeJob(), p.id, "2019-01-02", "2026-11-13", "weekly", "Classic weekly 2019→now")
        print(f"backtest done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    seed()

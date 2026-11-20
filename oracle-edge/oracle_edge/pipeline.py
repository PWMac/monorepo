"""The three big workflows: first-time load, daily after-close run, and backtests (replays)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import backup, db, emailer, notify, quarterly, rules, targets
from .engine import PriceBook, SnapshotBook, build_snapshots, evaluate
from .market import Macro
from .signals import BENCH, PortfolioSim, ensure_portfolio
from .sources import fred, prices, sec
from .tradecal import completed_session

log = logging.getLogger("oracle_edge")
MARKET_TICKERS = [BENCH, "VTI"]
PRICE_HISTORY_START = "2009-01-01"


def backfill_start() -> str:
    return db.cfg("backfill_start", "2019-01-01")


# ================================================================== helpers
def tickers_by_cik() -> dict[int, str]:
    return {r["cik"]: r["ticker"] for r in db.rows("SELECT cik, ticker FROM securities WHERE ticker IS NOT NULL")}


def candidate_ciks(profile_ids: list[int] | None = None) -> list[int]:
    """Companies that passed the gates (or nearly) in any snapshot, plus routed financials and owned stocks."""
    q = "SELECT DISTINCT cik FROM snapshots WHERE (gates_passed=1 OR near_miss IS NOT NULL)"
    params: list = []
    if profile_ids:
        # a profile that shares another profile's company scores (same scoring rules) uses that profile's rows
        ids = sorted(set(profile_ids) | {rules.get_profile(i).snap[0] for i in profile_ids})
        q += f" AND profile_id IN ({','.join('?' * len(ids))})"
        params = ids
    ciks = {r["cik"] for r in db.rows(q, params)}
    ciks |= {r["cik"] for r in db.rows("SELECT cik FROM positions")}
    ciks |= {r["cik"] for r in db.rows("SELECT cik FROM real_holdings")}
    return sorted(ciks)


def financial_suspects() -> list[int]:
    return [r["cik"] for r in db.rows(
        "SELECT DISTINCT cik FROM facts WHERE item IN ('deposits','premiums_earned')")]


def route_sic(ciks: list[int], progress=None) -> None:
    todo = [r["cik"] for r in db.rows(
        f"SELECT cik FROM securities WHERE sic IS NULL AND cik IN ({','.join('?' * len(ciks))})", ciks)] if ciks else []
    for i, cik in enumerate(todo):
        try:
            sic, desc = sec.fetch_sic(cik)
            sec.set_sic(cik, sic, desc)
        except Exception as e:
            db.log("sec", "sic", False, f"{cik}: {e}")
        if progress and i % 20 == 0:
            progress(i / max(len(todo), 1))


def detect_adr(ciks: list[int], progress=None) -> int:
    """Find the ADR share ratio for foreign candidates (manual overrides are never replaced)."""
    if not ciks:
        return 0
    todo = [r["cik"] for r in db.rows(
        f"SELECT cik FROM securities WHERE is_foreign=1 AND (adr_source IS NULL OR adr_source NOT LIKE 'manual%') "
        f"AND adr_checked_at IS NULL AND cik IN ({','.join('?' * len(ciks))})", ciks)]
    for i, cik in enumerate(todo):
        try:
            ratio, why = sec.detect_adr_ratio(cik)
            sec.set_adr_ratio(cik, ratio, f"auto: {why}")
        except Exception as e:
            db.log("sec", "adr_ratio", False, f"{cik}: {e}")
        if progress and i % 5 == 0:
            progress(i / max(len(todo), 1))
    return len(todo)


def price_tickers(ciks: list[int]) -> list[str]:
    tk = tickers_by_cik()
    return [tk[c] for c in ciks if c in tk] + MARKET_TICKERS


def fetch_prices_for(tickers: list[str], progress=None) -> dict:
    """Incremental: new tickers get full history, known tickers get the last ~10 days."""
    today = date.today()
    known = {r["ticker"]: r["m"] for r in db.rows("SELECT ticker, MAX(date) m FROM prices GROUP BY ticker")}
    new = [t for t in tickers if t not in known]
    old = [t for t in tickers if t in known]
    res_new = prices.fetch(new, date.fromisoformat(PRICE_HISTORY_START), today, progress) if new else {"rows": 0,
                                                                                                       "missing": []}
    oldest = min((known[t] for t in old), default=str(today))
    start = min(date.fromisoformat(oldest), today - timedelta(days=10)) if old else today
    res_old = prices.fetch(old, start, today) if old else {"rows": 0, "missing": []}
    return {"rows": res_new["rows"] + res_old["rows"], "missing": res_new["missing"] + res_old["missing"]}


# ================================================================== replay (live history + backtests)
def replay(run_id: int, profile: rules.Profile, end: str, cadence: str = "daily", job=None, lo=0.0, hi=1.0) -> int:
    return replay_many([run_id], profile, end, cadence, job, lo, hi, full={run_id})


def replay_many(run_ids: list[int], profile: rules.Profile, end: str, cadence: str = "daily", job=None, lo=0.0,
                hi=1.0, full: set[int] | None = None) -> int:
    """Replay one or more runs of the same profile over the same days. Companies are scored once per day and
    every run's portfolio decides on the same evaluations, so testing 15 portfolio sizes costs about as much
    as one backtest. Runs in `full` also store evaluations and Buy Zone stints (for the screens)."""
    full = full if full is not None else set(run_ids)
    runs = [db.one("SELECT * FROM runs WHERE id=?", (rid,)) for rid in run_ids]
    run = runs[0]
    start = run["last_date"] or run["start_date"]
    pids = [ensure_portfolio(rid, profile) for rid in run_ids]
    ciks = candidate_ciks([profile.id])
    pb = PriceBook(price_tickers(ciks))
    days = [d for d in pb.trading_days(BENCH, start if run["last_date"] else _prev_day(start), end)]
    if not days:
        return 0
    snaps = SnapshotBook(profile)
    macro = Macro()
    tk = tickers_by_cik()
    sims = [PortfolioSim(rid, pid, profile, pb, tk) for rid, pid in zip(run_ids, pids)]
    for sim in sims:
        sim.macro = macro  # VIX for the market overlay
    st = profile.settings
    con = db.get()
    n = 0
    for i, d in enumerate(days):
        con.execute("BEGIN")
        try:
            for sim in sims:
                fin, fout = sim.fill_pending(d)
                sim.mark(d, fin, fout)
            signal_day = cadence == "daily" or _is_week_end(d, days, i)
            if signal_day:
                regime = macro.regime(d, st["mos_gate"], st.get("regime_modifier", False), lambda x: pb.adj("VTI", x))
                owned = set().union(*(set(s.positions) for s in sims))
                evals = evaluate(d, profile, snaps, pb, regime, tk, owned=owned, macro=macro)
                for sim in sims:
                    res = sim.decide(d, evals, True, regime.mos_gate)
                    if sim.run_id in full:
                        _store_evals_tx(sim.run_id, d, evals)
                        sim.stints(d, evals)
                    sim.persist(d, res)
            else:
                for sim in sims:
                    sim.save_state(d)
            for rid in run_ids:
                con.execute("UPDATE runs SET last_date=?, progress=? WHERE id=?", (d, (i + 1) / len(days), rid))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        n += 1
        if job and i % 10 == 0:
            job.update(lo + (hi - lo) * (i + 1) / len(days), message=f"{profile.name}: {d}")
    for rid in full:
        _fill_forward_returns(rid, pb, tk)
    return n


def _store_evals_tx(run_id, d, evals):
    keep = [e for e in evals if e.list in ("buy", "pricey", "near", "pending")]
    con = db.get()
    con.execute("DELETE FROM evaluations WHERE run_id=? AND date=?", (run_id, d))
    con.executemany(
        "INSERT OR REPLACE INTO evaluations(run_id,date,cik,list,quality,value,rank_score,rank,iv,mos,exp_return,"
        "buy_below,strong_buy_below,price,confidence,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, d, e.cik, e.list, e.quality, e.value, e.rank_score, e.rank, e.iv, e.mos, e.er, e.buy_below,
          e.strong_buy_below, e.price, e.confidence,
          json.dumps({"note": e.note, "failed": e.failed, "near": e.near, "filed": e.filed,
                      "expensive_above": e.expensive_above, "pe_now": e.pe_now, "pe_median": e.pe_median,
                      **(e.detail or {})}))
         for e in keep],
    )


def _fill_forward_returns(run_id, pb, tk):
    for s in db.rows("SELECT * FROM stints WHERE run_id=? AND fwd_1y IS NULL", (run_id,)):
        d1 = (date.fromisoformat(s["entered_on"]) + timedelta(days=365)).isoformat()
        t = tk.get(s["cik"])
        last = pb.last_date(t, d1) if t else None
        if last and last >= (date.fromisoformat(d1) - timedelta(days=7)).isoformat() and s["entry_px"]:
            px = pb.adj(t, d1)
            if px:
                db.get().execute("UPDATE stints SET fwd_1y=? WHERE id=?", (px / s["entry_px"] - 1, s["id"]))


def _prev_day(d: str) -> str:
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


def _is_week_end(d, days, i) -> bool:
    if i + 1 >= len(days):
        return True
    return date.fromisoformat(days[i + 1]).isocalendar()[1] != date.fromisoformat(d).isocalendar()[1]


def live_run(profile: rules.Profile) -> int:
    r = db.one("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (profile.id,))
    if r:
        return r["id"]
    cur = db.get().execute(
        "INSERT INTO runs(kind,name,profile_id,start_date,status,started_at) VALUES('live',?,?,?, 'active', ?)",
        (f"Live · {profile.name}", profile.id, backfill_start(), db.now()),
    )
    return cur.lastrowid


# ================================================================== first-time load
def initial_load(job, skip_download: bool = False) -> str:
    sec.reset_http()
    rules.seed_profiles()
    exchanges = db.cfg("exchanges", ["Nasdaq", "NYSE", "CBOE"])
    job.update(0.01, "Downloading the SEC company list")
    n = sec.load_ticker_map(exchanges)
    zp = sec.bulk_zip_path()
    fresh = zp.exists() and (datetime.now().timestamp() - zp.stat().st_mtime) < 3 * 86400
    if not skip_download and not fresh:
        sec.download_bulk(job.sub(0.02, 0.30, "Downloading SEC financial data (large file, one time)"))
    loaded = sec.ingest_bulk(zp, job.sub(0.30, 0.50, f"Reading financial statements for {n:,} companies"))
    job.update(0.50, "Market data (Buffett Indicator, Treasury yields)")
    fred.refresh_all()
    finish_setup(job, lo=0.51)
    db.set_cfg("initial_load_done", db.now())
    return f"Loaded {loaded:,} companies. Live history replayed from {backfill_start()}."


def finish_setup(job, lo=0.5) -> None:
    """Shared tail of the first load: score → route banks/insurers → rescore them → prices → price-based
    checks → quarterly warnings → replay history."""
    profs = rules.active_profiles()
    all_ciks = [r["cik"] for r in db.rows("SELECT DISTINCT cik FROM facts")]
    since = (date.fromisoformat(backfill_start()) - timedelta(days=400)).isoformat()
    for p in profs:
        build_snapshots(p, all_ciks, since=since, progress=job.sub(lo, lo + 0.10, f"Scoring every company ({p.name})"))
    cands = candidate_ciks([p.id for p in profs])
    suspects = financial_suspects()
    route_sic(sorted(set(cands) | set(suspects)), job.sub(lo + 0.10, lo + 0.15, "Checking industries (banks, insurers)"))
    fin = [r["cik"] for r in db.rows("SELECT cik FROM securities WHERE category IN ('bank','insurer','excluded')")]
    for p in profs:
        build_snapshots(p, fin, since=since, progress=job.sub(lo + 0.15, lo + 0.17, "Scoring banks and insurers"))
    cands = candidate_ciks([p.id for p in profs])
    detect_adr(cands, job.sub(lo + 0.17, lo + 0.19, "Reading ADR share ratios for foreign companies"))
    res = fetch_prices_for(price_tickers(cands), job.sub(lo + 0.19, lo + 0.32,
                                                        f"Downloading prices for {len(cands):,} shortlisted companies"))
    db.set_cfg("last_price_fetch", {"at": db.now(), "missing": res["missing"][:200]})
    pb = PriceBook(price_tickers(cands))
    for p in profs:
        build_snapshots(p, cands, since=since, prices=pb,
                        progress=job.sub(lo + 0.32, lo + 0.35, "Adding price-based checks ($1 test)"))
    quarterly.build(profs[0], cands, since=since, progress=job.sub(lo + 0.35, lo + 0.37, "Quarterly early warnings"))
    end = completed_session()
    for k, p in enumerate(profs):
        rid = live_run(p)
        span = (0.98 - (lo + 0.37)) / max(len(profs), 1)
        a = lo + 0.37 + k * span
        job.update(a, f"Replaying {backfill_start()} → {end} for {p.name}")
        replay(rid, p, end, "daily", job, a, a + span)
    scan_targets(profs)


# ================================================================== daily run
def daily_run(job, force: bool = False, send_email: bool = True, do_backup: bool = True) -> str:
    sec.reset_http()
    if not db.cfg("initial_load_done"):
        return "First-time load hasn't run yet; nothing to do."
    end = completed_session()
    profs = rules.active_profiles()
    lasts = [db.scalar("SELECT last_date FROM runs WHERE kind='live' AND profile_id=?", (p.id,)) for p in profs]
    if not force and lasts and all(ld and ld >= end for ld in lasts):
        msg = f"Already up to date through {end}."
        if do_backup:
            _safe_backup(job)
        return msg
    last_map = db.cfg("last_ticker_refresh")
    if not last_map or (date.today() - date.fromisoformat(last_map)).days >= 7:
        job.update(0.01, "Refreshing the SEC company list")
        before = {r["cik"] for r in db.rows("SELECT cik FROM securities")}
        try:
            db.get().execute("UPDATE securities SET active=0")
            sec.load_ticker_map(db.cfg("exchanges", ["Nasdaq", "NYSE", "CBOE"]))
            for cik in [r["cik"] for r in db.rows("SELECT cik FROM securities WHERE active=1")] :
                if cik not in before:
                    try:
                        sec.refresh_company(cik)
                    except Exception as e:
                        db.log("sec", "new_company", False, f"{cik}: {e}")
            db.set_cfg("last_ticker_refresh", date.today().isoformat())
        except Exception as e:
            db.get().execute("UPDATE securities SET active=1 WHERE cik IN (SELECT DISTINCT cik FROM facts)")
            db.log("sec", "ticker_map", False, str(e))
    job.update(0.02, "Checking the SEC for new annual reports")
    since = date.fromisoformat(db.cfg("last_filing_check", (date.today() - timedelta(days=3)).isoformat()))
    filings = sec.annual_filings_since(since)
    universe = {r["cik"] for r in db.rows("SELECT cik FROM securities WHERE active=1")}
    watch = set(candidate_ciks([p.id for p in profs]))
    annual = {cik for form, cik, _ in filings if form not in sec.QUARTER_FORMS and cik in universe}
    q_only = {cik for form, cik, _ in filings if form in sec.QUARTER_FORMS and cik in watch} - annual
    changed = sorted(annual | q_only)
    for i, cik in enumerate(changed):
        try:
            sec.refresh_company(cik)
        except Exception as e:
            db.log("sec", "refresh", False, f"{cik}: {e}")
        if i % 10 == 0:
            job.update(0.02 + 0.2 * i / max(len(changed), 1), message=f"{len(changed)} companies filed annual reports")
    db.set_cfg("last_filing_check", date.today().isoformat())
    if annual:
        route_sic(sorted(annual))
        for p in profs:
            build_snapshots(p, sorted(annual), latest_only=True)
    job.update(0.25, "Updating prices")
    cands = candidate_ciks([p.id for p in profs])
    res = fetch_prices_for(price_tickers(cands), job.sub(0.25, 0.55, "Updating prices"))
    db.set_cfg("last_price_fetch", {"at": db.now(), "missing": res["missing"][:200]})
    if annual:
        pb = PriceBook(price_tickers(sorted(annual)))
        for p in profs:
            build_snapshots(p, sorted(annual), latest_only=True, prices=pb)
    if changed:
        quarterly.build(profs[0], changed, latest_only=True)
    have_q = {r["cik"] for r in db.rows("SELECT DISTINCT cik FROM qsnaps")}
    new_cands = [c for c in cands if c not in have_q]
    if new_cands:  # companies that just joined the shortlist get their quarterly history
        quarterly.build(profs[0], new_cands, since=(date.today() - timedelta(days=400)).isoformat())
    detect_adr(cands)
    job.update(0.56, "Market data")
    fred.refresh_all()
    total_signals = 0
    for k, p in enumerate(profs):
        rid = live_run(p)
        before = db.scalar("SELECT COUNT(*) FROM signals WHERE run_id=?", (rid,), 0)
        replay(rid, p, end, "daily", job, 0.6 + 0.3 * k / len(profs), 0.6 + 0.3 * (k + 1) / len(profs))
        total_signals += db.scalar("SELECT COUNT(*) FROM signals WHERE run_id=?", (rid,), 0) - before
    job.update(0.90, "Checking price targets")
    n_alerts = scan_targets(profs)
    db.set_cfg("last_daily_run", {"at": db.now(), "through": end, "signals": total_signals,
                                  "filings": len(annual), "quarterlies": len(q_only), "price_alerts": n_alerts})
    job.update(0.92, "Sending alerts")
    if send_email:
        try:
            emailer.send_digest()
        except Exception as e:
            db.log("email", "digest", False, str(e))
        if date.fromisoformat(end).weekday() == 4 and profs:
            try:
                emailer.send_weekly(live_run(profs[0]))
            except Exception as e:
                db.log("email", "weekly", False, str(e))
    try:
        notify.toast("Oracle Edge", notify.summary_line())
    except Exception:
        pass
    if do_backup:
        _safe_backup(job)
    return (f"Up to date through {end}: {total_signals} new signal(s), {n_alerts} price alert(s), "
            f"{len(annual)} new annual report(s), {len(q_only)} new 10-Q(s) checked.")


def scan_targets(profs) -> int:
    """Price-target alerts for the default profile's live run. The very first scan only records where each
    stock stands (no flood of 'reached' emails for stocks that were already in the zone)."""
    if not profs:
        return 0
    first = not db.scalar("SELECT COUNT(*) FROM alert_state", default=0)
    try:
        ev = targets.scan(live_run(profs[0]), profs[0].settings)
    except Exception as e:
        db.log("targets", "scan", False, str(e))
        return 0
    if first:
        db.get().execute("UPDATE price_alerts SET emailed=1")
        return 0
    return len(ev)


def _safe_backup(job):
    job.update(0.96, "Backing up the database")
    try:
        backup.run_backup()
    except Exception as e:
        db.log("backup", "run", False, str(e))


def backup_job(job) -> str:
    path = backup.run_backup()
    return f"Backup saved: {path}"


# ================================================================== backtests & profile rebuilds
POLICY_LABEL = {"best_value": "new money to best value", "best_rank": "new money to best Rank",
                "spread": "new money spread evenly", "patient": "patient (waits for strong-buy prices)",
                "reserve": "sliding reserve (part now, rest for strong buys)",
                "spread+fund": "spread evenly + sell weaker to fund strong buys",
                "reserve+fund": "sliding reserve + sell weaker to fund strong buys"}


def policy_label(policy) -> str:
    if not policy:
        return ""
    if policy in POLICY_LABEL:
        return POLICY_LABEL[policy]
    base, _, extra = policy.partition("+")
    return POLICY_LABEL.get(base, base) + (" + sell weaker to fund strong buys" if extra == "fund" else "")


def _portfolio_label(mode, n, sizing, policy=None) -> str:
    if mode != "focused":
        return f"variable holdings{f' (limit {n})' if n else ''}"
    if policy:
        return f"best {n} · {policy_label(policy)}"
    return f"best {n}, {'conviction' if sizing == 'conviction' else 'equal'} weights"


def run_backtest(job, profile_id: int, start: str, end: str, cadence: str = "weekly", name: str | None = None,
                 max_holdings: int | None = None, sizing: str | None = None, mode: str | None = None) -> str:
    """Holdings / sizing / mode default to the profile's portfolio settings; pass them to override."""
    p = rules.get_profile(profile_id)
    st = p.settings
    mode = mode or st.get("portfolio_mode", "variable")
    n = int(max_holdings or st.get("max_holdings") or 15) if mode == "focused" else max_holdings
    sizing = sizing or st.get("sizing", "equal")
    ensure_snapshots(job, p, start)
    pol = st.get("new_money", "best_value") + ("+fund" if st.get("fund_strong") else "")
    label = _portfolio_label(mode, n, sizing, pol if mode == "focused" else None)
    cur = db.get().execute(
        "INSERT INTO runs(kind,name,profile_id,start_date,end_date,status,started_at,settings_json) "
        "VALUES('backtest',?,?,?,?, 'running', ?, ?)",
        (name or f"{p.name} · {label} · {start}→{end}", p.id, start, end, db.now(),
         json.dumps({"cadence": cadence, "profile_version": p.version, "mode": mode, "max_holdings": n,
                     "sizing": sizing, "policy": pol, "overrides": bool(max_holdings or mode)})),
    )
    rid = cur.lastrowid
    ensure_portfolio(rid, p, mode=mode, max_holdings=n, sizing=sizing)
    replay(rid, p, end, cadence, job, 0.3, 0.99)
    db.get().execute("UPDATE runs SET status='done', finished_at=? WHERE id=?", (db.now(), rid))
    n_sig = db.scalar("SELECT COUNT(*) FROM signals WHERE run_id=?", (rid,), 0)
    return f"Backtest #{rid} finished ({label}): {n_sig} signals."


def recalc_backtest(job, run_id: int) -> str:
    """Re-run a backtest with its profile's CURRENT rules and portfolio settings (holdings, sizing, add limits),
    over the same dates, and replace the old results."""
    r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not r:
        return "That backtest no longer exists."
    s = json.loads(r["settings_json"] or "{}")
    msg = run_backtest(job, r["profile_id"], r["start_date"], r["end_date"], s.get("cadence", "weekly"))
    clear_run(run_id)
    return "Recalculated. " + msg


def recalc_all(job) -> str:
    """Recalculate every backtest, one after another, with each profile's current settings."""
    ids = [r["id"] for r in db.rows("SELECT id FROM runs WHERE kind='backtest' ORDER BY id")]
    for k, rid in enumerate(ids):
        job.update(k / max(len(ids), 1), f"Recalculating backtest {k + 1} of {len(ids)}")
        sub = job.sub(k / len(ids), (k + 1) / len(ids), f"Backtest {k + 1} of {len(ids)}")

        class _J:  # route the inner job's progress into this slice of the bar
            def update(self, progress=None, step=None, message=None):
                if progress is not None:
                    sub(progress)

            def sub(self, lo, hi, step):
                return lambda f: sub(lo + (hi - lo) * f)
        recalc_backtest(_J(), rid)
    return f"Recalculated {len(ids)} backtest(s)."


# ================================================================== holdings test (portfolio-size sweep)
SWEEP_SIZES = [5, 6, 8, 10, 12]
SWEEP_POLICIES = ["best_value", "best_rank", "spread", "patient", "reserve", "spread+fund", "reserve+fund"]


def run_sweep(job, profile_id: int, start: str, end: str, cadence: str = "weekly", sizes: list[int] | None = None,
              policies: list[str] | None = None, sizings: list[str] | None = None) -> str:
    """Find the best number of holdings and the best place for new money: replay the profile once and run a
    focused portfolio for every size x new-money policy (plus the old variable style for reference), using the
    profile's starting money and weekly amount, then score each robustly."""
    p = rules.get_profile(profile_id)
    sizes = sorted({int(x) for x in (sizes or SWEEP_SIZES) if int(x) > 0})
    policies = policies or SWEEP_POLICIES
    sz0 = (sizings or [p.settings.get("sizing", "equal")])[0]
    ensure_snapshots(job, p, start)
    con = db.get()
    sw = con.execute("INSERT INTO sweeps(profile_id,start_date,end_date,cadence,status,created_at) "
                     "VALUES(?,?,?,?, 'running', ?)", (p.id, start, end, cadence, db.now())).lastrowid
    variants = [("focused", n, sz0, pol) for pol in policies for n in sizes] + [("variable", None, "equal", None)]
    rids = []
    for mode, n, sz, pol in variants:
        rid = con.execute(
            "INSERT INTO runs(kind,name,profile_id,start_date,end_date,status,started_at,settings_json) "
            "VALUES('sweep',?,?,?,?, 'running', ?, ?)",
            (f"Holdings test #{sw}: {_portfolio_label(mode, n, sz, pol)}", p.id, start, end, db.now(),
             json.dumps({"cadence": cadence, "mode": mode, "max_holdings": n, "sizing": sz, "policy": pol,
                         "sweep": sw}))).lastrowid
        ensure_portfolio(rid, p, mode=mode, max_holdings=n, sizing=sz, policy=pol)
        rids.append(rid)
    job.update(0.3, f"Replaying {start} → {end} for {len(variants)} portfolio sizes at once")
    replay_many(rids, p, end, cadence, job, 0.3, 0.97, full=set())
    results = []
    for (mode, n, sz, pol), rid in zip(variants, rids):
        m = sweep_metrics(rid)
        m.update({"run_id": rid, "mode": mode, "n": n, "sizing": sz, "policy": pol,
                  "label": _portfolio_label(mode, n, sz, pol)})
        results.append(m)
    rec = recommend(results)
    con.execute("UPDATE runs SET status='done', finished_at=? WHERE id IN (%s)" % ",".join("?" * len(rids)),
                (db.now(), *rids))
    con.execute("UPDATE sweeps SET status='done', results_json=?, recommended_json=?, run_ids=? WHERE id=?",
                (json.dumps(results), json.dumps(rec), json.dumps(rids), sw))
    if not rec:
        return "Holdings test finished, but there wasn't enough history to recommend a size."
    return (f"Holdings test finished: best is {rec['label']} (CAGR {rec['cagr']:.1%} vs S&P {rec['bench_cagr']:.1%}).")


# ================================================================== experiments (risk-optimized configurations)
NO_LIMITS = {"max_sector": None, "max_correlated": None, "market_overlay": False}
EXPERIMENTS = [
    ("Live", "Your live setup: 7 holdings, equal slices, sliding reserve, no new limits",
     {"sizing": "equal", "max_position": None, **NO_LIMITS, "max_holdings": 7}),
    ("R", "Risk Optimized defaults: 7 holdings, value-and-quality sizing, 35% / 50% / 60% limits", {}),
    ("A", "7 holdings, 20% cap", {"max_holdings": 7, "max_position": 0.20, **NO_LIMITS}),
    ("B", "8 holdings, 20% cap", {"max_holdings": 8, "max_position": 0.20, **NO_LIMITS}),
    ("C", "9 holdings, 20% cap", {"max_holdings": 9, "max_position": 0.20, **NO_LIMITS}),
    ("D", "7 holdings, 20% cap + sector (50%) and correlation (60%) limits",
     {"max_holdings": 7, "max_position": 0.20, "max_sector": 0.50, "max_correlated": 0.60, "market_overlay": False}),
    ("E", "7 holdings, 20% cap + market overlay", {"max_holdings": 7, "max_position": 0.20, **NO_LIMITS,
                                                   "market_overlay": True}),
    ("F", "8 holdings, 20% cap + market overlay", {"max_holdings": 8, "max_position": 0.20, **NO_LIMITS,
                                                   "market_overlay": True}),
]
STAGE2 = [
    ("G", "Best of A–F + enhanced valuation", {"valuation_model": "enhanced"}),
    ("G+", "Best of A–F + enhanced valuation + bigger margin of safety for less predictable earnings",
     {"valuation_model": "enhanced", "predictability_mos": True}),
]


def _variant_profile(p: rules.Profile, extra: dict) -> rules.Profile:
    """The same profile (same company scores) with some settings changed, for one experiment's replay."""
    import copy

    body = copy.deepcopy(p.body)
    body["settings"].update(extra)
    q = copy.copy(p)
    q.body = body
    return q


def better_than(a: dict, b: dict) -> bool:
    """The stated goal: higher CAGR with equal or lower drawdown."""
    return (a.get("cagr") or -9) > (b.get("cagr") or -9) and abs(a.get("max_dd") or -1) <= abs(b.get("max_dd") or -1) + 1e-9


def run_experiments(job, profile_id: int | None, start: str, end: str, cadence: str = "weekly") -> str:
    """Experiments A-G on the Risk Optimized profile. Stage 1 replays once for the live setup, the profile's
    defaults and A-F (portfolio settings only); stage 2 replays the most robust of A-F with the enhanced valuation
    (G) and with the predictability margin of safety as well (G+). Ranked by robustness score."""
    pid = profile_id or db.scalar("SELECT id FROM profiles WHERE name=?", (rules.RISK_NAME,))
    if not pid:
        rules.seed_profiles()
        pid = db.scalar("SELECT id FROM profiles WHERE name=?", (rules.RISK_NAME,))
    p = rules.get_profile(pid)
    try:
        if not db.scalar("SELECT COUNT(*) FROM macro WHERE series='VIXCLS'", default=0):
            job.update(0.01, "Downloading the VIX history (market overlay)")
            df = fred.fetch_series("VIXCLS")
            with db.tx() as con_:
                con_.executemany("INSERT OR REPLACE INTO macro(series,date,value) VALUES(?,?,?)",
                                 [("VIXCLS", d_, float(v)) for d_, v in zip(df["date"], df["value"])])
    except Exception as e:  # the overlay then only uses SPY vs its 200-day average
        db.log("fred", "VIXCLS", False, str(e))
    ensure_snapshots(job, p, start)
    con = db.get()
    sw = con.execute("INSERT INTO sweeps(profile_id,start_date,end_date,cadence,status,created_at,kind) "
                     "VALUES(?,?,?,?, 'running', ?, 'experiments')", (p.id, start, end, cadence, db.now())).lastrowid

    def make_run(code, desc, ov, prof):
        rid = con.execute(
            "INSERT INTO runs(kind,name,profile_id,start_date,end_date,status,started_at,settings_json) "
            "VALUES('sweep',?,?,?,?, 'running', ?, ?)",
            (f"Experiment #{sw} {code}: {desc}", p.id, start, end, db.now(),
             json.dumps({"cadence": cadence, "experiment": code, "overrides": ov, "sweep": sw}))).lastrowid
        ensure_portfolio(rid, prof, mode="focused", overrides=ov)
        return rid

    results, rids = [], []
    stage1 = [(code, desc, ov, make_run(code, desc, ov, p)) for code, desc, ov in EXPERIMENTS]
    rids += [r for *_, r in stage1]
    job.update(0.25, f"Experiments: replaying {start} → {end} for {len(stage1)} configurations")
    replay_many([r for *_, r in stage1], p, end, cadence, job, 0.25, 0.65, full=set())
    for code, desc, ov, rid in stage1:
        m = sweep_metrics(rid)
        m.update({"run_id": rid, "code": code, "label": f"{code} · {desc}", "overrides": ov})
        results.append(m)
    pool = [r for r in results if r["code"] in "ABCDEF" and r.get("robustness")]
    best = max(pool, key=lambda r: r["robustness"]["score"]) if pool else None
    if best:
        for k, (code, desc, extra) in enumerate(STAGE2):
            prof = _variant_profile(p, extra)
            ov = {**best["overrides"], **extra}
            rid = make_run(code, f"{desc} ({best['code']})", ov, prof)
            rids.append(rid)
            a = 0.65 + k * 0.16
            job.update(a, f"Experiments: {code} ({best['code']} + enhanced valuation)")
            replay_many([rid], prof, end, cadence, job, a, a + 0.16, full=set())
            m = sweep_metrics(rid)
            m.update({"run_id": rid, "code": code, "label": f"{code} · {desc} ({best['code']})", "overrides": ov})
            results.append(m)
    live = next((r for r in results if r["code"] == "Live"), None)
    for r in results:
        r["beats_live"] = bool(live and r is not live and r.get("cagr") is not None and better_than(r, live))
    scored = [r for r in results if r.get("robustness")]
    rec = max(scored, key=lambda r: r["robustness"]["score"]) if scored else None
    note = ""
    sample = db.rows("SELECT val_inputs FROM snapshots WHERE profile_id=? AND profile_version=? "
                     "ORDER BY filed DESC LIMIT 50", p.snap)
    if sample and not any("incr_roic" in (r["val_inputs"] or "") for r in sample):
        note = ("The company scores were made before the enhanced inputs existed, so G and G+ fell back to the "
                "classic inputs. Run Data → 're-run first-time load' once, then run the experiments again.")
    con.execute("UPDATE runs SET status='done', finished_at=? WHERE id IN (%s)" % ",".join("?" * len(rids)),
                (db.now(), *rids))
    con.execute("UPDATE sweeps SET status='done', results_json=?, recommended_json=?, run_ids=? WHERE id=?",
                (json.dumps(results), json.dumps({"code": rec["code"], "label": rec["label"], "note": note}
                                                 if rec else {"note": note}), json.dumps(rids), sw))
    return (f"Experiments finished: most robust is {rec['label']} (score {rec['robustness']['score']:.0f})."
            if rec else "Experiments finished, but there wasn't enough history to score them.")


def sweep_metrics(run_id: int) -> dict:
    curve = db.rows("SELECT date, nav, bench_nav, holdings, cash, value, bench_value, contributed FROM equity_curve "
                    "WHERE run_id=? ORDER BY date", (run_id,))
    if len(curve) < 60:
        return {}
    import math

    def cagr(a, b, d0, d1):
        yrs = max((date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25, 1e-6)
        return (b / a) ** (1 / yrs) - 1 if a and b else None

    nav = [c["nav"] for c in curve]
    bench = [c["bench_nav"] for c in curve]
    i0 = next((i for i, b in enumerate(bench) if b), 0)
    out = {"cagr": cagr(nav[0], nav[-1], curve[0]["date"], curve[-1]["date"]),
           "bench_cagr": cagr(bench[i0], bench[-1], curve[i0]["date"], curve[-1]["date"])}
    peak, mdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    out["max_dd"] = mdd
    bpk, bdd = None, 0.0
    for v in bench:
        if v:
            bpk = max(bpk or v, v)
            bdd = min(bdd, v / bpk - 1)
    out["bench_max_dd"] = bdd
    from .ui.data import worst_12m

    dts = [c["date"] for c in curve]
    out["worst_12m"] = worst_12m(dts, nav)
    out["bench_worst_12m"] = worst_12m(dts, bench)
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav)) if nav[i - 1]]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(len(rets) - 1, 1))
    out["sharpe"] = (mu * 252) / (sd * math.sqrt(252)) if sd else None
    out["avg_holdings"] = sum(c["holdings"] or 0 for c in curve) / len(curve)
    cash = [c["cash"] for c in curve if c["cash"] is not None]
    out["avg_cash"] = sum(cash) / len(cash) if cash else None
    # excess return in each third of the period: the size has to work in all of them, not one lucky stretch
    thirds, period_cagr = [], []
    k = len(curve) // 3
    for a, b in ((0, k), (k, 2 * k), (2 * k, len(curve) - 1)):
        m_ = cagr(nav[a], nav[b], curve[a]["date"], curve[b]["date"])
        b_ = cagr(bench[a], bench[b], curve[a]["date"], curve[b]["date"]) if bench[a] and bench[b] else None
        thirds.append((m_ - b_) if (m_ is not None and b_ is not None) else None)
        period_cagr.append(m_)
    out["thirds_excess"] = thirds
    out["period_cagr"] = period_cagr
    out["period_dates"] = [curve[0]["date"], curve[k]["date"], curve[2 * k]["date"], curve[-1]["date"]]
    out["calmar"] = (out["cagr"] / abs(mdd)) if (out["cagr"] is not None and mdd < 0) else None
    last = curve[-1]
    out["end_value"], out["bench_end_value"], out["contributed"] = last["value"], last["bench_value"], last["contributed"]
    out["trades"] = db.scalar("SELECT COUNT(*) FROM signals WHERE run_id=? AND type IN ('BUY','STRONG_BUY','SELL',"
                              "'SWAP','TRIM','ADD')", (run_id,), 0)
    pf = db.one("SELECT * FROM portfolios WHERE run_id=? AND kind='model'", (run_id,))
    vals = [c["value"] for c in curve if c["value"]]
    yrs = max((date.fromisoformat(curve[-1]["date"]) - date.fromisoformat(curve[0]["date"])).days / 365.25, 1e-6)
    if pf is not None and "traded" in pf.keys() and vals:
        avg_v = sum(vals) / len(vals)
        out["turnover"] = (pf["traded"] or 0.0) / avg_v / yrs  # $ traded per year / average portfolio value
        out["costs"] = pf["costs"] or 0.0
        out["cost_drag"] = (pf["costs"] or 0.0) / avg_v / yrs
    out["robustness"] = robustness(out)
    return out


ROBUSTNESS_WEIGHTS = {"oos_cagr": 0.30, "max_dd": 0.25, "worst_12m": 0.20, "consistency": 0.15, "turnover": 0.10}


def robustness(m: dict) -> dict | None:
    """0-100 score that rewards configurations that work in every period, not one lucky stretch.
    Each part is scored on a fixed scale (so scores compare across tests), then weighted:
      30% median CAGR across the three independent periods (30%/yr = 100, 0% = 0)
      25% maximum drawdown (0% = 100, -50% = 0)
      20% worst rolling 12 months (0% or better = 100, -40% = 0)
      15% consistency: share of periods beating the S&P 500 (60%) and how even the period CAGRs are (40%)
      10% turnover and trading costs (no trading = 100, 200%/yr turnover = 0)"""
    import statistics

    pc = [x for x in (m.get("period_cagr") or []) if x is not None]
    if not pc or m.get("max_dd") is None:
        return None

    def clamp(x):
        return max(0.0, min(100.0, x))

    ex = [x for x in (m.get("thirds_excess") or []) if x is not None]
    parts = {
        "oos_cagr": clamp(statistics.median(pc) / 0.30 * 100),
        "max_dd": clamp((1 - abs(m["max_dd"]) / 0.50) * 100),
        "worst_12m": clamp((1 + min(m.get("worst_12m") or 0.0, 0.0) / 0.40) * 100),
        "consistency": clamp(100 * (0.6 * (sum(1 for x in ex if x > 0) / len(ex) if ex else 0.0)
                                    + 0.4 * (1 - min((statistics.pstdev(pc) if len(pc) > 1 else 0.0) / 0.20, 1.0)))),
        "turnover": clamp((1 - (m.get("turnover") or 0.0) / 2.0) * 100),
    }
    score = sum(parts[k] * w for k, w in ROBUSTNESS_WEIGHTS.items())
    return {"score": round(score, 1), "parts": {k: round(v, 1) for k, v in parts.items()}}


def recommend(results: list[dict]) -> dict | None:
    """Pick the size that beat the S&P most reliably: the middle (median) of its three sub-period excess returns,
    averaged with its neighbours (so one lucky size can't win on noise), minus a penalty for drawdowns deeper
    than the S&P 500's own worst drop. Ties go to fewer holdings."""
    import statistics

    foc = [r for r in results if r.get("mode") == "focused" and r.get("cagr") is not None
           and all(x is not None for x in r.get("thirds_excess", [None]))]
    if not foc:
        return None
    for r in foc:
        r["robust"] = statistics.median(r["thirds_excess"])
    by_sizing: dict[str, list[dict]] = {}
    for r in foc:
        by_sizing.setdefault(r.get("policy") or r["sizing"], []).append(r)
    for rs in by_sizing.values():
        rs.sort(key=lambda r: r["n"])
        for i, r in enumerate(rs):
            nb = [rs[j]["robust"] for j in (i - 1, i + 1) if 0 <= j < len(rs)]
            r["smoothed"] = 0.5 * r["robust"] + 0.5 * (sum(nb) / len(nb) if nb else r["robust"])
    bench_dd = next((r["bench_max_dd"] for r in results if r.get("bench_max_dd") is not None), None)
    for r in foc:
        extra_dd = max(0.0, (bench_dd - r["max_dd"])) if bench_dd is not None else 0.0  # deeper than the S&P's
        r["score"] = r["smoothed"] - 0.25 * extra_dd
    best = max(foc, key=lambda r: (round(r["score"], 4), -r["n"]))
    return {k: best.get(k) for k in ("run_id", "n", "sizing", "policy", "label", "cagr", "bench_cagr", "max_dd",
                                     "score", "robust", "avg_holdings", "avg_cash", "end_value", "bench_end_value",
                                     "contributed")}


def apply_sweep(sweep_id: int) -> tuple[int, str]:
    """Save the recommended size into the profile (a copy if the profile is locked). Returns (profile_id, text)."""
    sw = db.one("SELECT * FROM sweeps WHERE id=?", (sweep_id,))
    rec = json.loads(sw["recommended_json"] or "null")
    if not rec:
        raise ValueError("This holdings test has no recommendation.")
    return apply_holdings(sw["profile_id"], rec["n"], rec["sizing"], rec.get("policy"))


def apply_holdings(profile_id: int, n: int, sizing: str, policy: str | None = None) -> tuple[int, str]:
    import copy

    p = rules.get_profile(profile_id)
    body = copy.deepcopy(p.body)
    body["settings"].update({"portfolio_mode": "focused", "max_holdings": int(n), "sizing": sizing})
    if policy:
        base, _, extra = policy.partition("+")
        body["settings"]["new_money"] = base
        body["settings"]["fund_strong"] = extra == "fund"
    if p.locked:
        name = f"{p.name} · best {n}{' conviction' if sizing == 'conviction' else ''}"
        existing = db.one("SELECT id FROM profiles WHERE name=?", (name,))
        if existing:
            rules.update_profile(existing["id"], body)
            return existing["id"], f"Updated '{name}'."
        pid = rules.save_as(p.id, name, body)
        return pid, f"'{p.name}' is locked, so the setting was saved as a new profile, '{name}'."
    rules.update_profile(p.id, body)
    return p.id, f"Saved to '{p.name}'."


def ensure_snapshots(job, p: rules.Profile, start: str) -> None:
    have = db.scalar("SELECT COUNT(*) FROM snapshots WHERE profile_id=? AND profile_version=?", p.snap, 0)
    if have:
        return
    since = (date.fromisoformat(start) - timedelta(days=400)).isoformat()
    all_ciks = [r["cik"] for r in db.rows("SELECT DISTINCT cik FROM facts")]
    build_snapshots(p, all_ciks, since=since, progress=job.sub(0.0, 0.2, f"Scoring every company ({p.name})"))
    cands = candidate_ciks([p.id])
    detect_adr(cands)
    fetch_prices_for(price_tickers(cands), job.sub(0.2, 0.27, "Prices for new candidates"))
    pb = PriceBook(price_tickers(cands))
    build_snapshots(p, cands, since=since, prices=pb, progress=job.sub(0.27, 0.29, "Price-based checks"))
    have_q = {r["cik"] for r in db.rows("SELECT DISTINCT cik FROM qsnaps")}
    missing_q = [c for c in cands if c not in have_q]
    if missing_q:
        quarterly.build(p, missing_q, since=since, progress=job.sub(0.29, 0.3, "Quarterly early warnings"))


def rebuild_profile(job, profile_id: int) -> str:
    """After editing a profile: rescore and replay its live history."""
    p = rules.get_profile(profile_id)
    ensure_snapshots(job, p, backfill_start())
    r = db.one("SELECT id FROM runs WHERE kind='live' AND profile_id=?", (p.id,))
    if r:
        clear_run(r["id"])
    rid = live_run(p)
    replay(rid, p, completed_session(), "daily", job, 0.3, 0.99)
    return f"{p.name} rebuilt."


def clear_sweep(sweep_id: int) -> None:
    sw = db.one("SELECT run_ids FROM sweeps WHERE id=?", (sweep_id,))
    for rid in json.loads((sw["run_ids"] if sw else None) or "[]"):
        clear_run(rid)
    db.get().execute("DELETE FROM sweeps WHERE id=?", (sweep_id,))


def clear_run(run_id: int) -> None:
    with db.tx() as con:
        for t in ("evaluations", "badges", "signals", "episodes", "stints", "equity_curve", "signal_state"):
            con.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
        pids = [r["id"] for r in con.execute("SELECT id FROM portfolios WHERE run_id=?", (run_id,)).fetchall()]
        for pid in pids:
            con.execute("DELETE FROM positions WHERE portfolio_id=?", (pid,))
        con.execute("DELETE FROM portfolios WHERE run_id=?", (run_id,))
        con.execute("DELETE FROM runs WHERE id=?", (run_id,))

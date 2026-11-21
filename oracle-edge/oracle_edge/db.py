"""SQLite access: connection, schema migrations and small helpers.

Plain sqlite3 (no ORM) keeps the dependency list short and the SQL readable.
Migrations are an ordered list; each runs once and bumps `meta.schema_version`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

from . import paths

_local = threading.local()

MIGRATIONS: list[str] = [
    # ---- v1: initial schema -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT);

    CREATE TABLE IF NOT EXISTS securities (
        cik INTEGER PRIMARY KEY, ticker TEXT, name TEXT, exchange TEXT,
        sic INTEGER, sic_desc TEXT, category TEXT DEFAULT 'standard',
        is_foreign INTEGER DEFAULT 0, currency TEXT DEFAULT 'USD',
        active INTEGER DEFAULT 1, facts_loaded_at TEXT, last_filed TEXT, updated_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_sec_ticker ON securities(ticker);

    -- Normalized annual line items, one row per (company, item, fiscal year end, filing).
    -- Keeping every filing's value makes point-in-time reconstruction possible.
    CREATE TABLE IF NOT EXISTS facts (
        cik INTEGER, item TEXT, fy_end TEXT, value REAL, filed TEXT, form TEXT, unit TEXT,
        PRIMARY KEY (cik, item, fy_end, filed));
    CREATE INDEX IF NOT EXISTS ix_facts_cik_filed ON facts(cik, filed);

    CREATE TABLE IF NOT EXISTS shares_out (
        cik INTEGER, as_of TEXT, value REAL, filed TEXT, PRIMARY KEY (cik, as_of, filed));

    CREATE TABLE IF NOT EXISTS prices (
        ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, adj_close REAL,
        volume REAL, source TEXT, PRIMARY KEY (ticker, date)) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS macro (series TEXT, date TEXT, value REAL, PRIMARY KEY (series, date));

    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, parent_id INTEGER, version INTEGER DEFAULT 1,
        locked INTEGER DEFAULT 0, active INTEGER DEFAULT 0, body_json TEXT, notes TEXT,
        created_at TEXT, updated_at TEXT);

    -- Fundamental snapshot per company per filing date (quality side; price-independent).
    CREATE TABLE IF NOT EXISTS snapshots (
        cik INTEGER, profile_id INTEGER, profile_version INTEGER, filed TEXT, fy_end TEXT,
        quality REAL, gates_passed INTEGER, failed_gates TEXT, near_miss TEXT,
        category_scores TEXT, rules_json TEXT, val_inputs TEXT, confidence REAL, routed TEXT,
        PRIMARY KEY (cik, profile_id, profile_version, filed));

    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, profile_id INTEGER,
        start_date TEXT, end_date TEXT, status TEXT, progress REAL DEFAULT 0, message TEXT,
        started_at TEXT, finished_at TEXT, settings_json TEXT, last_date TEXT);

    CREATE TABLE IF NOT EXISTS evaluations (
        run_id INTEGER, date TEXT, cik INTEGER, list TEXT, quality REAL, value REAL, rank_score REAL,
        rank INTEGER, iv REAL, mos REAL, exp_return REAL, buy_below REAL, strong_buy_below REAL,
        price REAL, confidence REAL, detail TEXT, PRIMARY KEY (run_id, date, cik)) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS portfolios (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, name TEXT, kind TEXT,
        max_positions INTEGER, position_cap REAL DEFAULT 0.40, trim_to REAL DEFAULT 0.30,
        sizing TEXT DEFAULT 'equal', cash REAL, last_value REAL DEFAULT 0, last_nav REAL DEFAULT 1.0,
        last_bench REAL, last_date TEXT);

    CREATE TABLE IF NOT EXISTS positions (
        portfolio_id INTEGER, cik INTEGER, units REAL, avg_cost REAL, opened_on TEXT,
        quality_at_buy REAL, PRIMARY KEY (portfolio_id, cik));

    CREATE TABLE IF NOT EXISTS badges (
        run_id INTEGER, portfolio_id INTEGER, date TEXT, cik INTEGER, badge TEXT, reason TEXT,
        levels_json TEXT, PRIMARY KEY (run_id, portfolio_id, date, cik)) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, portfolio_id INTEGER, cik INTEGER,
        date TEXT, type TEXT, reason TEXT, reason_json TEXT, price REAL, units REAL,
        acted_on INTEGER DEFAULT 0, fill_price REAL, emailed INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ix_signals_run_date ON signals(run_id, date);

    CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, portfolio_id INTEGER, cik INTEGER,
        entry_date TEXT, entry_px REAL, exit_date TEXT, exit_px REAL, return_pct REAL,
        bench_return_pct REAL, excess_pct REAL, max_dd_pct REAL, max_gain_pct REAL, days_held INTEGER,
        entry_rank INTEGER, exit_reason TEXT, status TEXT, invested REAL DEFAULT 0, proceeds REAL DEFAULT 0);

    CREATE TABLE IF NOT EXISTS stints (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, cik INTEGER, entered_on TEXT, left_on TEXT,
        entry_px REAL, exit_px REAL, return_pct REAL, bench_return_pct REAL, fwd_1y REAL,
        entry_rank INTEGER, status TEXT);

    CREATE TABLE IF NOT EXISTS equity_curve (
        run_id INTEGER, portfolio_id INTEGER, date TEXT, nav REAL, bench_nav REAL, holdings INTEGER,
        PRIMARY KEY (run_id, portfolio_id, date)) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS signal_state (
        run_id INTEGER, portfolio_id INTEGER, cik INTEGER, buy_streak INTEGER DEFAULT 0,
        nose_streak INTEGER DEFAULT 0, review INTEGER DEFAULT 0, review_filed TEXT, last_add TEXT,
        PRIMARY KEY (run_id, portfolio_id, cik));

    CREATE TABLE IF NOT EXISTS user_notes (
        cik INTEGER PRIMARY KEY, outside_circle INTEGER DEFAULT 0, understand INTEGER DEFAULT 0,
        trust_mgmt INTEGER DEFAULT 0, moat_20y INTEGER DEFAULT 0, note TEXT, updated_at TEXT);

    CREATE TABLE IF NOT EXISTS real_holdings (cik INTEGER PRIMARY KEY, shares REAL, avg_cost REAL, added_on TEXT);

    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, args TEXT, status TEXT, progress REAL DEFAULT 0,
        step TEXT, message TEXT, pid INTEGER, heartbeat TEXT, started_at TEXT, finished_at TEXT, error TEXT);

    CREATE TABLE IF NOT EXISTS data_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, kind TEXT, ok INTEGER, detail TEXT);
    """,
    # ---- v2: financial rulebooks, foreign valuation, quarterly early warnings ----
    """
    ALTER TABLE securities ADD COLUMN adr_ratio REAL;
    ALTER TABLE securities ADD COLUMN adr_source TEXT;
    ALTER TABLE securities ADD COLUMN adr_checked_at TEXT;
    ALTER TABLE snapshots ADD COLUMN gate_margin REAL;
    ALTER TABLE snapshots ADD COLUMN gate_margin_rule TEXT;

    -- 10-Q year-to-date and quarter-end values (point-in-time by filing date)
    CREATE TABLE IF NOT EXISTS qfacts (
        cik INTEGER, item TEXT, start TEXT, end TEXT, value REAL, filed TEXT, form TEXT,
        PRIMARY KEY (cik, item, start, end, filed)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ix_qfacts_cik_filed ON qfacts(cik, filed);

    -- trailing-12-month checks after each 10-Q
    CREATE TABLE IF NOT EXISTS qsnaps (
        cik INTEGER, filed TEXT, period_end TEXT, flags_json TEXT, metrics_json TEXT,
        PRIMARY KEY (cik, filed));

    ALTER TABLE signal_state ADD COLUMN review_reason TEXT;
    """,
    # ---- v3: price targets and price alerts ----
    """
    -- one row per (company, target kind): idle -> approaching -> reached, re-armed after price moves away
    CREATE TABLE IF NOT EXISTS alert_state (
        cik INTEGER, kind TEXT, state TEXT, target REAL, updated TEXT, PRIMARY KEY (cik, kind));

    -- alert events (shown on Today and the stock page, emailed once)
    CREATE TABLE IF NOT EXISTS price_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cik INTEGER, kind TEXT, date TEXT, state TEXT, target REAL,
        price REAL, distance REAL, message TEXT, emailed INTEGER DEFAULT 0, created_at TEXT);
    CREATE INDEX IF NOT EXISTS ix_price_alerts_date ON price_alerts(date);

    -- your own price alerts
    CREATE TABLE IF NOT EXISTS custom_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cik INTEGER, price REAL, direction TEXT, note TEXT,
        active INTEGER DEFAULT 1, created_at TEXT);
    """,
    # ---- v4: focused portfolios (fixed number of holdings, cash), add limits, holdings tests ----
    """
    ALTER TABLE portfolios ADD COLUMN mode TEXT;            -- NULL/'variable' (legacy) | 'focused'
    ALTER TABLE portfolios ADD COLUMN contribution REAL DEFAULT 0;
    ALTER TABLE signal_state ADD COLUMN adds INTEGER DEFAULT 0;
    ALTER TABLE equity_curve ADD COLUMN cash REAL;
    CREATE TABLE IF NOT EXISTS sweeps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER, start_date TEXT, end_date TEXT, cadence TEXT,
        status TEXT, created_at TEXT, results_json TEXT, recommended_json TEXT, run_ids TEXT);
    """,
    # ---- v5: real dollars, weekly new money and where it goes ----
    """
    ALTER TABLE portfolios ADD COLUMN policy TEXT;           -- best_value | best_rank | spread | patient
    ALTER TABLE portfolios ADD COLUMN weekly REAL DEFAULT 0;  -- new money added each week ($)
    ALTER TABLE portfolios ADD COLUMN contributed REAL DEFAULT 0;
    ALTER TABLE portfolios ADD COLUMN bench_units REAL;       -- S&P 500 bought with the same money on the same days
    ALTER TABLE portfolios ADD COLUMN last_deploy TEXT;
    ALTER TABLE equity_curve ADD COLUMN value REAL;
    ALTER TABLE equity_curve ADD COLUMN bench_value REAL;
    ALTER TABLE equity_curve ADD COLUMN contributed REAL;
    """,
    # ---- v6: fast lookups of one profile's company scores (profile editor preview, backtests) ----
    """
    CREATE INDEX IF NOT EXISTS ix_snapshots_profile ON snapshots(profile_id, profile_version, cik, filed);
    """,
    # ---- v7: holdings tests fill these tables; keep stock pages and Performance fast ----
    """
    CREATE INDEX IF NOT EXISTS ix_signals_cik_date ON signals(cik, date);
    CREATE INDEX IF NOT EXISTS ix_episodes_run ON episodes(run_id, cik);
    CREATE INDEX IF NOT EXISTS ix_stints_run ON stints(run_id, status);
    """,
    # ---- v8: the "sliding reserve" new-money rule keeps its own cash balance ----
    """
    ALTER TABLE portfolios ADD COLUMN reserve REAL DEFAULT 0;
    """,
    # ---- v9: experiments (per-portfolio setting overrides), turnover and trading costs ----
    """
    ALTER TABLE portfolios ADD COLUMN settings_json TEXT;
    ALTER TABLE portfolios ADD COLUMN traded REAL DEFAULT 0;
    ALTER TABLE portfolios ADD COLUMN costs REAL DEFAULT 0;
    ALTER TABLE equity_curve ADD COLUMN traded REAL;
    ALTER TABLE sweeps ADD COLUMN kind TEXT DEFAULT 'holdings';
    """,
    # ---- v10: Bitcoin early-warning call log and market alerts ----
    """
    CREATE TABLE IF NOT EXISTS btc_calls (
        date TEXT PRIMARY KEY, lookback INTEGER, horizon INTEGER, btc_ret REAL, bear_cut REAL, bull_cut REAL,
        zone TEXT, qqq_px REAL, qqq_fwd REAL, resolved_on TEXT, logged_at TEXT);
    CREATE TABLE IF NOT EXISTS market_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, kind TEXT, state TEXT, message TEXT,
        emailed INTEGER DEFAULT 0, created_at TEXT);
    """,
    # ---- v11: Buy Zone entries: S&P over the same year, re-entries merged, run-start entries flagged ----
    """
    ALTER TABLE stints ADD COLUMN bench_fwd_1y REAL;
    ALTER TABLE stints ADD COLUMN reentries INTEGER DEFAULT 0;
    ALTER TABLE stints ADD COLUMN at_start INTEGER DEFAULT 0;
    """,
    # ---- v12: stock splits (so per-share figures from older filings match split-adjusted prices); news cache ----
    """
    CREATE TABLE IF NOT EXISTS splits (
        ticker TEXT, date TEXT, ratio REAL, source TEXT, PRIMARY KEY (ticker, date)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS news_cache (
        key TEXT PRIMARY KEY, fetched_at TEXT, items_json TEXT);
    """,
    # ---- v13: Wonderfulness scores (current and by fiscal year) and company profiles (management) ----
    """
    CREATE TABLE IF NOT EXISTS wonder (
        cik INTEGER, profile_id INTEGER, cohort TEXT, as_of TEXT, filed TEXT, score REAL, rank INTEGER, n INTEGER,
        pillars_json TEXT, feats_json TEXT, PRIMARY KEY (cik, profile_id, cohort)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS ix_wonder_cohort ON wonder(profile_id, cohort, score);
    CREATE TABLE IF NOT EXISTS company_info (
        ticker TEXT PRIMARY KEY, fetched_at TEXT, info_json TEXT);
    """,
    # ---- v14: CEO track records (who ran the company and when; your corrections) ----
    """
    CREATE TABLE IF NOT EXISTS ceo_cache (key TEXT PRIMARY KEY, fetched_at TEXT, json TEXT);
    CREATE TABLE IF NOT EXISTS ceo_manual (cik INTEGER PRIMARY KEY, name TEXT, start TEXT, updated_at TEXT);
    """,
    # ---- v15: analyst estimates (one copy a day, so revisions and accuracy can be tracked) and earnings surprises ----
    """
    CREATE TABLE IF NOT EXISTS estimates (ticker TEXT, day TEXT, fetched_at TEXT, json TEXT,
        PRIMARY KEY (ticker, day)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS surprises (ticker TEXT, date TEXT, eps_est REAL, eps_actual REAL, surprise REAL,
        PRIMARY KEY (ticker, date)) WITHOUT ROWID;
    """,
    # ---- v16: each company's CEO grade, for the ranking page (filled when a stock page is opened and in the background) ----
    """
    CREATE TABLE IF NOT EXISTS ceo_grades (cik INTEGER PRIMARY KEY, name TEXT, start TEXT, grade TEXT, score REAL,
        basis TEXT, years REAL, note TEXT, updated_at TEXT);
    """,
    # ---- v17: every company's value-driver valuation at the latest price, for the Screener ----
    """
    CREATE TABLE IF NOT EXISTS vd_values (cik INTEGER PRIMARY KEY, date TEXT, price REAL, iv REAL, low REAL, high REAL,
        er REAL, mos REAL, buy_below REAL, implied REAL, supports REAL, pe_now REAL, pe_med REAL, required REAL,
        ttm INTEGER, estimates INTEGER, updated_at TEXT);
    """,
]


def connect(path=None) -> sqlite3.Connection:
    path = path or paths.DB_PATH
    paths.ensure_dirs()
    con = sqlite3.connect(path, timeout=60, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def get() -> sqlite3.Connection:
    """Thread-local shared connection."""
    con = getattr(_local, "con", None)
    if con is None:
        con = connect()
        migrate(con)
        _local.con = con
    return con


def migrate(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    version = int(row[0]) if row else 0
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        con.executescript(sql)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(i),))


@contextmanager
def tx(con: sqlite3.Connection | None = None):
    con = con or get()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- config ----------------------------------------------------------------
def cfg(key: str, default: Any = None) -> Any:
    row = get().execute("SELECT value_json FROM config WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_cfg(key: str, value: Any) -> None:
    get().execute(
        "INSERT OR REPLACE INTO config(key,value_json,updated_at) VALUES(?,?,?)",
        (key, json.dumps(value), now()),
    )


def rows(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    return get().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable = ()):
    return get().execute(sql, tuple(params)).fetchone()


def scalar(sql: str, params: Iterable = (), default=None):
    r = get().execute(sql, tuple(params)).fetchone()
    return r[0] if r and r[0] is not None else default


def log(source: str, kind: str, ok: bool, detail: str = "") -> None:
    get().execute(
        "INSERT INTO data_log(ts,source,kind,ok,detail) VALUES(?,?,?,?,?)", (now(), source, kind, int(ok), detail[:2000])
    )

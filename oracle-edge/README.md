# Oracle Edge

A local, one-click, Buffett-style stock screener. It keeps only companies that pass Warren Buffett's
tests over 10 years of SEC filings, ranks the ones selling at a sensible price, and logs clear
BUY / ADD / HOLD / WATCH / REVIEW / SELL / SWAP / TRIM signals for a model portfolio, refreshed after
every market close.

> Research and signal-tracking tool, **not financial advice**. Nothing is ever sent to a broker.

> **Full guide:** open `README.html` (or **Guide** in the app menu) for what the app does, how it decides and why.

## Quick start (Windows)

1. Double-click **`Start.bat`**. The first start sets up a private Python inside `runtime\` (a few minutes, one time).
2. Your browser opens to **http://127.0.0.1:8765**. The setup wizard asks for your SEC identity
   (name + email), price source, backup folder, schedule and email alerts.
3. Click **Start first load**. It downloads SEC financial data (a few GB, once), scores every company,
   downloads prices for the shortlist and replays history from 2019, typically 30-90 minutes.

macOS/Linux: install [uv](https://docs.astral.sh/uv/) and run `./start.sh`.

### Installing from this repository

`uv.exe` (the 32 MB launcher) isn't stored in git. To build the one-click installer package:

1. Download `uv-x86_64-pc-windows-msvc.zip` from [uv releases](https://github.com/astral-sh/uv/releases) and put
   `uv.exe` next to `Start.bat`.
2. Run `python tools/build_zip.py <folder>`. It writes `OracleEdge-app.zip` there.
3. Copy `dist/Install-OracleEdge.bat` into the same folder and double-click it. It installs to `C:\OracleEdge`,
   keeps existing data, makes a desktop shortcut and starts the app. The folder can be anywhere (a cloud drive is handy
   because the daily backups can go there too).

## How it ranks

1. **Gates** (must pass): profitable 10 of 10 years · 10-yr avg ROE ≥ 15% (no year < 10%) · long-term debt
   ≤ 4 years of earnings · ≥ 7 years of data.
2. **Quality 0-100**: 24 scored rules (Brian Feroldi's income-statement rules of thumb, balance-sheet and
   cash-flow rules, ROIC, the $1 retained-earnings test, predictability), each scored on its 10-year level
   *and* consistency, weighted by category.
3. **Value 0-100**: two conservative models (owner-earnings DCF at the 30-yr Treasury rate, +2 pts while
   rates are under 5%; and a 10-year Buffettology-style projection). The lower value and lower expected
   return are used.
4. **Buffett Rank** = Quality^0.65 × Value^0.35 × Confidence.

Rules and thresholds live in `rules/oracle_classic.yaml` (locked default). Edit copies in the app.

**Banks and insurers** get their own rulebooks (ROA, efficiency, capital, funding, credit losses / combined
ratio, float growth, book-value growth, reserving) and an ROE-justified price-to-(tangible-)book valuation.

**Foreign SEC filers** (20-F / 40-F) are valued in US$ per traded share: reporting-currency figures × Federal
Reserve exchange rate (FRED) × the ADR ratio, which is read from the annual report's cover page (editable on
the stock page). An implied-P/E sanity check holds back anything that looks mis-scaled.

**Quarterly early warnings**: after every 10-Q the app checks trailing-12-month results. Hard flags
(TTM loss, debt payback over the limit, ROE/ROA below the floor) block new buys and turn an owned stock's
REVIEW into a SELL if the next filing confirms it. Soft flags (profits far above cash flow, sudden dilution,
a gate within 10% of its limit) are watch items only.

**Portfolio construction**: by default the model portfolio is *focused*: real dollars (starting money plus a
weekly amount), at most N holdings (try 5–12), and a new-money rule that decides which stock gets each week's cash
(best value, best Rank, spread evenly, patient, or a sliding reserve that saves part of ordinary-price weeks for
strong buys). An option trims or sells clearly weaker holdings to fund a strong buy when every slot is full. It only buys Buy Zone stocks with the thesis intact, never
pushes one stock above 25%, and lets cash wait when nothing qualifies. Backtest → "Find the best number of
holdings" tests every size × rule on your own history in one pass and compares the dollars with putting the same
money into the S&P 500 on the same days.

**Price targets & alerts**: value sets the buy/strong-buy/trim prices; the 50/200-day averages and 52-week
Fibonacci levels only pick a limit price inside the Buy Zone (and split the buy into thirds while the price is
sliding). After each close the app flags stocks *approaching* (within ~1.5 average daily ranges) or *reaching*
a target, plus your own price alerts, on Today and in the email digest. The "Oracle Classic + timing" profile
lets you backtest whether waiting out a falling 50-day average helps.

## Where things live

| Path | What |
|---|---|
| `oracle_edge/` | Python package: data sources, engine, signals, UI (NiceGUI) |
| `rules/` | Rule profiles (YAML) |
| `oracle_edge/concept_map.yaml` | Which SEC XBRL tags feed each line item |
| `data/` | SQLite database + download cache (**not** in git) |
| `runtime/` | Private Python + libraries (**not** in git) |
| Windows Credential Manager | Email password and API keys (**never** in the folder) |

Daily backups (consistent SQLite copy, zipped) go to the folder set in Settings (default: `Documents\Oracle Edge backups`; a cloud-synced folder is best).

## Development

```
uv sync --extra dev
uv run pytest            # 78 tests: parsing, point-in-time, scoring, valuation, signal ladder, ops
uv run python -m tests.offline_seed   # builds a demo DB from a synthetic market (no network)
```

Data: SEC EDGAR (fundamentals, free), Stooq / Yahoo (prices, free) or Tiingo / EODHD (paid), FRED (macro).
License: MIT.

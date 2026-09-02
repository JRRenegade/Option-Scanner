# Options Scanner

A personal toolkit for running a options-trading checklist across four
strategies, Covered Calls, Credit Spreads, Debit Spreads, and LEAPS,
against live Interactive Brokers data, plus an interactive checklist tool
for the manual side of the process.

This is the Stock/ETF layer of a larger playbook (Macro, Sector/Indices,
Stock/ETF, Charting). It's meant to sit downstream of a market-level
dashboard and feed a future Charting step, not to replace either.

**Not financial advice.** Every scanner here flags candidates worth a
manual look. Nothing picks strikes, sizes a trade, or should be read as a
recommendation to place one.

## What's in here

```
options-scanner/
├── scanner/              <- the live system, run this
│   ├── scanner_config.py     one file for every ticker list and threshold
│   ├── market_data_cache.py  the SQLite layer (reads/writes market_data.db)
│   ├── scanner_utils.py      shared math: RSI, Bollinger, IV Rank, pacing
│   ├── market_data_daemon.py long-lived process, keeps the cache current
│   └── combined_scanner.py   scores all four strategies from the cache
├── legacy_scanners/      <- the original four standalone scripts
│   ├── credit_spread_scanner.py
│   ├── debit_spread_scanner.py
│   ├── covered_call_scanner.py
│   └── leaps_scanner.py
├── checklist/
│   └── strike-check.html     interactive manual checklist, opens in a browser
├── requirements.txt
└── .gitignore
```

`scanner/` is the current, day-to-day system. `legacy_scanners/` holds
the four original scripts, each one self-contained and still fully
working, kept around as a reference or a fallback if you ever want to
run just one strategy without the daemon/cache setup. They aren't wired
into the cache and will make their own live IBKR requests each time they
run.

## How the live system works

Two pieces:

**The daemon** (`market_data_daemon.py`) connects to IBKR once, then
polls: every `DAEMON_REFRESH_INTERVAL_SEC` (30 minutes by default) it
checks the local cache against today's date, and for any ticker whose
newest cached bar isn't from the most recent trading day yet, makes a
plain one-shot `reqHistoricalData` request to catch it up. Checking
freshness is just a local SQLite read, so a poll cycle where nothing is
behind costs zero IBKR requests, most cycles in a day do exactly that.
Every update gets written into `market_data.db` (SQLite, created
automatically, lives in `scanner/`).

(An earlier version tried IBKR's "keep up to date" streaming mode
instead of polling, but that mode needs a live/real-time tick feed to
know when to push updates, and this project intentionally runs on
delayed data to avoid paying for real-time market data subscriptions.
Under delayed data every streaming subscription just hung and timed
out, so the daemon polls instead, the same plain request the original
standalone scanners already used successfully.)

**The scanner** (`combined_scanner.py`) reads from that database. It
makes no historical data requests at all, so there's no IBKR pacing
limit to think about, a scan finishes in under a second. If the cache
looks stale (the daemon isn't running, or hasn't updated recently
enough), it refuses to scan and tells you exactly which tickers are
behind rather than silently scoring on old data.

Why bother with a cache at all: the four strategies' watchlists overlap
heavily (credit and debit spreads share all 28 tickers, for instance), so
fetching data separately for each strategy meant repeating the same IBKR
request over and over. The cache means every unique ticker (69 today) is
fetched once, ever, not once per strategy per run.

## One-time setup

1. Install IB Gateway (lighter) or Trader Workstation (TWS) and log in.
   A paper account works fine for market data / screening purposes.
2. In TWS/Gateway: **File → Global Configuration → API → Settings**
   - Check "Enable ActiveX and Socket Clients"
   - Either add `127.0.0.1` to Trusted IPs, or check "Allow connections
     from localhost only"
   - Note the socket port: TWS live `7496`, TWS paper `7497`, Gateway
     live `4001`, Gateway paper `4002`
3. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Open `scanner/scanner_config.py` and confirm `IB_PORT` matches your
   setup, plus edit `CREDIT_TICKERS`, `DEBIT_TICKERS`, `COVERED_TICKERS`,
   `LEAPS_TICKERS`, and `SHARES_OWNED` to match your own watchlists and
   positions. This is the only file you should need to touch.

## Running it

```
cd scanner

# Start this first, leave it running (its own terminal window,
# or launched alongside TWS/Gateway each morning)
python market_data_daemon.py

# Then, any time you want a scan:
python combined_scanner.py
```

The first time the daemon runs against a cold cache, catching up every
ticker respects IBKR's pacing limit (~60 historical data requests per
rolling 10 minutes), so with 69 tickers it can take roughly 15-20
minutes. After that, most poll cycles find nothing behind and finish
instantly, no more waiting.

`combined_scanner.py` prints results to the terminal and saves, next to
itself:
- `credit_spread_scan_<date>.csv`
- `debit_spread_scan_<date>.csv`
- `covered_call_scan_<date>.csv` and a formatted `covered_call_scan_<date>.xlsx`
- `leaps_scan_<date>.csv`

## The checklist tool

`checklist/strike-check.html` is a standalone, interactive checklist
covering the manual side of each strategy (Covered Calls, Credit
Spreads, LEAPS), with checkboxes and notes that persist in the browser.
Open it directly in any browser, no server or install needed.

## The four strategies, in short

- **Credit spreads** (`CREDIT_*` settings): wants IV Rank high and
  momentum at a reversal extreme. You're selling premium and betting
  price stalls.
- **Debit spreads** (`DEBIT_*` settings): the mirror image. Wants IV Rank
  low and momentum confirmed and still running, not yet exhausted. You're
  buying premium and need the move to keep going.
- **LEAPS** (`LEAPS_*` settings): same "cheap IV + constructive momentum"
  logic as debit spreads, but on a 50/200-day timeframe instead of
  20/50-day, since a 12+ month hold cares about the golden/death cross
  and not chasing a name that's already run too far from its 200-day MA.
- **Covered calls** (`COVERED_*` settings): different question entirely.
  Not directional, it's about a position you already own. Scores whether
  now is a good time to write a call at your usual ~0.20 delta, weighing
  premium richness, room before the strike gets threatened, and whether
  a breakout is actively happening.

Each of the three directional strategies scores 0-4 conditions per
side (bullish/bearish) and flags at 3+, with near-misses (2 of 4) called
out separately rather than collapsing into the same "nothing here" as a
true 0-of-4. Covered calls scores 0-3 and returns a plain verdict
("Good week to sell" down to "Bad week to sell") with a reason for each
factor.

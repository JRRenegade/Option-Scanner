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

Why bother with a cache at all: Credit Spreads, Debit Spreads, and LEAPS
all score the same `DIRECTIONAL_TICKERS` watchlist now, and Covered Calls
adds a handful more, so fetching data separately for each strategy meant
repeating the same IBKR request over and over. The cache means every
unique ticker (69 today) is fetched once, ever, not once per strategy per
run.

## Getting started

For the step-by-step walkthrough — first-time setup on a new machine, and
what your day-to-day routine looks like after that — see
[GETTING_STARTED.md](GETTING_STARTED.md). Short version: one-time setup
installs IBKR and the Python dependencies and points `scanner_config.py`
at your watchlists; after that, each day you start
`market_data_daemon.py` and leave it running, then run
`combined_scanner.py` whenever you want a scan.

If Claude ever hands you a file update it couldn't commit itself (its
bridge to your computer can only write files, not run commands, if the
connection drops), see [GIT_WORKFLOW.md](GIT_WORKFLOW.md) for the steps
to push it to GitHub yourself.

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

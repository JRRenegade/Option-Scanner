#!/usr/bin/env python3
"""
scanner_config.py
=========================

Every ticker list, threshold, and connection setting for the whole scanner
system lives in this ONE file. Nothing else in market_data_daemon.py,
combined_scanner.py, or scanner_utils.py hardcodes a ticker or a number,
they all import it from here.

This is deliberate, not just tidiness: it's the first step toward being
able to hand this whole toolkit to someone else. Today, "handing it off"
still means they need their own IBKR account, their own TWS/Gateway
running locally, and their own market data subscriptions, that part can't
be avoided. But it means the only file a less technical person would ever
need to open is this one: their own IB_PORT, their own tickers, maybe
their own SHARES_OWNED. Everything else (the daemon, the cache, the
scoring logic) stays untouched.

If you only came here to change your watchlists or a threshold, this is
the only file you need.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# IBKR CONNECTION
# ---------------------------------------------------------------------------

IB_HOST = "127.0.0.1"
IB_PORT = 7496          # 7496 TWS live | 7497 TWS paper | 4001 Gateway live | 4002 Gateway paper

# Distinct client IDs so the daemon (which stays connected all day) and a
# scan (which connects briefly, only for the covered-call dividend lookup)
# never collide if they're ever running at the same moment.
DAEMON_CLIENT_ID = 67
SCANNER_CLIENT_ID = 87

USE_DELAYED_DATA = True     # True = works without a live data subscription


# ---------------------------------------------------------------------------
# DATA CACHE
# ---------------------------------------------------------------------------

DB_PATH = "market_data.db"   # SQLite file, created automatically next to these scripts

LOOKBACK_FOR_MAS = "260 D"   # need ~252 trading days for a 200-day MA (the daemon's initial backfill)
LOOKBACK_FOR_IV = "1 Y"      # window IV Rank is computed over (the daemon's initial backfill)

# How many calendar days old the cache's most recent bar is allowed to be
# before a scan refuses to run and tells you to start the daemon instead.
# 1 gives a little slack for "the daemon hasn't picked up this morning's
# bar yet" without accepting genuinely stale data. This is a simple
# weekday-aware check, not holiday-aware, so it can be off by a day around
# market holidays. Bump it if that gets annoying.
MAX_STALE_DAYS = 1


# ---------------------------------------------------------------------------
# SHARED INDICATOR SETTINGS (identical across all four strategies today)
# ---------------------------------------------------------------------------

RSI_LEN = 14
BOLLINGER_LEN = 20
BOLLINGER_STDEV = 2
VOLUME_SPIKE_MULT = 1.5   # today's volume vs 20-day average to call it "elevated"


# ---------------------------------------------------------------------------
# PACING / FILE-SAVING (used by the daemon's one-time backfill only, the
# scans themselves no longer make historical data requests at all)
# ---------------------------------------------------------------------------

REQUEST_PAUSE_SEC = 1.0     # be polite to IBKR's pacing limits between requests
PACING_LIMIT = 55           # stay safely under IBKR's ~60-requests-per-10-minutes historical data limit
PACING_WINDOW_SEC = 600     # the rolling window that limit applies over
DIVIDEND_WAIT_SEC = 2.0     # covered call only: time to let the dividend tick populate

# How often the daemon wakes up to check whether anything needs a refresh.
# Cheap to check often, it's a local SQLite read, not an IBKR request, so
# this just controls how quickly the daemon notices a new trading day's
# bar is available. It does NOT mean 138 requests every 30 minutes: most
# cycles find nothing to do and skip straight back to sleep.
DAEMON_REFRESH_INTERVAL_SEC = 1800   # 30 minutes

MAX_FILENAME_ATTEMPTS = 20   # how many _1, _2, ... suffixes to try before giving up


# ---------------------------------------------------------------------------
# CREDIT SPREAD
# ---------------------------------------------------------------------------

CREDIT_TICKERS = [
    "XLK", "XLV", "XLE", "XLB", "XLY", "XLF", "XLP", "XLC", "XLI", "XLRE",
    "XLU", "SPY", "GLD", "SLV", "IGV", "XBI", "TAN", "SMH", "XME", "OIH",
    "GDX", "IYT", "ITA", "JETS", "XHB", "KRE", "XOP", "XRT",
]

CREDIT_RSI_OVERSOLD = 35
CREDIT_RSI_OVERBOUGHT = 65
CREDIT_IV_RANK_ELEVATED = 50   # credit spreads want IV Rank above this


# ---------------------------------------------------------------------------
# DEBIT SPREAD (mirror image of credit spreads)
# ---------------------------------------------------------------------------

DEBIT_TICKERS = [
    "XLK", "XLV", "XLE", "XLB", "XLY", "XLF", "XLP", "XLC", "XLI", "XLRE",
    "XLU", "SPY", "GLD", "SLV", "IGV", "XBI", "TAN", "SMH", "XME", "OIH",
    "GDX", "IYT", "ITA", "JETS", "XHB", "KRE", "XOP", "XRT",
]
# Same watchlist as CREDIT_TICKERS today -- edit either independently if you want them to diverge.

DEBIT_RSI_BULL_MIN = 45
DEBIT_RSI_BULL_MAX = 70
DEBIT_RSI_BEAR_MIN = 30
DEBIT_RSI_BEAR_MAX = 55
DEBIT_BAND_ZONE_BULL = (0.3, 0.9)
DEBIT_BAND_ZONE_BEAR = (0.1, 0.7)
DEBIT_IV_RANK_CHEAP = 40


# ---------------------------------------------------------------------------
# COVERED CALLS
# ---------------------------------------------------------------------------

COVERED_TICKERS = ["GOOGL", "JEPQ", "MSFT", "NVO", "PEP", "SOFI", "V", "XLE"]

SHARES_OWNED = {
    "GOOGL": None,
    "JEPQ": None,
    "MSFT": None,
    "NVO": None,
    "PEP": None,
    "SOFI": None,
    "V": None,
    "XLE": None,
}

COVERED_RSI_OVERSOLD = 35
COVERED_RSI_OVERBOUGHT = 65
COVERED_IV_RANK_RICH = 50
COVERED_IV_RANK_THIN = 25
COVERED_KEEP_DELTA_TARGET = 0.20   # reference only, this script doesn't pick strikes
COVERED_EXIT_DELTA_TARGET = 0.35
COVERED_BAND_CUSHION_MAX = 0.85
COVERED_BREAKOUT_RSI = 60


# ---------------------------------------------------------------------------
# LEAPS
# ---------------------------------------------------------------------------

LEAPS_TICKERS = [
    "SOFI", "SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
    "IWM", "DIA", "XLK", "XLF", "V", "JPM", "HD", "COST", "AVGO", "AMD",
    "INTC", "CSCO", "DELL", "LMT", "CRM", "ADP", "NOW", "NFLX", "ORCL",
    "PLTR", "JNJ", "PG", "CVX", "XOM", "LOW", "ULTA", "FTNT", "ADM", "DRI",
    "NVO", "TSM", "MRK", "ADBE",
]

LEAPS_RSI_BULL_MIN = 45
LEAPS_RSI_BULL_MAX = 70
LEAPS_RSI_BEAR_MIN = 30
LEAPS_RSI_BEAR_MAX = 55
LEAPS_EXTENSION_MAX = 0.25
LEAPS_IV_RANK_CHEAP = 40


# ---------------------------------------------------------------------------
# The unique ticker list -- what the daemon actually subscribes to
# ---------------------------------------------------------------------------

def _dedup_preserve_order(*lists) -> list:
    seen = []
    for lst in lists:
        for t in lst:
            if t not in seen:
                seen.append(t)
    return seen


ALL_TICKERS = _dedup_preserve_order(CREDIT_TICKERS, DEBIT_TICKERS, COVERED_TICKERS, LEAPS_TICKERS)

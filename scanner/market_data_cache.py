#!/usr/bin/env python3
"""
market_data_cache.py
=========================

The SQLite layer. Everything that reads or writes market_data.db goes
through this file, both market_data_daemon.py (which writes to it) and
combined_scanner.py (which reads from it).

WHY RAW BARS, NOT COMPUTED INDICATORS
----------------------------------------
Only two tables, both holding raw daily values straight from IBKR:

  price_history(ticker, date, open, high, low, close, volume)
  iv_history(ticker, date, iv)

RSI, moving averages, Bollinger Bands, and IV Rank are NOT stored here.
They get computed fresh every time something reads from the cache (see
scanner_utils.compute_snapshot). That way, if RSI_LEN or the IV Rank
lookback ever changes in scanner_config.py, you're never stuck with
derived numbers that were computed under the old settings, a read always
reflects the current settings.

ONE FILE, NO SERVER
-----------------------
SQLite ships with Python (the sqlite3 module), so there's nothing extra
to install. market_data.db is a single file that lives next to these
scripts, created automatically the first time the daemon runs.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pandas as pd

import scanner_config as cfg


def get_connection(db_path: str = cfg.DB_PATH) -> sqlite3.Connection:
    """Open (creating if necessary) the cache database and make sure both
    tables exist. Safe to call every time, CREATE TABLE IF NOT EXISTS is a
    no-op once the tables are already there."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            open   REAL,
            high   REAL,
            low    REAL,
            close  REAL,
            volume REAL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS iv_history (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            iv     REAL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Writes (used by market_data_daemon.py)
# ---------------------------------------------------------------------------

def upsert_price_bar(conn: sqlite3.Connection, ticker: str, date_str: str,
                      open_: float, high: float, low: float, close: float, volume: float) -> None:
    conn.execute(
        """
        INSERT INTO price_history (ticker, date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open = excluded.open, high = excluded.high, low = excluded.low,
            close = excluded.close, volume = excluded.volume
        """,
        (ticker, date_str, open_, high, low, close, volume),
    )
    conn.commit()


def upsert_iv_bar(conn: sqlite3.Connection, ticker: str, date_str: str, iv: float) -> None:
    conn.execute(
        """
        INSERT INTO iv_history (ticker, date, iv)
        VALUES (?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET iv = excluded.iv
        """,
        (ticker, date_str, iv),
    )
    conn.commit()


def bar_date_str(bar_date) -> str:
    """Normalize whatever date format an ib_async BarData gives us
    ('20260828', a datetime, a date, ...) into a plain 'YYYY-MM-DD'
    string for storage."""
    return pd.Timestamp(bar_date).date().isoformat()


# ---------------------------------------------------------------------------
# Reads (used by combined_scanner.py)
# ---------------------------------------------------------------------------

def read_price_history(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, open, high, low, close, volume FROM price_history "
        "WHERE ticker = ? ORDER BY date",
        conn, params=(ticker,),
    )


def read_iv_history(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, iv FROM iv_history WHERE ticker = ? ORDER BY date",
        conn, params=(ticker,),
    )


def latest_price_date(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM price_history WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row[0] if row and row[0] else None


def latest_iv_date(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM iv_history WHERE ticker = ?", (ticker,)
    ).fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------

def _last_expected_trading_day(today: dt.date) -> dt.date:
    """Most recent Mon-Fri on or before today. Doesn't know about market
    holidays, just weekends, so it can be off by a day around a holiday."""
    d = today
    while d.weekday() >= 5:   # 5 = Saturday, 6 = Sunday
        d -= dt.timedelta(days=1)
    return d


def _weekdays_elapsed(latest: dt.date, today: dt.date) -> int:
    """Count of Mon-Fri days strictly after latest, up to and including
    today. Weekends are skipped entirely rather than counted, so crossing
    one costs nothing: Friday's data checked on Monday is 1 weekday
    elapsed (Monday itself), not the 3 calendar days naive subtraction
    would give you. This is what fixes the "Monday looks 3 days stale"
    bug -- MAX_STALE_DAYS now means trading days, not calendar days."""
    if today <= latest:
        return 0
    n = 0
    d = latest
    while d < today:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def is_fresh(latest_date_str: str | None, max_stale_days: int = cfg.MAX_STALE_DAYS) -> bool:
    """True if latest_date_str is within max_stale_days *trading* days of
    today -- weekends don't count against you, so Friday's data is 0
    trading days stale on a Monday, not 3. Still not holiday-aware (a
    single-weekday market holiday can still trip this, since there's no
    way to tell "the market was closed" from "the daemon broke" using
    date math alone -- that's what the scanner's stale-data override
    prompt is for). None (nothing cached yet) is always stale."""
    if not latest_date_str:
        return False
    latest = dt.date.fromisoformat(latest_date_str[:10])
    return _weekdays_elapsed(latest, dt.date.today()) <= max_stale_days


def check_freshness(conn: sqlite3.Connection, tickers: list) -> dict:
    """Returns {ticker: {"price_ok": bool, "iv_ok": bool, "price_date": str|None,
    "iv_date": str|None}} for every ticker in the list, so a caller can
    report exactly which tickers/series are stale rather than a single
    yes/no for the whole cache."""
    report = {}
    for ticker in tickers:
        p_date = latest_price_date(conn, ticker)
        iv_date = latest_iv_date(conn, ticker)
        report[ticker] = {
            "price_ok": is_fresh(p_date),
            "iv_ok": is_fresh(iv_date),
            "price_date": p_date,
            "iv_date": iv_date,
        }
    return report


def _is_caught_up(latest_date_str: str | None) -> bool:
    """Stricter than is_fresh: true only if latest_date_str is exactly the
    most recent expected trading day, no slack. Used by the daemon to
    decide whether a ticker needs a re-fetch. (is_fresh's 1-day grace is
    the right call for combined_scanner.py deciding whether to trust the
    cache enough to score against, but it's the wrong call here: it would
    let the daemon skip re-fetching a ticker for a full extra day after
    a new close is already available.)"""
    if not latest_date_str:
        return False
    latest = dt.date.fromisoformat(latest_date_str[:10])
    expected = _last_expected_trading_day(dt.date.today())
    return latest >= expected


def tickers_needing_refresh(conn: sqlite3.Connection, tickers: list) -> dict:
    """Returns {ticker: {"price_needs_refresh": bool, "iv_needs_refresh": bool}}
    for every ticker in the list. Used by market_data_daemon.py to decide
    which tickers are actually behind, so a poll cycle where nothing has
    changed does zero IBKR requests instead of re-checking everything."""
    report = {}
    for ticker in tickers:
        p_date = latest_price_date(conn, ticker)
        iv_date = latest_iv_date(conn, ticker)
        report[ticker] = {
            "price_needs_refresh": not _is_caught_up(p_date),
            "iv_needs_refresh": not _is_caught_up(iv_date),
        }
    return report

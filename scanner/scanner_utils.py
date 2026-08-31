#!/usr/bin/env python3
"""
scanner_utils.py
=========================

Shared helpers used by market_data_daemon.py and combined_scanner.py:
request pacing, safe file saving, the indicator math, and the function
that turns a slice of cached raw bars into the same {last, prev, iv_r,
percent_b, sma20_5ago} shape the four scoring functions expect.

Keeping this in one place means the RSI/Bollinger/IV Rank formulas exist
exactly once in the whole system, not copy-pasted across five files.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

import scanner_config as cfg


# ---------------------------------------------------------------------------
# Request pacing (used only by market_data_daemon.py's one-time backfill --
# the scans themselves read from the cache and make no historical data
# requests at all)
# ---------------------------------------------------------------------------

_request_times: list = []


def pace_request(pacing_limit: int = cfg.PACING_LIMIT, window_sec: float = cfg.PACING_WINDOW_SEC) -> None:
    """IBKR enforces a hard cap of roughly 60 historical data requests
    within any rolling 10-minute window, regardless of which symbol
    they're for. This tracks our own request timestamps and sleeps just
    long enough to stay under that cap, instead of firing requests until
    IBKR starts rejecting them partway through a long watchlist."""
    now = time.time()
    while _request_times and now - _request_times[0] > window_sec:
        _request_times.pop(0)
    if len(_request_times) >= pacing_limit:
        sleep_for = window_sec - (now - _request_times[0]) + 1
        print(f"  Pausing {sleep_for:.0f}s to stay under IBKR's historical data pacing limit...")
        time.sleep(sleep_for)
        now = time.time()
        while _request_times and now - _request_times[0] > window_sec:
            _request_times.pop(0)
    _request_times.append(time.time())


# ---------------------------------------------------------------------------
# File saving (won't crash just because yesterday's file is still open)
# ---------------------------------------------------------------------------

def save_with_retry(save_fn, base_path: str, max_attempts: int = cfg.MAX_FILENAME_ATTEMPTS) -> str:
    """Call save_fn(path), starting with base_path. If that raises
    PermissionError -- typically because the file is already open in Excel
    or another program -- retry with base_path_1, base_path_2, etc. instead
    of crashing. Returns whichever path actually succeeded."""
    stem, ext = os.path.splitext(base_path)
    path = base_path
    for attempt in range(1, max_attempts + 1):
        try:
            save_fn(path)
            return path
        except PermissionError:
            next_path = f"{stem}_{attempt}{ext}"
            print(f"  '{path}' is open or locked -- trying '{next_path}' instead...")
            path = next_path
    # One last try with no safety net, so the real error surfaces if every
    # numbered variant is also locked.
    save_fn(path)
    return path


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def rsi(closes: pd.Series, length: int = cfg.RSI_LEN) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(closes: pd.Series, length: int = cfg.BOLLINGER_LEN, stdevs: float = cfg.BOLLINGER_STDEV):
    mid = closes.rolling(length).mean()
    sd = closes.rolling(length).std(ddof=0)
    upper = mid + stdevs * sd
    lower = mid - stdevs * sd
    return upper, mid, lower


def iv_rank(iv_series: pd.Series) -> float:
    iv_series = iv_series.dropna()
    if iv_series.empty:
        return float("nan")
    current = iv_series.iloc[-1]
    lo, hi = iv_series.min(), iv_series.max()
    if hi == lo:
        return float("nan")
    return round((current - lo) / (hi - lo) * 100, 1)


def rsi_read(value: float) -> str:
    if np.isnan(value):
        return "n/a"
    if value >= cfg.COVERED_RSI_OVERBOUGHT:
        return "Overbought"
    if value <= cfg.COVERED_RSI_OVERSOLD:
        return "Oversold"
    return "Neutral"


def premium_label(iv_r: float) -> str:
    if np.isnan(iv_r):
        return "n/a"
    if iv_r >= cfg.COVERED_IV_RANK_RICH:
        return "Rich"
    if iv_r <= cfg.COVERED_IV_RANK_THIN:
        return "Thin"
    return "Average"


# ---------------------------------------------------------------------------
# Turn cached raw bars into the snapshot the scoring functions expect
# ---------------------------------------------------------------------------

def compute_snapshot(price_df: pd.DataFrame, iv_df: pd.DataFrame) -> dict:
    """price_df: columns [date, open, high, low, close, volume], ascending
    by date (as read_price_history returns it). iv_df: columns [date, iv],
    same ordering. Returns {last, prev, sma20_5ago, iv_r, percent_b}, the
    same shape the four score_* functions in combined_scanner.py were
    already written against, whether that data came from a live IBKR
    request (the old design) or a cache read (this one)."""
    if len(price_df) < 2:
        raise RuntimeError("not enough cached price history (need at least 2 trading days)")

    df = price_df.copy()
    df["rsi"] = rsi(df["close"])
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    df["upper_bb"], df["mid_bb"], df["lower_bb"] = bollinger_bands(df["close"])
    df["avg_vol20"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]
    sma20_5ago = df["sma20"].iloc[-6] if len(df) > 6 else np.nan

    iv_r = float("nan")
    if not iv_df.empty:
        iv_r = iv_rank(iv_df["iv"])

    band_width = last["upper_bb"] - last["lower_bb"]
    percent_b = (last["close"] - last["lower_bb"]) / band_width if band_width else float("nan")

    return {
        "last": last,
        "prev": prev,
        "sma20_5ago": sma20_5ago,
        "iv_r": iv_r,
        "percent_b": percent_b,
    }

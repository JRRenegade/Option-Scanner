#!/usr/bin/env python3
"""
credit_spread_scanner.py
=========================

Runs your credit-spread checklist against a fixed watchlist of sector/index
ETFs using live data from Interactive Brokers (TWS API), and flags which
ones currently look like put-credit-spread or call-credit-spread candidates.

WHY THIS SHAPE
--------------
IBKR doesn't hand out a copy-paste API key. The standard way an individual
trader automates against their own account is the TWS API: you run TWS or
IB Gateway locally, enable API access, and this script connects to it over
a local socket. No credentials live in this file.

ONE-TIME SETUP
---------------
1. Install IB Gateway (lighter) or Trader Workstation (TWS) from
   interactivebrokers.com and log in (paper account is fine for market
   data / screening purposes).
2. In TWS/Gateway: File -> Global Configuration -> API -> Settings
     - Check "Enable ActiveX and Socket Clients"
     - Add 127.0.0.1 to "Trusted IPs"
     - Note the socket port (defaults: TWS live 7496, TWS paper 7497,
       Gateway live 4001, Gateway paper 4002)
     - Uncheck "Read-Only API" is fine either way -- this script never
       places an order, it only reads market data.
3. Install Python dependencies:
       pip install ib_async pandas numpy
4. Make sure your IBKR market data subscriptions cover US equities/ETFs
   (delayed data works fine for screening -- see USE_DELAYED_DATA below).

RUNNING IT
----------
    python credit_spread_scanner.py

Leave TWS or IB Gateway open and logged in while it runs. Results print
as a table and are also written to credit_spread_scan_<date>.csv next to
this script.

WHAT IT CHECKS (maps to your credit spread checklist)
------------------------------------------------------
  - Trend & momentum: RSI(14), 20/50-day SMA relationship
  - Volatility: IV Rank over the trailing year (via IBKR's own historical
    OPTION_IMPLIED_VOLATILITY series for the underlying)
  - Strike-placement cushion: price position vs. 20-day Bollinger Bands
  - Volume: today's volume vs. its 20-day average
  - Flags a symbol as a put-credit-spread or call-credit-spread candidate
    only when several of these line up -- it does not pick strikes, DTE,
    or size the trade for you. Treat a flag as "worth pulling up the chain
    and running the rest of the manual checklist," not as a signal to
    place a trade.
  - Every ticker also gets a Bull Score and Bear Score (0-4, one point per
    condition met) plus the specific conditions that hit, so a 2-of-4
    near-miss is visible in the table instead of collapsing into the same
    "-" as a 0-of-4. Results are sorted by whichever score is highest, and
    anything scoring 2+ without quite flagging gets called out separately
    at the bottom as worth a manual look. %B (0 = lower band, 1 = upper
    band, and it can go outside that range) is also printed raw, since an
    asset can be RSI-extended while still reading "mid-range" on the bands
    if the bands themselves have widened with a strong trend.

TUNE ME
--------
Everything you'd plausibly want to adjust lives in the CONFIG block below.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

import numpy as np
import pandas as pd

try:
    from ib_async import IB, Stock, util
except ImportError:
    print("Missing dependency. Install with:  pip install ib_async pandas numpy")
    sys.exit(1)


# ---------------------------------------------------------------------------
# CONFIG -- edit this block to taste
# ---------------------------------------------------------------------------

TICKERS = [
    "XLK", "XLV", "XLE", "XLB", "XLY", "XLF", "XLP", "XLC", "XLI", "XLRE",
    "XLU", "SPY", "GLD", "SLV", "IGV", "XBI", "TAN", "SMH", "XME", "OIH",
    "GDX", "IYT", "ITA", "JETS", "XHB", "KRE", "XOP", "XRT",
]
# (deduped from what you sent -- XLU and IGV were each listed twice)

IB_HOST = "127.0.0.1"
IB_PORT = 7496          # 7496 TWS live | 7497 TWS paper | 4001 Gateway live | 4002 Gateway paper
IB_CLIENT_ID = 17       # any int not already in use by another API client

USE_DELAYED_DATA = True     # True = works without a live data subscription
LOOKBACK_FOR_MAS = "260 D"  # need ~252 trading days for a 200-day MA
LOOKBACK_FOR_IV = "1 Y"     # window IV Rank is computed over

RSI_LEN = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

BOLLINGER_LEN = 20
BOLLINGER_STDEV = 2

IV_RANK_ELEVATED = 50     # credit spreads generally want IV Rank above this
VOLUME_SPIKE_MULT = 1.5   # today's volume vs 20-day average to call it "elevated"

REQUEST_PAUSE_SEC = 1.0     # be polite to IBKR's pacing limits between requests
PACING_LIMIT = 55           # stay safely under IBKR's ~60-requests-per-10-minutes historical data limit
PACING_WINDOW_SEC = 600     # the rolling window that limit applies over

MAX_FILENAME_ATTEMPTS = 20   # how many _1, _2, ... suffixes to try before giving up


# ---------------------------------------------------------------------------
# Request pacing (avoids IBKR's historical-data pacing violation errors --
# 28 tickers x 2 requests each is already at 56, close to the ~60/10min
# ceiling, and this list keeps growing)
# ---------------------------------------------------------------------------

_request_times: list = []


def pace_request(pacing_limit: int = PACING_LIMIT, window_sec: float = PACING_WINDOW_SEC) -> None:
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

def save_with_retry(save_fn, base_path: str, max_attempts: int = MAX_FILENAME_ATTEMPTS) -> str:
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

def rsi(closes: pd.Series, length: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(closes: pd.Series, length: int = 20, stdevs: float = 2):
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


# ---------------------------------------------------------------------------
# Per-symbol scan
# ---------------------------------------------------------------------------

def scan_symbol(ib: IB, symbol: str) -> dict:
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    pace_request()
    price_bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=LOOKBACK_FOR_MAS,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    time.sleep(REQUEST_PAUSE_SEC)

    pace_request()
    iv_bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=LOOKBACK_FOR_IV,
        barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY",
        useRTH=True,
        formatDate=1,
    )
    time.sleep(REQUEST_PAUSE_SEC)

    if not price_bars:
        raise RuntimeError("no price history returned (check market data subscription)")

    df = util.df(price_bars)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    df["upper_bb"], df["mid_bb"], df["lower_bb"] = bollinger_bands(
        df["close"], BOLLINGER_LEN, BOLLINGER_STDEV
    )
    df["avg_vol20"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    iv_r = float("nan")
    if iv_bars:
        iv_df = util.df(iv_bars)
        iv_r = iv_rank(iv_df["close"])

    green_day = last["close"] > prev["close"]
    volume_spike = last["volume"] > VOLUME_SPIKE_MULT * last["avg_vol20"]
    uptrend = last["sma20"] > last["sma50"]
    near_lower_band = last["close"] <= last["lower_bb"] * 1.01
    near_upper_band = last["close"] >= last["upper_bb"] * 0.99
    rsi_turning_up = last["rsi"] > prev["rsi"]
    rsi_turning_down = last["rsi"] < prev["rsi"]
    iv_elevated = not np.isnan(iv_r) and iv_r >= IV_RANK_ELEVATED

    # %B: where price sits between the bands (0 = lower band, 1 = upper band).
    # Can go below 0 or above 1 when price has pushed outside the bands entirely.
    band_width = last["upper_bb"] - last["lower_bb"]
    percent_b = (last["close"] - last["lower_bb"]) / band_width if band_width else float("nan")

    # Named conditions so a near-miss is visible instead of collapsing into a bare "-".
    bull_conditions = [
        ("RSI oversold + turning up", bool(last["rsi"] < RSI_OVERSOLD and rsi_turning_up)),
        ("Near lower band", bool(near_lower_band)),
        ("Uptrend (20>50MA)", bool(uptrend)),
        ("IV Rank elevated", bool(iv_elevated)),
    ]
    bear_conditions = [
        ("RSI overbought + turning down", bool(last["rsi"] > RSI_OVERBOUGHT and rsi_turning_down)),
        ("Near upper band", bool(near_upper_band)),
        ("Downtrend (20<50MA)", bool(not uptrend)),
        ("IV Rank elevated", bool(iv_elevated)),
    ]

    bull_score = sum(1 for _, hit in bull_conditions if hit)
    bear_score = sum(1 for _, hit in bear_conditions if hit)
    bull_reasons = ", ".join(label for label, hit in bull_conditions if hit) or "-"
    bear_reasons = ", ".join(label for label, hit in bear_conditions if hit) or "-"

    setup = "-"
    if bull_score >= 3:
        setup = "PUT credit spread"
    elif bear_score >= 3:
        setup = "CALL credit spread"

    return {
        "Ticker": symbol,
        "Close": round(last["close"], 2),
        "Green Day": "Y" if green_day else "N",
        "RSI": round(last["rsi"], 1),
        "20MA vs 50MA": "above" if uptrend else "below",
        "Bollinger": (
            "near lower" if near_lower_band else "near upper" if near_upper_band else "mid-range"
        ),
        "%B": round(percent_b, 2) if not np.isnan(percent_b) else float("nan"),
        "Vol vs 20D Avg": f"{last['volume'] / last['avg_vol20']:.2f}x" if last["avg_vol20"] else "n/a",
        "Volume Spike": "Y" if volume_spike else "N",
        "IV Rank": iv_r,
        "Bull Score": bull_score,
        "Bull Reasons": bull_reasons,
        "Bear Score": bear_score,
        "Bear Reasons": bear_reasons,
        "Setup Flag": setup,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ib = IB()
    print(f"Connecting to IBKR at {IB_HOST}:{IB_PORT} (clientId={IB_CLIENT_ID})...")
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, readonly=True)

    if USE_DELAYED_DATA:
        ib.reqMarketDataType(3)  # 3 = delayed, works without a live subscription

    rows = []
    for symbol in TICKERS:
        print(f"Scanning {symbol}...")
        try:
            rows.append(scan_symbol(ib, symbol))
        except Exception as exc:
            print(f"  -> skipped {symbol}: {exc}")
            rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {exc}"})

    ib.disconnect()

    results = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", None)

    # Rank by how close each ticker got to a flag, not alphabetically -- a 2/4
    # near-miss like the one that started this conversation should surface at
    # the top instead of hiding next to a 0/4 in the middle of the table.
    if "Bull Score" in results.columns:
        results["_rank"] = results[["Bull Score", "Bear Score"]].max(axis=1).fillna(-1)
        results = results.sort_values("_rank", ascending=False).drop(columns="_rank")

    print("\n" + "=" * 100)
    print(f"Credit spread scan -- {dt.date.today().isoformat()}")
    print("=" * 100)
    display_cols = [c for c in results.columns if c not in ("Bull Reasons", "Bear Reasons")]
    print(results[display_cols].to_string(index=False))

    flagged = results[results["Setup Flag"].isin(["PUT credit spread", "CALL credit spread"])]
    if not flagged.empty:
        print("\nFlagged for a closer look:")
        print(flagged[["Ticker", "Setup Flag"]].to_string(index=False))
    else:
        print("\nNothing flagged today -- but check the near-misses below before assuming")
        print("there's nothing worth a closer look.")

    near_misses = results[
        (results["Setup Flag"] == "-")
        & ((results["Bull Score"] >= 2) | (results["Bear Score"] >= 2))
    ] if "Bull Score" in results.columns else pd.DataFrame()
    if not near_misses.empty:
        print("\nClose but no flag (2+ of 4 conditions hit -- worth a manual look):")
        for _, r in near_misses.iterrows():
            side = "bullish" if r["Bull Score"] >= r["Bear Score"] else "bearish"
            reasons = r["Bull Reasons"] if side == "bullish" else r["Bear Reasons"]
            score = max(r["Bull Score"], r["Bear Score"])
            print(f"  {r['Ticker']}: {score}/4 {side} -- {reasons}")

    out_path = f"credit_spread_scan_{dt.date.today().isoformat()}.csv"
    saved_path = save_with_retry(lambda p: results.to_csv(p, index=False), out_path)
    print(f"\nSaved full results to {saved_path}")


if __name__ == "__main__":
    main()

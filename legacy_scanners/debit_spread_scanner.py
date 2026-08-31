#!/usr/bin/env python3
"""
debit_spread_scanner.py
=========================

Runs the same 28-name sector/index/thematic ETF watchlist as
credit_spread_scanner.py through live Interactive Brokers data, but flags
bull-call and bear-put DEBIT spread candidates instead of credit spreads.

THIS IS THE MIRROR IMAGE OF THE CREDIT SPREAD SCANNER
--------------------------------------------------------
Same setup (IB Gateway/TWS, ib_async, no API key -- see
credit_spread_scanner.py's own docstring for the one-time setup steps,
they're identical here). What's different is the logic, because a debit
spread wants the opposite conditions from a credit spread on two of the
four checks:

  - VOLATILITY FLIPS. A credit spread collects premium, so it wants IV
    Rank HIGH (rich premium for the risk taken). A debit spread PAYS
    premium, so it wants IV Rank LOW (cheap to buy, and there's room for
    IV to expand in your favor rather than crush against you).
  - MOMENTUM FRAMING FLIPS. A credit spread looks for RSI at an extreme
    that's starting to turn (a reversal, since you're betting price
    stalls out and stays on one side of your short strike). A debit
    spread needs the underlying to keep moving in your favor before
    expiration to overcome the debit paid, so it looks for a CONFIRMED
    trend with RSI in a constructive, not-yet-exhausted zone -- momentum
    that has room left to run, not momentum about to snap back.

Trend direction (20-day MA vs. 50-day MA) and the general shape of the
scoring (4 conditions per side, flag at 3+, near-misses called out
separately, sorted by score) carry over unchanged from the credit spread
scanner.

WHAT IT CHECKS
---------------
  - Trend: 20-day MA vs. 50-day MA
  - Momentum: RSI(14) in a constructive zone (RSI_BULL_MIN-RSI_BULL_MAX
    for calls, RSI_BEAR_MIN-RSI_BEAR_MAX for puts) and moving further in
    that direction, not already at an extreme
  - Room to run: %B (where price sits between the 20-day Bollinger Bands)
    in a healthy zone -- trending, but not already pinned to the band's
    outer edge with little room left before expiration
  - Volatility: IV Rank over the trailing year, but LOW is what you want
    here, the opposite of the credit spread scanner
  - Flags "BULL CALL debit spread" or "BEAR PUT debit spread" only when
    3+ of the 4 conditions line up. Like the credit spread scanner, this
    doesn't pick strikes, width, or DTE -- it tells you which names are
    worth pulling up the chain for.

RUNNING IT
----------
    python debit_spread_scanner.py

Saves debit_spread_scan_<date>.csv next to this script (retries with
_1, _2, etc. if that file is already open, same as the other two
scanners).

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
# Same watchlist as credit_spread_scanner.py -- edit either list independently if you want them to diverge.

IB_HOST = "127.0.0.1"
IB_PORT = 7496          # 7496 TWS live | 7497 TWS paper | 4001 Gateway live | 4002 Gateway paper
IB_CLIENT_ID = 37       # different from the other two scanners' clientIds, in case all three ever run close together

USE_DELAYED_DATA = True     # True = works without a live data subscription
LOOKBACK_FOR_MAS = "260 D"  # need ~252 trading days for a 200-day MA
LOOKBACK_FOR_IV = "1 Y"     # window IV Rank is computed over

RSI_LEN = 14
RSI_BULL_MIN = 45   # constructive-and-rising zone for a bull call spread
RSI_BULL_MAX = 70   # above this, momentum is already extended -- less room to run before your DTE
RSI_BEAR_MIN = 30   # below this, momentum is already extended the other way
RSI_BEAR_MAX = 55   # constructive-and-falling zone for a bear put spread

BOLLINGER_LEN = 20
BOLLINGER_STDEV = 2
BAND_ZONE_BULL = (0.3, 0.9)   # %B range = "trending up, room left before the band's edge"
BAND_ZONE_BEAR = (0.1, 0.7)   # mirror image for a downtrend

IV_RANK_CHEAP = 40        # IV Rank at/below this -> cheap enough to be a buyer (opposite of the credit spread scanner)
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
    rsi_turning_up = last["rsi"] > prev["rsi"]
    rsi_turning_down = last["rsi"] < prev["rsi"]
    iv_cheap = not np.isnan(iv_r) and iv_r <= IV_RANK_CHEAP

    # %B: where price sits between the bands (0 = lower band, 1 = upper band).
    # Can go below 0 or above 1 when price has pushed outside the bands entirely.
    band_width = last["upper_bb"] - last["lower_bb"]
    percent_b = (last["close"] - last["lower_bb"]) / band_width if band_width else float("nan")

    bull_band_ok = (not np.isnan(percent_b)) and BAND_ZONE_BULL[0] <= percent_b <= BAND_ZONE_BULL[1]
    bear_band_ok = (not np.isnan(percent_b)) and BAND_ZONE_BEAR[0] <= percent_b <= BAND_ZONE_BEAR[1]
    rsi_bull_zone = RSI_BULL_MIN <= last["rsi"] <= RSI_BULL_MAX and rsi_turning_up
    rsi_bear_zone = RSI_BEAR_MIN <= last["rsi"] <= RSI_BEAR_MAX and rsi_turning_down

    # Named conditions so a near-miss is visible instead of collapsing into a bare "-".
    bull_conditions = [
        ("Uptrend (20>50MA)", bool(uptrend)),
        (f"RSI constructive+rising ({RSI_BULL_MIN}-{RSI_BULL_MAX})", bool(rsi_bull_zone)),
        (f"Room to run (%B {BAND_ZONE_BULL[0]}-{BAND_ZONE_BULL[1]})", bool(bull_band_ok)),
        ("IV Rank cheap", bool(iv_cheap)),
    ]
    bear_conditions = [
        ("Downtrend (20<50MA)", bool(not uptrend)),
        (f"RSI constructive+falling ({RSI_BEAR_MIN}-{RSI_BEAR_MAX})", bool(rsi_bear_zone)),
        (f"Room to fall (%B {BAND_ZONE_BEAR[0]}-{BAND_ZONE_BEAR[1]})", bool(bear_band_ok)),
        ("IV Rank cheap", bool(iv_cheap)),
    ]

    bull_score = sum(1 for _, hit in bull_conditions if hit)
    bear_score = sum(1 for _, hit in bear_conditions if hit)
    bull_reasons = ", ".join(label for label, hit in bull_conditions if hit) or "-"
    bear_reasons = ", ".join(label for label, hit in bear_conditions if hit) or "-"

    setup = "-"
    if bull_score >= 3:
        setup = "BULL CALL debit spread"
    elif bear_score >= 3:
        setup = "BEAR PUT debit spread"

    return {
        "Ticker": symbol,
        "Close": round(last["close"], 2),
        "Green Day": "Y" if green_day else "N",
        "RSI": round(last["rsi"], 1),
        "20MA vs 50MA": "above" if uptrend else "below",
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

    # Rank by how close each ticker got to a flag, not alphabetically.
    if "Bull Score" in results.columns:
        results["_rank"] = results[["Bull Score", "Bear Score"]].max(axis=1).fillna(-1)
        results = results.sort_values("_rank", ascending=False).drop(columns="_rank")

    print("\n" + "=" * 100)
    print(f"Debit spread scan -- {dt.date.today().isoformat()}")
    print("=" * 100)
    display_cols = [c for c in results.columns if c not in ("Bull Reasons", "Bear Reasons")]
    print(results[display_cols].to_string(index=False))

    flagged = results[results["Setup Flag"].isin(["BULL CALL debit spread", "BEAR PUT debit spread"])]
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

    out_path = f"debit_spread_scan_{dt.date.today().isoformat()}.csv"
    saved_path = save_with_retry(lambda p: results.to_csv(p, index=False), out_path)
    print(f"\nSaved full results to {saved_path}")


if __name__ == "__main__":
    main()

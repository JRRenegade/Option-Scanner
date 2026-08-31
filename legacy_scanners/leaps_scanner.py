#!/usr/bin/env python3
"""
leaps_scanner.py
=========================

Scans a 42-name watchlist (mega-cap tech, broad index/sector ETFs, and a
mix of blue-chip and growth names) for long-dated (12+ month) call or put
candidates, using live Interactive Brokers data.

HOW THIS RELATES TO THE OTHER TWO SCANNERS
---------------------------------------------
Same setup as credit_spread_scanner.py and debit_spread_scanner.py (IB
Gateway/TWS, ib_async, no API key). Logically, a LEAPS long call is much
closer to a debit spread than a credit spread -- you're paying premium
and need the underlying to move your way -- so this reuses that same
"IV Rank cheap + constructive, not-yet-exhausted momentum" framing rather
than the credit spread scanner's "rich IV + reversal from an extreme."

What's different from the debit spread scanner is the timeframe. A
vertical spread with a 30-45 day expiration cares about the 20/50-day
moving averages. A position you might hold for a year or more cares
about the 50/200-day relationship (the "golden cross" / "death cross")
and how far price has already run from the 200-day MA -- chasing a name
that's already 30% above its 200-day average for a 12+ month commitment
gives you a much worse entry than the same thesis six weeks earlier.

WHAT IT CHECKS
---------------
  - Long-term trend: 50-day MA vs. 200-day MA, and price vs. 200-day MA
    (the "golden cross" / "death cross" from your checklist)
  - Momentum: RSI(14) in a constructive zone and still moving that
    direction, not already at an extreme. This is a daily RSI used as a
    proxy for the checklist's "weekly RSI" check -- reasonable for a
    scan across many names, but pull up the actual weekly chart before
    committing capital.
  - Extension: how far price has run from the 200-day MA, as a percentage.
    Too far in your direction (past EXTENSION_MAX) means you'd be paying
    up for a move that's already happened, with more downside than
    upside left before your DTE.
  - Volatility: IV Rank over the trailing year, and LOW is what you want
    (same logic as the debit spread scanner -- you're buying time
    premium, so cheap is good).
  - Flags "BULLISH LEAP (long call)" or "BEARISH LEAP (long put)" only
    when 3+ of these 4 line up.

WHAT IT DOESN'T CHECK
-----------------------
Fundamentals/upcoming catalysts, the actual bid-ask/open interest on the
specific far-dated contracts (LEAPS chains are thinner than front-month,
and this only looks at the underlying), and dividend dates -- all still
manual checks from your checklist. This also doesn't suggest a delta or
strike; per your own approach, that's a decision you make once you know
which names are worth a closer look.

A NOTE ON REQUEST PACING
---------------------------
42 tickers x 2 historical data requests each = 84 requests per run.
IBKR enforces a hard limit of roughly 60 historical data requests within
any rolling 10-minute window, so this scanner tracks its own request
timestamps and will pause mid-run if it gets close to that ceiling,
rather than erroring out on the back half of your list. You'll see a
"Pausing ...s to stay under IBKR's pacing limit" message if that kicks
in -- it's expected, not a bug. (The credit and debit spread scanners
were updated with the same guard, since 28 tickers is already close to
the limit and you've been steadily growing these lists.)

RUNNING IT
----------
    python leaps_scanner.py

Saves leaps_scan_<date>.csv next to this script (retries with _1, _2,
etc. if that file is already open, same as the other scanners).

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
    "SOFI", "SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
    "IWM", "DIA", "XLK", "XLF", "V", "JPM", "HD", "COST", "AVGO", "AMD",
    "INTC", "CSCO", "DELL", "LMT", "CRM", "ADP", "NOW", "NFLX", "ORCL",
    "PLTR", "JNJ", "PG", "CVX", "XOM", "LOW", "ULTA", "FTNT", "ADM", "DRI",
    "NVO", "TSM", "MRK", "ADBE",
]
# (AMZN was listed twice across your messages -- deduped to 42 unique names)

IB_HOST = "127.0.0.1"
IB_PORT = 7496          # 7496 TWS live | 7497 TWS paper | 4001 Gateway live | 4002 Gateway paper
IB_CLIENT_ID = 47       # different from the other scanners' clientIds, in case more than one runs close together

USE_DELAYED_DATA = True     # True = works without a live data subscription
LOOKBACK_FOR_MAS = "260 D"  # need ~252 trading days for a 200-day MA
LOOKBACK_FOR_IV = "1 Y"     # window IV Rank is computed over

RSI_LEN = 14
RSI_BULL_MIN = 45   # constructive-and-rising zone for a bullish LEAP (long call)
RSI_BULL_MAX = 70   # above this, momentum is already extended
RSI_BEAR_MIN = 30   # below this, momentum is already extended the other way
RSI_BEAR_MAX = 55   # constructive-and-falling zone for a bearish LEAP (long put)

BOLLINGER_LEN = 20   # informational only for this scanner (printed as %B), not scored
BOLLINGER_STDEV = 2

EXTENSION_MAX = 0.25   # max distance from the 200-day MA (as a fraction) before "too extended to chase"

IV_RANK_CHEAP = 40        # IV Rank at/below this -> cheap enough to be a buyer
VOLUME_SPIKE_MULT = 1.5   # today's volume vs 20-day average to call it "elevated"

REQUEST_PAUSE_SEC = 1.0     # be polite to IBKR's pacing limits between requests
PACING_LIMIT = 55           # stay safely under IBKR's ~60-requests-per-10-minutes historical data limit
PACING_WINDOW_SEC = 600     # the rolling window that limit applies over

MAX_FILENAME_ATTEMPTS = 20   # how many _1, _2, ... suffixes to try before giving up


# ---------------------------------------------------------------------------
# Request pacing (avoids IBKR's historical-data pacing violation errors)
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
    df["sma50"] = df["close"].rolling(50).mean()
    df["sma200"] = df["close"].rolling(200).mean()
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

    volume_spike = last["volume"] > VOLUME_SPIKE_MULT * last["avg_vol20"]
    rsi_turning_up = last["rsi"] > prev["rsi"]
    rsi_turning_down = last["rsi"] < prev["rsi"]
    iv_cheap = not np.isnan(iv_r) and iv_r <= IV_RANK_CHEAP

    has_200 = not np.isnan(last["sma200"])
    golden_trend = has_200 and last["sma50"] > last["sma200"] and last["close"] > last["sma200"]
    death_trend = has_200 and last["sma50"] < last["sma200"] and last["close"] < last["sma200"]
    extension = (last["close"] - last["sma200"]) / last["sma200"] if has_200 else float("nan")
    not_overextended_up = has_200 and extension <= EXTENSION_MAX
    not_overextended_down = has_200 and -extension <= EXTENSION_MAX

    rsi_bull_zone = RSI_BULL_MIN <= last["rsi"] <= RSI_BULL_MAX and rsi_turning_up
    rsi_bear_zone = RSI_BEAR_MIN <= last["rsi"] <= RSI_BEAR_MAX and rsi_turning_down

    band_width = last["upper_bb"] - last["lower_bb"]
    percent_b = (last["close"] - last["lower_bb"]) / band_width if band_width else float("nan")

    bull_conditions = [
        ("Golden trend (50>200MA, price>200MA)", bool(golden_trend)),
        (f"RSI constructive+rising ({RSI_BULL_MIN}-{RSI_BULL_MAX})", bool(rsi_bull_zone)),
        (f"Not overextended vs 200MA (<{int(EXTENSION_MAX * 100)}%)", bool(not_overextended_up)),
        ("IV Rank cheap", bool(iv_cheap)),
    ]
    bear_conditions = [
        ("Death trend (50<200MA, price<200MA)", bool(death_trend)),
        (f"RSI constructive+falling ({RSI_BEAR_MIN}-{RSI_BEAR_MAX})", bool(rsi_bear_zone)),
        (f"Not overextended vs 200MA (<{int(EXTENSION_MAX * 100)}%)", bool(not_overextended_down)),
        ("IV Rank cheap", bool(iv_cheap)),
    ]

    bull_score = sum(1 for _, hit in bull_conditions if hit)
    bear_score = sum(1 for _, hit in bear_conditions if hit)
    bull_reasons = ", ".join(label for label, hit in bull_conditions if hit) or "-"
    bear_reasons = ", ".join(label for label, hit in bear_conditions if hit) or "-"

    setup = "-"
    if bull_score >= 3:
        setup = "BULLISH LEAP (long call)"
    elif bear_score >= 3:
        setup = "BEARISH LEAP (long put)"

    return {
        "Ticker": symbol,
        "Close": round(last["close"], 2),
        "RSI": round(last["rsi"], 1),
        "50MA vs 200MA": "above" if (has_200 and last["sma50"] > last["sma200"]) else "below" if has_200 else "n/a",
        "% vs 200MA": round(extension * 100, 1) if has_200 else float("nan"),
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

    if "Bull Score" in results.columns:
        results["_rank"] = results[["Bull Score", "Bear Score"]].max(axis=1).fillna(-1)
        results = results.sort_values("_rank", ascending=False).drop(columns="_rank")

    print("\n" + "=" * 100)
    print(f"LEAPS scan -- {dt.date.today().isoformat()}")
    print("=" * 100)
    display_cols = [c for c in results.columns if c not in ("Bull Reasons", "Bear Reasons")]
    print(results[display_cols].to_string(index=False))

    flagged = results[results["Setup Flag"].isin(["BULLISH LEAP (long call)", "BEARISH LEAP (long put)"])]
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

    out_path = f"leaps_scan_{dt.date.today().isoformat()}.csv"
    saved_path = save_with_retry(lambda p: results.to_csv(p, index=False), out_path)
    print(f"\nSaved full results to {saved_path}")


if __name__ == "__main__":
    main()

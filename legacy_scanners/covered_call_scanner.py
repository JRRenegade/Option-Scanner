#!/usr/bin/env python3
"""
covered_call_scanner.py
=========================

Runs your covered-call checklist against a fixed watchlist of names you
hold (or are considering writing calls against), using live data from
Interactive Brokers (TWS API).

This is a SEPARATE script from credit_spread_scanner.py on purpose: a
covered call isn't a directional bet like a credit spread, it's a
decision about a position you already own.

You place these at ~0.20 delta by default and only move toward
~0.30-0.40 delta when you're deliberately trying to get called away, so
this script doesn't bother suggesting strikes. What it answers instead
is the question that actually matters week to week: is this a good or
bad time to write a call on this name at all, and why.

ONE-TIME SETUP
---------------
Same as credit_spread_scanner.py -- if you already have that one running,
you're already set up:
1. IB Gateway or TWS installed, logged in, API access enabled under
   Configure/File -> Global Configuration -> API -> Settings
     - "Enable ActiveX and Socket Clients" checked
     - Socket port noted (TWS live 7496, TWS paper 7497, Gateway live
       4001, Gateway paper 4002)
     - 127.0.0.1 trusted (or "Allow connections from localhost only" on)
2. pip install ib_async pandas numpy openpyxl
3. Leave TWS/IB Gateway open and logged in while this runs.

Uses a different IB_CLIENT_ID than the credit spread script so the two
can, in principle, run back to back without colliding.

RUNNING IT
----------
    python covered_call_scanner.py

Prints a table plus a per-ticker verdict with reasons, and saves both
covered_call_scan_<date>.csv (raw data) and covered_call_scan_<date>.xlsx
(formatted: color-coded factors, wrapped text, frozen header/ticker
column, autofilter) next to this script.

THE SELL/HOLD VERDICT
-----------------------
Since you write at a conservative ~0.20 delta specifically to keep the
shares, the real risk isn't the stock falling (you keep the premium and
the stock either way) -- it's the stock ripping higher through your
strike while you gave away the upside for a comparatively small credit.
So the verdict weighs three things:

  1. Is the premium actually worth collecting?
     IV Rank at/above IV_RANK_RICH -> yes, richer credit for the same
     strike distance. Below IV_RANK_THIN -> you're giving up upside for
     very little in return.
  2. Is there room before your strike would even be threatened?
     Uses %B (0 = lower Bollinger Band, 1 = upper band) as a proxy for
     how close price already is to where a ~0.20-delta strike typically
     sits. Above BAND_CUSHION_MAX means the stock is already stretched
     toward the band, so a normally-safe 0.20 delta has less room than
     usual.
  3. Is a breakout actively happening right now?
     A green day + a volume spike + RSI pushing into overbought territory
     together suggest real upward momentum, which is exactly when a call
     is most likely to get tested or exercised. Any one of these alone
     is normal; all three together is the pattern to be wary of.

Each ticker gets a score out of 3 and a plain-language "Why" line listing
what's working for the trade and what's working against it, plus a
separate ex-dividend callout (not scored, since the data isn't always
available) since an ITM call into an ex-div date raises early-assignment
risk regardless of anything else.

This intentionally does not pull the option chain to find your exact
0.20-delta strike -- that needs live options greeks, which many
accounts' data subscriptions don't cover cleanly. You already know how
to find that strike once you've decided it's a good week to sell; this
just tells you whether it is one.

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
    print("Missing dependency. Install with:  pip install ib_async pandas numpy openpyxl")
    sys.exit(1)

try:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    print("Missing dependency. Install with:  pip install ib_async pandas numpy openpyxl")
    sys.exit(1)


# ---------------------------------------------------------------------------
# CONFIG -- edit this block to taste
# ---------------------------------------------------------------------------

TICKERS = ["GOOGL", "JEPQ", "MSFT", "NVO", "PEP", "SOFI", "V", "XLE"]

# Optional: fill in shares you hold so the script can tell you your max
# contracts (1 per 100 shares). Leave as None for anything you don't want
# tracked -- it'll just print "n/a".
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

IB_HOST = "127.0.0.1"
IB_PORT = 7496          # 7496 TWS live | 7497 TWS paper | 4001 Gateway live | 4002 Gateway paper
IB_CLIENT_ID = 27       # different from credit_spread_scanner.py's clientId, in case both ever run close together

USE_DELAYED_DATA = True     # True = works without a live data subscription
LOOKBACK_FOR_MAS = "260 D"  # need ~252 trading days for a 200-day MA
LOOKBACK_FOR_IV = "1 Y"     # window IV Rank is computed over

RSI_LEN = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

BOLLINGER_LEN = 20
BOLLINGER_STDEV = 2

IV_RANK_RICH = 50   # IV Rank at/above this -> "Rich" premium environment
IV_RANK_THIN = 25   # IV Rank at/below this -> "Thin" premium environment

VOLUME_SPIKE_MULT = 1.5   # today's volume vs 20-day average to call it "elevated"

KEEP_DELTA_TARGET = 0.20   # your default -- reference only, this script doesn't pick strikes
EXIT_DELTA_TARGET = 0.35   # midpoint of the ~0.30-0.40 range you move to when trying to exit

BAND_CUSHION_MAX = 0.85   # %B above this = price already stretched toward the upper band
BREAKOUT_RSI = 60         # RSI at/above this, combined with a green day + volume spike, = active breakout

REQUEST_PAUSE_SEC = 1.0   # be polite to IBKR's pacing limits between requests
DIVIDEND_WAIT_SEC = 2.0   # time to let the dividend tick populate

MAX_FILENAME_ATTEMPTS = 20   # how many _1, _2, ... suffixes to try before giving up


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


def rsi_read(value: float) -> str:
    if np.isnan(value):
        return "n/a"
    if value >= RSI_OVERBOUGHT:
        return "Overbought"
    if value <= RSI_OVERSOLD:
        return "Oversold"
    return "Neutral"


def premium_label(iv_r: float) -> str:
    if np.isnan(iv_r):
        return "n/a"
    if iv_r >= IV_RANK_RICH:
        return "Rich"
    if iv_r <= IV_RANK_THIN:
        return "Thin"
    return "Average"


# ---------------------------------------------------------------------------
# Per-symbol scan
# ---------------------------------------------------------------------------

def scan_symbol(ib: IB, symbol: str) -> dict:
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

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
    df["sma200"] = df["close"].rolling(200).mean()
    df["upper_bb"], df["mid_bb"], df["lower_bb"] = bollinger_bands(
        df["close"], BOLLINGER_LEN, BOLLINGER_STDEV
    )
    df["avg_vol20"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]
    sma20_5ago = df["sma20"].iloc[-6] if len(df) > 6 else np.nan

    iv_r = float("nan")
    if iv_bars:
        iv_df = util.df(iv_bars)
        iv_r = iv_rank(iv_df["close"])

    green_day = last["close"] > prev["close"]
    volume_spike = last["volume"] > VOLUME_SPIKE_MULT * last["avg_vol20"]
    sma20_rising = (not np.isnan(sma20_5ago)) and last["sma20"] > sma20_5ago
    vs_50 = "above" if (not np.isnan(last["sma50"]) and last["close"] > last["sma50"]) else "below"
    vs_200 = (
        "above" if (not np.isnan(last["sma200"]) and last["close"] > last["sma200"]) else "below"
        if not np.isnan(last["sma200"]) else "n/a (needs 200 days)"
    )

    band_width = last["upper_bb"] - last["lower_bb"]
    percent_b = (last["close"] - last["lower_bb"]) / band_width if band_width else float("nan")

    # Best-effort ex-dividend lookup. Some symbols / data permissions won't
    # return this -- that's fine, it just prints "n/a" rather than failing
    # the whole ticker.
    next_ex_div = "n/a"
    try:
        div_ticker = ib.reqMktData(contract, genericTickList="456", snapshot=False)
        ib.sleep(DIVIDEND_WAIT_SEC)
        if div_ticker.dividends and div_ticker.dividends.nextDate:
            next_ex_div = str(div_ticker.dividends.nextDate)
        ib.cancelMktData(contract)
    except Exception:
        pass

    shares = SHARES_OWNED.get(symbol)
    max_contracts = shares // 100 if isinstance(shares, (int, float)) else "n/a"

    # --- Sell/hold verdict -------------------------------------------------
    # Three independent checks, each answering a different question about
    # whether NOW is a good moment to write a ~0.20-delta call and give up
    # the upside above it.
    iv_rich = not np.isnan(iv_r) and iv_r >= IV_RANK_RICH
    iv_thin = not np.isnan(iv_r) and iv_r <= IV_RANK_THIN
    band_cushion_ok = np.isnan(percent_b) or percent_b <= BAND_CUSHION_MAX
    breakout_in_progress = bool(green_day and volume_spike and last["rsi"] >= BREAKOUT_RSI)

    # Each factor gets its own status ("good"/"neutral"/"bad", used for the
    # Excel color fill) and its own plain-language sentence, kept separate
    # rather than merged into one paragraph.
    if iv_rich:
        premium_status, premium_why = "good", f"IV Rank is rich ({iv_r}). Premium is worth collecting."
    elif iv_thin:
        premium_status, premium_why = "bad", f"IV Rank is thin ({iv_r}). You'd give up upside for very little credit."
    elif np.isnan(iv_r):
        premium_status, premium_why = "neutral", "IV Rank unavailable for this symbol."
    else:
        premium_status, premium_why = "neutral", f"IV Rank is average ({iv_r}). Premium is reasonable but not rich."

    if np.isnan(percent_b):
        band_status, band_why = "neutral", "Bollinger data unavailable."
    elif band_cushion_ok:
        band_status, band_why = "good", f"Plenty of room below the upper band (%B {round(percent_b, 2)})."
    else:
        band_status, band_why = "bad", f"Price is stretched toward the upper band (%B {round(percent_b, 2)}). Less cushion than usual."

    if breakout_in_progress:
        breakout_status = "bad"
        breakout_why = (
            f"Green day, volume spike, and RSI {round(last['rsi'], 1)} together look like a breakout. "
            "Raises the odds this gets called away."
        )
    else:
        breakout_status, breakout_why = "good", "No active breakout. Momentum looks normal."

    sell_score = int(iv_rich) + int(band_cushion_ok) + int(not breakout_in_progress)

    if sell_score == 3:
        verdict = "Good week to sell"
    elif sell_score == 2:
        verdict = "OK, minor caveat"
    elif sell_score == 1:
        verdict = "Weak, lean toward waiting"
    else:
        verdict = "Bad week to sell"

    next_ex_div_display = (
        f"{next_ex_div} (confirm it lands after your expiration)" if next_ex_div != "n/a" else "n/a"
    )

    return {
        "Ticker": symbol,
        "Close": round(last["close"], 2),
        "Verdict": verdict,
        "Sell Score": f"{sell_score}/3",
        "Why: Premium": premium_why,
        "Why: Band Cushion": band_why,
        "Why: Breakout": breakout_why,
        "Green Day": "Y" if green_day else "N",
        "RSI": round(last["rsi"], 1),
        "RSI Read": rsi_read(last["rsi"]),
        "20MA Trend": "rising" if sma20_rising else "flat/falling",
        "vs 50MA": vs_50,
        "vs 200MA": vs_200,
        "%B": round(percent_b, 2) if not np.isnan(percent_b) else float("nan"),
        "Vol vs 20D Avg": f"{last['volume'] / last['avg_vol20']:.2f}x" if last["avg_vol20"] else "n/a",
        "Volume Spike": "Y" if volume_spike else "N",
        "IV Rank": iv_r,
        "Premium": premium_label(iv_r),
        "Next Ex-Div": next_ex_div_display,
        "Max Contracts": max_contracts,
        "_sort_score": sell_score,
        "_premium_status": premium_status,
        "_band_status": band_status,
        "_breakout_status": breakout_status,
    }


# ---------------------------------------------------------------------------
# Formatted Excel export
# ---------------------------------------------------------------------------

# Header-cell descriptions (shown as a hover comment on row 1), and the
# hidden status column each Why column pulls its color from.
_WHY_COLUMNS = {
    "Why: Premium": (
        "_premium_status",
        "Is IV Rank rich enough that the premium is worth collecting? "
        "Rich = IV Rank >= IV_RANK_RICH, Thin = IV Rank <= IV_RANK_THIN.",
    ),
    "Why: Band Cushion": (
        "_band_status",
        "How much room is left before price reaches the upper Bollinger Band, "
        "used as a proxy for how close a ~0.20-delta strike is to being tested. "
        "Flags when %B is above BAND_CUSHION_MAX.",
    ),
    "Why: Breakout": (
        "_breakout_status",
        "Flags an active breakout: a green day, a volume spike, and RSI at/above "
        "BREAKOUT_RSI all at once -- the pattern most likely to get your shares "
        "called away.",
    ),
}
_VERDICT_COMMENT = (
    "Sell Score out of 3, combining the three Why columns: "
    "Good week to sell (3/3), OK minor caveat (2/3), "
    "Weak lean toward waiting (1/3), or Bad week to sell (0/3)."
)

_STATUS_FILL = {
    "good": PatternFill("solid", fgColor="C6EFCE"),
    "bad": PatternFill("solid", fgColor="FFC7CE"),
    "neutral": PatternFill("solid", fgColor="FFEB9C"),
}
_STATUS_FONT = {
    "good": Font(name="Arial", color="006100"),
    "bad": Font(name="Arial", color="9C0006"),
    "neutral": Font(name="Arial", color="9C6500"),
}
_VERDICT_STYLE = {
    "Good week to sell": ("C6EFCE", "006100"),
    "OK, minor caveat": ("FFEB9C", "9C6500"),
    "Weak, lean toward waiting": ("FFD9B3", "9C5700"),
    "Bad week to sell": ("FFC7CE", "9C0006"),
}

_COLUMN_WIDTHS = {
    "Ticker": 10, "Close": 10, "Verdict": 30, "Sell Score": 12,
    "Why: Premium": 42, "Why: Band Cushion": 42, "Why: Breakout": 42,
    "Green Day": 10, "RSI": 8, "RSI Read": 12, "20MA Trend": 14,
    "vs 50MA": 10, "vs 200MA": 14, "%B": 8, "Vol vs 20D Avg": 14,
    "Volume Spike": 12, "IV Rank": 10, "Premium": 10, "Next Ex-Div": 32,
    "Max Contracts": 14,
}


def write_excel_report(results: pd.DataFrame, out_path: str) -> None:
    """Write the formatted .xlsx version: color-coded factor columns, wrapped
    text, a 30-wide Verdict column, frozen header row + Ticker column, and
    an autofilter. The plain .csv is still written separately for anything
    that just wants raw data."""
    display_cols = [c for c in results.columns if not c.startswith("_")]

    wb = Workbook()
    ws = wb.active
    ws.title = "Covered Call Scan"

    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="44546A")
    header_align = Alignment(wrap_text=True, vertical="center", horizontal="center")
    body_align = Alignment(wrap_text=True, vertical="top")

    for col_idx, col_name in enumerate(display_cols, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        if col_name in _WHY_COLUMNS:
            cell.comment = Comment(_WHY_COLUMNS[col_name][1], "Covered Call Scanner")
        elif col_name == "Verdict":
            cell.comment = Comment(_VERDICT_COMMENT, "Covered Call Scanner")

    for row_idx, (_, row) in enumerate(results.iterrows(), start=2):
        for col_idx, col_name in enumerate(display_cols, start=1):
            value = row[col_name]
            if isinstance(value, float) and np.isnan(value):
                value = "n/a"
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = Font(name="Arial")
            cell.alignment = body_align

        for col_name, (status_key, _desc) in _WHY_COLUMNS.items():
            status = row.get(status_key)
            if status in _STATUS_FILL:
                col_idx = display_cols.index(col_name) + 1
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.fill = _STATUS_FILL[status]
                cell.font = _STATUS_FONT[status]

        verdict = row.get("Verdict")
        if verdict in _VERDICT_STYLE:
            fill_hex, font_hex = _VERDICT_STYLE[verdict]
            col_idx = display_cols.index("Verdict") + 1
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.fill = PatternFill("solid", fgColor=fill_hex)
            cell.font = Font(name="Arial", bold=True, color=font_hex)

    for col_idx, col_name in enumerate(display_cols, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = _COLUMN_WIDTHS.get(col_name, 16)

    ws.row_dimensions[1].height = 46
    for row_idx in range(2, len(results) + 2):
        ws.row_dimensions[row_idx].height = 60

    # Freezes both the header row and column A (Ticker) together.
    ws.freeze_panes = "B2"

    last_col = get_column_letter(len(display_cols))
    ws.auto_filter.ref = f"A1:{last_col}{len(results) + 1}"

    wb.save(out_path)


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
            rows.append({"Ticker": symbol, "Premium": f"ERROR: {exc}"})

    ib.disconnect()

    results = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", None)

    # Best names to write against first, worst last.
    if "_sort_score" in results.columns:
        results["_sort_score"] = results["_sort_score"].fillna(-1)
        results = results.sort_values("_sort_score", ascending=False)

    print("\n" + "=" * 100)
    print(f"Covered call scan -- {dt.date.today().isoformat()}")
    print("=" * 100)
    table_cols = [c for c in results.columns if c not in ("Why: Premium", "Why: Band Cushion", "Why: Breakout") and not c.startswith("_")]
    print(results[table_cols].to_string(index=False))

    if "Verdict" in results.columns:
        print("\nWhy, ticker by ticker (best to worst):")
        for _, r in results.iterrows():
            if pd.isna(r.get("Verdict")):
                print(f"  {r['Ticker']}: skipped -- {r.get('Premium')}")
                continue
            print(f"  {r['Ticker']} -- {r['Verdict']} ({r['Sell Score']})")
            print(f"      Premium:      {r['Why: Premium']}")
            print(f"      Band Cushion: {r['Why: Band Cushion']}")
            print(f"      Breakout:     {r['Why: Breakout']}")

    helper_cols = [c for c in results.columns if c.startswith("_")]
    clean_results = results.drop(columns=helper_cols, errors="ignore")

    csv_path = f"covered_call_scan_{dt.date.today().isoformat()}.csv"
    csv_saved_path = save_with_retry(lambda p: clean_results.to_csv(p, index=False), csv_path)
    print(f"\nSaved raw data to {csv_saved_path}")

    xlsx_path = f"covered_call_scan_{dt.date.today().isoformat()}.xlsx"
    xlsx_saved_path = save_with_retry(lambda p: write_excel_report(results, p), xlsx_path)
    print(f"Saved formatted report to {xlsx_saved_path}")


if __name__ == "__main__":
    main()

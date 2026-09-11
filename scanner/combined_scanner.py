#!/usr/bin/env python3
"""
combined_scanner.py
=========================

Scores all four strategies (Credit Spreads, Debit Spreads, Covered Calls,
LEAPS) by reading from the local market_data.db cache that
market_data_daemon.py keeps current. No historical data requests happen
here at all, which means no IBKR pacing limit to think about and a scan
that finishes in under a second instead of tens of minutes.

THIS SCRIPT DOES NOT FETCH HISTORICAL DATA
-----------------------------------------------
market_data_daemon.py is the only thing that talks to IBKR for price and
IV history. This script just reads what the daemon has already cached.
If the daemon isn't running, or hasn't updated recently enough, this
script REFUSES TO SCAN and tells you which tickers are stale rather than
silently scoring on old numbers. Start market_data_daemon.py first (and
leave it running), then run this.

The one thing this script still talks to IBKR for directly is the covered
call ex-dividend lookup, a live snapshot request (not historical data, so
it isn't subject to the pacing limit), made only for the 8 covered call
tickers. If IBKR isn't reachable for that, the scan still runs, ex-div
just shows "n/a" for everyone, same graceful fallback as before.

OUTPUT FILES
-------------------------------------------------------------------------
Every scan now saves both an unrefined .csv (the raw scored rows, easy to
pull into anything else) and a formatted .xlsx (color-coded, wrapped
text, frozen header, autofilter -- meant to actually be read):
  credit_spread_scan_<date>.csv   (+ matching .xlsx)
  debit_spread_scan_<date>.csv    (+ matching .xlsx)
  covered_call_scan_<date>.csv    (+ matching .xlsx)
  leaps_scan_<date>.csv           (+ matching .xlsx)

RUNNING IT
----------
    1. python market_data_daemon.py     (leave running in its own window)
    2. python combined_scanner.py       (run anytime you want a scan)

TUNE ME
--------
All tickers and thresholds live in scanner_config.py now, not in this
file. Edit that one file for watchlists, thresholds, or connection
settings.
"""

from __future__ import annotations

import datetime as dt
import sys

import numpy as np
import pandas as pd

import scanner_config as cfg
import scanner_utils as su
import market_data_cache as cache

try:
    from ib_async import IB, Stock
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
# CREDIT SPREAD scoring (unchanged logic, now fed from the cache)
# ---------------------------------------------------------------------------

def score_credit_spread(symbol: str, d: dict) -> dict:
    last, prev, iv_r, percent_b = d["last"], d["prev"], d["iv_r"], d["percent_b"]

    green_day = last["close"] > prev["close"]
    volume_spike = last["volume"] > cfg.VOLUME_SPIKE_MULT * last["avg_vol20"]
    uptrend = last["sma20"] > last["sma50"]
    near_lower_band = last["close"] <= last["lower_bb"] * 1.01
    near_upper_band = last["close"] >= last["upper_bb"] * 0.99
    rsi_turning_up = last["rsi"] > prev["rsi"]
    rsi_turning_down = last["rsi"] < prev["rsi"]
    iv_elevated = not np.isnan(iv_r) and iv_r >= cfg.CREDIT_IV_RANK_ELEVATED

    bull_conditions = [
        ("RSI oversold + turning up", bool(last["rsi"] < cfg.CREDIT_RSI_OVERSOLD and rsi_turning_up)),
        ("Near lower band", bool(near_lower_band)),
        ("Uptrend (20>50MA)", bool(uptrend)),
        ("IV Rank elevated", bool(iv_elevated)),
    ]
    bear_conditions = [
        ("RSI overbought + turning down", bool(last["rsi"] > cfg.CREDIT_RSI_OVERBOUGHT and rsi_turning_down)),
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
# DEBIT SPREAD scoring (unchanged logic, now fed from the cache)
# ---------------------------------------------------------------------------

def score_debit_spread(symbol: str, d: dict) -> dict:
    last, prev, iv_r, percent_b = d["last"], d["prev"], d["iv_r"], d["percent_b"]

    green_day = last["close"] > prev["close"]
    volume_spike = last["volume"] > cfg.VOLUME_SPIKE_MULT * last["avg_vol20"]
    uptrend = last["sma20"] > last["sma50"]
    rsi_turning_up = last["rsi"] > prev["rsi"]
    rsi_turning_down = last["rsi"] < prev["rsi"]
    iv_cheap = not np.isnan(iv_r) and iv_r <= cfg.DEBIT_IV_RANK_CHEAP

    bull_band_ok = (not np.isnan(percent_b)) and cfg.DEBIT_BAND_ZONE_BULL[0] <= percent_b <= cfg.DEBIT_BAND_ZONE_BULL[1]
    bear_band_ok = (not np.isnan(percent_b)) and cfg.DEBIT_BAND_ZONE_BEAR[0] <= percent_b <= cfg.DEBIT_BAND_ZONE_BEAR[1]
    rsi_bull_zone = cfg.DEBIT_RSI_BULL_MIN <= last["rsi"] <= cfg.DEBIT_RSI_BULL_MAX and rsi_turning_up
    rsi_bear_zone = cfg.DEBIT_RSI_BEAR_MIN <= last["rsi"] <= cfg.DEBIT_RSI_BEAR_MAX and rsi_turning_down

    bull_conditions = [
        ("Uptrend (20>50MA)", bool(uptrend)),
        (f"RSI constructive+rising ({cfg.DEBIT_RSI_BULL_MIN}-{cfg.DEBIT_RSI_BULL_MAX})", bool(rsi_bull_zone)),
        (f"Room to run (%B {cfg.DEBIT_BAND_ZONE_BULL[0]}-{cfg.DEBIT_BAND_ZONE_BULL[1]})", bool(bull_band_ok)),
        ("IV Rank cheap", bool(iv_cheap)),
    ]
    bear_conditions = [
        ("Downtrend (20<50MA)", bool(not uptrend)),
        (f"RSI constructive+falling ({cfg.DEBIT_RSI_BEAR_MIN}-{cfg.DEBIT_RSI_BEAR_MAX})", bool(rsi_bear_zone)),
        (f"Room to fall (%B {cfg.DEBIT_BAND_ZONE_BEAR[0]}-{cfg.DEBIT_BAND_ZONE_BEAR[1]})", bool(bear_band_ok)),
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
# LEAPS scoring (unchanged logic, now fed from the cache)
# ---------------------------------------------------------------------------

def score_leaps(symbol: str, d: dict) -> dict:
    last, prev, iv_r, percent_b = d["last"], d["prev"], d["iv_r"], d["percent_b"]

    volume_spike = last["volume"] > cfg.VOLUME_SPIKE_MULT * last["avg_vol20"]
    rsi_turning_up = last["rsi"] > prev["rsi"]
    rsi_turning_down = last["rsi"] < prev["rsi"]
    iv_cheap = not np.isnan(iv_r) and iv_r <= cfg.LEAPS_IV_RANK_CHEAP

    has_200 = not np.isnan(last["sma200"])
    golden_trend = has_200 and last["sma50"] > last["sma200"] and last["close"] > last["sma200"]
    death_trend = has_200 and last["sma50"] < last["sma200"] and last["close"] < last["sma200"]
    extension = (last["close"] - last["sma200"]) / last["sma200"] if has_200 else float("nan")
    not_overextended_up = has_200 and extension <= cfg.LEAPS_EXTENSION_MAX
    not_overextended_down = has_200 and -extension <= cfg.LEAPS_EXTENSION_MAX

    rsi_bull_zone = cfg.LEAPS_RSI_BULL_MIN <= last["rsi"] <= cfg.LEAPS_RSI_BULL_MAX and rsi_turning_up
    rsi_bear_zone = cfg.LEAPS_RSI_BEAR_MIN <= last["rsi"] <= cfg.LEAPS_RSI_BEAR_MAX and rsi_turning_down

    bull_conditions = [
        ("Golden trend (50>200MA, price>200MA)", bool(golden_trend)),
        (f"RSI constructive+rising ({cfg.LEAPS_RSI_BULL_MIN}-{cfg.LEAPS_RSI_BULL_MAX})", bool(rsi_bull_zone)),
        (f"Not overextended vs 200MA (<{int(cfg.LEAPS_EXTENSION_MAX * 100)}%)", bool(not_overextended_up)),
        ("IV Rank cheap", bool(iv_cheap)),
    ]
    bear_conditions = [
        ("Death trend (50<200MA, price<200MA)", bool(death_trend)),
        (f"RSI constructive+falling ({cfg.LEAPS_RSI_BEAR_MIN}-{cfg.LEAPS_RSI_BEAR_MAX})", bool(rsi_bear_zone)),
        (f"Not overextended vs 200MA (<{int(cfg.LEAPS_EXTENSION_MAX * 100)}%)", bool(not_overextended_down)),
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
# COVERED CALL scoring (unchanged logic, now fed from the cache -- still
# makes a live IBKR snapshot request for the ex-dividend lookup only)
# ---------------------------------------------------------------------------

def score_covered_call(ib, symbol: str, d: dict) -> dict:
    last, prev, iv_r, percent_b = d["last"], d["prev"], d["iv_r"], d["percent_b"]
    sma20_5ago = d["sma20_5ago"]

    green_day = last["close"] > prev["close"]
    volume_spike = last["volume"] > cfg.VOLUME_SPIKE_MULT * last["avg_vol20"]
    sma20_rising = (not np.isnan(sma20_5ago)) and last["sma20"] > sma20_5ago
    vs_50 = "above" if (not np.isnan(last["sma50"]) and last["close"] > last["sma50"]) else "below"
    vs_200 = (
        "above" if (not np.isnan(last["sma200"]) and last["close"] > last["sma200"]) else "below"
        if not np.isnan(last["sma200"]) else "n/a (needs 200 days)"
    )

    # Best-effort ex-dividend lookup. A live snapshot request, not
    # historical data, so it isn't subject to the pacing limit. Degrades
    # to "n/a" if ib is None (couldn't connect) or the lookup fails for
    # this symbol, same graceful fallback as the standalone script had.
    next_ex_div = "n/a"
    if ib is not None:
        try:
            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)
            div_ticker = ib.reqMktData(contract, genericTickList="456", snapshot=False)
            ib.sleep(cfg.DIVIDEND_WAIT_SEC)
            if div_ticker.dividends and div_ticker.dividends.nextDate:
                next_ex_div = str(div_ticker.dividends.nextDate)
            ib.cancelMktData(contract)
        except Exception:
            pass

    shares = cfg.SHARES_OWNED.get(symbol)
    max_contracts = shares // 100 if isinstance(shares, (int, float)) else "n/a"

    # --- Sell/hold verdict -------------------------------------------------
    iv_rich = not np.isnan(iv_r) and iv_r >= cfg.COVERED_IV_RANK_RICH
    iv_thin = not np.isnan(iv_r) and iv_r <= cfg.COVERED_IV_RANK_THIN
    band_cushion_ok = np.isnan(percent_b) or percent_b <= cfg.COVERED_BAND_CUSHION_MAX
    breakout_in_progress = bool(green_day and volume_spike and last["rsi"] >= cfg.COVERED_BREAKOUT_RSI)

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
        "RSI Read": su.rsi_read(last["rsi"]),
        "20MA Trend": "rising" if sma20_rising else "flat/falling",
        "vs 50MA": vs_50,
        "vs 200MA": vs_200,
        "%B": round(percent_b, 2) if not np.isnan(percent_b) else float("nan"),
        "Vol vs 20D Avg": f"{last['volume'] / last['avg_vol20']:.2f}x" if last["avg_vol20"] else "n/a",
        "Volume Spike": "Y" if volume_spike else "N",
        "IV Rank": iv_r,
        "Premium": su.premium_label(iv_r),
        "Next Ex-Div": next_ex_div_display,
        "Max Contracts": max_contracts,
        "_sort_score": sell_score,
        "_premium_status": premium_status,
        "_band_status": band_status,
        "_breakout_status": breakout_status,
    }


# ---------------------------------------------------------------------------
# Covered call formatted Excel export (unchanged)
# ---------------------------------------------------------------------------

_WHY_COLUMNS = {
    "Why: Premium": (
        "_premium_status",
        "Is IV Rank rich enough that the premium is worth collecting? "
        "Rich = IV Rank >= COVERED_IV_RANK_RICH, Thin = IV Rank <= COVERED_IV_RANK_THIN.",
    ),
    "Why: Band Cushion": (
        "_band_status",
        "How much room is left before price reaches the upper Bollinger Band, "
        "used as a proxy for how close a ~0.20-delta strike is to being tested. "
        "Flags when %B is above COVERED_BAND_CUSHION_MAX.",
    ),
    "Why: Breakout": (
        "_breakout_status",
        "Flags an active breakout: a green day, a volume spike, and RSI at/above "
        "COVERED_BREAKOUT_RSI all at once -- the pattern most likely to get your shares "
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
    an autofilter."""
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
            cell.comment = Comment(_WHY_COLUMNS[col_name][1], "Combined Scanner")
        elif col_name == "Verdict":
            cell.comment = Comment(_VERDICT_COMMENT, "Combined Scanner")

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

    ws.freeze_panes = "B2"

    last_col = get_column_letter(len(display_cols))
    ws.auto_filter.ref = f"A1:{last_col}{len(results) + 1}"

    wb.save(out_path)


# ---------------------------------------------------------------------------
# Formatted Excel export for the three flag-style scans (Credit Spread,
# Debit Spread, LEAPS) -- same visual language as the covered call report
# above (dark header, wrapped text, frozen panes, autofilter), with
# color-coding tuned to what these three actually show: Bull/Bear Score
# and Setup Flag instead of a Verdict and three Why columns.
# ---------------------------------------------------------------------------

_FLAG_COLUMN_WIDTHS = {
    "Ticker": 10, "Close": 10, "Green Day": 10, "RSI": 8,
    "20MA vs 50MA": 14, "50MA vs 200MA": 14, "% vs 200MA": 12,
    "Bollinger": 14, "%B": 8, "Vol vs 20D Avg": 14, "Volume Spike": 12,
    "IV Rank": 10, "Bull Score": 11, "Bull Reasons": 46,
    "Bear Score": 11, "Bear Reasons": 46, "Setup Flag": 24,
}

_FLAG_COMMENTS = {
    "Bull Score": "How many of the 4 bullish conditions hit (see Bull Reasons). 3+ triggers the Setup Flag.",
    "Bear Score": "How many of the 4 bearish conditions hit (see Bear Reasons). 3+ triggers the Setup Flag.",
    "Setup Flag": "Set when Bull Score or Bear Score reaches 3 out of 4. A 2-of-4 near-miss is called out in the terminal output but not flagged here.",
}


def write_flag_scan_excel_report(results: pd.DataFrame, out_path: str, sheet_title: str) -> None:
    """Write the formatted .xlsx version of a Credit Spread / Debit Spread /
    LEAPS scan: color-coded Bull/Bear Score and Setup Flag cells, wrapped
    text, frozen header row + Ticker column, and an autofilter."""
    display_cols = [c for c in results.columns if not c.startswith("_")]

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title

    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="44546A")
    header_align = Alignment(wrap_text=True, vertical="center", horizontal="center")
    body_align = Alignment(wrap_text=True, vertical="top")

    for col_idx, col_name in enumerate(display_cols, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        if col_name in _FLAG_COMMENTS:
            cell.comment = Comment(_FLAG_COMMENTS[col_name], "Combined Scanner")

    for row_idx, (_, row) in enumerate(results.iterrows(), start=2):
        for col_idx, col_name in enumerate(display_cols, start=1):
            value = row[col_name]
            if isinstance(value, float) and np.isnan(value):
                value = "n/a"
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = Font(name="Arial")
            cell.alignment = body_align

        for score_col in ("Bull Score", "Bear Score"):
            if score_col in display_cols:
                score = row.get(score_col)
                if isinstance(score, (int, float)) and not (isinstance(score, float) and np.isnan(score)):
                    col_idx = display_cols.index(score_col) + 1
                    cell = ws.cell(row=row_idx, column=col_idx)
                    if score >= 3:
                        cell.fill = _STATUS_FILL["good"]
                        cell.font = _STATUS_FONT["good"]
                    elif score == 2:
                        cell.fill = _STATUS_FILL["neutral"]
                        cell.font = _STATUS_FONT["neutral"]

        if "Setup Flag" in display_cols:
            flag = row.get("Setup Flag")
            col_idx = display_cols.index("Setup Flag") + 1
            cell = ws.cell(row=row_idx, column=col_idx)
            if isinstance(flag, str) and flag.startswith("ERROR"):
                cell.fill = _STATUS_FILL["bad"]
                cell.font = _STATUS_FONT["bad"]
            elif isinstance(flag, str) and flag != "-":
                cell.fill = _STATUS_FILL["good"]
                cell.font = Font(name="Arial", bold=True, color="006100")

    for col_idx, col_name in enumerate(display_cols, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = _FLAG_COLUMN_WIDTHS.get(col_name, 16)

    ws.row_dimensions[1].height = 46
    for row_idx in range(2, len(results) + 2):
        ws.row_dimensions[row_idx].height = 60

    ws.freeze_panes = "B2"

    last_col = get_column_letter(len(display_cols))
    ws.auto_filter.ref = f"A1:{last_col}{len(results) + 1}"

    wb.save(out_path)


# ---------------------------------------------------------------------------
# Shared report printing/saving for the three flag-style scanners
# ---------------------------------------------------------------------------

def report_flag_scan(results: pd.DataFrame, title: str, flag_values: list, out_prefix: str, sheet_title: str) -> None:
    if results.empty:
        print(f"\n{title} -- {dt.date.today().isoformat()}: no tickers to scan.")
        return

    if "Bull Score" in results.columns:
        results = results.copy()
        results["_rank"] = results[["Bull Score", "Bear Score"]].max(axis=1).fillna(-1)
        results = results.sort_values("_rank", ascending=False).drop(columns="_rank")

    print("\n" + "=" * 100)
    print(f"{title} -- {dt.date.today().isoformat()}")
    print("=" * 100)
    display_cols = [c for c in results.columns if c not in ("Bull Reasons", "Bear Reasons")]
    print(results[display_cols].to_string(index=False))

    flagged = results[results["Setup Flag"].isin(flag_values)]
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

    csv_path = f"{out_prefix}_{dt.date.today().isoformat()}.csv"
    csv_saved_path = su.save_with_retry(lambda p: results.to_csv(p, index=False), csv_path)
    print(f"\nSaved raw data to {csv_saved_path}")

    xlsx_path = f"{out_prefix}_{dt.date.today().isoformat()}.xlsx"
    xlsx_saved_path = su.save_with_retry(lambda p: write_flag_scan_excel_report(results, p, sheet_title), xlsx_path)
    print(f"Saved formatted report to {xlsx_saved_path}")


# ---------------------------------------------------------------------------
# Freshness gate
# ---------------------------------------------------------------------------

def _abort_if_stale(conn) -> None:
    freshness = cache.check_freshness(conn, cfg.ALL_TICKERS)
    stale = {t: info for t, info in freshness.items() if not (info["price_ok"] and info["iv_ok"])}
    if not stale:
        return

    print("Cache is stale, refusing to scan. The following tickers aren't current:\n")
    for ticker, info in stale.items():
        price_msg = info["price_date"] or "never cached"
        iv_msg = info["iv_date"] or "never cached"
        print(f"  {ticker}: price last {price_msg}, IV last {iv_msg}")
    print(f"\n{len(stale)} of {len(cfg.ALL_TICKERS)} tickers are stale (more than "
          f"{cfg.MAX_STALE_DAYS} day(s) old).")
    print("Start market_data_daemon.py, let it catch up, then run this again.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = cache.get_connection()
    _abort_if_stale(conn)

    print(f"Cache is fresh for all {len(cfg.ALL_TICKERS)} tickers. Scoring from {cfg.DB_PATH}...")

    data_cache: dict = {}
    read_errors: dict = {}
    for ticker in cfg.ALL_TICKERS:
        try:
            price_df = cache.read_price_history(conn, ticker)
            iv_df = cache.read_iv_history(conn, ticker)
            data_cache[ticker] = su.compute_snapshot(price_df, iv_df)
        except Exception as exc:
            read_errors[ticker] = str(exc)
    conn.close()

    # --- Credit spread -------------------------------------------------
    credit_rows = []
    for symbol in cfg.CREDIT_TICKERS:
        if symbol in read_errors:
            credit_rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {read_errors[symbol]}"})
            continue
        try:
            credit_rows.append(score_credit_spread(symbol, data_cache[symbol]))
        except Exception as exc:
            credit_rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {exc}"})
    report_flag_scan(
        pd.DataFrame(credit_rows),
        "Credit spread scan",
        ["PUT credit spread", "CALL credit spread"],
        "credit_spread_scan",
        "Credit Spread Scan",
    )

    # --- Debit spread ----------------------------------------------------
    debit_rows = []
    for symbol in cfg.DEBIT_TICKERS:
        if symbol in read_errors:
            debit_rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {read_errors[symbol]}"})
            continue
        try:
            debit_rows.append(score_debit_spread(symbol, data_cache[symbol]))
        except Exception as exc:
            debit_rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {exc}"})
    report_flag_scan(
        pd.DataFrame(debit_rows),
        "Debit spread scan",
        ["BULL CALL debit spread", "BEAR PUT debit spread"],
        "debit_spread_scan",
        "Debit Spread Scan",
    )

    # --- LEAPS -------------------------------------------------------------
    leaps_rows = []
    for symbol in cfg.LEAPS_TICKERS:
        if symbol in read_errors:
            leaps_rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {read_errors[symbol]}"})
            continue
        try:
            leaps_rows.append(score_leaps(symbol, data_cache[symbol]))
        except Exception as exc:
            leaps_rows.append({"Ticker": symbol, "Setup Flag": f"ERROR: {exc}"})
    report_flag_scan(
        pd.DataFrame(leaps_rows),
        "LEAPS scan",
        ["BULLISH LEAP (long call)", "BEARISH LEAP (long put)"],
        "leaps_scan",
        "LEAPS Scan",
    )

    # --- Covered calls (needs a brief live IBKR connection for ex-div) ---
    ib = IB()
    try:
        print(f"\nConnecting to IBKR at {cfg.IB_HOST}:{cfg.IB_PORT} (clientId={cfg.SCANNER_CLIENT_ID}) "
              f"for ex-dividend lookups...")
        ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=cfg.SCANNER_CLIENT_ID, readonly=True)
        if cfg.USE_DELAYED_DATA:
            ib.reqMarketDataType(3)
    except Exception as exc:
        print(f"  -> couldn't connect ({exc}). Continuing without ex-dividend dates.")
        ib = None

    covered_rows = []
    for symbol in cfg.COVERED_TICKERS:
        if symbol in read_errors:
            covered_rows.append({"Ticker": symbol, "Premium": f"ERROR: {read_errors[symbol]}"})
            continue
        try:
            covered_rows.append(score_covered_call(ib, symbol, data_cache[symbol]))
        except Exception as exc:
            covered_rows.append({"Ticker": symbol, "Premium": f"ERROR: {exc}"})

    if ib is not None:
        ib.disconnect()

    covered_results = pd.DataFrame(covered_rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", None)

    if "_sort_score" in covered_results.columns:
        covered_results["_sort_score"] = covered_results["_sort_score"].fillna(-1)
        covered_results = covered_results.sort_values("_sort_score", ascending=False)

    print("\n" + "=" * 100)
    print(f"Covered call scan -- {dt.date.today().isoformat()}")
    print("=" * 100)
    table_cols = [c for c in covered_results.columns if c not in ("Why: Premium", "Why: Band Cushion", "Why: Breakout") and not c.startswith("_")]
    print(covered_results[table_cols].to_string(index=False))

    if "Verdict" in covered_results.columns:
        print("\nWhy, ticker by ticker (best to worst):")
        for _, r in covered_results.iterrows():
            if pd.isna(r.get("Verdict")):
                print(f"  {r['Ticker']}: skipped -- {r.get('Premium')}")
                continue
            print(f"  {r['Ticker']} -- {r['Verdict']} ({r['Sell Score']})")
            print(f"      Premium:      {r['Why: Premium']}")
            print(f"      Band Cushion: {r['Why: Band Cushion']}")
            print(f"      Breakout:     {r['Why: Breakout']}")

    helper_cols = [c for c in covered_results.columns if c.startswith("_")]
    clean_covered_results = covered_results.drop(columns=helper_cols, errors="ignore")

    csv_path = f"covered_call_scan_{dt.date.today().isoformat()}.csv"
    csv_saved_path = su.save_with_retry(lambda p: clean_covered_results.to_csv(p, index=False), csv_path)
    print(f"\nSaved raw data to {csv_saved_path}")

    xlsx_path = f"covered_call_scan_{dt.date.today().isoformat()}.xlsx"
    xlsx_saved_path = su.save_with_retry(lambda p: write_excel_report(covered_results, p), xlsx_path)
    print(f"Saved formatted report to {xlsx_saved_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
market_data_daemon.py
=========================

A long-lived process that keeps market_data.db current so combined_scanner.py
never has to talk to IBKR for historical data at all.

THIS IS A POLLING DAEMON, NOT A STREAMING ONE
--------------------------------------------------
An earlier version of this script tried to use IBKR's "keep up to date"
historical data mode (reqHistoricalData(..., keepUpToDate=True)) to hold
standing subscriptions that IBKR streams updates into. In testing against
a real account, every single subscription timed out immediately
(ib_async's own "reqHistoricalData: Timeout" followed by IBKR's "Error
366: No historical data query found") no matter which ticker. That
pattern, universal and instant across every symbol, is the signature of
a structural mismatch, not a per-symbol data problem: keepUpToDate needs
a live tick stream to know when to push a bar update, and this setup
intentionally runs on delayed data (USE_DELAYED_DATA = True in
scanner_config.py, so it works without paying for real-time market data
subscriptions). Delayed data has no live feed to drive keepUpToDate, so
the request just hangs until the client gives up.

So this version does the boring, reliable thing instead: it wakes up
every DAEMON_REFRESH_INTERVAL_SEC (scanner_config.py), checks the cache
against today's date, and for any ticker that's actually behind (its
newest cached bar isn't from the most recent trading day yet), makes a
plain one-shot reqHistoricalData call, the exact same call the original
standalone scanners already used successfully on delayed data, no
keepUpToDate involved.

Checking freshness is a local SQLite read, so a poll cycle where nothing
is behind costs zero IBKR requests. In practice: the first run of a new
day does the full pacing-limited pass across every ticker that's fallen
behind (up to the familiar ~15-20 minutes if starting from a cold cache),
and every cycle after that for the rest of the day finds nothing to do
and goes straight back to sleep.

RUNNING IT
----------
    python market_data_daemon.py

Leave it running (a terminal window, or start it alongside TWS/IB
Gateway each morning). Ctrl+C stops it cleanly. combined_scanner.py will
refuse to run and tell you to start this first if the cache looks stale
(see MAX_STALE_DAYS in scanner_config.py).
"""

from __future__ import annotations

import datetime as dt
import sys
import time

import scanner_config as cfg
import scanner_utils as su
import market_data_cache as cache

try:
    from ib_async import IB, Stock
except ImportError:
    print("Missing dependency. Install with:  pip install ib_async pandas numpy")
    sys.exit(1)


def refresh_ticker(ib: IB, conn, ticker: str, needs: dict) -> None:
    """Re-fetch whichever of price/IV history is actually behind for this
    ticker, and upsert the full returned series into the cache. A plain
    one-shot request, no keepUpToDate, this is the exact call the
    standalone scanners already proved works on delayed data."""
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)

    if needs["price_needs_refresh"]:
        su.pace_request()
        price_bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=cfg.LOOKBACK_FOR_MAS,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        time.sleep(cfg.REQUEST_PAUSE_SEC)
        for bar in price_bars:
            cache.upsert_price_bar(
                conn, ticker, cache.bar_date_str(bar.date),
                bar.open, bar.high, bar.low, bar.close, bar.volume,
            )

    if needs["iv_needs_refresh"]:
        su.pace_request()
        iv_bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=cfg.LOOKBACK_FOR_IV,
            barSizeSetting="1 day",
            whatToShow="OPTION_IMPLIED_VOLATILITY",
            useRTH=True,
            formatDate=1,
        )
        time.sleep(cfg.REQUEST_PAUSE_SEC)
        for bar in iv_bars:
            cache.upsert_iv_bar(conn, ticker, cache.bar_date_str(bar.date), bar.close)


def run_refresh_pass(ib: IB, conn) -> None:
    needs_map = cache.tickers_needing_refresh(conn, cfg.ALL_TICKERS)
    to_refresh = [t for t, n in needs_map.items()
                  if n["price_needs_refresh"] or n["iv_needs_refresh"]]

    if not to_refresh:
        print(f"  Cache already current for all {len(cfg.ALL_TICKERS)} tickers. Nothing to do.")
        return

    print(f"  {len(to_refresh)} of {len(cfg.ALL_TICKERS)} tickers are behind, refreshing...")
    for i, ticker in enumerate(to_refresh, start=1):
        print(f"  [{i}/{len(to_refresh)}] Refreshing {ticker}...")
        try:
            refresh_ticker(ib, conn, ticker, needs_map[ticker])
        except Exception as exc:
            print(f"    -> failed to refresh {ticker}: {exc}")
    print("  Refresh pass complete. Cache is current.")


def main():
    ib = IB()
    print(f"Connecting to IBKR at {cfg.IB_HOST}:{cfg.IB_PORT} (clientId={cfg.DAEMON_CLIENT_ID})...")
    ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=cfg.DAEMON_CLIENT_ID, readonly=True)

    if cfg.USE_DELAYED_DATA:
        ib.reqMarketDataType(3)   # 3 = delayed, works without a live subscription

    conn = cache.get_connection()

    print(f"\nWatching {len(cfg.ALL_TICKERS)} unique tickers. Checking every "
          f"{cfg.DAEMON_REFRESH_INTERVAL_SEC // 60} minutes for anything behind.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] Checking cache...")
            run_refresh_pass(ib, conn)
            time.sleep(cfg.DAEMON_REFRESH_INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\nStopping daemon...")
    finally:
        conn.close()
        ib.disconnect()
        print("Daemon stopped. The cache keeps whatever data it already has, "
              "it just won't get fresher until the daemon runs again.")


if __name__ == "__main__":
    main()

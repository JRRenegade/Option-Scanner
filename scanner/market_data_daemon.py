#!/usr/bin/env python3
"""
market_data_daemon.py
=========================

A long-lived process that keeps market_data.db current so combined_scanner.py
never has to talk to IBKR for historical data at all.

WHY A DAEMON INSTEAD OF FETCHING ON EVERY SCAN
--------------------------------------------------
Across the four strategies there are 69 unique tickers, which is 138
historical data requests (a price series and an IV series per ticker).
IBKR enforces a hard ceiling of roughly 60 historical data requests per
rolling 10-minute window, so fetching everything fresh on every scan means
running into that ceiling and pausing partway through, twice, adding
around 15-20 minutes to a run.

This script instead opens a STANDING subscription per ticker per series
using IBKR's "keep up to date" historical data mode. That's one request
per (ticker, series) to open, same pacing cost as a normal fetch, but
after that IBKR pushes bar updates to this process as they happen instead
of you re-requesting the whole history. So the pacing limit only applies
once, when this daemon starts (or restarts after being off for a while),
never again while it keeps running. Every update gets written straight
into market_data.db (see market_data_cache.py).

combined_scanner.py then just reads from that database. No live IBKR
connection needed for historical data, no pacing limit to think about, a
scan finishes in under a second instead of tens of minutes.

RUNNING IT
----------
    python market_data_daemon.py

Leave it running (a terminal window, or start it alongside TWS/IB
Gateway each morning). Ctrl+C stops it cleanly. combined_scanner.py will
refuse to run and tell you to start this first if the cache looks stale
(see MAX_STALE_DAYS in scanner_config.py), so there's no risk of silently
scanning on old data if you forget to start the daemon.

A HONEST CAVEAT
------------------
keepUpToDate historical data streaming is well-documented for TRADES
bars. Whether IBKR accepts it the same way for OPTION_IMPLIED_VOLATILITY
bars specifically hasn't been tested against a live account here. If you
see the IV subscription for a symbol fail or never update while the price
subscription for the same symbol works fine, that's the thing to flag,
the fallback in that case is restarting the daemon periodically (e.g. a
scheduled restart every morning) to force a fresh IV pull rather than
relying on it staying live all day.
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


def _make_price_handler(conn, ticker: str):
    def _on_update(bars, has_new_bar):
        bar = bars[-1]
        cache.upsert_price_bar(
            conn, ticker, cache.bar_date_str(bar.date),
            bar.open, bar.high, bar.low, bar.close, bar.volume,
        )
    return _on_update


def _make_iv_handler(conn, ticker: str):
    def _on_update(bars, has_new_bar):
        bar = bars[-1]
        cache.upsert_iv_bar(conn, ticker, cache.bar_date_str(bar.date), bar.close)
    return _on_update


def subscribe_ticker(ib: IB, conn, ticker: str, active_subscriptions: list) -> None:
    """Open the two standing subscriptions for one ticker (price + IV),
    backfill the cache with whatever history comes back immediately, and
    wire up the callback that keeps writing to the cache as new bars
    arrive for as long as this process runs."""
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)

    su.pace_request()
    price_bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=cfg.LOOKBACK_FOR_MAS,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
        keepUpToDate=True,
    )
    time.sleep(cfg.REQUEST_PAUSE_SEC)
    for bar in price_bars:
        cache.upsert_price_bar(
            conn, ticker, cache.bar_date_str(bar.date),
            bar.open, bar.high, bar.low, bar.close, bar.volume,
        )
    price_bars.updateEvent += _make_price_handler(conn, ticker)
    active_subscriptions.append(("price", ticker, price_bars))

    su.pace_request()
    iv_bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=cfg.LOOKBACK_FOR_IV,
        barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY",
        useRTH=True,
        formatDate=1,
        keepUpToDate=True,
    )
    time.sleep(cfg.REQUEST_PAUSE_SEC)
    for bar in iv_bars:
        cache.upsert_iv_bar(conn, ticker, cache.bar_date_str(bar.date), bar.close)
    iv_bars.updateEvent += _make_iv_handler(conn, ticker)
    active_subscriptions.append(("iv", ticker, iv_bars))


def main():
    ib = IB()
    print(f"Connecting to IBKR at {cfg.IB_HOST}:{cfg.IB_PORT} (clientId={cfg.DAEMON_CLIENT_ID})...")
    ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=cfg.DAEMON_CLIENT_ID, readonly=True)

    if cfg.USE_DELAYED_DATA:
        ib.reqMarketDataType(3)   # 3 = delayed, works without a live subscription

    conn = cache.get_connection()

    print(f"\nSubscribing to {len(cfg.ALL_TICKERS)} unique tickers "
          f"({len(cfg.ALL_TICKERS) * 2} historical data requests total, "
          f"pacing-limited the same way a scan used to be). "
          f"This only happens once, not on every scan.\n")

    active_subscriptions: list = []
    for i, ticker in enumerate(cfg.ALL_TICKERS, start=1):
        print(f"[{i}/{len(cfg.ALL_TICKERS)}] Subscribing {ticker}...")
        try:
            subscribe_ticker(ib, conn, ticker, active_subscriptions)
        except Exception as exc:
            print(f"  -> failed to subscribe {ticker}: {exc}")

    print(f"\n{len(active_subscriptions)} live subscriptions open. Cache is warm.")
    print("Leave this running and use combined_scanner.py to scan anytime.")
    print("Press Ctrl+C to stop the daemon.\n")

    try:
        while True:
            ib.sleep(30)
            timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] daemon alive, {len(active_subscriptions)} subscriptions streaming.")
    except KeyboardInterrupt:
        print("\nStopping daemon...")
    finally:
        for _kind, _ticker, bars in active_subscriptions:
            try:
                ib.cancelHistoricalData(bars)
            except Exception:
                pass
        conn.close()
        ib.disconnect()
        print("Daemon stopped. The cache keeps whatever data it already has, "
              "it just won't get fresher until the daemon runs again.")


if __name__ == "__main__":
    main()

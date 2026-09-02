# Getting Started

This is the plain-language walkthrough: what to do the very first time you
set this up on a machine, and what your day-to-day routine looks like after
that. If you want the deeper "how it works" explanation, that's in
[README.md](README.md) — this doc is just the steps.

## Part 1: First-time setup

Do this once per machine. It takes 20-30 minutes, most of it waiting on the
initial data download.

**1. Get Interactive Brokers running.**

Install IB Gateway (lighter weight) or Trader Workstation (TWS), and log
in. A paper trading account works fine — this only reads market data, it
never places a trade.

**2. Turn on API access.**

Inside TWS/Gateway: **File → Global Configuration → API → Settings**

- Check "Enable ActiveX and Socket Clients"
- Check "Allow connections from localhost only" (or add `127.0.0.1` to
  Trusted IPs)
- Write down the socket port shown there. The default is `7496` for TWS
  live, `7497` for TWS paper, `4001` for Gateway live, `4002` for Gateway
  paper.

Leave TWS/Gateway open. Everything below needs it running.

**3. Get the code onto this machine.**

If it's not already here:
```
git clone https://github.com/JRRenegade/Option-Scanner.git
cd Option-Scanner
```

**4. Install Python dependencies.**

You'll need Python 3.10+ installed. Then, from the project folder:
```
pip install -r requirements.txt
```

**5. Open `scanner/scanner_config.py` and check three things:**

- `IB_PORT` matches the port you wrote down in step 2
- `CREDIT_TICKERS`, `DEBIT_TICKERS`, `COVERED_TICKERS`, and `LEAPS_TICKERS`
  are the watchlists you actually want
- `SHARES_OWNED` reflects what you actually hold, if you're using covered
  calls

This is the only file you should ever need to edit. Save it and close it.

**6. Start the daemon and let it do its first backfill.**

```
cd scanner
python market_data_daemon.py
```

Leave this running. The very first time, it has to pull a full history for
every ticker, and IBKR only allows so many requests per 10 minutes, so with
~69 tickers this takes roughly **15-20 minutes**. You'll see it print
progress as it works through the list. Don't close this window — just let
it run in the background.

**7. Once it settles into "Checking cache..." with nothing to refresh, you're set up.**

You'll see it print something like `Cache already current for all 69
tickers. Nothing to do.` on its next check. That's the signal the initial
setup is done. From here on, jump to Part 2 — you won't need to do steps
1-6 again on this machine (steps 1-2 only need repeating if you restart
your computer or TWS).

## Part 2: Day-to-day use (after the first run)

This is what you'll actually do most mornings.

**1. Make sure TWS/Gateway is open and logged in.**

**2. Start the daemon, in its own terminal window, and leave it running.**
```
cd scanner
python market_data_daemon.py
```
Most days this catches up in a few seconds, not the 15-20 minutes from
first-time setup — it's only pulling the one new day's bar per ticker, not
the full history.

**3. Whenever you want a scan, open a second terminal and run:**
```
cd scanner
python combined_scanner.py
```
This takes under a second and can be run as many times as you want during
the day — it's just reading the local cache, not talking to IBKR.

**4. Check your results.**

The scan prints its findings straight to the terminal, and also saves,
next to itself:
- `credit_spread_scan_<date>.csv`
- `debit_spread_scan_<date>.csv`
- `covered_call_scan_<date>.csv` and a formatted `covered_call_scan_<date>.xlsx`
- `leaps_scan_<date>.csv`

**5. At the end of the day, stop the daemon with `Ctrl+C`** in its
terminal window (or just leave it running overnight if that's easier —
either is fine). Whatever it's already downloaded stays saved in
`market_data.db`, nothing is lost by stopping it.

## The checklist tool

Anytime, independent of all of the above: open `checklist/strike-check.html`
directly in a web browser (just double-click the file). No terminal, no
Python, no IBKR connection needed. Your checkmarks and notes are saved in
that browser as you go.

## If something looks wrong

**The scanner refuses to run and lists tickers as stale.** This means the
cache is behind and it doesn't want to score on old data. Start (or check
on) `market_data_daemon.py` and let it catch up, then run the scanner
again.

**The daemon shows errors for every single ticker.** That usually means
either TWS/Gateway isn't running, isn't logged in, or the port in
`scanner_config.py` doesn't match what's set in step 2 above. Double-check
those first.

**You changed a watchlist ticker or a threshold in `scanner_config.py`.**
No need to touch the daemon for a threshold change (RSI levels, IV Rank
cutoffs, etc.) — those are applied fresh every time the scanner reads the
cache. If you added a brand-new ticker, restart the daemon so it notices
and backfills history for it.

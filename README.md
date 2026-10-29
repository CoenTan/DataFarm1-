# IBKR Market Data

Downloads the full price history of stocks, ETFs, futures, FX, indices and metals
from Interactive Brokers, stores one CSV per security, and keeps it up to date.
It's the data layer for my other quant projects.

- **Full history.** For each security it goes back to the oldest bar IBKR has, in
  daily and hourly bars.
- **Pick any security** with one command, e.g. `--add ES --type future --exchange CME`.
- **Safe to interrupt.** Each chunk is saved as it arrives. Stop it any time and
  re-run to resume.
- **Notices revised history.** If IBKR changes past prices (it does after a stock
  split), the file is downloaded again so it never mixes two price scales.
- **Copes with IBKR's limits.** It paces requests, retries on timeouts and rate
  limits, and steps past gaps in the data instead of stopping at them.

## What you need

| Requirement | Details |
|---|---|
| IBKR account | IBKR **Pro** (not Lite), with at least USD 500 in it plus the cost of your data subscriptions |
| Market Data API Acknowledgement | One-time form. In Client Portal: Settings → Market Data Subscriptions → *Market Data API Acknowledgement* → Yes → sign. Then log out of every IBKR app and back in. Without it, every request fails with "not subscribed". |
| Market data subscriptions | One per exchange you pull (see below). Make sure you're classed as **non-professional**, because professional pricing is far higher. |
| IB Gateway | Download *IB Gateway (stable)* from the IBKR website. TWS works too. |
| Python 3.10+ | Then `pip install -r requirements.txt` |

### Which subscriptions cover what

| Asset class | In the default universe | Data needed |
|---|---|---|
| US stocks / ETFs | SPY, AAPL, NVDA, ... | US equities bundle (NYSE / Nasdaq / ARCA) |
| Futures | ES, NQ | CME |
| | ZN | CBOT |
| | CL | NYMEX |
| | GC | COMEX |
| Indices | SPX, VIX | Cboe index data |
| | NDX | Nasdaq index data |
| FX | EURUSD, USDJPY, USDSGD, ... | IDEALPRO FX |
| Metals | XAUUSD, XAGUSD | IBKR spot metals |

You don't need to sort this out up front. If a subscription is missing, the
script logs `no market data permission ... Skipping` for that security and
carries on with the rest. That log tells you exactly what to subscribe to.

## Setup (Windows, PowerShell)

```powershell
cd C:\path\to\ibkr-market-data
py -m venv .venv                     # or: python -m venv .venv
.venv\Scripts\activate               # run this in every new terminal
pip install -r requirements.txt
```

If PowerShell says running scripts is disabled, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again.

Then in **IB Gateway** (log in with *IB API* selected), go to Configure → Settings → API → Settings:

- Tick **Enable ActiveX and Socket Clients**.
- Set **Socket port** to `4001`.
- Tick **Read-Only API**. This script never places orders.
- Tick **Allow connections from localhost only**.

Ports: Gateway live `4001`, Gateway paper `4002`, TWS live `7496`, TWS paper `7497`.
The script uses 4001 by default; pass `--port` for anything else.

## Running it

```powershell
# 1. Smoke test: one security, daily bars only (about a minute)
python src\fetch.py --symbols SPY --bars 1d

# 2. Full download of everything in config\universe.csv. Leave it running overnight.
python src\fetch.py

# 3. See how much history you got
python src\fetch.py --report
```

Ctrl+C is safe at any point. Running the same command again carries on where it stopped.

### Adding securities

`--add` checks the contract with IBKR, adds it to `config/universe.csv`, and downloads its full history:

```powershell
python src\fetch.py --add AMD COST                              # stocks / ETFs
python src\fetch.py --add RTY --type future --exchange CME      # futures
python src\fetch.py --add USDCHF EURJPY --type fx               # FX pairs
python src\fetch.py --add RUT --type index --exchange RUSSELL   # indices
```

You can also edit `config/universe.csv` by hand.

### Keeping it up to date

Schedule `python src\fetch.py` once a day after the US close. In Singapore that's
04:00 SGT during US summer time and 05:00 in winter, so 06:00 SGT Tuesday to
Saturday is safe. In Windows Task Scheduler:

- **Program:** `C:\path\to\ibkr-market-data\.venv\Scripts\python.exe`
- **Arguments:** `src\fetch.py`
- **Start in:** `C:\path\to\ibkr-market-data`

IB Gateway has to be running and logged in when the task fires. Turn on its
auto-restart (Configure → Settings → Lock and Exit) so it stays up. IBKR still
asks you to log in again roughly once a week.

A daily update costs about one request per security per bar size, so it
finishes in a few minutes.

## Output

```
data/raw/1d/SPY.csv            stocks / ETFs: plain symbol
data/raw/1h/ES_CONTFUT.csv     everything else: SYMBOL_TYPE
data/raw/1d/EURUSD_CASH.csv
reports/coverage.csv           history coverage per security (safe to commit)
logs/fetch.log
```

Columns: `timestamp, open, high, low, close, volume, wap, bar_count`

- **Hourly** timestamps are the bar's start time in UTC, e.g. `2024-03-01T14:30:00+00:00`.
- **Daily** timestamps are the trading date, e.g. `2024-03-01`.
- **Stocks, ETFs and indices** use regular trading hours only. Futures, FX and
  metals include every session.
- In regular hours the first hourly bar of a US stock's day runs 09:30 to 10:00
  ET, so it's only half an hour long.

`data/` is git-ignored on purpose. IBKR data is licensed for personal use and
must not be republished.

## Limitations

- **Survivorship bias.** IBKR has no data for delisted securities, so the
  universe is today's survivors.
- **Dividends.** Stock, ETF, futures and index prices are IBKR "TRADES" data,
  which is adjusted for splits but not for dividends. Total-return
  adjustment belongs in the cleaning step.
- **Futures** are IBKR's continuous front-month series. IBKR only accepts
  requests for that series ending "now", so the history comes in one request.
  If IBKR caps how far back that request can go, the script finds the longest
  span it accepts. Individual contract months aren't stored, and IBKR drops
  them 2 years after expiry.
- **Options** aren't supported. IBKR keeps no history for expired options.
- **FX and metals** are bid/ask midpoints, so their volume column isn't meaningful.
- **Hourly history** usually starts later than daily. `--report` shows where each one starts.

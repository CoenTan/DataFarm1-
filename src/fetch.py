"""
fetch.py - download the full price history of any security from Interactive
Brokers into one CSV per security, and keep it up to date.

Pick what to download
---------------------
--add checks the security with IBKR, saves it to config/universe.csv, then
downloads everything IBKR has for it (daily + hourly):

    python src/fetch.py --add NVDA                                  # stock / ETF
    python src/fetch.py --add ES NQ --type future --exchange CME    # futures (continuous)
    python src/fetch.py --add GC --type future --exchange COMEX
    python src/fetch.py --add EURUSD USDJPY --type fx
    python src/fetch.py --add SPX --type index --exchange CBOE
    python src/fetch.py --add XAUUSD --type metal
    python src/fetch.py --add BTC --type crypto

Update everything already in config/universe.csv (schedule this daily):

    python src/fetch.py
    python src/fetch.py --symbols AAPL ES     # only these
    python src/fetch.py --bars 1d             # only daily bars

See how much history you've got (reads local files, no IBKR needed):

    python src/fetch.py --report

"Everything IBKR has" means back to the oldest bar IBKR reports for that
security. Not available from IBKR at all: delisted stocks, expired options,
and individual futures contracts that expired more than 2 years ago.
Hourly history usually starts later than daily; the script finds where.

How it works
------------
Stocks, ETFs, indices, FX, metals, crypto:
    The first download walks backwards from today one chunk at a time
    (1 year of daily bars or 1 month of hourly bars per request) until it
    reaches the oldest bar. Each chunk is saved as it arrives, so Ctrl+C or a
    crash loses at most one chunk. Run again and it carries on.

Futures:
    Stored as IBKR's continuous series (front contract, rolled automatically).
    IBKR only accepts continuous-futures requests that end "now", so there are
    no chunks: the whole history comes in one big request. If IBKR refuses a
    span that long, the script searches for the longest one it accepts.

Every later run:
    Downloads from a little before the last saved bar up to now. The overlap
    is compared with what's already saved. If IBKR has changed past prices
    (after a stock split, IBKR re-adjusts all earlier prices), the file is
    downloaded again from scratch so it never mixes two price scales.

Output
------
data/raw/<bar>/<NAME>.csv with columns:
    timestamp, open, high, low, close, volume, wap, bar_count

NAME is the symbol for stocks/ETFs (AAPL.csv) and SYMBOL_TYPE for everything
else (ES_CONTFUT.csv, EURUSD_CASH.csv, SPX_IND.csv, XAUUSD_CMDTY.csv).

  - hourly: timestamp = bar START time in UTC, e.g. 2024-03-01T14:30:00+00:00
  - daily : timestamp = trading date,           e.g. 2024-03-01

Prices, as IBKR supplies them:
  - stocks, ETFs, futures, indices: traded prices ("TRADES"), adjusted for
    splits but NOT for dividends
  - FX and metals: bid/ask midpoint, so volume is meaningless
  - crypto: aggregated trades

Never edit raw files by hand. They are the source of truth that data/clean/
gets rebuilt from.

Before running
--------------
1. IB Gateway (or TWS) open and logged in.
2. Settings -> API -> Settings: tick "Enable ActiveX and Socket Clients" and
   keep "Read-Only API" ticked (this script never places orders).
3. Check the socket port: Gateway live 4001, Gateway paper 4002,
   TWS live 7496, TWS paper 7497.
4. Market data subscriptions for the exchanges you pull (e.g. CME for ES).
"""

import argparse
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from ib_async import IB, Contract

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent  # repo root (this file lives in src/)
UNIVERSE_FILE = ROOT / "config" / "universe.csv"
RAW_DIR = ROOT / "data" / "raw"
STATE_FILE = RAW_DIR / "_backfill_state.json"
LOG_FILE = ROOT / "logs" / "fetch.log"

HOST = "127.0.0.1"
DEFAULT_PORT = 4001  # IB Gateway, live account
CLIENT_ID = 17       # any number not used by another API connection

# One entry per bar size we store.
# "chunk" is how much history one request asks for. IBKR limits how many bars
# one request can return, so these are kept conservative.
BAR_SETTINGS = {
    "1d": {"bar_size": "1 day", "chunk": "1 Y", "chunk_days": 365},
    "1h": {"bar_size": "1 hour", "chunk": "1 M", "chunk_days": 30},
}

# Every kind of security we can download, and how IBKR wants it requested.
#   what_to_show: which price series to ask for. FX and metals have no trade
#                 prints on IBKR, so we take the bid/ask midpoint instead.
#   rth_only:     True  = regular trading hours only (09:30-16:00 ET for US stocks)
#                 False = every session (futures, FX and crypto trade ~24h)
#   exchange:     used when universe.csv leaves it blank. "" = you must give one.
SECURITY_TYPES = {
    "STK":     {"what_to_show": "TRADES",    "rth_only": True,  "exchange": "SMART"},     # stocks, ETFs
    "IND":     {"what_to_show": "TRADES",    "rth_only": True,  "exchange": ""},          # indices, e.g. CBOE
    "CONTFUT": {"what_to_show": "TRADES",    "rth_only": False, "exchange": ""},          # futures, e.g. CME
    "CASH":    {"what_to_show": "MIDPOINT",  "rth_only": False, "exchange": "IDEALPRO"},  # FX
    "CMDTY":   {"what_to_show": "MIDPOINT",  "rth_only": False, "exchange": "SMART"},     # spot metals
    "CRYPTO":  {"what_to_show": "AGGTRADES", "rth_only": False, "exchange": "PAXOS"},
}

# Plain-English names accepted by --type
TYPE_NAMES = {
    "stock": "STK", "etf": "STK", "index": "IND", "future": "CONTFUT",
    "fx": "CASH", "metal": "CMDTY", "crypto": "CRYPTO",
}

UNIVERSE_COLUMNS = ["symbol", "sec_type", "exchange", "currency", "primary_exchange"]

MAX_YEARS_BACK = None          # None = as far back as IBKR has. Set e.g. 10 to cap it.
SECONDS_BETWEEN_REQUESTS = 2   # IBKR soft-throttles heavy users and can disconnect you
REQUEST_TIMEOUT = 120          # seconds before giving up on one normal request
BIG_REQUEST_TIMEOUT = 600      # same, for the one-shot futures download
MAX_RETRIES = 5                # attempts per request before giving up for this run
PACING_PAUSE = 60              # seconds to wait after a pacing violation
MAX_EMPTY_CHUNKS = 3           # empty chunks in a row before deciding history has ended.
                               # >1 so a gap (trading halt, exchange move) doesn't end it early.

OVERLAP_BARS = 20              # updates re-download this many saved bars, to compare
REVISION_TOLERANCE = 0.01      # >1% typical change in saved closes = IBKR revised history

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "wap", "bar_count"]

log = logging.getLogger("fetch")


class NoDataPermission(Exception):
    """Raised when the account has no market data subscription for a security."""


# ---------------------------------------------------------------------------
# IBKR error tracking
# ---------------------------------------------------------------------------
# IBKR reports problems through an error callback, not as Python exceptions,
# and a failed request just comes back empty. We remember the most recent
# error so we can tell "no more history" apart from "slow down" or "timed out".

last_error = {"code": None, "message": ""}


def on_error(req_id, error_code, error_string, contract=None, *extra):
    # Codes 2100-2199 are status notices ("market data farm is OK"), not errors.
    if 2100 <= error_code < 2200:
        return
    last_error["code"] = error_code
    last_error["message"] = error_string


# ---------------------------------------------------------------------------
# Universe: which securities we track
# ---------------------------------------------------------------------------

def load_universe():
    if not UNIVERSE_FILE.exists():
        return pd.DataFrame(columns=UNIVERSE_COLUMNS)
    universe = pd.read_csv(UNIVERSE_FILE, dtype=str).fillna("")
    for column in UNIVERSE_COLUMNS:  # tolerate older files missing a column
        if column not in universe.columns:
            universe[column] = ""
    return universe[UNIVERSE_COLUMNS]


def file_name(row):
    """Name of a security's CSV: AAPL, ES_CONTFUT, EURUSD_CASH, SPX_IND, ..."""
    symbol = row["symbol"].replace(" ", "_")  # e.g. "BRK B" -> "BRK_B"
    if row["sec_type"] == "STK":
        return symbol
    if row["sec_type"] == "CASH":
        symbol = symbol + row["currency"]     # EUR + USD -> EURUSD
    return f"{symbol}_{row['sec_type']}"


def new_universe_row(symbol, sec_type, exchange, currency):
    """Build a universe.csv row for a security given on the command line."""
    symbol = symbol.upper()
    if sec_type == "CASH" and len(symbol) == 6:
        symbol, currency = symbol[:3], symbol[3:]  # "EURUSD" -> EUR priced in USD
    return {
        "symbol": symbol,
        "sec_type": sec_type,
        "exchange": exchange or SECURITY_TYPES[sec_type]["exchange"],
        "currency": currency,
        "primary_exchange": "",
    }


def make_contract(row):
    """Build an IBKR contract from one row of universe.csv."""
    sec_type = row["sec_type"] or "STK"
    return Contract(
        symbol=row["symbol"],
        secType=sec_type,
        exchange=row["exchange"] or SECURITY_TYPES[sec_type]["exchange"],
        currency=row["currency"] or "USD",
        primaryExchange=row["primary_exchange"],  # blank is fine; set it if a stock is "ambiguous"
    )


def qualify(ib, row):
    """Ask IBKR to confirm the contract exists. Returns it, or None."""
    name = file_name(row)
    if row["sec_type"] not in SECURITY_TYPES:
        log.error(f"{name}: sec_type '{row['sec_type']}' isn't supported "
                  f"(use one of {', '.join(SECURITY_TYPES)}). Skipping.")
        return None
    if not (row["exchange"] or SECURITY_TYPES[row["sec_type"]]["exchange"]):
        log.error(f"{name}: needs an exchange (e.g. CME for ES, CBOE for SPX). Skipping.")
        return None

    contract = ib.qualifyContracts(make_contract(row))[0]
    if contract is None:
        log.error(f"{name}: IBKR couldn't identify this contract. Check the symbol, "
                  f"exchange and currency (stocks: set primary_exchange if ambiguous). Skipping.")
    return contract


def add_securities(ib, symbols, sec_type, exchange, currency):
    """Check each new security with IBKR and add it to universe.csv.
    Returns (row, contract) pairs for the ones IBKR recognised."""
    universe = load_universe()
    known = {file_name(row) for _, row in universe.iterrows()}
    targets = []

    for symbol in symbols:
        row = new_universe_row(symbol, sec_type, exchange, currency)
        contract = qualify(ib, row)
        if contract is None:
            continue
        if file_name(row) in known:
            log.info(f"{file_name(row)}: already in universe.csv")
        else:
            universe = pd.concat([universe, pd.DataFrame([row])], ignore_index=True)
            UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            universe.to_csv(UNIVERSE_FILE, index=False)
            known.add(file_name(row))
            log.info(f"{file_name(row)}: added to universe.csv")
        targets.append((row, contract))

    return targets


def universe_targets(ib, symbols_filter):
    """(row, contract) pairs for everything in universe.csv, or only the
    symbols asked for. "ES", "ES_CONTFUT", "EURUSD" and "EURUSD_CASH" all work."""
    universe = load_universe()
    wanted = {s.upper() for s in symbols_filter} if symbols_filter else None
    targets = []

    for _, row in universe.iterrows():
        names = {row["symbol"].upper(), file_name(row).upper()}
        if row["sec_type"] == "CASH":
            names.add((row["symbol"] + row["currency"]).upper())  # EUR + USD -> EURUSD
        if wanted and not (names & wanted):
            continue
        contract = qualify(ib, row)
        if contract is not None:
            targets.append((row, contract))

    return targets


# ---------------------------------------------------------------------------
# Timestamps and CSV files
# ---------------------------------------------------------------------------

def to_utc_datetime(ts):
    """Turn a stored timestamp (date or ISO datetime string) into a UTC datetime,
    so daily and hourly timestamps can be compared the same way."""
    if len(ts) == 10:  # "YYYY-MM-DD" -> midnight UTC of that day
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def bars_to_frame(bars, bar_key):
    """Convert ib_async bar objects into a DataFrame with our fixed columns."""
    rows = []
    for bar in bars:
        if bar_key == "1d":
            day = bar.date.date() if isinstance(bar.date, datetime) else bar.date
            ts = day.isoformat()                                 # "2024-03-01"
        else:
            ts = bar.date.astimezone(timezone.utc).isoformat()   # "2024-03-01T14:30:00+00:00"
        rows.append([ts, bar.open, bar.high, bar.low, bar.close,
                     bar.volume, bar.average, bar.barCount])
    return pd.DataFrame(rows, columns=COLUMNS)


def csv_path_for(name, bar_key):
    return RAW_DIR / bar_key / f"{name}.csv"


def load_csv(path):
    if path.exists():
        return pd.read_csv(path, dtype={"timestamp": str})
    return pd.DataFrame(columns=COLUMNS)


def merge_and_save(path, new_rows):
    """Merge new bars into the CSV: drop duplicate timestamps, sort, save.

    If a timestamp already exists, the NEW bar wins. That matters when the
    script ran during market hours and saved a half-finished bar for today:
    the next run overwrites it with the complete one.
    """
    existing = load_csv(path)
    if existing.empty:
        combined = new_rows.copy()
    else:
        combined = pd.concat([existing, new_rows], ignore_index=True)
    combined = combined.drop_duplicates(subset="timestamp", keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(path)  # swap in one step, so a crash mid-write can't corrupt the file
    return combined


def overlap_start(existing):
    """Timestamp OVERLAP_BARS bars before the newest saved bar.

    Updates re-download from here rather than from the newest bar, so the new
    data overlaps what's saved and history_revised() has something to compare.
    """
    i = max(0, len(existing) - OVERLAP_BARS)
    return to_utc_datetime(existing["timestamp"].iloc[i])


def history_revised(existing, new_rows):
    """True if IBKR's prices for bars we already saved have changed.

    After a stock split, IBKR re-adjusts every earlier price, so our saved
    file would be in the old price scale and new bars in the new one. When
    that happens the caller re-downloads the whole history.

    Uses the median change across the overlap, so a single corrected bar
    doesn't trigger a full re-download (merging already fixes that bar).
    """
    if new_rows.empty:
        return False
    saved = existing.iloc[:-1]  # newest saved bar may have been half-finished; skip it
    fresh = new_rows.drop_duplicates(subset="timestamp", keep="last")
    both = saved.merge(fresh, on="timestamp", suffixes=("_saved", "_fresh"))
    if len(both) < 3:
        return False
    change = (both["close_fresh"] / both["close_saved"] - 1).abs()
    return change.median() > REVISION_TOLERANCE


# ---------------------------------------------------------------------------
# Backfill progress
# ---------------------------------------------------------------------------
# For each "<bar>/<name>" we record how far back the backfill has been
# completed. Without this, every daily run would re-ask IBKR for history
# that doesn't exist. Delete an entry to force a re-check.

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Talking to IBKR
# ---------------------------------------------------------------------------

def earliest_available(ib, contract, sec):
    """Ask IBKR for the timestamp of the oldest bar it has for this contract."""
    try:
        head = ib.reqHeadTimeStamp(contract, whatToShow=sec["what_to_show"],
                                   useRTH=sec["rth_only"], formatDate=2)
    except Exception as exc:
        log.warning(f"{contract.symbol}: couldn't get earliest timestamp ({exc})")
        return None
    if isinstance(head, datetime):
        return head.astimezone(timezone.utc)
    return None


def history_floor(head):
    """How far back to go: IBKR's oldest bar, or MAX_YEARS_BACK, whichever is later."""
    floor = head or datetime(1980, 1, 1, tzinfo=timezone.utc)
    if MAX_YEARS_BACK:
        cap = datetime.now(timezone.utc) - timedelta(days=365.25 * MAX_YEARS_BACK)
        floor = max(floor, cap)
    return floor


def request_chunk(ib, contract, bar_key, sec, end_dt, duration=None,
                  retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT):
    """Ask IBKR for bars ending at end_dt (None = now).

    duration defaults to the normal chunk size for this bar size.

    Returns (bars, status), where status is one of:
      "ok"           - got bars
      "no_more_data" - IBKR says there is nothing in that period
      "gave_up"      - kept failing (timeouts, pacing); try again next run
    """
    settings = BAR_SETTINGS[bar_key]

    for attempt in range(1, retries + 1):
        ib.sleep(SECONDS_BETWEEN_REQUESTS)  # ib.sleep keeps the connection alive while waiting
        last_error["code"], last_error["message"] = None, ""

        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_dt or "",
            durationStr=duration or settings["chunk"],
            barSizeSetting=settings["bar_size"],
            whatToShow=sec["what_to_show"],
            useRTH=sec["rth_only"],
            formatDate=2,  # UTC timestamps, whatever timezone TWS/Gateway is set to
            timeout=timeout,
        )
        if bars:
            return list(bars), "ok"

        if not ib.isConnected():
            raise ConnectionError("Lost connection to IB Gateway/TWS. Run again to resume.")

        message = last_error["message"].lower()
        if "returned no data" in message:
            return [], "no_more_data"
        if "permission" in message or "not subscribed" in message:
            raise NoDataPermission(last_error["message"])

        reason = last_error["message"] or "empty response / timeout"
        if attempt == retries:
            log.warning(f"Attempt {attempt}/{retries} failed ({reason}), giving up for this run")
            break
        if "pacing" in message:
            log.warning(f"Pacing violation, pausing {PACING_PAUSE}s")
            ib.sleep(PACING_PAUSE)
        else:
            wait = 10 * attempt
            log.warning(f"Attempt {attempt}/{retries} failed ({reason}), retrying in {wait}s")
            ib.sleep(wait)

    return [], "gave_up"


def walk_backwards(ib, contract, bar_key, sec, start_from, stop_at,
                   csv_path=None, label=""):
    """Fetch chunks going back in time from start_from (None = now) until stop_at.

    If csv_path is given, each chunk is merged into that CSV as soon as it
    arrives, so a crash or Ctrl+C loses at most one chunk.

    Returns (new_rows, reached_end):
      new_rows    - every bar fetched, as one DataFrame
      reached_end - True if we got back to stop_at or ran out of history,
                    False if we stopped because IBKR kept failing
    """
    frames = []
    cursor = start_from
    empty_in_a_row = 0

    while True:
        bars, status = request_chunk(ib, contract, bar_key, sec, cursor)
        if status == "gave_up":
            reached_end = False
            break
        if status == "no_more_data":
            # Nothing in this window. Usually that's the start of history, but it
            # could be a gap, so step back past it a few times before giving up.
            empty_in_a_row += 1
            cursor = (cursor or datetime.now(timezone.utc)) - timedelta(
                days=BAR_SETTINGS[bar_key]["chunk_days"])
            if empty_in_a_row >= MAX_EMPTY_CHUNKS or cursor <= stop_at:
                reached_end = True
                break
            continue
        empty_in_a_row = 0

        chunk = bars_to_frame(bars, bar_key)
        frames.append(chunk)
        if csv_path is not None:
            merge_and_save(csv_path, chunk)

        earliest = to_utc_datetime(chunk["timestamp"].min())
        log.info(f"{label}: +{len(chunk)} bars, back to {earliest:%Y-%m-%d}")

        if earliest <= stop_at:
            reached_end = True
            break
        if cursor is not None and earliest >= cursor:
            # IBKR sent nothing older than last time. Stop instead of looping forever.
            reached_end = True
            break
        cursor = earliest  # next request ends where this one started

    if frames:
        new_rows = pd.concat(frames, ignore_index=True)
    else:
        new_rows = pd.DataFrame(columns=COLUMNS)
    return new_rows, reached_end


def duration_string(days):
    """IBKR duration covering `days` back from now. IBKR accepts at most 365 D,
    so anything longer is rounded up to whole years (68 Y is its maximum)."""
    if days <= 365:
        return f"{days} D"
    return f"{min(68, math.ceil(days / 365))} Y"


def request_until_now(ib, contract, bar_key, sec, since, label):
    """One request covering `since` up to now (continuous futures can't be
    requested in chunks). Returns a DataFrame, or None if it failed.

    If IBKR refuses a span that long, we search for the longest whole number
    of years it does accept, so we still get as much history as possible.
    """
    def ask(duration):
        return request_chunk(ib, contract, bar_key, sec, end_dt=None, duration=duration,
                             retries=2, timeout=BIG_REQUEST_TIMEOUT)

    days = (datetime.now(timezone.utc) - since).days + 2
    full = duration_string(days)
    bars, status = ask(full)
    if status == "ok":
        return bars_to_frame(bars, bar_key)
    if status == "no_more_data":
        return pd.DataFrame(columns=COLUMNS)
    if days <= 365:
        return None  # even a short request failed: try again next run

    # Binary search: `works` = most years known to be accepted, `fails` = fewest refused.
    log.info(f"{label}: IBKR refused {full}, searching for the longest span it accepts")
    works, fails, best = 0, min(68, math.ceil(days / 365)), None
    while fails - works > 1:
        years = (works + fails) // 2
        bars, status = ask(f"{years} Y")
        if status == "ok":
            works, best = years, bars
        else:
            fails = years

    if best is None:
        return None
    log.warning(f"{label}: longest span IBKR accepted is {works} Y")
    return bars_to_frame(best, bar_key)


# ---------------------------------------------------------------------------
# One security, one bar size
# ---------------------------------------------------------------------------

def update_security(ib, contract, row, bar_key, state):
    name = file_name(row)
    label = f"{name} {bar_key}"
    sec = SECURITY_TYPES[row["sec_type"]]
    path = csv_path_for(name, bar_key)
    state_key = f"{bar_key}/{name}"

    floor = history_floor(earliest_available(ib, contract, sec))

    if row["sec_type"] == "CONTFUT":
        update_continuous_future(ib, contract, sec, bar_key, path, floor, label)
        return

    existing = load_csv(path)

    # --- Step 1: bring an existing file up to date ---------------------------
    # Saved only once the whole gap is filled. Saving part-way would put
    # today's bars in the file, and the next run would think the gap was closed.
    if not existing.empty:
        start = overlap_start(existing)
        log.info(f"{label}: updating from {start:%Y-%m-%d %H:%M} UTC")
        new_rows, reached_end = walk_backwards(ib, contract, bar_key, sec,
                                               start_from=None, stop_at=start,
                                               label=label)
        if not reached_end:
            log.warning(f"{label}: update incomplete, nothing saved; will retry next run")
        elif history_revised(existing, new_rows):
            log.warning(f"{label}: IBKR changed past prices (likely a split). "
                        f"Re-downloading the full history.")
            path.unlink()
            state.pop(state_key, None)
            existing = load_csv(path)  # now empty, so step 2 does a full download
        else:
            before = len(existing)
            existing = merge_and_save(path, new_rows)
            log.info(f"{label}: {len(existing) - before} new bars, {len(existing)} total")

    # --- Step 2: download history we don't have yet ---------------------------
    if existing.empty:
        log.info(f"{label}: downloading full history back to {floor:%Y-%m-%d}")
        _, reached_end = walk_backwards(ib, contract, bar_key, sec,
                                        start_from=None, stop_at=floor,
                                        csv_path=path, label=label)
    else:
        done_back_to = state.get(state_key)
        if done_back_to and to_utc_datetime(done_back_to) <= floor:
            return  # finished on an earlier run
        oldest = to_utc_datetime(existing["timestamp"].min())
        if oldest <= floor:
            state[state_key] = floor.isoformat()
            return
        log.info(f"{label}: resuming download from {oldest:%Y-%m-%d} back to {floor:%Y-%m-%d}")
        _, reached_end = walk_backwards(ib, contract, bar_key, sec,
                                        start_from=oldest, stop_at=floor,
                                        csv_path=path, label=label)

    if reached_end:
        state[state_key] = floor.isoformat()
        log.info(f"{label}: full history downloaded")
    else:
        log.warning(f"{label}: download paused, run again to continue")


def update_continuous_future(ib, contract, sec, bar_key, path, floor, label):
    """Futures: IBKR only accepts continuous-futures requests that end now,
    so each run is a single request instead of a backwards walk."""
    existing = load_csv(path)

    if not existing.empty:
        start = overlap_start(existing)
        log.info(f"{label}: updating from {start:%Y-%m-%d %H:%M} UTC")
        new_rows = request_until_now(ib, contract, bar_key, sec, start, label)
        if new_rows is None:
            log.warning(f"{label}: update failed, nothing saved; will retry next run")
            return
        if not history_revised(existing, new_rows):
            before = len(existing)
            after = len(merge_and_save(path, new_rows))
            log.info(f"{label}: {after - before} new bars, {after} total")
            return
        log.warning(f"{label}: IBKR changed past prices (series re-adjusted). "
                    f"Re-downloading the full history.")

    log.info(f"{label}: downloading full history back to {floor:%Y-%m-%d} in one request")
    rows = request_until_now(ib, contract, bar_key, sec, floor, label)
    if rows is None or rows.empty:
        log.warning(f"{label}: download failed, will retry next run")
        return

    path.unlink(missing_ok=True)  # replace, don't merge: old file may be in another price scale
    saved = merge_and_save(path, rows)
    oldest = to_utc_datetime(saved["timestamp"].min())
    log.info(f"{label}: {len(saved)} bars, back to {oldest:%Y-%m-%d}")


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------

def coverage_report():
    """Print and save how much history each CSV holds. Reads local files only.

    reports/coverage.csv is safe to commit: it describes the data without
    containing any of it.
    """
    table = {}
    for bar_key in BAR_SETTINGS:
        for path in sorted((RAW_DIR / bar_key).glob("*.csv")):
            timestamps = pd.read_csv(path, usecols=["timestamp"], dtype=str)["timestamp"]
            if timestamps.empty:
                continue
            first, last = timestamps.min()[:10], timestamps.max()[:10]
            years = (to_utc_datetime(last) - to_utc_datetime(first)).days / 365.25
            row = table.setdefault(path.stem, {"security": path.stem})
            row[f"{bar_key}_from"] = first
            row[f"{bar_key}_years"] = round(years, 1)
            row[f"{bar_key}_bars"] = len(timestamps)
            row["last"] = max(row.get("last", ""), last)

    if not table:
        print("No data yet. Run a download first.")
        return

    report = pd.DataFrame(table.values()).sort_values("security")
    print(report.to_string(index=False, na_rep="-"))
    out = ROOT / "reports" / "coverage.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(out, index=False)
    print(f"\nSaved to {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Download IBKR historical bars into one CSV per security.")
    parser.add_argument("--add", nargs="+", metavar="SYMBOL",
                        help="add securities to universe.csv and download their full history")
    parser.add_argument("--type", choices=list(TYPE_NAMES), default="stock",
                        help="what --add symbols are (default: stock)")
    parser.add_argument("--exchange", default="",
                        help="exchange for --add (needed for futures and indices, e.g. CME, CBOE)")
    parser.add_argument("--currency", default="USD", help="currency for --add (default: USD)")
    parser.add_argument("--symbols", nargs="+",
                        help="only update these symbols from universe.csv (default: all)")
    parser.add_argument("--bars", nargs="+", choices=list(BAR_SETTINGS),
                        default=list(BAR_SETTINGS), help="bar sizes (default: 1d 1h)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TWS/Gateway port")
    parser.add_argument("--report", action="store_true",
                        help="show how much history you have (no IBKR connection needed)")

    args = parser.parse_args()
    if args.add and args.symbols:
        parser.error("use either --add or --symbols, not both")
    return args


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    logging.getLogger("ib_async").setLevel(logging.WARNING)  # hide routine library chatter


def main():
    args = parse_args()
    if args.report:
        coverage_report()
        return
    setup_logging()

    ib = IB()
    ib.errorEvent += on_error
    ib.connect(HOST, args.port, clientId=CLIENT_ID, readonly=True, timeout=20)

    state = load_state()
    try:
        if args.add:
            targets = add_securities(ib, args.add, TYPE_NAMES[args.type],
                                     args.exchange.upper(), args.currency.upper())
        else:
            targets = universe_targets(ib, args.symbols)

        if not targets:
            log.error("Nothing to download.")
            return
        log.info(f"Connected. {len(targets)} securities, bars: {', '.join(args.bars)}")

        for row, contract in targets:
            for bar_key in args.bars:
                try:
                    update_security(ib, contract, row, bar_key, state)
                except NoDataPermission as exc:
                    log.error(f"{file_name(row)}: no market data permission ({exc}). Skipping.")
                    break
                finally:
                    save_state(state)  # record progress after every security
    finally:
        save_state(state)
        ib.disconnect()
        log.info("Disconnected.")


if __name__ == "__main__":
    main()

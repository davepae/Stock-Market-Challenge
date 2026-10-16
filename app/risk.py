#!/usr/bin/env python3
"""Risk helpers shared by trading_cycle.py and app.py.

  - Volatility-scaled stop loss (stop_pct_from_volatility), recorded per
    position in PEAKS_JSON so risk-only checks and the exporter reuse it.
  - Trailing stop: tracks the highest price observed for each open position
    in PEAKS_JSON. Once the peak is TRAILING_STOP_ACTIVATION% above entry, the
    position is sold if price falls TRAILING_STOP_PCT% below the peak
    (no take-profit cap).
  - Lightweight last-price lookup for the hourly --risk-only check.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import config


# --- Stop levels --------------------------------------------------------------

def stop_pct_from_volatility(volatility_pct: float) -> float:
    """Stop loss in percent: 3 x weekly volatility (daily stdev x sqrt(5)), clamped."""
    raw = 3 * volatility_pct * math.sqrt(5)
    return max(config.STOP_LOSS_MIN_PCT, min(config.STOP_LOSS_MAX_PCT, raw))


def stored_stop_pct(record: dict[str, object] | None) -> float | None:
    """The stop_pct recorded in a peaks.json entry, None if absent or invalid."""
    try:
        value = float((record or {})["stop_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def stop_loss_pct(stop_pct: float | None = None) -> float:
    """Stop loss below entry in percent: ``stop_pct`` if given, else the fallback default."""
    return stop_pct if stop_pct else config.DEFAULT_STOP_LOSS_PCT


def trailing_params() -> tuple[float, float]:
    """(activation %, trail %) for the trailing stop."""
    return config.TRAILING_STOP_ACTIVATION, config.TRAILING_STOP_PCT


@dataclass
class StopLevels:
    """Exit levels for an open position."""

    ticker: str
    entry: float
    peak: float
    stop_loss: float             # fixed stop price below entry
    stop_pct: float              # stop distance below entry, in percent
    trailing_stop: float | None  # trailing stop price, None until activated

    @property
    def trailing_active(self) -> bool:
        """True once the trailing stop is armed."""
        return self.trailing_stop is not None

    @property
    def effective_stop(self) -> float:
        """The higher of the fixed and trailing stop prices."""
        return max(self.stop_loss, self.trailing_stop or 0.0)


def stop_levels(ticker: str, entry: float, peak: float,
                stop_pct: float | None = None,
                volatility_pct: float | None = None) -> StopLevels:
    """Compute the fixed and trailing stop prices for a position.

    The stop distance is ``stop_pct`` if given, else derived from
    ``volatility_pct``, else the fallback DEFAULT_STOP_LOSS_PCT.
    """
    if not stop_pct and volatility_pct:
        stop_pct = stop_pct_from_volatility(volatility_pct)
    stop_pct = stop_loss_pct(stop_pct)
    activation, trail = trailing_params()
    peak = max(peak, entry)
    fixed = entry * (1 - stop_pct / 100)
    trailing = (max(entry, peak * (1 - trail / 100))  # armed: never below breakeven
                if entry > 0 and peak >= entry * (1 + activation / 100) else None)
    return StopLevels(ticker, entry, peak, fixed, stop_pct, trailing)


def check_stops(levels: StopLevels, price: float) -> str:
    """Return the exit reason if price breaches a stop, else ""."""
    if levels.entry <= 0:
        return ""
    pnl = (price / levels.entry - 1) * 100
    if price <= levels.stop_loss:
        return (f"stop loss ({pnl:+.1f}% vs entry {levels.entry:.2f}, "
                f"limit -{levels.stop_pct:.1f}%)")
    if levels.trailing_stop is not None and price <= levels.trailing_stop:
        drop = (price / levels.peak - 1) * 100
        return (f"trailing stop ({drop:+.1f}% from peak {levels.peak:.2f}, "
                f"stop {levels.trailing_stop:.2f}; P&L {pnl:+.1f}%)")
    return ""


# --- Peak tracking ------------------------------------------------------------

def load_peaks(path: str | Path = config.PEAKS_JSON) -> dict[str, dict[str, object]]:
    """Read {ticker: {"opened": date, "peak": price, "stop_pct": pct}}; empty if missing or unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"WARNING: cannot read peaks file {path}: {e}", file=sys.stderr)
        return {}


def merge_peaks(stored: dict[str, dict[str, object]],
                positions: dict[str, tuple[float, str]],
                prices: dict[str, float],
                stop_pcts: dict[str, float] | None = None) -> dict[str, dict[str, object]]:
    """Combine stored peaks with fresh prices for the open positions.

    ``positions`` maps ticker -> (entry price, opened date). A stored peak
    from a different (earlier) position in the same ticker is discarded, and
    the peak is never below the entry price. Closed positions are dropped.
    ``stop_pcts`` holds fresh volatility-derived stops; a stored stop_pct of the
    same position is kept when no fresh one is given.
    """
    stop_pcts = stop_pcts or {}
    merged: dict[str, dict[str, object]] = {}
    for ticker, old in stored.items():  # closed positions keep only their last-sell record
        if ticker not in positions and old.get("last_sell_date"):
            merged[ticker] = {k: old[k] for k in ("last_sell_date", "last_sell_reason") if k in old}
    for ticker, (entry, opened) in positions.items():
        old = stored.get(ticker, {})
        try:
            peak = float(old["peak"]) if old.get("opened") == opened else 0.0
        except (KeyError, TypeError, ValueError):
            peak = 0.0
        merged[ticker] = {"opened": opened,
                          "peak": round(max(peak, entry, prices.get(ticker, 0.0)), 4)}
        stop_pct = stop_pcts.get(ticker)
        if stop_pct is None and old.get("opened") == opened:
            stop_pct = stored_stop_pct(old)
        if stop_pct:
            merged[ticker]["stop_pct"] = round(stop_pct, 2)
        for k in ("last_sell_date", "last_sell_reason"):
            if k in old:
                merged[ticker][k] = old[k]
    return merged


def update_peaks(positions: dict[str, tuple[float, str]], prices: dict[str, float],
                 path: str | Path = config.PEAKS_JSON, save: bool = True,
                 stop_pcts: dict[str, float] | None = None) -> dict[str, float]:
    """Record new highs for open positions and return {ticker: peak}.

    With save=False the file is only read (used by dry runs and the exporter).
    The read-modify-write is serialized with a lock file and written atomically.
    """
    path = Path(path)
    if not save:
        merged = merge_peaks(load_peaks(path), positions, prices, stop_pcts)
        return {t: float(v["peak"]) for t, v in merged.items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            merged = merge_peaks(load_peaks(path), positions, prices, stop_pcts)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, path)
    except OSError as e:
        print(f"WARNING: cannot update peaks file {path}: {e}", file=sys.stderr)
        merged = merge_peaks(load_peaks(path), positions, prices, stop_pcts)
    return {t: float(v["peak"]) for t, v in merged.items() if "peak" in v}


def sell_kind(reason: str) -> str:
    """"stop" for stop-loss/trailing-stop exits, else "discretionary"."""
    return "stop" if reason.startswith(("stop loss", "trailing stop")) else "discretionary"


def record_sell(ticker: str, reason: str, day: date | None = None,
                path: str | Path = config.PEAKS_JSON) -> None:
    """Record last_sell_date / last_sell_reason ("stop" or "discretionary") for a ticker."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = load_peaks(path)
            entry = data.setdefault(ticker, {})
            entry["last_sell_date"] = (day or date.today()).isoformat()
            entry["last_sell_reason"] = sell_kind(reason)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, path)
    except OSError as e:
        print(f"WARNING: cannot record sell in peaks file {path}: {e}", file=sys.stderr)


def business_days_since(start: str, today: date | None = None) -> int | None:
    """Weekdays elapsed from ISO date ``start`` to today (same day = 0); None if unparsable."""
    try:
        day = date.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    today = today or date.today()
    n = 0
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n += 1
    return n


def in_reentry_cooldown(record: dict[str, object] | None, today: date | None = None) -> bool:
    """True if a discretionary sell happened within REENTRY_COOLDOWN_DAYS business days."""
    if not record or record.get("last_sell_reason") == "stop":
        return False
    n = business_days_since(str(record.get("last_sell_date", "")), today)
    return n is not None and n <= config.REENTRY_COOLDOWN_DAYS


# --- Prices -------------------------------------------------------------------

def fetch_prices(tickers: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """Latest prices from yfinance. Returns (prices, errors by ticker)."""
    import yfinance as yf  # imported lazily: slow import

    prices: dict[str, float] = {}
    errors: dict[str, str] = {}
    for ticker in tickers:
        try:
            price = yf.Ticker(ticker).fast_info["lastPrice"]
            if price and price > 0:
                prices[ticker] = float(price)
            else:
                errors[ticker] = f"invalid price {price!r}"
        except Exception as e:  # network/parse errors from yfinance
            errors[ticker] = str(e)
    return prices, errors
